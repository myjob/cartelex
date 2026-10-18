"""
One-shot / repeatable backfill: verify every fact and populate the evidence
ledger.

Iterates ``celex_facts.facts`` and runs
:class:`pipelines.agent_dspy.evidence_verifier_module.EvidenceVerifierModule`
on each, persisting one ``fact_evidence`` row plus the denormalized
``facts.verification_status`` per fact. Excluded fact names (see
``settings.EVIDENCE_EXCLUDED_FACT_NAMES``) are skipped without writes.

The verifier is deterministic-first: only concept/relation facts reach the LLM,
so a full corpus pass stays cheap. The DSPy LM is configured once up-front so
the support judge has a backend.

Idempotency
-----------
``FactEvidenceRepository.upsert_evidence`` deletes the fact's existing
``fact_evidence`` rows and inserts the one current verdict in a single
transaction, so re-running overwrites prior verdicts rather than duplicating
them. This also covers a fact whose strategy changed between runs (e.g.
``llm_support`` -> ``derived``): no stale row from the old strategy survives.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/Main_verify_facts_backfill.py
    PYTHONPATH=src .venv/bin/python src/batch/Main_verify_facts_backfill.py --dry-run
    PYTHONPATH=src .venv/bin/python src/batch/Main_verify_facts_backfill.py --celex-id CELEX-31982D0123
    PYTHONPATH=src .venv/bin/python src/batch/Main_verify_facts_backfill.py --celex-id 39813_1554_3
    PYTHONPATH=src .venv/bin/python src/batch/Main_verify_facts_backfill.py --fact-name fines
"""

import argparse
import logging
import sys
from collections import Counter

import dspy
from sqlalchemy import text

from config.llm_factory import batch_profile, create_lm
from config.ollama_health_gate import (
    ThermalAbort,
    build_thermal_gate,
    stop_on_thermal_abort,
)
from pipelines.agent_dspy.evidence_verifier_module import EvidenceVerifierModule
from pipelines.rag.pgvector.chunk_repository import ChunkRepository
from pipelines.rag.pgvector.fact_evidence_repository import FactEvidenceRepository
from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository
from pipelines.rag.pgvector.query_embedding_repository import QueryEmbeddingRepository
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("verify_facts_backfill")

_LOG_INTERVAL = 50  # log progress every N facts


def _configure_dspy() -> None:
    """Configure the global DSPy LM for the support judge from the batch profile."""
    dspy.configure(lm=create_lm(batch_profile()), adapter=dspy.JSONAdapter())


def _fetch_facts(
    conn,
    celex_filter: str | None,
    fact_name_filter: str | None = None,
) -> list[dict]:
    """Return ``(celex_id, name)`` for every fact, narrowed by the given filters.

    Parameters
    ----------
    conn:
        Open SQLAlchemy connection to the facts database.
    celex_filter:
        If set, restrict the selection to this ``facts.celex_id``.
    fact_name_filter:
        If set, restrict the selection to this ``facts.name``. Combines with
        ``celex_filter`` conjunctively.

    Returns
    -------
    list[dict]
        One ``{"celex_id": ..., "name": ...}`` mapping per selected fact,
        ordered by CELEX then fact name.
    """
    clauses: list[str] = []
    params: dict[str, str] = {}
    if celex_filter:
        clauses.append("celex_id = :celex_id")
        params["celex_id"] = celex_filter
    if fact_name_filter:
        clauses.append("name = :fact_name")
        params["fact_name"] = fact_name_filter
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = text(
        f"SELECT celex_id, name FROM celex_facts.facts{where} "
        "ORDER BY celex_id, name"
    )
    rows = conn.execute(sql, params).fetchall()
    return [{"celex_id": r.celex_id, "name": r.name} for r in rows]


def _scope_suffix(celex_filter: str | None, fact_name_filter: str | None) -> str:
    """Render the active scope filters for the progress log, empty if none."""
    filters = []
    if celex_filter:
        filters.append(f"celex={celex_filter}")
    if fact_name_filter:
        filters.append(f"name={fact_name_filter}")
    return f" (filter: {', '.join(filters)})" if filters else ""


