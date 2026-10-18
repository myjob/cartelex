"""
One-shot backfill: record lineage for ``heideltime_cartel_period`` facts.

Background
----------
The ``heideltime_cartel_period`` fact stores the global infringement window as a
single string, e.g. ``"1992-05-22 : 2007-12-19"`` (see
``HeideltimeCartelPeriodModule``).  Those two dates are *derived from* individual
HeidelTime temporal expressions persisted in
``celex_facts.heideltime_extractions``.  This script reconnects each cartel-period
fact to the extraction rows whose normalised ``timex_value`` matches the start
and end dates, writing the missing provenance into:

  - ``celex_facts.fact_source_registry``   (one ``heideltime`` source per extraction)
  - ``celex_facts.fact_source_link``  (fact -> source, ``extracted_from``)

Idempotency
-----------
Source registration upserts on ``heideltime_event_id`` and linking upserts on
``(fact_id, source_id)``, so re-running never creates duplicate rows.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/pipelines/heideltime/heideltime_source_register_fisher.py
    PYTHONPATH=src .venv/bin/python src/pipelines/heideltime/heideltime_source_register_fisher.py --dry-run
    PYTHONPATH=src .venv/bin/python src/pipelines/heideltime/heideltime_source_register_fisher.py --celex-id CELEX-32003D0570
"""

import argparse
import datetime
import json
import logging
import sys
from uuid import UUID

from sqlalchemy import text

from pipelines.rag.pgvector.lineage_repository import LineageRepository
from pipelines.rag.pgvector.models import FactSourceLink
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("heideltime_lineage_fisher")

_FACT_NAME = "heideltime_cartel_period"
_DATE_RANGE_SEPARATOR = " : "
_EXTRACTION_METHOD = "heideltime_cartel_period"
_LOG_INTERVAL = 100  # log progress every N facts


def _fetch_cartel_period_facts(conn, celex_filter: str | None) -> list[dict]:
    """Return ``heideltime_cartel_period`` facts with a non-null value_json."""
    where = "name = :fact_name AND value_json IS NOT NULL"
    params: dict[str, str] = {"fact_name": _FACT_NAME}
    if celex_filter:
        where += " AND celex_id = :celex_id"
        params["celex_id"] = celex_filter
    sql = text(f"SELECT id, celex_id, value_json FROM celex_facts.facts WHERE {where}")
    rows = conn.execute(sql, params).fetchall()
    return [dict(r._mapping) for r in rows]


def _parse_period_dates(value_json: str) -> tuple[str, str] | None:
    """Extract the (start, end) ISO dates from a cartel-period ``value_json``.

    The fact is stored as ``[{"date_range": "1992-05-22 : 2007-12-19"}]``.

    Returns
    -------
    tuple[str, str] or None
        ``(start_date, end_date)``, or ``None`` when the value cannot be parsed.
    """
    try:
        payload = json.loads(value_json)
    except (TypeError, json.JSONDecodeError):
        return None

    date_range: str | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        date_range = payload[0].get("date_range")
    elif isinstance(payload, dict):
        date_range = payload.get("date_range")
    elif isinstance(payload, str):
        date_range = payload

    if not date_range or _DATE_RANGE_SEPARATOR not in date_range:
        return None

    start_date, end_date = (part.strip() for part in date_range.split(_DATE_RANGE_SEPARATOR, 1))
    if not start_date or not end_date:
        return None
    return start_date, end_date


def _is_valid_iso_date(value: str) -> bool:
    """Return ``True`` when ``value`` is a real ISO date.

    Filters out HeidelTime sentinels such as ``"0000-00-00"`` that PostgreSQL
    rejects when comparing against a date-typed column.
    """
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _find_extractions(conn, celex_id: str, timex_value: str) -> list[dict]:
    """Return heideltime_extractions rows for a CELEX whose timex_value matches.

    Parameters
    ----------
    celex_id:
        CELEX document identifier.
    timex_value:
        Normalised ISO date to match against ``timex_value``.

    Returns
    -------
    list of dicts with keys ``id`` (UUID) and ``timex_text`` (str).
    """
    sql = text(
        "SELECT id, timex_text FROM celex_facts.heideltime_extractions"
        " WHERE celex_id = :celex_id AND timex_type = 'DATE' AND timex_value = :timex_value"
    )
    rows = conn.execute(sql, {"celex_id": celex_id, "timex_value": timex_value}).fetchall()
    return [{"id": UUID(str(r.id)), "timex_text": r.timex_text} for r in rows]


