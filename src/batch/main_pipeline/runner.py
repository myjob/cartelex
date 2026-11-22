"""Batched execution of a run plan with progress and restart state.

Each step calls its existing ``Main_*`` / ``reporting`` script as a subprocess
(``stdin`` closed, output appended to a per-step log) over batches of
``settings.PIPELINE_BATCH_SIZE`` cases. After every successful batch the plan is
saved, so a restart resumes with the unfinished cases. The first non-zero exit
stops the run: no further cases, no later chapters, no report -- except
``EXIT_CODE_LLM_TIMEOUTS`` (some decisions lost facts to LLM timeouts, the rest
succeeded): that batch counts as done, a note goes to ``RunPlan.tolerated``, and
the next plan picks the decisions up again, as stale facts (the old value was
kept) or as gaps (``planner.fillable_gaps`` re-plans a failed attempt).

Progress goes to the ``batch.main_pipeline.progress`` logger so the operator
always sees chapter, step, cases done, elapsed time and ETA; with a
``progress_factory`` a live bar per step (``console.StepProgress``) also
advances inside a batch, fed by the step log's per-case marker.
"""

import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from batch.BatchJobRunner import EXIT_CODE_LLM_TIMEOUTS
from batch.main_pipeline.console import LogCaseCounter, StepProgress, fmt_duration
from batch.main_pipeline.models import RunPlan, StepPlan
from batch.main_pipeline.prerequisites import CheckResult
from batch.main_pipeline.state_store import StateStore
from config.settings import settings

logger = logging.getLogger(__name__)
progress = logging.getLogger("batch.main_pipeline.progress")

# Steps taking repeatable --ids, and steps taking a single --celex-id.
_IDS_STEPS = frozenset({"download", "convert", "decide", "translate"})
_PER_CASE_STEPS = frozenset({"embed", "classify", "enrich", "entity_graph"})
_LIST_FILE_STEPS = frozenset({"facts", "summaries"})
_ZIP_MODULE = "builtin.zip_case_summary"
_EDDY_STEP = "report_eddy_celex_facts"
_FACTS_REPORT_GLOB = "celex_fact_report_*.xlsx"


_fmt = fmt_duration


def step_argvs(
    step: StepPlan, batch: list[str], workdir: Path, facts_report: Path | None, python: str
) -> list[list[str]]:
    """Build the subprocess command lines of one batch.

    Args:
        step: The step to run.
        batch: Cases of this batch (empty for corpus-wide report steps).
        workdir: Directory for ``--celex-list-file`` files.
        facts_report: Workbook passed to ``report_eddy_celex_facts``.
        python: Interpreter, normally ``sys.executable`` of the ``.venv``.

    Returns:
        One argv per subprocess call; several for per-case steps.
    """
    base = [python, "-m", step.module, *step.extra_args]
    if step.name in _PER_CASE_STEPS:
        return [[*base, "--celex-id", case] for case in batch]
    if step.name in _IDS_STEPS:
        return [[*base, *(arg for case in batch for arg in ("--ids", case))]]
    if step.name in _LIST_FILE_STEPS:
        workdir.mkdir(parents=True, exist_ok=True)
        list_file = workdir / f"{step.name}-{batch[0]}-{len(batch)}.txt"
        list_file.write_text("\n".join(batch) + "\n", encoding="utf-8")
        argv = [*base, "--celex-list-file", str(list_file)]
        if step.force_facts:
            argv += ["--force-facts", ",".join(step.force_facts)]
        if step.select_facts:
            argv += ["--select-facts", ",".join(step.select_facts)]
        return [argv]
    if step.name == _EDDY_STEP and facts_report is not None:
        return [[*base, "--facts-report", str(facts_report)]]
    return [base]


