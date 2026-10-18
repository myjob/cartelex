"""
One-shot / repeatable backfill: cross-check extracted CELEX facts against the
Russo-Schinkel infringement book.

For each case present in the book corpus (``celex_facts.celex_case_id``),
runs :class:`pipelines.agent_dspy.book_cross_check_module.BookCrossCheckModule`
to judge every extracted fact. Every judged fact is logged to
``celex_facts.fact2book_checks`` with its routing decision in the ``action``
column, so the table is a complete per-fact check ledger:

  * **corroborated** -> ledger row + lineage corroboration (``book_case``
                        source + ``corroborated_by`` link).
  * **mismatch**     -> ledger row recording the contradiction.
  * **skipped**      -> ledger row recording the no-verdict outcome.

Every visited case is additionally recorded in the case-level run ledger
``celex_facts.fact2book_case_runs`` (in book or why not, reference type, the
per-fact counts, run id and timestamps). Cases absent from the book corpus
are counted as ``not_in_book`` split by reason (``no_surface`` / ``no_anchor``)
and produce no fact rows or LLM calls; rows lacking a ``celex_id`` or
``case_id`` are counted separately as ``incomplete_rows``. Cases the book
mentions only in its index or back-matter (``reference_type='index'``) count in
``reference_types`` but judge no facts.

Idempotency and resume
----------------------
``register_book_source``, ``link_fact_to_source``, ``upsert_check`` and
``upsert_case_run`` all upsert on natural keys — ``upsert_check`` on
``(fact_id, celex_id)``, one verdict per fact per case — so re-running heals
rather than duplicates. A fact the re-run no longer corroborates (or whose
citation anchor drifted) has its earlier book ``corroborated_by`` link
retracted, so lineage always matches the ledger. Pass ``--skip-existing`` to
leave facts that already have a ledger row untouched (no re-judging, no LLM
call) for an incremental top-up; the default re-judges and overwrites.

A run carries a ``--run-id`` (default: a timestamp). After a crash or a
thermal abort, re-invoke with the same ``--run-id`` plus ``--resume`` to skip
every case the run already finished; everything written stays committed.

Thermal gate
------------
The DGX health/temperature gate (``config.ollama_health_gate``) is checked
before every case, exactly as ``BatchJobRunner`` does for the ingestion
batches; ``--no-thermal-gate`` disables it for a deliberate, watched run.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py --dry-run
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py --celex-id CELEX-31982D0123
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py --case-id COMP/35.587
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py --celex-id CELEX-31976D0353
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py --skip-existing
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py --fact-prefix infringement_
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py --run-id book-20260917
    PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py --run-id book-20260917 --resume
"""

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime, timezone

import dspy

from config.llm_factory import batch_profile, create_lm
from config.ollama_health_gate import (
    ThermalAbort,
    build_thermal_gate,
    stop_on_thermal_abort,
)
from extract.rag.hybrid_book_search import HybridBookSearchToolkit
from extract.rag.ranker.ranker_factory import create_ranker
from pipelines.agent_dspy.book_cross_check_module import BookCrossCheckModule
from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository
from pipelines.rag.pgvector.celex_identity_repository import CelexIdentityRepository
from pipelines.rag.pgvector.fact2book_check_repository import (
    Fact2BookCheckRepository,
)
from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository
from tools.celex_tools import CelexDatabaseToolkit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("cross_check_facts_vs_book")

_LOG_INTERVAL = 10  # log progress every N cases processed
_RUN_ID_FORMAT = "book-%Y%m%d-%H%M%S"


def _configure_dspy() -> None:
    """Configure the global DSPy LM from the shared batch profile."""
    dspy.configure(lm=create_lm(batch_profile()))


def _default_run_id(now: datetime | None = None) -> str:
    """Return the run id used when the operator passes none.

    Parameters
    ----------
    now:
        Timestamp to format; defaults to the current UTC time.

    Returns
    -------
    str
        ``book-YYYYMMDD-HHMMSS``.
    """
    return (now or datetime.now(timezone.utc)).strftime(_RUN_ID_FORMAT)


def _fetch_cases(
    identity_repo: CelexIdentityRepository,
    celex_filter: str | None,
    case_id_filter: str | None,
) -> list[dict]:
    """Return distinct book-case rows, optionally filtered.

    Parameters
    ----------
    identity_repo:
        Repository providing ``select_distinct_book_cases``.
    celex_filter:
        When set, keep only rows whose ``celex_id`` matches exactly.
    case_id_filter:
        When set, keep only rows whose ``case_id`` matches exactly.

    Returns
    -------
    list[dict]
        Rows with keys ``"celex_id"``, ``"case_x"``, ``"case_id"``.
    """
    rows = identity_repo.select_distinct_book_cases()
    if celex_filter:
        rows = [r for r in rows if r["celex_id"] == celex_filter]
    if case_id_filter:
        rows = [r for r in rows if r["case_id"] == case_id_filter]
    return rows


