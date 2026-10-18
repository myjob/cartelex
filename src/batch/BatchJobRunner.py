import argparse
import os
import logging
import json
import logging.config
import concurrent.futures
import sys
import uuid
from datetime import datetime
from tqdm import tqdm
from batch.utils.Filter_MasterJsnOrg_CELEX import filterMasterJsonListFor  # type: ignore
from batch.utils.Job_Utils import save_list_to_file  # type: ignore
from config.llm_usage import (
    UsageTotals,
    drain_usage,
    format_usage_totals,
    process_usage_totals,
    usage_scope,
)
from config.ollama_health_gate import (
    EXIT_CODE_THERMAL_ABORT,
    OllamaHealthGate,
    ThermalAbort,
    build_thermal_gate,
    play_alert_sound,
)
from config.settings import settings

__all__ = ["BatchJobRunner", "EXIT_CODE_THERMAL_ABORT"]

_BOLD = "\033[1m"
_RESET = "\033[0m"


class _AuditEncoder(json.JSONEncoder):
    """JSON encoder that serialises UUID objects as strings."""

    def default(self, o):
        if isinstance(o, uuid.UUID):
            return str(o)
        return super().default(o)


class BatchJobRunner:
    def __init__(self, description="Batch Process"):
        self.parser = argparse.ArgumentParser(description=description)
        self._setup_default_args()
        self.logger = logging.getLogger(__name__)
        # Built in run() once the CLI is parsed; None means "no gate", which is
        # also the correct state when _safe_process_wrapper is exercised alone.
        self._gate: OllamaHealthGate | None = None

    def _setup_default_args(self):
        self.parser.add_argument(
            "--json-fn",
            default=settings.MASTER_DOC_DESCRIPTORS_PATH,
            help="Path to the master document descriptor sheet",
        )
        self.parser.add_argument(
            "--filter-fn",
            default=settings.CELEX_FILTER_PATH,
            help="Path to filter file",
        )
        self.parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Limit number of CELEX IDs to process. 0 for all.",
        )
        self.parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print actions but do not modify database/call LLM.",
        )
        self.parser.add_argument(
            "--max-workers",
            type=int,
            default=1,
            help="Number of concurrent worker threads. Default 1 (serial).",
        )
        self.parser.add_argument(
            "--filter-celex-id",
            type=str,
            default=None,
            help="Process only this specific CELEX ID, ignoring the input list.",
        )
        self.parser.add_argument(
            "--filter-fact",
            type=str,
            default=None,
            help="Process only this specific target_fact_name.",
        )
        self.parser.add_argument(
            "--audit-file",
            type=str,
            default="./logs/pipeline_audit.jsonl",
            help="File to append audit trail JSON lines.",
        )
        self.parser.add_argument(
            "--celex-list-file",
            type=str,
            default=None,
            help=(
                "Path to a single-column text file of CELEX IDs (one per line). "
                "Blank lines and lines starting with '#' are ignored. "
                "Supersedes --json-fn/--filter-fn; is itself superseded by --filter-celex-id."
            ),
        )
        self.parser.add_argument(
            "--no-thermal-gate",
            action="store_true",
            help=(
                "Skip the DGX health/temperature check before each CELEX. "
                "The gate exists because the Ollama box dies silently above "
                "~85C, so disable it only for a deliberate, watched run."
            ),
        )

    def _load_celex_ids_from_file(self, path: str) -> list[str]:
        """Load CELEX IDs from a plain-text file, one ID per line.

        Blank lines and lines whose first non-whitespace character is ``#``
        are silently skipped, allowing inline comments.

        Args:
            path: Filesystem path to the CELEX list file.

        Returns:
            Ordered list of CELEX ID strings.  Returns an empty list if the
            file cannot be opened or is empty after filtering.
        """
        try:
            with open(path, "rt") as fh:
                ids = [
                    line.strip()
                    for line in fh
                    if line.strip() and not line.strip().startswith("#")
                ]
            self.logger.info("Loaded %d CELEX IDs from list file '%s'.", len(ids), path)
            return ids
        except OSError as exc:
            self.logger.error("Failed to read --celex-list-file '%s': %s", path, exc)
            return []

    def load_celex_ids(self, json_fn, filter_fn, dry_run=False):
        try:
            if os.path.exists(json_fn) and os.path.exists(filter_fn):
                celex_list_tuples = filterMasterJsonListFor(json_fn, filter_fn)
                celex_ids = [item[0] for item in celex_list_tuples]
                return celex_ids
            else:
                self.logger.warning(
                    f"Master sheet {json_fn} or filter file {filter_fn} not found. Exiting."
                )
                if dry_run:
                    self.logger.info(
                        "Dry run with no input files: skipping list loading."
                    )
                return []
        except Exception as e:
            self.logger.critical(f"Failed to load CELEX list: {e}")
            return []

    def _build_gate(self, args) -> OllamaHealthGate | None:
        """Construct the thermal gate, or None when it should not run.

        Args:
            args: Parsed CLI arguments.

        Returns:
            One gate shared by every worker (its reading cache is what keeps a
            concurrent run to a single SSH probe per window), or None when the
            run is a dry run, the operator passed --no-thermal-gate, or
            ``settings.THERMAL_GATE_ENABLED`` is off.
        """
        # The constructor is passed by this module's own name so a test can
        # patch ``batch.BatchJobRunner.OllamaHealthGate`` as before.
        return build_thermal_gate(
            args.dry_run, args.no_thermal_gate, gate_factory=OllamaHealthGate
        )

    def _abort_on_thermal(self, unprocessed: list[str], abort: ThermalAbort) -> None:
        """Record the unfinished work, explain how to resume, and exit.

        Everything already extracted stays committed and its audit line is
        already written, so the run stops on a clean boundary: the resume file
        lists exactly the CELEX IDs that were never started.

        Args:
            unprocessed: CELEX IDs that the gate prevented from running.
            abort: The abort carrying the final health check.
        """
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        resume_path = str(
            settings.PRJ_ROOT_DIR / "logs" / f"thermal_abort_{stamp}.txt"
        )
        try:
            save_list_to_file(unprocessed, resume_path)
        except OSError as exc:
            # Losing the resume file must not also lose the reason for stopping.
            self.logger.error(
                "Could not write the resume file %s: %s. Unprocessed CELEX IDs: %s",
                resume_path,
                exc,
                ", ".join(unprocessed),
            )
            resume_path = "(not written)"

        reading = abort.result.reading
        self.logger.critical(
            "%s%s\nRUN STOPPED BY THE DGX THERMAL GATE: %s\n%s\n"
            "%d CELEX ID(s) were not processed; everything already extracted "
            "is committed.\nResume with:\n"
            "  .venv/bin/python <this script> --celex-list-file %s\n%s%s",
            _BOLD,
            "=" * 72,
            abort.result.reason,
            reading.describe() if reading else "no reading available",
            len(unprocessed),
            resume_path,
            "=" * 72,
            _RESET,
        )
        # The run capped the clock on its way here and is about to exit, so lift
        # it: the next thing to use this box should not inherit a throttle from
        # a batch that is no longer running. Done explicitly rather than left to
        # the atexit hook so it is ordered before the usage totals in the log.
        if self._gate is not None:
            self._gate.clock_controller.restore()

        # Sound the alarm again on the abort itself. The cooldown alerts fired
        # while the operator may have been away; this is the one that means the
        # run has stopped and will not resume on its own.
        play_alert_sound()
        drain_usage(process_usage_totals())
        self.logger.info(format_usage_totals(process_usage_totals()))
        sys.exit(EXIT_CODE_THERMAL_ABORT)

    def run(self, process_fn, context=None, post_stages=None):
        """
        Run the batch job.

        Args:
            process_fn: A callable with signature (celex_id, context, dry_run) -> bool
            context: Any object to pass to the process_fn (e.g. Ingestor instance)
            post_stages: Optional iterable of callables, each ``stage(args)``,
                run once after the per-CELEX loop completes and before process
                exit. Use for bulk, cross-CELEX finalisation steps. A stage that
                raises is logged and does not block the remaining stages.
        """
        args = self.parser.parse_args()
        self.args = args  # Store args so they can be accessed from the caller
        self.logger = logging.getLogger(__name__)  # Re-init logger after setup

        self.logger.info(f"Starting batch job (Workers: {args.max_workers})...")

        if args.filter_celex_id:
            celex_ids = [args.filter_celex_id]
            self.logger.info(f"Using single --filter-celex-id: {args.filter_celex_id}")
        elif args.celex_list_file:
            celex_ids = self._load_celex_ids_from_file(args.celex_list_file)
            if not celex_ids:
                self.logger.info("No CELEX IDs found in list file.")
                return
        else:
            celex_ids = self.load_celex_ids(args.json_fn, args.filter_fn, args.dry_run)
            if not celex_ids:
                self.logger.info("No CELEX IDs found to process.")
                return

            self.logger.info(f"Loaded {len(celex_ids)} CELEX IDs.")

        final_celex_list = celex_ids
        if args.limit > 0:
            final_celex_list = celex_ids[: args.limit]

        count = 0
        failures: dict[str, str] = {}  # celex_id -> error message

        # One gate for the whole run, shared by every worker. Pre-flighted here
        # so a run that cannot start fails in seconds rather than after the
        # first document has already been half-extracted.
        self._gate = self._build_gate(args)
        if self._gate is not None:
            # Start from default clocks whatever happened to the previous run.
            # An nvidia-smi -lgc cap lives on the device, not in the process
            # that set it, so a batch killed with SIGKILL leaves the Spark
            # throttled with nothing left to undo it. This reset is idempotent
            # and costs one SSH round trip, which makes a leaked cap
            # self-healing at the next run rather than a silent, permanent
            # slowdown of every future batch.
            self._gate.clock_controller.ensure_default()
            try:
                health = self._gate.check_or_wait()
            except ThermalAbort as abort:
                self._abort_on_thermal(list(final_celex_list), abort)
            else:
                if health.reading is not None:
                    self.logger.info(
                        "DGX thermal pre-flight: %s", health.reading.describe()
                    )

        thermal_abort: ThermalAbort | None = None
        unprocessed: list[str] = []

        with tqdm(total=len(final_celex_list)) as pbar:
            if args.max_workers > 1:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=args.max_workers
                ) as executor:
                    # Submit tasks
                    future_to_celex = {
                        executor.submit(
                            self._safe_process_wrapper,
                            process_fn,
                            cid,
                            context,
                            args.dry_run,
                        ): cid
                        for cid in final_celex_list
                    }

                    for future in concurrent.futures.as_completed(future_to_celex):
                        celex_id = future_to_celex[future]
                        try:
                            res, err, usage = future.result()
                            if err is not None:
                                failures[celex_id] = err
                            self._write_audit_line(
                                args.audit_file, celex_id, res, usage
                            )
                        except ThermalAbort as abort:
                            # The gate latches after the first abort, so every
                            # queued worker raises at once instead of each
                            # serving its own full cooldown. Nothing ran for
                            # this CELEX, so it belongs in the resume file
                            # rather than in failures.
                            thermal_abort = thermal_abort or abort
                            unprocessed.append(celex_id)
                        except Exception as exc:
                            self.logger.error(
                                f"Generated an exception for {celex_id}: {exc}"
                            )
                            failures[celex_id] = str(exc)

                        count += 1
                        pbar.update(1)
            else:
                # Serial execution
                for index, celex_id in enumerate(final_celex_list):
                    self.logger.info(f"Processing CELEX ID: {celex_id}")
                    try:
                        res, err, usage = self._safe_process_wrapper(
                            process_fn, celex_id, context, args.dry_run
                        )
                    except ThermalAbort as abort:
                        # Raised by the gate before any work began, so this
                        # CELEX and every one after it are still unprocessed.
                        thermal_abort = abort
                        unprocessed = list(final_celex_list[index:])
                        break

                    if err is not None:
                        failures[celex_id] = err
                    self._write_audit_line(args.audit_file, celex_id, res, usage)
                    count += 1
                    pbar.update(1)

        if thermal_abort is not None:
            # Reported before the completion line and the post-stages: the run
            # did not complete, and a post-stage that aggregates across CELEX
            # would build its output from a knowingly partial corpus.
            if failures:
                failure_detail = "; ".join(
                    f"{cid}: {msg}" for cid, msg in failures.items()
                )
                self.logger.error(f"Failed CELEX IDs: {failure_detail}")
            self._abort_on_thermal(unprocessed, thermal_abort)

        error_count = len(failures)
        success_count = count - error_count
        self.logger.info(
            f"Completed. Processed {count} CELEX IDs: {success_count} succeeded, {error_count} failed."
        )
        if failures:
            failure_detail = "; ".join(f"{cid}: {msg}" for cid, msg in failures.items())
            # ERROR, not INFO: a per-CELEX failure means the run under-populated
            # the database, and at INFO that is indistinguishable from progress
            # chatter in a long log.
            self.logger.error(f"Failed CELEX IDs: {failure_detail}")

        for stage in post_stages or ():
            stage_name = getattr(stage, "__name__", repr(stage))
            try:
                self.logger.info(f"Running post-stage: {stage_name}")
                stage(args)
            except Exception as exc:
                self.logger.error(f"Post-stage {stage_name} failed: {exc}")

        # What the run consumed, once, rather than only as one line per request.
        # Logged after the post-stages so it covers every LLM call the process
        # made, including those outside the per-CELEX loop. Drained first: the
        # per-CELEX scopes settle their own requests, but a post-stage's do not
        # belong to any scope, and sys.exit below would not wait for them.
        drain_usage(process_usage_totals())
        self.logger.info(format_usage_totals(process_usage_totals()))

        # Force exit to ensure no dangling threads keep process alive, preserving original behavior.
        # Non-zero when any CELEX failed: the loop deliberately continues past a
        # failure, so without this a run that silently dropped rows looks
        # identical to a clean one to any caller (shell wrapper, cron, CI).
        # Post-stage failures are logged above but deliberately do not affect
        # the exit code -- they are not per-CELEX data loss.
        sys.exit(1 if failures else 0)

    def _write_audit_line(
        self, audit_file: str, celex_id: str, results, usage: UsageTotals
    ) -> None:
        """Append one CELEX's outcome and LLM consumption to the audit trail.

        Args:
            audit_file: Path to the JSONL audit file. Falsy disables auditing.
            celex_id: The CELEX identifier that was processed.
            results: Whatever process_fn returned for it.
            usage: Token, cost and latency totals for the LLM requests that
                producing this result made.
        """
        if not audit_file:
            return

        record = {
            "celex_id": celex_id,
            "results": results,
            # What producing this row consumed. The audit trail records what was
            # extracted; without this it cannot say what the extraction cost,
            # and the per-request log lines cannot be attributed back to a CELEX
            # once a run goes concurrent.
            "llm_usage": usage.model_dump(),
        }
        try:
            with open(audit_file, "a") as f:
                f.write(json.dumps(record, cls=_AuditEncoder) + "\n")
        except OSError as exc:
            # An unwritable audit file must not lose the extraction that already
            # succeeded and was already persisted.
            self.logger.error(
                f"Failed to append audit line for {celex_id} to {audit_file}: {exc}"
            )

    def _safe_process_wrapper(
        self, process_fn, celex_id: str, context, dry_run: bool
    ) -> tuple:
        """
        Invoke process_fn and return a (result, error, usage) tuple.

        The LLM usage scope is opened here, and this is the only correct place
        for it: with --max-workers above 1 this method is what runs on the pool
        worker, so it is the innermost point that still corresponds one-to-one
        with a CELEX. The scope is a contextvar, so it reaches the LiteLLM
        callback through the asyncio.run and asyncio.to_thread hops the
        pipelines make below this frame (see config.llm_usage).

        For the same reason it is where the thermal gate is applied, and the
        gate runs before the usage scope is opened so a cooldown is not
        attributed to this CELEX as LLM latency.

        Args:
            process_fn: Callable with signature (celex_id, context, dry_run) -> any.
            celex_id: The CELEX identifier being processed.
            context: Arbitrary context object passed through to process_fn.
            dry_run: When True, process_fn should skip side-effects.

        Returns:
            (result, None, usage) on success; (False, error_message, usage) on
            failure. The usage totals are returned either way: a CELEX that
            failed after twenty LLM calls consumed twenty LLM calls.

        Raises:
            ThermalAbort: If the DGX stayed unhealthy past the cooldown budget.
                Deliberately propagated rather than turned into a failure: this
                CELEX was never started, so it belongs in the resume file and
                not in the failure list.
        """
        if self._gate is not None:
            health = self._gate.check_or_wait()
            if health.reading is not None:
                # At INFO, not DEBUG: over a long run these lines are the only
                # record of how hot the box ran, which is what a post-mortem
                # after a silent shutdown needs and what tuning the limit needs.
                self.logger.info(
                    "DGX thermal before %s: %s", celex_id, health.reading.describe()
                )

        with usage_scope() as usage:
            try:
                return process_fn(celex_id, context, dry_run), None, usage
            except Exception as e:
                self.logger.error(f"Error processing {celex_id}: {e}")
                return False, str(e), usage
