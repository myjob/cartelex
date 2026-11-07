"""One-off repair of the first Main_decide_source_document run (2026-11-01 11:40).

That run's corpus identity was too narrow and its title date too loose. The
whole-branch review found eight promoted decisions that duplicate decisions
already in the corpus and one wrong date; each was verified against the
registry and the documents. This script folds every duplicate into its
existing owner -- all its document rows and case links move, its registry
row and master entry are removed -- and, where the promoted copy is the
better one (an English original beside a German source, or a much longer
text; user decision "switch to the better copy"), makes that copy the
owner's source. Each merge is recorded in ``decision_merge`` as
``repair_decided_duplicates``. It also resets the amended-decision date of
DEC-AT38238-20170616.

Dry run by default; ``--apply`` writes, one transaction per decision with
the master flush inside it.

Usage (from the repo root):
    .venv/bin/python -m batch.utils.repair_decided_duplicates [--apply]
"""

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import text

from config.settings import settings
from pipelines.decide.models import DecisionPlan, Repoint, Verdict
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.rag.pgvector.registry_dedupe_repository import MergeStep
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.source_decision_repository import CandidateRow, SourceDecisionRepository

logger = logging.getLogger(__name__)


class DuplicatePair(BaseModel):
    """A promoted decision that duplicates an existing one."""

    decision_id: str = Field(description="Decision promoted by the first run.")
    owner: str = Field(description="Existing decision that receives it.")
    better_copy: str | None = Field(description="Document of the promoted decision that becomes the owner's source.")


# Verified 2026-11-01 (review of branch feature/decide-source-document);
# better copies by stripped text length / EN original.
DUPLICATES: tuple[DuplicatePair, ...] = (
    DuplicatePair(decision_id="DEC-AT39406-20090128", owner="39406_1902_1", better_copy=None),
    DuplicatePair(decision_id="DEC-AT39401-20091016", owner="39401_1535_1", better_copy=None),
    DuplicatePair(decision_id="DEC-35470-20031016", owner="35470_14_4", better_copy="dbcomp-1414"),
    DuplicatePair(decision_id="DEC-AT37614-20011205", owner="CELEX-32003D0569", better_copy=None),
    DuplicatePair(decision_id="DEC-AT37730-20020705", owner="CELEX-32002D0746", better_copy=None),
    DuplicatePair(decision_id="DEC-36623-20051005", owner="36623_320_1", better_copy=None),
    DuplicatePair(decision_id="DEC-38359-20031203", owner="38359_36_1", better_copy="dbcomp-1241"),
    DuplicatePair(decision_id="DEC-AT39389-20081126", owner="39389_2761_1", better_copy=None),
)
DATE_RESETS: dict[str, dt.date] = {"DEC-AT38238-20170616": dt.date(2017, 6, 16)}

_SET_DATE = text(
    f"UPDATE {settings.DB_SCHEMA}.decision_registry SET decision_date = :date, updated_at = now() "
    "WHERE decision_id = :decision_id")
_EXISTS = text(f"SELECT 1 FROM {settings.DB_SCHEMA}.decision_registry WHERE decision_id = :d")


def merge_plan(pair: DuplicatePair, rows: dict[str, CandidateRow]) -> DecisionPlan:
    """Build the MERGE plan that folds a promoted duplicate into its owner.

    Args:
        pair: The verified pair.
        rows: Document rows with a PDF of the promoted decision.

    Returns:
        A MERGE plan re-pointing every row (the better copy as the owner's source).

    Raises:
        KeyError: If the better copy is not among the rows.
    """
    if pair.better_copy is not None and pair.better_copy not in rows:
        raise KeyError(pair.better_copy)
    return DecisionPlan(
        decision_id=pair.decision_id, verdict=Verdict.MERGE, merge_into=pair.owner,
        repoints=[Repoint(document_id=doc, owner_decision_id=pair.owner, make_source=doc == pair.better_copy)
                  for doc in sorted(rows)])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        Namespace with ``apply``.
    """
    p = argparse.ArgumentParser(description="Repair duplicates and a date of the first source-decision run.")
    p.add_argument("--apply", action="store_true", help="Write; default is a dry run.")
    return p.parse_args(argv)


def _apply_merge(repository: SourceDecisionRepository, master: MasterDescriptorStore, pair: DuplicatePair,
                 plan: DecisionPlan, rows: dict[str, CandidateRow]) -> bool:
    """Apply one merge; master changes run inside the transaction."""
    def hook() -> None:
        if pair.better_copy is not None and master.has_key(pair.owner):
            r = rows[pair.better_copy]
            master.switch_source(pair.owner, local_file_pdf=r.local_file_pdf, txt_md_file=r.local_file_md or "",
                                 language_init=(r.language or "NA").lower())
        master.remove(pair.decision_id)
        master.flush()
    try:
        repository.apply(plan, rows, before_commit=hook)
        return True
    except Exception:  # noqa: BLE001 - report and continue with the next pair
        logger.exception("%s: merge failed, rolled back", pair.decision_id)
        return False


def main(argv: list[str] | None = None) -> int:
    """Run the repair.

    Args:
        argv: Command-line arguments.

    Returns:
        0 ok, 1 any merge failed.
    """
    args = parse_args(argv)
    engine = make_engine()
    repository = SourceDecisionRepository(engine, merge_step=MergeStep.REPAIR_DECIDED_DUPLICATES)
    master = MasterDescriptorStore(Path(settings.MASTER_DOC_DESCRIPTORS_PATH))
    failures = 0
    try:
        for pair in DUPLICATES:
            with engine.connect() as conn:
                gone = conn.execute(_EXISTS, {"d": pair.decision_id}).first() is None
                owner_missing = conn.execute(_EXISTS, {"d": pair.owner}).first() is None
            if gone or owner_missing:
                logger.warning("%s -> %s: %s, skipped", pair.decision_id, pair.owner,
                               "already merged" if gone else "owner missing")
                continue
            rows = repository.document_rows(pair.decision_id)
            plan = merge_plan(pair, rows)
            logger.info("%s -> %s: %d documents%s%s", pair.decision_id, pair.owner, len(rows),
                        f", {pair.better_copy} becomes the source" if pair.better_copy else "",
                        "" if args.apply else " [dry run]")
            if args.apply:
                failures += not _apply_merge(repository, master, pair, plan, rows)
        for decision_id, date in DATE_RESETS.items():
            logger.info("%s: decision_date -> %s%s", decision_id, date, "" if args.apply else " [dry run]")
            if args.apply:
                with engine.begin() as conn:
                    conn.execute(_SET_DATE, {"date": date, "decision_id": decision_id})
        logger.info("repair done: %d merges planned, %d failed", len(DUPLICATES), failures)
        return 1 if failures else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
