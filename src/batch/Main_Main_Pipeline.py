"""-----------------------------------------------------------------------------------
This is the main for the entire pipeline starting with downloads ending with reporting
The main purpose is to act on minor changes such as
    *   a new document got added in one of the two loader files and all pipeline steps need to be executed
    *   a bug in a certain step got fixed and all or affected facts have to be recomputed
    *   a refresh of a fact has impact on the accuracy of all successor steps in the pipeline and those steps have to be recomputed.
Key functions
    Identify deviations either in the loader file
    Identify deviations by comparing in the fact audit trail for all cases and all facts
        (celex_facts.fact_ingestion_audit_trail_vw attribute inserted_update_at)
        if fact case_summary is older than the fact.
    derive a list of cases to be processed based and pass it to the chain of steps
---------------------------------------------------------------------------------------

Chapters (``batch.main_pipeline.models.Chapter``):
    0 prerequisites  - DB, document folder, DGX serving, no other batch, GPU memory,
                       FACT_EXTRACTOR.yaml parses and its declarations resolve
    1 download       - added loader rows, rows whose download URL changed
    2 document prep  - MinerU, source decision, translation, embeddings,
                       section classification, chunk context
    3 ingest facts   - stale, refreshed or missing facts (never case_summary)
    4 entity graph   - decisions whose graph input facts changed
    5 fact summaries - case_summary, rebuilt only when an input value changed
    6 report         - facts report, eddy report, case summaries, zip

Usage (from the repo root)::

    .venv/bin/python -m batch.Main_Main_Pipeline                 # detect, plan, ask, run
    .venv/bin/python -m batch.Main_Main_Pipeline --dry-run       # prerequisites + plan only
    .venv/bin/python -m batch.Main_Main_Pipeline --resume        # continue a pending or failed run
    .venv/bin/python -m batch.Main_Main_Pipeline --status        # progress of the open run
    .venv/bin/python -m batch.Main_Main_Pipeline --refresh-fact products
    .venv/bin/python -m batch.Main_Main_Pipeline --only report
    .venv/bin/python -m batch.Main_Main_Pipeline --yes           # skip the start question

Exit codes: 0 done / nothing to do / saved as pending, 1 a step failed (resume
command logged), 2 usage or out-of-date plan, 3 prerequisite failed or locked.
Design: docs/Design Main_Main_Pipeline.md.
"""

import argparse
import logging
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from batch.main_pipeline.console import TIME_PREFIX_WIDTH, configure_console, fmt_duration, plan_table, terminal_width
from batch.main_pipeline.models import Chapter, RunPlan
from batch.main_pipeline.prerequisites import CheckResult
from batch.main_pipeline.state_store import PipelineLocked, StateStore
from config.settings import settings

logger = logging.getLogger(__name__)
progress = logging.getLogger("batch.main_pipeline.progress")

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3
RUN_COMMAND = ".venv/bin/python -m batch.Main_Main_Pipeline"
RESUME_COMMAND = f"{RUN_COMMAND} --resume"

_ONLY = {
    "download": Chapter.DOWNLOAD,
    "documents": Chapter.DOCUMENT_PREPARATION,
    "facts": Chapter.FACTS,
    "entity_graph": Chapter.ENTITY_GRAPH,
    "summaries": Chapter.SUMMARIES,
    "report": Chapter.REPORT,
}


