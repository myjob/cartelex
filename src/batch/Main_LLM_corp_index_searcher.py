"""
Batch: profile the roster companies no register could resolve, using a cloud LLM.

Consumes ``celex_facts.company_investigated2lei2corpidx2hr_vw`` where
``fk_opencorp_idx = 'NA'`` -- 1,738 rows over 1,704 distinct company names on
2026-08-01, out of 6,109. Those are the companies for which every deterministic
path is exhausted: GLEIF found no LEI, the German Handelsregister join found no
number, and the OpenCorporates relaxation ladder asked under every rung it had
(verbatim, stripped, jurisdiction-filtered, unfiltered) and got nothing back.
OpenCorporates resolved rather more than 4,000 roster rows; this batch is the
attempt to close the remaining gap from the model's own knowledge.

For each name it asks the ``corp_index`` LLM profile
(``settings.CORP_INDEX_LLM_PROVIDER`` / ``settings.CORP_INDEX_LLM_MODEL_ID``,
DeepSeek's reasoning model by default -- configured independently of the UI
planner's) via :class:`LlmCompanyIndexModule` for a structured profile --
canonical name, abbreviation, legal form, year established, life cycle status,
succession, products/services, geo market, headquarters address, country,
jurisdiction, and the LEI / EUID / BRIS / Handelsregister / SIREN identifiers
plus a generic id-and-registrar pair for every registrar with no column of its
own -- and upserts it into
``celex_facts.llm_index_companies``, keyed by the company name.

Two rules govern that answer, and they pull in opposite directions. The
descriptive fields want the model's best supported answer, because a hedge or a
description by way of the group that absorbed the company is worth more than
``'NA'``; the identifier fields want the reverse, because a reconstructed
identifier is indistinguishable downstream from a register match. Both rules sit
on the individual fields of ``LlmCompanyIndexProfile``, which is where the
adapter renders them into the prompt.

Everything the roster already knows about a company is handed to the model as a
hint: its address, country, resolved jurisdiction, and any LEI or
Handelsregister number that *did* resolve. The roster's ``'NA'`` markers are
folded to "unknown" first, because ``'NA'`` means "we asked and there was no
match", which is not a fact about the company.

What this batch does NOT do:
    - It writes nothing back to ``company_investigated2celex_id``. The roster's
      ``fk_lei = 'NA'`` keeps meaning "GLEIF says no"; an LLM-recalled LEI is a
      weaker claim and stays in its own table, labelled with the model that made
      it. Promoting one into the roster would make an assertion indistinguishable
      from a register match.
    - It never marks a company as "asked and unknown". A failed call writes
      nothing, so the name simply comes back on the next run.

Resume:
    Stored names are anti-joined out of the target set, so an interrupted run
    continues where it stopped and a completed run is a no-op. ``--refresh``
    re-asks names that already have a profile (e.g. after changing
    ``CORP_INDEX_LLM_MODEL_ID``, or to fill columns added since the last pass)
    and overwrites every column of the stored row except the company name and
    ``created_at``. ``ensure_tables()`` adds any missing column before the run,
    so a refresh is all a new field needs.

    A refresh only works because ``settings.CORP_INDEX_LLM_CACHE`` is false for
    this profile: DSPy's LM cache is keyed by the request, so with it on an
    unchanged prompt replays the stored answer and the run reports as renewed a
    set of rows it never re-asked. Set it true to make repeated questions free
    again, at the cost of that guarantee.

Cost safety:
    A full pass is ~1,700 cloud calls against a reasoning model. ``--dry-run``
    lists the target set without calling or writing anything, ``--limit`` caps a
    run, and the resolved corp_index model id is recorded per row so a re-run
    under a different model is visible rather than silent.

Throughput:
    One call costs 20-46s and 2,000-5,100 completion tokens, of which ~97% is a
    chain-of-thought the pipeline discards: ``deepseek-v4-*`` reasons whatever
    it is told, and none of ``thinking={"type": "disabled"}``,
    ``reasoning_effort="none"`` or the ``chat_template_kwargs`` variants
    silences it (measured 2026-08-01, recorded in ISSUES.md). That makes the
    backlog latency-bound rather than compute-bound, so ``--workers`` (default
    8) profiles that many companies at once and turns a ~16-hour serial pass
    into roughly 2 hours. Only the LLM call is threaded; every write, counter
    and log line stays on the main thread, so there is no lock and the database
    still sees a single caller. ``--workers 1`` restores the serial,
    roster-ordered run. There is no throttle by default (``--sleep-seconds``
    is 0.0); raise it if the provider starts rate-limiting.

Failure handling:
    A failure specific to one name (an LLM error, or output that fails Pydantic
    validation) is logged, counted, and skipped -- the row is simply not
    written, so the next run retries it. ``--max-consecutive-errors`` (default
    5) stops the run once failures come back-to-back, which is what an expired
    key or a provider outage looks like; without it a single bad afternoon would
    burn the whole roster on failures.

This is a company-driven job, not a CELEX-driven one, so it does not use
``BatchJobRunner``; it has its own argparse CLI.
"""

