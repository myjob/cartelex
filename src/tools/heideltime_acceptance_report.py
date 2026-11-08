"""Before/after acceptance report for a HeidelTime fact re-run.

Compares the latest ``heideltime_event_agg``, ``cartel_period`` and
``eu_commission_decision_date`` facts of a list of decisions against a CSV
backup taken before the re-run (``celex_id,name,value_md,value_json,created_at``
as exported from ``celex_facts.facts``), and reports:

* coverage of each fact and the size of the event aggregates;
* cartel periods that are unparseable, inverted, or echo the
  ``settings.HEIDELTIME_MIN_YEAR``/``HEIDELTIME_MAX_YEAR`` search bounds;
* whether the period ends before the decision date;
* periods that disappeared in the re-run, by likely reason;
* decision dates against the date embedded in ``DEC-*-YYYYMMDD`` ids.

Fact values are read by shape (first value of the first row), because the
decision-date fact stores its value under varying keys.

Usage (from project root):

    .venv/bin/python -m tools.heideltime_acceptance_report \\
        --celex-list-file reports/heideltime_corpus_celex_20261105.txt \\
        --backup-csv reports/heideltime_corpus_backup_20261105.csv
"""

import argparse
import json
import logging
import re
from pathlib import Path

import polars as pl
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from config.settings import settings
from pipelines.heideltime.event_dates import PartialDate, parse_partial_date
from pipelines.rag.pgvector.session import make_engine
from tools.fact_completeness_checker import EMPTY_FACT_VALUES, read_celex_list

logger = logging.getLogger(__name__)

AGG_FACT = "heideltime_event_agg"
PERIOD_FACT = "cartel_period"
DECISION_FACT = "eu_commission_decision_date"
FACTS = (AGG_FACT, PERIOD_FACT, DECISION_FACT)

_RANGE = re.compile(r"^\s*(\S+)\s*:\s*(\S+)\s*$")
_DEC_ID_DATE = re.compile(r"^DEC-\S+-(\d{4})(\d{2})(\d{2})$")
_SENTINELS = (f"{settings.HEIDELTIME_MIN_YEAR}-01-01", f"{settings.HEIDELTIME_MAX_YEAR}-12-31")

_LATEST_FACTS_SQL = text(
    """
    SELECT DISTINCT ON (celex_id, name) celex_id, name, value_json::text AS value_json
    FROM celex_facts.facts
    WHERE name = ANY(:facts) AND celex_id = ANY(:ids)
    ORDER BY celex_id, name, created_at DESC
    """
)


class AcceptanceStats(BaseModel):
    """Corpus-level counts over the latest facts of a decision list."""

    agg_present: int = Field(description="Decisions with an event aggregate")
    period_present: int = Field(description="Decisions with a cartel-period fact")
    decision_present: int = Field(description="Decisions with a decision-date fact")
    agg_events_median: int = Field(description="Median events per aggregate")
    agg_events_max: int = Field(description="Largest aggregate, in events")
    period_filled: int = Field(description="Cartel periods with a non-null date range")
    period_invalid: int = Field(description="Ranges whose bounds are not partial ISO dates")
    period_sentinel: int = Field(description="Ranges echoing the 1900/2030 search bounds")
    period_inverted: int = Field(description="Ranges starting after they end")
    decision_filled: int = Field(description="Decision dates with a value")
    decision_invalid: int = Field(description="Decision dates that are not partial ISO dates")
    end_before_decision: int = Field(description="Period end earlier than the decision date")
    end_on_or_after_decision: int = Field(description="Period end on or after the decision date")
    compare_na: int = Field(description="Decisions lacking a period end or a decision date")


class FlaggedRow(BaseModel):
    """One decision whose cartel period fails a check."""

    celex_id: str = Field(description="Decision key")
    reason: str = Field(description="Which check failed")
    date_range: str = Field(description="The stored date range")
    decision_date: str | None = Field(default=None, description="The stored decision date")


def first_value(value_json: str | None) -> str | None:
    """Return the first value of the first row of a fact, or ``None`` when empty.

    Args:
        value_json: The fact's ``value_json`` as text.

    Returns:
        The value as a stripped string, or ``None`` for missing/empty/``NA``.
    """
    if value_json is None:
        return None
    rows = json.loads(value_json)
    rows = rows if isinstance(rows, list) else [rows]
    if not (rows and isinstance(rows[0], dict) and rows[0]):
        return None
    value = next(iter(rows[0].values()))
    if value is None or str(value).strip() in EMPTY_FACT_VALUES:
        return None
    return str(value).strip()