class CliDeps(BaseModel):
    """Boundaries of the CLI flow, injected so tests drive it without infrastructure."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    store: StateStore = Field(description="Plan persistence and lock")
    prerequisites: Callable[[], list[CheckResult]] = Field(description="Runs every prerequisite check")
    gpu_check: Callable[[str], CheckResult] = Field(description="Checks free GPU memory for a need")
    build_plan: Callable[[argparse.Namespace, datetime | None], RunPlan] = Field(description="Builds a fresh plan")
    run_plan: Callable[[RunPlan], RunPlan] = Field(description="Executes a plan (StepRunner.run)")
    known_facts: Callable[[], set[str]] = Field(description="Fact names of the current catalogue")
    input_fn: Callable[[str], str] = Field(description="Asks the operator")
    isatty: Callable[[], bool] = Field(description="True when an operator can answer")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the CLI.

    Args:
        argv: Arguments; None reads ``sys.argv``.

    Returns:
        The namespace; ``only`` is a :class:`Chapter` or None.

    Raises:
        SystemExit: On a usage error (code 2).
    """
    p = argparse.ArgumentParser(description="End-to-end pipeline: detect what changed, plan, run, report.")
    p.add_argument("--dry-run", action="store_true", help="Run the prerequisites and show the plan; run nothing.")
    p.add_argument("--resume", action="store_true", help="Continue the pending or failed plan.")
    p.add_argument("--status", action="store_true", help="Show the progress of the open plan and exit.")
    p.add_argument("--refresh-fact", action="append", default=[], metavar="FACT",
                   help="Re-extract FACT and its dependents for every decision (repeatable).")
    p.add_argument("--only", choices=sorted(_ONLY), default=None, help="Plan only this chapter.")
    p.add_argument("--skip-case", action="append", default=[], metavar="ID",
                   help="With --resume: drop a decision that cannot succeed (e.g. a dead URL) "
                        "from the remaining steps of the open plan (repeatable).")
    p.add_argument("--yes", action="store_true", help="Start a long plan without asking.")
    args = p.parse_args(argv)
    if args.skip_case and not args.resume:
        p.error("--skip-case only applies with --resume")
    args.only = _ONLY[args.only] if args.only else None
    return args


_fmt = fmt_duration


def log_plan(plan: RunPlan) -> None:
    """Log the plan summary as a table that fits the terminal width.

    Args:
        plan: The plan to show.
    """
    for line in plan_table(plan, terminal_width() - TIME_PREFIX_WIDTH):
        progress.info("%s", line)


def _plan_facts(plan: RunPlan) -> set[str]:
    """Fact names a plan relies on (forced facts and --filter-fact values)."""
    names = {f for step in plan.steps for f in step.force_facts}
    for step in plan.steps:
        if "--filter-fact" in step.extra_args:
            names.add(step.extra_args[step.extra_args.index("--filter-fact") + 1])
    return names


def _prerequisites_pass(deps: CliDeps) -> bool:
    results = deps.prerequisites()
    for result in results:
        (progress.info if result.ok else progress.error)(
            "prerequisite %-22s %s  %s", result.name, "ok" if result.ok else "FAILED", result.detail,
        )
    return all(r.ok for r in results)


def _ask(deps: CliDeps, prompt: str) -> str:
    """Ask the operator; EOF or no terminal answers the empty string."""
    if not deps.isatty():
        return ""
    try:
        return deps.input_fn(prompt).strip().lower()
    except EOFError:
        return ""


def _out_of_date(plan: RunPlan, deps: CliDeps) -> bool:
    """Log and return True when the plan names facts the catalogue no longer has."""
    unknown = sorted(_plan_facts(plan) - deps.known_facts())
    if unknown:
        progress.error(
            "plan %s is out of date (facts no longer in the catalogue: %s); run without --resume",
            plan.run_id, ", ".join(unknown),
        )
    return bool(unknown)


def _skip_cases(plan: RunPlan, case_ids: list[str]) -> None:
    """Drop ``case_ids`` from every unfinished step of ``plan``."""
    skipped = set(case_ids)
    for step in plan.steps:
        if step.status != "done":
            step.case_ids = [c for c in step.case_ids if c not in skipped or c in step.done_case_ids]
    plan.triggers.append(f"skipped by operator: {', '.join(sorted(skipped))}")
    progress.warning("plan %s: skipping %s in the remaining steps", plan.run_id, ", ".join(sorted(skipped)))