def run(dry_run: bool = False, celex_filter: str | None = None) -> None:
    """Execute the heideltime cartel-period lineage backfill.

    Parameters
    ----------
    dry_run:
        When ``True``, resolves extractions but writes no lineage rows.
    celex_filter:
        If set, only process the given CELEX document.
    """
    engine = make_engine(DATABASE_URL)
    repo = LineageRepository(engine=engine)

    with engine.connect() as conn:
        facts = _fetch_cartel_period_facts(conn, celex_filter)

    logger.info(
        "Found %d %s facts%s",
        len(facts), _FACT_NAME,
        f" (filter: {celex_filter})" if celex_filter else "",
    )

    skipped_no_dates = 0
    skipped_no_extractions = 0
    skipped_existing = 0
    linked = 0
    errors = 0

    for idx, fact in enumerate(facts, start=1):
        fact_id: UUID = fact["id"] if isinstance(fact["id"], UUID) else UUID(str(fact["id"]))
        celex_id: str = fact["celex_id"]

        try:
            dates = _parse_period_dates(fact["value_json"])
            if dates is None:
                logger.warning("Could not parse cartel period for %s — skipping", celex_id)
                skipped_no_dates += 1
                continue

            # De-duplicate so a single-day period (start == end) is queried once,
            # and drop sentinel/invalid boundary dates the DB cannot compare.
            boundary_dates = {d for d in dates if _is_valid_iso_date(d)}
            if not boundary_dates:
                logger.warning(
                    "No valid boundary dates in cartel period %s for %s — skipping",
                    dates, celex_id,
                )
                skipped_no_dates += 1
                continue

            with engine.connect() as conn:
                extractions: dict[UUID, str] = {}
                for boundary_date in boundary_dates:
                    for row in _find_extractions(conn, celex_id, boundary_date):
                        extractions[row["id"]] = row["timex_text"]

            if not extractions:
                logger.debug(
                    "No heideltime_extractions matching %s for %s", dates, celex_id
                )
                skipped_no_extractions += 1
                continue

            for event_id, timex_text in extractions.items():
                if dry_run:
                    logger.debug(
                        "[DRY-RUN] Would link fact %s → heideltime %s (%s)",
                        fact_id, event_id, timex_text,
                    )
                    linked += 1
                    continue

                with repo.Session() as session:
                    source_id = repo.register_heideltime_source(
                        celex_id=celex_id,
                        heideltime_event_id=event_id,
                        description=timex_text,
                        session=session,
                    )
                    if session.get(FactSourceLink, (fact_id, source_id)) is not None:
                        skipped_existing += 1
                        continue
                    repo.link_fact_to_source(
                        fact_id=fact_id,
                        source_id=source_id,
                        extraction_method=_EXTRACTION_METHOD,
                        relation_type="extracted_from",
                        source_type="heideltime",
                        pipeline="heideltime",
                        session=session,
                    )
                    session.commit()
                linked += 1

        except Exception:
            logger.exception("Error processing cartel period for %s", celex_id)
            errors += 1

        if idx % _LOG_INTERVAL == 0:
            logger.info(
                "Progress: %d/%d facts — linked=%d, skipped_no_dates=%d,"
                " skipped_no_extractions=%d, skipped_existing=%d, errors=%d",
                idx, len(facts), linked, skipped_no_dates,
                skipped_no_extractions, skipped_existing, errors,
            )

    logger.info(
        "Fisher complete%s — linked=%d, skipped_no_dates=%d,"
        " skipped_no_extractions=%d, skipped_existing=%d, errors=%d",
        " [DRY-RUN]" if dry_run else "",
        linked, skipped_no_dates, skipped_no_extractions, skipped_existing, errors,
    )


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill heideltime_cartel_period fact lineage from heideltime_extractions"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve extractions without writing any lineage rows",
    )
    parser.add_argument(
        "--celex-id",
        metavar="CELEX_ID",
        default=None,
        help="Restrict the backfill to a single CELEX document",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, celex_filter=args.celex_id)


if __name__ == "__main__":
    main()