import argparse
import logging
import time
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import islice

from pipelines.corp_index.corp_index_module import LlmCompanyIndexModule
from pipelines.corp_index.llm_index_repository import (
    MARKER_NO_HIT,
    LlmCompanyIndexRepository,
)
from pipelines.corp_index.models import LlmCompanyIndexRow, LlmCompanyIndexTarget

logger = logging.getLogger(__name__)

# ANSI bold for the run-ending banner (no emoji / unicode-emoji per project
# style). Rendered bold on a terminal; harmless escape noise in a log file.
_BOLD = "\033[1m"
_RESET = "\033[0m"

# A failing name is skipped rather than allowed to end the run; this many
# failures back-to-back means the provider side is unhealthy (expired key, model
# withdrawn, outage) rather than one awkward company, so the run stops and
# resumes later.
DEFAULT_MAX_CONSECUTIVE_ERRORS = 5
# Shortest company name still worth spending a call on. Below this the roster
# holds extraction junk rather than a company.
DEFAULT_MIN_NAME_LENGTH = 3
# No throttle by default: the cloud endpoint is not the quota-fragile resource
# OpenCorporates is.
DEFAULT_SLEEP_SECONDS = 0.0
# Companies profiled at once. One call takes 20-46s because deepseek-v4-* emits
# thousands of reasoning tokens that cannot be turned off (measured 2026-08-01,
# see ISSUES.md), which makes a serial pass over the ~1,700-company backlog a
# ~16-hour job. The calls are independent, so the whole cost is latency waiting
# to be overlapped. Matches dspy.settings.num_threads, whose default is also 8.
DEFAULT_WORKERS = 8
# Companies submitted to the pool per worker. Anything above 1 keeps a worker
# from idling between a result being taken and the next call starting; the
# ceiling is what a stop can waste, since cancellation cannot reach a call
# that has already started.
IN_FLIGHT_MULTIPLIER = 2