def _choose_plan(args: argparse.Namespace, deps: CliDeps) -> tuple[RunPlan | None, int | None, bool]:
    """Return ``(plan, exit code, confirmed)``.

    ``confirmed`` is True when the operator already chose this plan (``--resume``
    or "y" at the open-plan offer), so the start question is not asked again.
    """
    open_plan = deps.store.load_open()
    if args.resume:
        if open_plan is None:
            progress.error(
                "nothing to resume: no pending or failed plan in %s; plan a fresh run with: %s",
                deps.store.state_dir, RUN_COMMAND,
            )
            return None, EXIT_USAGE, False
        if _out_of_date(open_plan, deps):
            return None, EXIT_USAGE, False
        if args.skip_case:
            _skip_cases(open_plan, args.skip_case)
            deps.store.save(open_plan)
        return open_plan, None, True
    if open_plan is not None:
        log_plan(open_plan)
        answer = _ask(deps, f"Open plan {open_plan.run_id} ({open_plan.status}). Start it? [y/N/r=replan] ")
        if answer in ("y", "yes"):
            if _out_of_date(open_plan, deps):
                return None, EXIT_USAGE, False
            return open_plan, None, True
        if answer != "r":
            progress.info("open plan %s kept; resume with: %s", open_plan.run_id, RESUME_COMMAND)
            return None, EXIT_OK, False
        open_plan.status = "superseded"
        deps.store.save(open_plan)
    return deps.build_plan(args, deps.store.baseline()), None, False


def _refresh_facts_valid(args: argparse.Namespace, deps: CliDeps) -> bool:
    """Reject unknown ``--refresh-fact`` names and ``case_summary`` before any work."""
    names = set(args.refresh_fact)
    if "case_summary" in names:
        progress.error("--refresh-fact case_summary: summaries rebuild by themselves when an input value changed")
        return False
    unknown = sorted(names - deps.known_facts())
    if unknown:
        progress.error("--refresh-fact names unknown facts: %s", ", ".join(unknown))
        return False
    return True


def _run(args: argparse.Namespace, deps: CliDeps) -> int:
    if not _refresh_facts_valid(args, deps):
        return EXIT_USAGE
    if not _prerequisites_pass(deps):
        progress.error("prere quisites failed: nothing was run")
        return EXIT_BLOCKED
    plan, code, confirmed = _choose_plan(args, deps)
    if plan is None:
        return code if code is not None else EXIT_OK
    if plan.is_empty:
        progress.info("nothing to do: no added or changed documents, no stale or missing facts")
        return EXIT_OK
    log_plan(plan)
    for need in sorted({s.gpu for s in plan.steps if s.gpu != "none" and s.status != "done"}):
        check = deps.gpu_check(need)
        if not check.ok:
            progress.error("prerequisite gpu memory FAILED  %s", check.detail)
            return EXIT_BLOCKED
    if args.dry_run:
        progress.info("dry run: nothing run, nothing saved")
        return EXIT_OK
    threshold_s = settings.PIPELINE_ASK_THRESHOLD_MIN * 60
    if not confirmed and plan.estimate_s > threshold_s and not args.yes:
        answer = _ask(deps, f"Estimated {_fmt(plan.estimate_s)}. Start now? [y/N] ")
        if answer not in ("y", "yes"):
            deps.store.save(plan)
            progress.info("plan %s saved as pending; start it with: %s", plan.run_id, RESUME_COMMAND)
            return EXIT_OK
    result = deps.run_plan(plan)
    log_plan(result)
    if result.status != "done":
        progress.error("run %s stopped: %s", result.run_id, result.failure)
        progress.error("after fixing the cause, resume with: %s", RESUME_COMMAND)
        progress.error("a decision that cannot succeed (e.g. dead URL) can be dropped: %s --skip-case ID", RESUME_COMMAND)
        return EXIT_FAILED
    progress.info("run %s done; reports in %s", result.run_id, settings.REPORT_PATH)
    if result.tolerated:
        progress.warning(
            "%d batches had LLM timeouts; the run is done, so --resume finds nothing: "
            "a fresh run re-plans the affected decisions: %s", len(result.tolerated), RUN_COMMAND,
        )
    return EXIT_OK