def _build_module(
    skip_existing: bool, fact_prefixes: tuple[str, ...] | None
) -> tuple[BookCrossCheckModule, Fact2BookCheckRepository]:
    """Provision the cross-check module and its ledger repository.

    Built only for a live run: the LM, the cross-encoder reranker and the
    repositories (one of which probes the DB) are all real resources.

    Parameters
    ----------
    skip_existing:
        Passed as ``overwrite=not skip_existing``.
    fact_prefixes:
        Optional fact-name prefix filter.

    Returns
    -------
    tuple[BookCrossCheckModule, Fact2BookCheckRepository]
        The module and the shared check repository (for the run ledger).
    """
    _configure_dspy()
    shared_fact_repo = FactRepository()
    toolkit = CelexDatabaseToolkit(fact_repo=shared_fact_repo)
    check_repo = Fact2BookCheckRepository(engine=toolkit.engine)
    module = BookCrossCheckModule(
        book_searcher=HybridBookSearchToolkit(tools=toolkit),
        book_chunk_repo=BookChunkRepository(),
        lineage_repo=LineageRepository(engine=toolkit.engine),
        check_repo=check_repo,
        fact_repo=shared_fact_repo,
        ranker=create_ranker(),
        overwrite=not skip_existing,
        fact_prefixes=fact_prefixes,
    )
    return module, check_repo


def _filter_suffix(celex_filter: str | None, case_id_filter: str | None) -> str:
    """Return a log suffix naming the active filters, or an empty string."""
    filters: list[str] = []
    if celex_filter:
        filters.append(f"celex={celex_filter}")
    if case_id_filter:
        filters.append(f"case_id={case_id_filter}")
    return f" (filter: {', '.join(filters)})" if filters else ""