def period_value(value_json: str | None) -> str | None:
    """Return the ``date_range`` of a cartel-period fact, or ``None``.

    Args:
        value_json: The fact's ``value_json`` as text.

    Returns:
        The stored date range, or ``None`` when absent.
    """
    if value_json is None:
        return None
    rows = json.loads(value_json)
    if not (isinstance(rows, list) and rows and isinstance(rows[0], dict)):
        return None
    return rows[0].get("date_range")


def _event_count(value_json: str | None) -> int:
    """Return the number of rows of a fact value."""
    rows = json.loads(value_json) if value_json else []
    return len(rows) if isinstance(rows, list) else 1


def _bounds(date_range: str) -> tuple[PartialDate, PartialDate] | None:
    """Parse ``START : END`` into two partial dates, or ``None`` if invalid."""
    match = _RANGE.match(date_range)
    if not match:
        return None
    start, end = parse_partial_date(match[1]), parse_partial_date(match[2])
    return (start, end) if start and end else None


def _by_key(frame: pl.DataFrame) -> dict[tuple[str, str], str]:
    """Index a latest-fact frame by ``(celex_id, name)``."""
    return {(r["celex_id"], r["name"]): r["value_json"] for r in frame.iter_rows(named=True)}


def assess(frame: pl.DataFrame, celex_ids: list[str]) -> tuple[AcceptanceStats, list[FlaggedRow]]:
    """Run the corpus-level checks over the latest facts.

    Args:
        frame: Latest facts with columns ``celex_id``, ``name``, ``value_json``.
        celex_ids: The decisions to assess.

    Returns:
        The counts and the decisions whose period fails a check.
    """
    by = _by_key(frame)
    agg_sizes = sorted(_event_count(by[(c, AGG_FACT)]) for c in celex_ids if (c, AGG_FACT) in by)
    counts = dict.fromkeys(
        ("period_filled", "period_invalid", "period_sentinel", "period_inverted", "decision_filled",
         "decision_invalid", "end_before_decision", "end_on_or_after_decision", "compare_na"),
        0,
    )
    flagged: list[FlaggedRow] = []
    for celex_id in celex_ids:
        date_range = period_value(by.get((celex_id, PERIOD_FACT)))
        decision_raw = first_value(by.get((celex_id, DECISION_FACT)))
        decision = parse_partial_date(decision_raw) if decision_raw else None
        if decision_raw:
            counts["decision_filled"] += 1
            counts["decision_invalid"] += decision is None
        end = None
        if date_range:
            counts["period_filled"] += 1
            bounds = _bounds(date_range)
            reasons = []
            if bounds is None:
                reasons.append("invalid")
            else:
                start, end = bounds
                if any(s in date_range for s in _SENTINELS):
                    reasons.append("sentinel")
                if start.first_day > end.last_day:
                    reasons.append("inverted")
            for reason in reasons:
                counts[f"period_{reason}"] += 1
            if end and decision and end.first_day >= decision.first_day:
                reasons.append("end>=decision")
            flagged += [FlaggedRow(celex_id=celex_id, reason=r, date_range=date_range, decision_date=decision_raw)
                        for r in reasons]
        if end and decision:
            key = "end_before_decision" if end.first_day < decision.first_day else "end_on_or_after_decision"
            counts[key] += 1
        else:
            counts["compare_na"] += 1
    stats = AcceptanceStats(
        agg_present=sum((c, AGG_FACT) in by for c in celex_ids),
        period_present=sum((c, PERIOD_FACT) in by for c in celex_ids),
        decision_present=sum((c, DECISION_FACT) in by for c in celex_ids),
        agg_events_median=agg_sizes[len(agg_sizes) // 2] if agg_sizes else 0,
        agg_events_max=agg_sizes[-1] if agg_sizes else 0,
        **counts,
    )
    return stats, flagged


def lost_periods(before: pl.DataFrame, after: pl.DataFrame, celex_ids: list[str]) -> pl.DataFrame:
    """List periods present before the re-run and null after it, with a reason.

    The reason is ``sentinel`` (the old range echoed the search bounds),
    ``end>=decision`` (the old range ran to or past the new decision date) or
    ``plausible`` (a possibly real period that was lost).

    Args:
        before: Latest facts before the re-run.
        after: Latest facts after the re-run.
        celex_ids: The decisions to compare.

    Returns:
        Frame with ``celex_id``, ``before``, ``decision_date``, ``reason``.
    """
    old, new = _by_key(before), _by_key(after)
    rows = []
    for celex_id in celex_ids:
        was = period_value(old.get((celex_id, PERIOD_FACT)))
        if not was or period_value(new.get((celex_id, PERIOD_FACT))):
            continue
        decision_raw = first_value(new.get((celex_id, DECISION_FACT)))
        decision = parse_partial_date(decision_raw) if decision_raw else None
        bounds = _bounds(was)
        if any(s in was for s in _SENTINELS):
            reason = "sentinel"
        elif bounds and decision and bounds[1].first_day >= decision.first_day:
            reason = "end>=decision"
        else:
            reason = "plausible"
        rows.append((celex_id, was, decision_raw, reason))
    return pl.DataFrame(rows, schema=["celex_id", "before", "decision_date", "reason"], orient="row")


def decision_id_truth(after: pl.DataFrame, celex_ids: list[str]) -> pl.DataFrame:
    """Compare decision dates with the date embedded in ``DEC-*-YYYYMMDD`` ids.

    Args:
        after: Latest facts after the re-run.
        celex_ids: The decisions to compare; ids without an embedded date are skipped.

    Returns:
        Frame with ``celex_id``, ``id_date``, ``extracted``, ``match``.
    """
    by = _by_key(after)
    rows = []
    for celex_id in celex_ids:
        match = _DEC_ID_DATE.match(celex_id)
        if match:
            truth = "-".join(match.groups())
            got = first_value(by.get((celex_id, DECISION_FACT)))
            rows.append((celex_id, truth, got, got == truth))
    return pl.DataFrame(rows, schema=["celex_id", "id_date", "extracted", "match"], orient="row")


def load_live(engine: Engine, celex_ids: list[str]) -> pl.DataFrame:
    """Fetch the latest HeidelTime facts of the decisions from ``celex_facts.facts``.

    Args:
        engine: SQLAlchemy engine of the facts database.
        celex_ids: The decisions to fetch.

    Returns:
        Frame with ``celex_id``, ``name``, ``value_json``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On connection or query failure.
    """
    with engine.connect() as conn:
        rows = conn.execute(_LATEST_FACTS_SQL, {"facts": list(FACTS), "ids": celex_ids}).all()
    return pl.DataFrame([tuple(r) for r in rows], schema=["celex_id", "name", "value_json"], orient="row")


def load_backup(path: Path) -> pl.DataFrame:
    """Read a ``celex_facts.facts`` CSV backup and keep the latest row per fact.

    Args:
        path: CSV with ``celex_id``, ``name``, ``value_json``, ``created_at``.

    Returns:
        Frame with ``celex_id``, ``name``, ``value_json``.

    Raises:
        OSError: If the file cannot be read.
    """
    return (
        pl.read_csv(path, infer_schema=False)
        .filter(pl.col("name").is_in(FACTS))
        .sort("created_at", descending=True)
        .unique(["celex_id", "name"], keep="first")
        .select("celex_id", "name", "value_json")
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--celex-list-file", type=Path, required=True, help="One decision id per line")
    parser.add_argument("--backup-csv", type=Path, required=True, help="facts backup taken before the re-run")
    parser.add_argument("--csv", type=Path, help="Write the flagged rows of the re-run to this CSV")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the before/after acceptance report and log it.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    celex_ids = read_celex_list(args.celex_list_file)
    before, after = load_backup(args.backup_csv), load_live(make_engine(), celex_ids)
    (old, _), (new, flagged) = assess(before, celex_ids), assess(after, celex_ids)
    logger.info("decisions: %d", len(celex_ids))
    logger.info("%-26s %8s %8s", "check", "before", "after")
    for field in AcceptanceStats.model_fields:
        logger.info("%-26s %8d %8d", field, getattr(old, field), getattr(new, field))
    flagged_frame = pl.DataFrame([f.model_dump() for f in flagged]) if flagged else pl.DataFrame()
    lost = lost_periods(before, after, celex_ids)
    truth = decision_id_truth(after, celex_ids)
    with pl.Config(tbl_rows=-1, tbl_width_chars=250, fmt_str_lengths=120):
        logger.info("flagged periods after the re-run:\n%s", flagged_frame)
        logger.info("lost periods by reason:\n%s", lost.group_by("reason").len().sort("reason") if lost.height else lost)
        logger.info("lost plausible periods:\n%s", lost.filter(pl.col("reason") == "plausible") if lost.height else lost)
        if truth.height:
            logger.info("decision date vs DEC-id date: %d/%d exact", truth["match"].sum(), truth.height)
            logger.info("%s", truth.filter(~pl.col("match")))
    if args.csv and flagged:
        flagged_frame.write_csv(args.csv)
        logger.info("wrote %s", args.csv)


if __name__ == "__main__":
    main()
