"""Decision date agreement report (read-only; ISSUES #369, review A26).

Compares ``eu_commission_decision_date`` with its three independent references
-- the date embedded in ``DEC-AT<case>-YYYYMMDD`` ids, ``human_index_decision_date``
and ``human_index_year`` -- and counts per reference how often the value is
exact, in the same month or year only, in another year, or ``NA``.

``--dry-run`` additionally reads the date the way ``DecisionDateModule`` will
(date line, else title) for every decision, with the LLM fallback disabled, and
reports the same table for the new values next to the stored ones. Nothing is
written to the database; ``--csv`` writes the per-decision rows.

Usage (from project root):

    .venv/bin/python -m tools.decision_date_report
    .venv/bin/python -m tools.decision_date_report --dry-run --csv reports/decision_date_dry_run_YYYYMMDD.csv
"""

import argparse
import datetime as dt
import logging
from enum import StrEnum
from pathlib import Path

import dspy
import polars as pl
from sqlalchemy import Engine, text

from config.settings import settings
from pipelines.legal.decision_date import id_date, parse_date_value, parse_year_value
from pipelines.rag.pgvector.session import make_engine
from tools.fact_completeness_checker import read_celex_list

logger = logging.getLogger(__name__)

REFERENCES = ("dec_id_date", "human_date", "human_year")

_LATEST_SQL = text(
    """
    SELECT DISTINCT ON (celex_id, name) celex_id, name, value_md
    FROM celex_facts.facts
    WHERE name = ANY(:facts)
    ORDER BY celex_id, name, created_at DESC
    """
)


class Agreement(StrEnum):
    """How a decision date compares with one reference."""

    EXACT = "exact"
    SAME_MONTH = "same_month"
    SAME_YEAR = "same_year"
    OTHER_YEAR = "other_year"
    NA = "na"


def agreement(value: dt.date | None, reference: dt.date | int | None) -> Agreement | None:
    """Classify a decision date against a reference date or year.

    Args:
        value: The decision date; None when the fact is missing or ``NA``.
        reference: A reference date, or a year for the year reference.

    Returns:
        The agreement class, or None when the reference is absent.
    """
    if reference is None:
        return None
    if value is None:
        return Agreement.NA
    if isinstance(reference, int):
        return Agreement.EXACT if value.year == reference else Agreement.OTHER_YEAR
    if value == reference:
        return Agreement.EXACT
    if value.year != reference.year:
        return Agreement.OTHER_YEAR
    return Agreement.SAME_MONTH if value.month == reference.month else Agreement.SAME_YEAR


def build_frame(
    facts: dict[str, dict[str, str]],
    celex_ids: list[str],
    new_values: dict[str, dt.date | None] | None = None,
) -> pl.DataFrame:
    """Build one row per decision with its values and agreement classes.

    Args:
        facts: ``celex_id -> {fact name: value_md}`` of the latest facts.
        celex_ids: The decisions to report.
        new_values: Dry-run dates per decision; None omits the ``new_*`` columns.

    Returns:
        Frame with ``celex_id``, the stored and reference values, and
        ``stored_vs_<reference>`` (plus ``new``/``new_vs_<reference>``) classes.
    """
    rows = []
    for celex_id in celex_ids:
        own = facts.get(celex_id, {})
        refs = {
            "dec_id_date": id_date(celex_id),
            "human_date": parse_date_value(own.get(settings.DECISION_DATE_HUMAN_DATE_FACT)),
            "human_year": parse_year_value(own.get(settings.DECISION_DATE_HUMAN_YEAR_FACT)),
        }
        stored = parse_date_value(own.get(settings.DECISION_DATE_FACT))
        row: dict[str, str | None] = {"celex_id": celex_id, "stored": stored.isoformat() if stored else None}
        row |= {name: str(ref) if ref is not None else None for name, ref in refs.items()}
        candidates = {"stored": stored}
        if new_values is not None:
            new = new_values.get(celex_id)
            row["new"] = new.isoformat() if new else None
            candidates["new"] = new
        for label, value in candidates.items():
            for name, ref in refs.items():
                cls = agreement(value, ref)
                row[f"{label}_vs_{name}"] = cls.value if cls else None
        rows.append(row)
    return pl.DataFrame(rows, infer_schema_length=None)