def run(
    dry_run: bool = False,
    celex_filter: str | None = None,
    case_id_filter: str | None = None,
    skip_existing: bool = False,
    fact_prefixes: tuple[str, ...] | None = None,
    run_id: str | None = None,
    resume: bool = False,
    no_thermal_gate: bool = False,
) -> None:
    """Cross-check all book cases (optionally scoped) against extracted facts.

    Parameters
    ----------
    dry_run:
        When ``True``, lists the cases that would be processed and returns
        before the LM, the reranker or any repository beyond the case source
        is built: no LLM calls, no model load, no writes.
    celex_filter:
        If set, only process rows for this CELEX document.
    case_id_filter:
        If set, only process rows for this COMP/IV/AT case identifier.
    skip_existing:
        When ``True``, facts that already have a ``fact2book_checks`` row are
        left untouched and not re-judged (incremental top-up). When ``False``
        (default), facts are re-judged and their rows overwritten.
    fact_prefixes:
        If set, only facts whose name starts with one of these prefixes are
        checked.
    run_id:
        Label written to every case-run row; defaults to a timestamp.
    resume:
        Skip cases already finished under *run_id* (restart after a crash or
        a thermal abort).
    no_thermal_gate:
        Skip the DGX health/temperature check before each case.
    """
    cases = _fetch_cases(CelexIdentityRepository(), celex_filter, case_id_filter)
    logger.info(
        "Cross-checking %d book-case rows%s%s",
        len(cases),
        _filter_suffix(celex_filter, case_id_filter),
        " [DRY-RUN]" if dry_run else "",
    )
    if dry_run:
        for row in cases:
            logger.info("[DRY-RUN] Would cross-check %s / %s", row["celex_id"], row["case_id"])
        logger.info("Cross-check complete [DRY-RUN] — %d case(s) listed", len(cases))
        return

    run_id = run_id or _default_run_id()
    module, check_repo = _build_module(skip_existing, fact_prefixes)
    gate = build_thermal_gate(dry_run=False, disabled=no_thermal_gate)
    if gate is not None:
        gate.clock_controller.ensure_default()
    finished = check_repo.select_finished_case_keys(run_id) if resume else set()
    logger.info(
        "Run id %s%s", run_id, f" — resuming past {len(finished)} finished case(s)" if resume else ""
    )

    action_counts: Counter = Counter()
    reference_type_counts: Counter = Counter()
    absence_counts: Counter = Counter()
    already_checked = 0
    resumed = 0
    incomplete_rows = 0
    errors = 0

    for idx, row in enumerate(cases, start=1):
        celex_id: str | None = row["celex_id"]
        case_id: str | None = row["case_id"]
        case_x: str | None = row.get("case_x")

        if not celex_id or not case_id:
            logger.info("Skipping row %d — missing celex_id or case_id: %r", idx, row)
            incomplete_rows += 1
            continue
        if (celex_id, case_id) in finished:
            resumed += 1
            continue

        # Outside the try below: an abort is not a per-case failure.
        if gate is not None:
            try:
                gate.check_or_wait()
            except ThermalAbort as abort:
                stop_on_thermal_abort(
                    logger, gate, abort, len(cases) - idx + 1,
                    f".venv/bin/python src/batch/Main_cross_check_facts_vs_book.py "
                    f"--run-id {run_id} --resume",
                )

        started_at = datetime.now(timezone.utc)
        try:
            result = module(celex_id=celex_id, case_id=case_id, case_x=case_x)
            if not result.in_book:
                absence_counts[result.absence_reason.value] += 1
            else:
                reference_type_counts[result.reference_type.value] += 1
                action_counts["corroborated"] += result.corroborated_count
                action_counts["mismatch"] += result.mismatch_count
                action_counts["skipped"] += result.skipped_count
                already_checked += result.already_checked_count
            check_repo.upsert_case_run(
                celex_id=celex_id,
                case_id=case_id,
                run_id=run_id,
                in_book=result.in_book,
                absence_reason=result.absence_reason.value,
                reference_type=result.reference_type.value if result.in_book else None,
                corroborated=result.corroborated_count,
                mismatch=result.mismatch_count,
                skipped=result.skipped_count,
                already_checked=result.already_checked_count,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc),
            )
        except Exception:
            logger.exception("Error processing %s / %s", celex_id, case_id)
            errors += 1

        if idx % _LOG_INTERVAL == 0:
            logger.info(
                "Progress: %d/%d — actions=%s, not_in_book=%s, already_checked=%d, "
                "errors=%d",
                idx,
                len(cases),
                dict(action_counts),
                dict(absence_counts),
                already_checked,
                errors,
            )

    logger.info(
        "Cross-check complete (run %s) — actions=%s, reference_types=%s, "
        "not_in_book=%s, already_checked=%d, resumed_past=%d, incomplete_rows=%d, "
        "errors=%d",
        run_id,
        dict(action_counts),
        dict(reference_type_counts),
        dict(absence_counts),
        already_checked,
        resumed,
        incomplete_rows,
        errors,
    )


def main() -> None:
    """CLI entry point.

    CELEX / case scoping:
        ``--celex-id`` and ``--case-id`` can be combined to pinpoint one row.
        Without either flag the full ``celex_facts.celex_case_id`` table is
        processed.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Cross-check extracted CELEX facts against the Russo-Schinkel "
            "infringement book and record corroborations / contradictions."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the cases that would be processed; no model load, LLM call or write",
    )
    parser.add_argument(
        "--celex-id",
        metavar="CELEX_ID",
        default=None,
        help="Restrict cross-check to a single CELEX document",
    )
    parser.add_argument(
        "--case-id",
        metavar="CASE_ID",
        default=None,
        help="Restrict cross-check to a single COMP/IV/AT case identifier",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help=(
            "Leave facts that already have a fact2book_checks row untouched "
            "(no re-judging) instead of overwriting them"
        ),
    )
    parser.add_argument(
        "--fact-prefix",
        metavar="PREFIX",
        action="append",
        default=None,
        help=(
            "Only check facts whose name starts with PREFIX "
            "(repeatable, e.g. --fact-prefix infringement_)"
        ),
    )
    parser.add_argument(
        "--run-id",
        metavar="RUN_ID",
        default=None,
        help="Label for this run's fact2book_case_runs rows (default: a timestamp)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip cases already finished under --run-id (restart after a crash)",
    )
    parser.add_argument(
        "--no-thermal-gate",
        action="store_true",
        help=(
            "Skip the DGX health/temperature check before each case. The gate "
            "exists because the Ollama box dies silently above ~85C."
        ),
    )
    args = parser.parse_args()
    run(
        dry_run=args.dry_run,
        celex_filter=args.celex_id,
        case_id_filter=args.case_id,
        skip_existing=args.skip_existing,
        fact_prefixes=tuple(args.fact_prefix) if args.fact_prefix else None,
        run_id=args.run_id,
        resume=args.resume,
        no_thermal_gate=args.no_thermal_gate,
    )


if __name__ == "__main__":
    main()