class LlmCorpIndexSearcher:
    """Profiles unresolved roster companies with an LLM and stores the answers.

    Follows the Provisioner pattern: the DSPy module and the repository are
    injected in ``__init__`` so the searcher is testable with mocks at the LLM
    and DB boundaries.

    Parameters
    ----------
    module:
        DSPy module that turns a company name plus hints into a validated
        profile row.
    repository:
        DAO for ``celex_facts.llm_index_companies``; also the source of the
        target set, since selecting it needs the anti-join against that table.
    """

    def __init__(
        self,
        module: LlmCompanyIndexModule,
        repository: LlmCompanyIndexRepository,
    ) -> None:
        self.module = module
        self.repository = repository

    def run(
        self,
        *,
        marker: str | None = MARKER_NO_HIT,
        iso_country_code: str | None = None,
        refresh: bool = False,
        retry_unknown: bool = False,
        use_address: bool = False,
        min_name_length: int = DEFAULT_MIN_NAME_LENGTH,
        limit: int = 0,
        sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
        workers: int = DEFAULT_WORKERS,
        max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
        dry_run: bool = False,
    ) -> dict:
        """Profile the outstanding companies and persist what the model asserts.

        Args:
            marker: ``fk_opencorp_idx`` value to select, ``'NA'`` by default
                (the companies OpenCorporates could not resolve). ``None``
                selects every row of the roster view regardless of marker.
            iso_country_code: ISO 3166-1 country code to restrict the run to,
                or None for every country.
            refresh: When True, also re-ask companies that already have a
                stored profile, overwriting it in place.
            retry_unknown: When True, ask *only* about companies whose stored
                profile left both life cycle status and founding year unknown.
            use_address: When True, ask about each company by name and postal
                address together, and hand the address over with its country
                filled in from the roster's separate country column.
            min_name_length: Shortest company name still worth asking about.
            limit: Process at most this many companies; ``0`` for all.
            sleep_seconds: Throttle applied before each LLM call.
            workers: Companies profiled at once. ``1`` runs the loop serially,
                in roster order. Above that, only the LLM call is threaded --
                every write, counter and log line stays on this thread, so no
                lock is needed and the DB sees one caller.
            max_consecutive_errors: Stop the run once this many companies fail
                back-to-back. Isolated failures skip only their own company.
                Concurrently, "back-to-back" is counted in completion order: a
                streak is any run of failures with no success landing between
                them, which is what an expired key or an outage produces.
            dry_run: When True, list the targets but make no LLM call and no
                write.

        Returns:
            A summary dict with ``targets``, ``processed`` (profiles written),
            ``failed`` (companies whose call or validation failed, left for the
            next run), ``stopped_on_error``, and the run's ``llm_model_id``.
        """
        targets = self.repository.select_targets(
            marker=marker,
            iso_country_code=iso_country_code,
            include_existing=refresh,
            retry_unknown=retry_unknown,
            min_name_length=min_name_length,
            limit=limit,
        )

        summary = {
            "marker": marker,
            "iso_country_code": iso_country_code,
            # Recorded because they change what was asked, and nothing on the
            # stored row says which arm produced it -- the same reason
            # llm_model_id is stamped per row.
            "retry_unknown": retry_unknown,
            "use_address": use_address,
            "targets": len(targets),
            "processed": 0,
            "failed": 0,
            "stopped_on_error": False,
            # What actually answers, taken from the module rather than
            # from settings, so an injected LM cannot be mislabelled.
            "llm_model_id": self.module.llm_model_id,
        }

        if not targets:
            logger.info(
                "No companies awaiting an LLM profile for marker=%s, country=%s.",
                marker,
                iso_country_code or "ALL",
            )
            return summary

        if dry_run:
            for target in targets:
                # With --use-address the composed question is the thing worth
                # eyeballing, and a dry run is the only free way to see it.
                logger.info(
                    "[DRY RUN] %r (country=%s, jurisdiction=%s, address=%s)",
                    target.query_name(use_address),
                    target.iso_country_code or "-",
                    target.jurisdication_code or "-",
                    "yes" if target.address else "no",
                )
            logger.info(
                "[DRY RUN] %d company/companies would be profiled with %s; "
                "no calls made, nothing written.",
                len(targets),
                self.module.llm_model_id,
            )
            return summary

        logger.info(
            "Profiling %d company/companies with %s, %d worker(s).",
            len(targets),
            self.module.llm_model_id,
            max(workers, 1),
        )

        consecutive_errors = 0
        total_targets = len(targets)
        completed = 0
        for target, row, profiling_error in self._profiled(
            targets,
            workers=workers,
            sleep_seconds=sleep_seconds,
            use_address=use_address,
        ):
            completed += 1
            if profiling_error is not None:
                # A verdict on one company, not on the run: the row is left
                # unwritten, so the next run asks about it again. Marking it
                # would record "asked and unknown" for a question the model
                # never actually answered.
                consecutive_errors += 1
                summary["failed"] += 1
                logger.warning(
                    "Profiling failed for %r (%d consecutive), moving on: %s",
                    target.company_name,
                    consecutive_errors,
                    profiling_error,
                )
                if consecutive_errors >= max_consecutive_errors:
                    self._log_error_banner(consecutive_errors, profiling_error)
                    summary["stopped_on_error"] = True
                    break
                continue

            consecutive_errors = 0
            self.repository.upsert_company(row)
            summary["processed"] += 1
            logger.info(
                "Profiled %r as %r (%s, est. %s, %s); succession=%s; "
                "LEI=%s EUID=%s. [%d/%d, %.1f%%]",
                target.company_name,
                row.profile.canonical_name,
                row.profile.legal_form,
                row.profile.established_since,
                row.profile.life_cycle_status,
                row.profile.succession,
                row.profile.lei,
                row.profile.euid,
                completed,
                total_targets,
                completed / total_targets * 100,
            )

        return summary

    def _profiled(
        self,
        targets: list[LlmCompanyIndexTarget],
        *,
        workers: int,
        sleep_seconds: float,
        use_address: bool = False,
    ) -> Iterator[tuple[LlmCompanyIndexTarget, LlmCompanyIndexRow | None, Exception | None]]:
        """Yield each company's profile as it arrives.

        The only thing concurrency changes is the order results come back in and
        how soon; the caller's bookkeeping is identical either way, which is why
        the two modes differ here and nowhere else.

        Only ``workers * IN_FLIGHT_MULTIPLIER`` companies are ever submitted at
        once, topped up as results land. Queueing the whole backlog would make
        the stop nearly useless: cancellation can only reach work the pool has
        not started, so the bound is what keeps a stop from paying for hundreds
        of calls the caller no longer wants.

        Args:
            targets: The companies to profile.
            workers: Companies in flight at once; ``1`` or less runs serially.
            sleep_seconds: Throttle applied before each LLM call.
            use_address: Ask about each company by name and postal address.

        Yields:
            ``(target, row, error)`` per company, with exactly one of ``row``
            and ``error`` set. Serially the order is the roster's; concurrently
            it is completion order.
        """
        if workers <= 1:
            for target in targets:
                yield self._profile_or_error(target, sleep_seconds, use_address)
            return

        remaining = iter(targets)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {
                pool.submit(self._profile_or_error, target, sleep_seconds, use_address)
                for target in islice(remaining, workers * IN_FLIGHT_MULTIPLIER)
            }
            try:
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        yield future.result()
                        # Topped up only after the caller has taken the result,
                        # so a caller that stops submits nothing further.
                        for next_target in islice(remaining, 1):
                            pending.add(
                                pool.submit(
                                    self._profile_or_error,
                                    next_target,
                                    sleep_seconds,
                                    use_address,
                                )
                            )
            finally:
                # Reached when the caller stops early (an error streak) as well
                # as on normal exhaustion. Calls already in flight still finish
                # -- the executor cannot unstart them -- so a stop costs at most
                # one window, never the rest of the backlog.
                pool.shutdown(cancel_futures=True)

    def _profile_or_error(
        self,
        target: LlmCompanyIndexTarget,
        sleep_seconds: float,
        use_address: bool = False,
    ) -> tuple[LlmCompanyIndexTarget, LlmCompanyIndexRow | None, Exception | None]:
        """Profile one company, returning its failure instead of raising it.

        Runs on a worker thread when the batch is concurrent. It touches no
        shared state: ``dspy.context`` overrides are contextvar-scoped, so each
        thread configures its own LM binding, and nothing here writes to the DB.

        Args:
            target: The roster company to profile.
            sleep_seconds: Throttle applied before the call.
            use_address: Ask about the company by name and postal address.

        Returns:
            ``(target, row, None)`` on success, ``(target, None, error)`` when
            the call or the output validation failed.
        """
        if sleep_seconds:
            time.sleep(sleep_seconds)
        try:
            return target, self._profile(target, use_address), None
        except Exception as profiling_error:  # noqa: BLE001 - reported, not raised
            return target, None, profiling_error

    def _profile(
        self, target: LlmCompanyIndexTarget, use_address: bool = False
    ) -> LlmCompanyIndexRow:
        """Ask the LLM for one company's profile.

        Args:
            target: The roster company to profile, with its known attributes.
            use_address: Ask about the company by name and postal address.

        Returns:
            The validated :class:`LlmCompanyIndexRow` the module produced.

        Raises:
            Exception: Whatever the LM or the output validation raises; the
                caller decides whether it ends the run.
        """
        prediction = self.module(
            company_name=target.company_name,
            address=target.address,
            iso_country_code=target.iso_country_code,
            jurisdication_code=target.jurisdication_code,
            lei=target.lei,
            handelsregister=target.handelsregister,
            use_address=use_address,
        )
        return prediction.output

    @staticmethod
    def _log_error_banner(consecutive_errors: int, error: Exception) -> None:
        """Log a bold banner for a run stopped by back-to-back failures.

        Args:
            consecutive_errors: How many companies failed in a row.
            error: The failure that reached the limit.
        """
        logger.warning(
            "%s%s\nLLM COMPANY INDEX STOPPED after %d consecutive failure(s): "
            "%s\nThis is a provider-side stop (expired key, withdrawn model, "
            "outage), not a data problem. Stored profiles are kept; unprofiled "
            "companies resume on the next run.\n%s%s",
            _BOLD,
            "=" * 72,
            consecutive_errors,
            error,
            "=" * 72,
            _RESET,
        )


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the LLM company index batch.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Profile the investigated companies that no register could resolve "
            "using the configured cloud LLM, and store the answers in "
            "celex_facts.llm_index_companies (resumable)."
        )
    )
    parser.add_argument(
        "--marker",
        default=MARKER_NO_HIT,
        help=(
            "fk_opencorp_idx value to select (default: NA -- the companies "
            "OpenCorporates was asked about and could not resolve)."
        ),
    )
    parser.add_argument(
        "--all-markers",
        action="store_true",
        help=(
            "Select every company on the roster view regardless of "
            "fk_opencorp_idx, not just the unresolved ones. Overrides --marker."
        ),
    )
    parser.add_argument(
        "--iso-country-code",
        default=None,
        help=(
            "ISO 3166-1 country code to restrict the run to (e.g. DE). Omit to "
            "process every outstanding company across all countries."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Re-ask companies that already have a stored profile and overwrite "
            "every column of it except company_name and created_at. Use after "
            "changing CORP_INDEX_LLM_MODEL_ID, or to fill columns added since "
            "the last pass; without it, stored companies are skipped and the "
            "run resumes the backlog."
        ),
    )
    parser.add_argument(
        "--retry-unknown",
        action="store_true",
        help=(
            "Ask only about companies whose stored profile left BOTH its life "
            "cycle status and its founding year unknown -- the rows that cost a "
            "call and answered nothing the fact pipeline wanted. A column that "
            "is NULL counts as unknown alongside 'NA', so rows written before "
            "established_since existed are included. Implies --refresh."
        ),
    )
    parser.add_argument(
        "--use-address",
        action="store_true",
        help=(
            "Ask about each company by name AND postal address in one question, "
            "and hand the address over completed with the country the roster "
            "keeps in a separate column ('Av. Emile Digneffe 26, 4000 Liege' "
            "becomes '..., Belgium'). Composes with --retry-unknown rather than "
            "implying it, so the same rows can be run with the address off and "
            "then on to see whether it makes any difference."
        ),
    )
    parser.add_argument(
        "--min-name-length",
        type=int,
        default=DEFAULT_MIN_NAME_LENGTH,
        help=(
            "Shortest company name still worth a call (default: 3). Names below "
            "it, and names with no alphanumeric character at all, are "
            "extraction junk and are never sent."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Profile at most this many companies. 0 for all (~1,700).",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help="Throttle before each LLM call (default: 0.0, no throttle).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=(
            "Companies profiled at once (default: 8). One call takes 20-46s "
            "because the model reasons at length whatever it is told, so the "
            "backlog is latency-bound, not compute-bound: 8 workers turn a "
            "~16-hour serial pass into roughly 2 hours. Use 1 for the serial, "
            "roster-ordered run."
        ),
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_ERRORS,
        help=(
            "Stop once this many companies fail back-to-back; isolated failures "
            "are skipped and retried by the next run (default: 5)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the target companies but make no LLM call and no writes.",
    )
    return parser


def main() -> None:
    """Entry point: profile the roster companies no register could resolve."""
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args()

    repository = LlmCompanyIndexRepository()
    repository.ensure_tables()

    searcher = LlmCorpIndexSearcher(
        module=LlmCompanyIndexModule(),
        repository=repository,
    )
    result = searcher.run(
        marker=None if args.all_markers else args.marker,
        iso_country_code=args.iso_country_code,
        refresh=args.refresh,
        retry_unknown=args.retry_unknown,
        use_address=args.use_address,
        min_name_length=args.min_name_length,
        limit=args.limit,
        sleep_seconds=args.sleep_seconds,
        workers=args.workers,
        max_consecutive_errors=args.max_consecutive_errors,
        dry_run=args.dry_run,
    )
    logger.info("LLM company index result: %s", result)


if __name__ == "__main__":
    main()


"""
# Dry run: the companies that would be profiled (no LLM calls, no writes):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --dry-run

# Smallest real run -- three companies, then inspect what was stored:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --limit 10

#   SELECT company_name, canonical_name, legal_form, established_since,
#          life_cycle_status, succession, country, lei, euid, generic_corp_id,
#          generic_corp_registrar, llm_model_id
#   FROM celex_facts.llm_index_companies
#   ORDER BY created_at DESC LIMIT 3;

# Backfill columns added after an earlier pass (established_since, succession,
# generic_corp_id, generic_corp_registrar): ensure_tables() adds
# them, --refresh re-asks every stored company and overwrites the whole row.
# The LM cache is off for this profile, so these calls really are re-asked:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --refresh --limit 25

# The rows that answered nothing: life cycle AND founding year both unknown.
# See the target set and, with --use-address, the composed questions -- free:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py \
    --retry-unknown --use-address --dry-run

# The A/B the two flags exist for: the same rows, address off, then on.
# Whether the decision's address unlocks those two fields is measurable only
# this way, and ISSUES.md records that single calls to the reasoning model do
# not reproduce -- so read a slice, not one company:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --retry-unknown --limit 25
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py \
    --retry-unknown --use-address --limit 25

#   SELECT count(*)                                          AS rows,
#          count(*) FILTER (WHERE life_cycle_status <> 'NA') AS with_status,
#          count(*) FILTER (WHERE established_since <> 'NA') AS with_founding
#   FROM celex_facts.llm_index_companies
#   WHERE updated_at > now() - interval '1 hour';
#
#   Read that as a change signal, not a quality signal: a rise is equally
#   consistent with the model inventing from the address it was just handed.
#   Spot-check the text of a few life_cycle_status values before the rest.

#   How much of the NA problem the descriptive fields actually close:
#   SELECT count(*)                                            AS profiled,
#          count(*) FILTER (WHERE life_cycle_status <> 'NA')   AS with_status,
#          count(*) FILTER (WHERE succession       <> 'NA')    AS with_successor,
#          count(*) FILTER (WHERE established_since <> 'NA')   AS with_founding
#   FROM celex_facts.llm_index_companies;

# Full run over the fk_opencorp_idx = 'NA' backlog (~1,700 cloud calls).
# Interruptible: stored companies are anti-joined out, so re-running resumes:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py

# One country at a time, which is the cheapest way to judge answer quality
# before committing to the whole backlog:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --iso-country-code DE --limit 25

# Re-ask everything under a different model, overwriting the stored profiles
# (llm_model_id records which model produced each row):
CORP_INDEX_LLM_MODEL_ID=deepseek-v4-flash PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --refresh

# Profile every company on the roster, not only the unresolved ones -- useful
# for the descriptive fields (products/services, geo market, life cycle), which
# no register supplies at all. existing records stay untouched:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --all-markers --dry-run

    
# --all-markers only widens which roster rows are eligible (it drops the fk_opencorp_idx = 'NA' filter). 
# It does not touch the resume anti-join. 
# Since you'd be running without --refresh and without --retry-unknown

# The identifiers stored here are model assertions, never register matches.
# Compare them against what the registers actually said:
#
#   SELECT r.company_name, r.fk_lei AS gleif, l.lei AS asserted, l.llm_model_id
#   FROM celex_facts.company_investigated2lei2corpidx2hr_vw r
#   JOIN celex_facts.llm_index_companies l USING (company_name)
#   WHERE r.fk_lei = 'NA' AND l.lei <> 'NA';
"""