def summarise(frame: pl.DataFrame) -> pl.DataFrame:
    """Count the agreement classes per compared column.

    Args:
        frame: Output of :func:`build_frame`.

    Returns:
        Frame with ``comparison`` and one count column per :class:`Agreement`.
    """
    columns = [c for c in frame.columns if "_vs_" in c]
    rows = []
    for column in columns:
        counts = frame[column].drop_nulls().value_counts()
        by = dict(zip(counts[column].to_list(), counts["count"].to_list(), strict=True))
        rows.append({"comparison": column, **{a.value: by.get(a.value, 0) for a in Agreement}})
    return pl.DataFrame(rows)


def load_facts(engine: Engine) -> dict[str, dict[str, str]]:
    """Fetch the latest decision date and human index facts of every decision.

    Args:
        engine: SQLAlchemy engine of the facts database.

    Returns:
        ``celex_id -> {fact name: value_md}``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On connection or query failure.
    """
    names = [settings.DECISION_DATE_FACT, settings.DECISION_DATE_HUMAN_DATE_FACT,
             settings.DECISION_DATE_HUMAN_YEAR_FACT]
    out: dict[str, dict[str, str]] = {}
    with engine.connect() as conn:
        for celex_id, name, value_md in conn.execute(_LATEST_SQL, {"facts": names}):
            out.setdefault(celex_id, {})[name] = value_md
    return out


class _NoFallback(dspy.Module):
    """LLM fallback stand-in for the dry run (no ``context_facts``, so never called)."""

    def forward(self, params: object) -> dspy.Prediction:
        """Raise so the module records the decision as needing the LLM fallback.

        Raises:
            ValueError: Always.
        """
        raise ValueError("LLM fallback disabled in the dry run")


def dry_run(celex_ids: list[str]) -> dict[str, dt.date | None]:
    """Read every decision's date as ``DecisionDateModule`` will, without writing.

    Args:
        celex_ids: The decisions to read.

    Returns:
        ``celex_id -> date``; None when the text states no date (these would
        go to the LLM fallback).

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On connection or query failure.
    """
    from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
    from pipelines.agent_dspy.universal_modules import DecisionDateModule
    from pipelines.rag.pgvector.chunk_repository import ChunkRepository

    module = DecisionDateModule(sql_store=None, chunk_repo=ChunkRepository(), fallback=_NoFallback())
    out: dict[str, dt.date | None] = {}
    for celex_id in celex_ids:
        params = FactExtractorParamVO(
            target_fact_name=settings.DECISION_DATE_FACT, question="", celex_id=celex_id,
            output_schema="str", pipeline="decision_date", force_override=True,
        )
        items = module(params).output.items
        out[celex_id] = parse_date_value(items[0][settings.DECISION_DATE_FACT]) if items else None
    return out


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--celex-list-file", type=Path, help="One decision id per line (default: every decision)")
    parser.add_argument("--dry-run", action="store_true", help="Also read the new date line / title values")
    parser.add_argument("--csv", type=Path, help="Write the per-decision rows to this CSV")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the agreement report and log it.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    facts = load_facts(make_engine())
    celex_ids = read_celex_list(args.celex_list_file) if args.celex_list_file else sorted(facts)
    new_values = dry_run(celex_ids) if args.dry_run else None
    frame = build_frame(facts, celex_ids, new_values)
    with pl.Config(tbl_rows=-1, tbl_width_chars=200):
        logger.info("decisions: %d", len(celex_ids))
        logger.info("%s", summarise(frame))
        if new_values is not None:
            logger.info("no text date (LLM fallback needed): %d", sum(v is None for v in new_values.values()))
    if args.csv:
        frame.write_csv(args.csv)
        logger.info("wrote %s", args.csv)


if __name__ == "__main__":
    main()