def zip_case_summaries(report_dir: Path, now: datetime) -> Path:
    """Zip ``<report_dir>/case_summary`` with a date/time tag.

    Args:
        report_dir: ``settings.REPORT_PATH``.
        now: Tag time.

    Returns:
        Path of ``case_summary-<YYYYMMDDHHMMSS>.zip``.
    """
    base = report_dir / f"case_summary-{now:%Y%m%d%H%M%S}"
    return Path(shutil.make_archive(str(base), "zip", root_dir=report_dir, base_dir="case_summary"))


def default_run_fn(argv: list[str], log_path: Path) -> int:
    """Run one step command from the repo root, appending its output to the log.

    Args:
        argv: Command line.
        log_path: Per-step log file.

    Returns:
        The exit code (negative when killed by a signal).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n$ {' '.join(argv)}\n")
        log.flush()
        return subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=settings.PRJ_ROOT_DIR, check=False,
        ).returncode


class StepRunner:
    """Execute a plan's steps in batches, saving progress after each batch."""

    def __init__(
        self,
        store: StateStore,
        run_fn: Callable[[list[str], Path], int],
        gpu_check: Callable[[str], CheckResult],
        log_dir: Path,
        batch_size: int,
        report_dir: Path,
        python: str = sys.executable,
        clock: Callable[[], float] = time.monotonic,
        progress_factory: Callable[[StepPlan, float], StepProgress] | None = None,
    ) -> None:
        """Provision the runner.

        Args:
            store: Plan persistence.
            run_fn: Runs one command, returns its exit code.
            gpu_check: Re-checks GPU memory for a step's ``gpu`` need.
            log_dir: Per-step logs and list files.
            batch_size: Cases per batch.
            report_dir: ``settings.REPORT_PATH`` (workbooks, case summaries, zip).
            python: Interpreter for the subprocesses.
            clock: Monotonic clock (injected for tests).
            progress_factory: Opens a live bar for a step, given the estimate
                of the steps after it; None draws no bar (tests, --status).
        """
        self.store = store
        self.run_fn = run_fn
        self.gpu_check = gpu_check
        self.log_dir = Path(log_dir)
        self.batch_size = max(1, batch_size)
        self.report_dir = Path(report_dir)
        self.python = python
        self.clock = clock
        self.progress_factory = progress_factory

    def run(self, plan: RunPlan) -> RunPlan:
        """Run every unfinished step; stop at the first failure.

        Args:
            plan: The plan to execute (mutated and saved as it progresses).

        Returns:
            The plan with status ``done`` or ``failed``.
        """
        plan.status = "running"
        plan.failure = None
        self.store.save(plan)
        chapter_start: dict[int, float] = {}
        for index, step in enumerate(plan.steps):
            if step.status == "done":
                continue
            chapter_start.setdefault(step.chapter, self.clock())
            if not self._run_step(plan, index, step):
                return plan
            step.status = "done"
            self.store.save(plan)
            if index + 1 == len(plan.steps) or plan.steps[index + 1].chapter != step.chapter:
                cases = {c for s in plan.steps if s.chapter == step.chapter for c in s.case_ids}
                progress.info(
                    "chapter %d %s finished in %s (%d cases)", step.chapter, step.chapter.label,
                    _fmt(self.clock() - chapter_start[step.chapter]), len(cases),
                )
        plan.status = "done"
        plan.finished_at = datetime.now()
        self.store.save(plan)
        return plan

    def _fail(self, plan: RunPlan, step: StepPlan, reason: str) -> bool:
        step.status = "failed"
        plan.status = "failed"
        plan.failure = reason
        self.store.save(plan)
        progress.error("chapter %d %s | %s | STOPPED: %s", step.chapter, step.chapter.label, step.name, reason)
        return False

    def _run_step(self, plan: RunPlan, index: int, step: StepPlan) -> bool:
        if step.gpu != "none":
            check = self.gpu_check(step.gpu)
            if not check.ok:
                return self._fail(plan, step, f"{step.name}: {check.detail}")
        step.status = "running"
        self.store.save(plan)
        log_path = self.log_dir / f"{plan.run_id}-{step.name}.log"
        total = len(step.case_ids)
        progress.info(
            "chapter %d %s | %s | start: %s, log %s", step.chapter, step.chapter.label, step.name,
            f"{len(step.remaining_case_ids)}/{total} cases to do" if total else "corpus-wide", log_path,
        )
        if step.module == _ZIP_MODULE:
            path = zip_case_summaries(self.report_dir, datetime.now())
            progress.info("chapter %d %s | %s | wrote %s", step.chapter, step.chapter.label, step.name, path)
            return True
        facts_report = None
        if step.name == _EDDY_STEP:
            facts_report = self._facts_report_since(plan.created_at)
            if facts_report is None:
                return self._fail(
                    plan, step, f"{step.name}: no {_FACTS_REPORT_GLOB} written in {self.report_dir} since {plan.created_at}",
                )
        remaining = step.remaining_case_ids
        if step.case_ids and not remaining:
            return True  # every batch finished before a crash; never fall back to a corpus-wide call
        batches = [remaining[i:i + self.batch_size] for i in range(0, len(remaining), self.batch_size)] or [[]]
        bar = None
        if self.progress_factory is not None and step.case_ids:
            later = sum(s.estimate_s for s in plan.steps[index + 1:] if s.status != "done")
            bar = self.progress_factory(step, later)
        try:
            return self._run_batches(plan, index, step, batches, log_path, facts_report, bar)
        finally:
            if bar is not None:
                bar.close()

    def _run_batches(
        self, plan: RunPlan, index: int, step: StepPlan, batches: list[list[str]],
        log_path: Path, facts_report: Path | None, bar: StepProgress | None,
    ) -> bool:
        total = len(step.case_ids)
        started = self.clock()
        for number, batch in enumerate(batches, start=1):
            workdir = self.log_dir / plan.run_id
            if bar is not None:
                bar.batch_started(len(step.done_case_ids))
            for position, argv in enumerate(step_argvs(step, batch, workdir, facts_report, self.python), start=1):
                code = self._run_watched(argv, log_path, bar)
                if bar is not None and step.name in _PER_CASE_STEPS:
                    bar.batch_done(len(step.done_case_ids) + position)  # one subprocess per case
                if code == EXIT_CODE_LLM_TIMEOUTS:
                    note = f"{step.name} batch {number}/{len(batches)}: LLM timeouts, see {log_path}"
                    plan.tolerated.append(note)
                    progress.warning(
                        "chapter %d %s | %s | batch %d/%d had LLM timeouts; continuing, the next plan retries them",
                        step.chapter, step.chapter.label, step.name, number, len(batches),
                    )
                elif code != 0:
                    return self._fail(plan, step, f"{step.name} exit {code} at batch {number}/{len(batches)}, log {log_path}")
            if batch:
                self.store.mark_batch_done(plan, index, batch)
                if bar is not None:
                    bar.batch_done(len(step.done_case_ids))
                elapsed = self.clock() - started
                eta = elapsed / number * (len(batches) - number)
                progress.info(
                    "chapter %d %s | %s | %d/%d cases | %s elapsed | ETA %s", step.chapter, step.chapter.label,
                    step.name, len(step.done_case_ids), total, _fmt(elapsed), _fmt(eta),
                )
        return True

    def _run_watched(self, argv: list[str], log_path: Path, bar: StepProgress | None) -> int:
        """Run one command; while it runs, advance the bar from its log."""
        if bar is None:
            return self.run_fn(argv, log_path)
        with LogCaseCounter(log_path, bar.cases_started):
            return self.run_fn(argv, log_path)

    def _facts_report_since(self, since: datetime) -> Path | None:
        """Return the newest facts workbook written at or after ``since``."""
        cutoff = since.timestamp()
        fresh = [p for p in self.report_dir.glob(_FACTS_REPORT_GLOB) if p.stat().st_mtime >= cutoff]
        return max(fresh, key=lambda p: p.stat().st_mtime, default=None)