def main(argv: list[str] | None = None, *, deps: CliDeps | None = None) -> int:
    """Run the orchestrator.

    Args:
        argv: CLI arguments; None reads ``sys.argv``.
        deps: Injected boundaries; None builds the production ones.

    Returns:
        The exit code (see the module docstring).
    """
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    deps = deps or default_cli_deps()
    if args.status:
        open_plan = deps.store.load_open()
        if open_plan is None:
            progress.info("no open plan in %s", deps.store.state_dir)
        else:
            log_plan(open_plan)
        return EXIT_OK
    try:
        deps.store.acquire_lock()
    except PipelineLocked as exc:
        progress.error("%s", exc)
        return EXIT_BLOCKED
    try:
        return _run(args, deps)
    finally:
        deps.store.release_lock()


def default_cli_deps() -> CliDeps:
    """Build the production boundaries from settings.

    Returns:
        Deps on the real DB, DGX, GPU, scripts and terminal.
    """
    from batch.fact_dependency_manager import load_structs
    from batch.main_pipeline.planner import PlannerRepository, build_plan, fillable_gaps, loader_rows
    from batch.main_pipeline.prerequisites import default_deps, gpu_ok, run_prerequisites
    from batch.main_pipeline.console import StepProgress
    from batch.main_pipeline.runner import StepRunner, default_run_fn
    from pipelines.rag.pgvector.session import make_engine
    from tools.fact_completeness_checker import build_completeness_frame, fetch_rows, load_catalogue

    store = StateStore(Path(settings.PIPELINE_STATE_DIR))
    prerequisite_deps = default_deps()
    catalogue_path = Path(settings.FACT_EXTRACTOR_CONFIG_DIR)

    def plan(args: argparse.Namespace, last_done_at: datetime | None) -> RunPlan:
        engine = make_engine()
        repo = PlannerRepository(engine)
        structs = load_structs(catalogue_path)
        cases = sorted(repo.all_cases())
        progress.info("planning: %d decisions, %d catalogue facts", len(cases), len(structs))
        facts, audit = fetch_rows(engine, cases)
        frame = build_completeness_frame(cases, load_catalogue(catalogue_path), facts, audit)
        return build_plan(
            repo, structs,
            refresh_facts=set(args.refresh_fact),
            completeness_gaps=fillable_gaps(frame, structs),
            loader=loader_rows(Path(settings.PIPELINE_LOADER_FILE)),
            loader_file=settings.PIPELINE_LOADER_FILE,
            last_done_at=last_done_at,
            only=args.only,
            now=datetime.now(),
            chunk_age_trigger=settings.PIPELINE_CHUNK_AGE_TRIGGER,
        )

    def gpu(need: str) -> CheckResult:
        return gpu_ok(prerequisite_deps, need)

    runner = StepRunner(
        store=store, run_fn=default_run_fn, gpu_check=gpu, log_dir=Path(settings.PIPELINE_LOG_DIR),
        batch_size=settings.PIPELINE_BATCH_SIZE, report_dir=Path(settings.REPORT_PATH),
        progress_factory=StepProgress,
    )
    return CliDeps(
        store=store,
        prerequisites=lambda: run_prerequisites(prerequisite_deps, set()),
        gpu_check=gpu,
        build_plan=plan,
        run_plan=runner.run,
        known_facts=lambda: {s["target_fact_name"] for s in load_structs(catalogue_path) if s.get("target_fact_name")},
        input_fn=input,
        isatty=sys.stdin.isatty,
    )


if __name__ == "__main__":
    configure_console()
    sys.exit(main())