def run(
    dry_run: bool = False,
    celex_filter: str | None = None,
    fact_name_filter: str | None = None,
    no_thermal_gate: bool = False,
) -> None:
    """Verify the selected facts and record evidence + verdicts.

    Parameters
    ----------
    dry_run:
        When ``True``, runs the full verification -- LLM calls included -- but
        writes nothing: the module's ``_persist`` step is stubbed out for the
        call, so the would-be verdict is only counted and logged. The thermal
        gate still runs, because the LLM calls are real.
    celex_filter:
        If set, only verify facts for this CELEX document.
    fact_name_filter:
        If set, only verify facts carrying this name, across every CELEX unless
        ``celex_filter`` narrows it further. Re-verifying one fact name after
        its extractor changed costs one pass over that name instead of a full
        corpus pass.
    no_thermal_gate:
        Skip the DGX health/temperature check before each fact.
    """
    engine = make_engine(DATABASE_URL)
    _configure_dspy()
    gate = build_thermal_gate(dry_run=False, disabled=no_thermal_gate)
    if gate is not None:
        gate.clock_controller.ensure_default()

    evidence_repo = FactEvidenceRepository(engine=engine)
    module = EvidenceVerifierModule(
        chunk_repo=ChunkRepository(),
        lineage_repo=LineageRepository(engine=engine),
        fact_repo=FactRepository(),
        evidence_repo=evidence_repo,
        query_embedding_repo=QueryEmbeddingRepository(),
    )

    with engine.connect() as conn:
        facts = _fetch_facts(conn, celex_filter, fact_name_filter)

    suffix = _scope_suffix(celex_filter, fact_name_filter)
    logger.info("Verifying %d fact rows%s%s", len(facts), suffix,
                " [DRY-RUN]" if dry_run else "")

    strategy_counts: Counter = Counter()
    status_counts: Counter = Counter()
    excluded = 0
    errors = 0

    for idx, fact in enumerate(facts, start=1):
        # Outside the try below: an abort is not a per-fact failure.
        if gate is not None:
            try:
                gate.check_or_wait()
            except ThermalAbort as abort:
                stop_on_thermal_abort(
                    logger, gate, abort, len(facts) - idx + 1,
                    "re-run this script with the same filters; verified facts "
                    "are re-judged idempotently",
                )
        try:
            if dry_run:
                # Persist nothing: temporarily neuter the evidence writes.
                result = _dry_run_verify(module, fact["celex_id"], fact["name"])
            else:
                result = module(celex_id=fact["celex_id"], fact_name=fact["name"])
            if result is None:
                excluded += 1
            else:
                strategy_counts[result.strategy.value] += 1
                status_counts[result.status.value] += 1
        except Exception:
            logger.exception(
                "Error verifying %s/%s", fact["celex_id"], fact["name"]
            )
            errors += 1

        if idx % _LOG_INTERVAL == 0:
            logger.info(
                "Progress: %d/%d — strategies=%s, statuses=%s, excluded=%d, errors=%d",
                idx, len(facts), dict(strategy_counts), dict(status_counts),
                excluded, errors,
            )

    logger.info(
        "Verification complete%s — strategies=%s, statuses=%s, "
        "excluded=%d, errors=%d",
        " [DRY-RUN]" if dry_run else "",
        dict(strategy_counts), dict(status_counts), excluded, errors,
    )


def _dry_run_verify(module: EvidenceVerifierModule, celex_id: str, fact_name: str):
    """Run verification without persisting by stubbing the persist step."""
    original = module._persist
    module._persist = lambda result: None  # type: ignore[method-assign]
    try:
        return module(celex_id=celex_id, fact_name=fact_name)
    finally:
        module._persist = original  # type: ignore[method-assign]


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Verify facts and backfill the celex_facts.fact_evidence ledger"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify and report verdicts without writing any rows",
    )
    parser.add_argument(
        "--celex-id",
        metavar="CELEX_ID",
        default=None,
        help="Restrict verification to a single CELEX document",
    )
    parser.add_argument(
        "--fact-name",
        metavar="FACT_NAME",
        default=None,
        help="Restrict verification to a single fact name (e.g. fines)",
    )
    parser.add_argument(
        "--no-thermal-gate",
        action="store_true",
        help=(
            "Skip the DGX health/temperature check before each fact. The gate "
            "exists because the Ollama box dies silently above ~85C."
        ),
    )
    args = parser.parse_args()
    run(
        dry_run=args.dry_run,
        celex_filter=args.celex_id,
        fact_name_filter=args.fact_name,
        no_thermal_gate=args.no_thermal_gate,
    )


if __name__ == "__main__":
    main()
