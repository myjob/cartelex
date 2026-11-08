"""Fact completeness check for a list of decisions.

Answers "are all facts extracted for these decisions?" by comparing the facts
selected by a default ``Main_ingest_generic_facts`` run (the configs flagged
``run_state_active`` in ``FACT_EXTRACTOR.yaml``) against
``celex_facts.facts``.

A missing fact is either:

* **conditional** -- its config carries a ``condition``; absence is expected
  when the condition is not met. The audit trail records attempts only, never
  skips, so the check cannot tell a skip from a gap; it reports both apart.
* **unconditional** -- a genuine gap, usually caused by a predecessor whose
  value is empty (``NA``/``[]``/``{}``), which the ingest cascade treats as
  unfulfilled. Such empty predecessors are listed separately.

Usage (from project root):

    .venv/bin/python -m tools.fact_completeness_checker \\
        --celex-list-file reports/celex_list_new_decisions_20261101.txt
    .venv/bin/python -m tools.fact_completeness_checker \\
        --celex-list-file <file> --csv reports/fact_gaps.csv
"""

import argparse
import logging
from pathlib import Path

import polars as pl
import yaml
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from config.settings import settings
from pipelines.rag.pgvector.session import make_engine

logger = logging.getLogger(__name__)

# Fact outputs the ingest cascade treats as unfulfilled (mirrors
# ``Main_ingest_generic_facts._has_value``).
EMPTY_FACT_VALUES = frozenset({"", "[]", "{}", "NA"})

_FACT_ROWS_SQL = text(
    """
    SELECT celex_id, name, value_md
    FROM celex_facts.facts
    WHERE celex_id = ANY(:ids)
    """
)

_LAST_AUDIT_SQL = text(
    """
    SELECT DISTINCT ON (celex_id, target_fact_name)
           celex_id, target_fact_name, success, errors, created_at
    FROM celex_facts.fact_ingestion_audit_trail
    WHERE celex_id = ANY(:ids)
    ORDER BY celex_id, target_fact_name, created_at DESC
    """
)


class FactCatalogue(BaseModel):
    """The facts a default ingest run is expected to produce."""

    active: list[str] = Field(description="Fact names flagged run_state_active, in YAML order")
    conditional: frozenset[str] = Field(description="Active fact names gated by a condition")


class FactRow(BaseModel):
    """One stored fact of a decision (subset of ``celex_facts.facts``)."""

    celex_id: str = Field(description="Decision key")
    name: str = Field(description="Fact name")
    value_md: str | None = Field(default=None, description="Markdown value of the fact")


class AuditRow(BaseModel):
    """The latest ingestion attempt of one fact for one decision."""

    celex_id: str = Field(description="Decision key")
    target_fact_name: str = Field(description="Fact name")
    success: bool | None = Field(default=None, description="Whether the attempt succeeded")
    errors: str | None = Field(default=None, description="Error text of the attempt")


def read_celex_list(path: Path) -> list[str]:
    """Read decision ids from a ``--celex-list-file`` style file.

    Args:
        path: Text file with one id per line; blank and ``#`` lines are skipped.

    Returns:
        The ids in file order, without duplicates.

    Raises:
        OSError: If the file cannot be read.
    """
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped not in ids:
            ids.append(stripped)
    return ids


def load_catalogue(path: Path = settings.FACT_EXTRACTOR_CONFIG_DIR) -> FactCatalogue:
    """Load the active fact catalogue from ``FACT_EXTRACTOR.yaml``.

    Args:
        path: Location of the fact extractor YAML.

    Returns:
        The active fact names and the conditional subset.

    Raises:
        OSError: If the YAML cannot be read.
        yaml.YAMLError: If the YAML is malformed.
    """
    structs = yaml.safe_load(path.read_text(encoding="utf-8"))
    active = [s["target_fact_name"] for s in structs if s.get("run_state_active", True)]
    conditional = frozenset(
        s["target_fact_name"] for s in structs if s.get("condition") and s["target_fact_name"] in active
    )
    return FactCatalogue(active=active, conditional=conditional)


def fetch_rows(engine: Engine, celex_ids: list[str]) -> tuple[list[FactRow], list[AuditRow]]:
    """Fetch stored facts and the latest audit attempt per fact.

    Args:
        engine: SQLAlchemy engine on the celex_facts database.
        celex_ids: Decisions to inspect.

    Returns:
        A tuple of (fact rows, latest audit rows).

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database failures.
    """
    with engine.connect() as conn:
        facts = [FactRow(**r._mapping) for r in conn.execute(_FACT_ROWS_SQL, {"ids": celex_ids})]
        audit = [AuditRow(**r._mapping) for r in conn.execute(_LAST_AUDIT_SQL, {"ids": celex_ids})]
    return facts, audit


def build_completeness_frame(
    celex_ids: list[str],
    catalogue: FactCatalogue,
    facts: list[FactRow],
    audit: list[AuditRow],
) -> pl.DataFrame:
    """Cross every decision with every active fact.

    Args:
        celex_ids: Decisions to inspect.
        catalogue: Expected facts.
        facts: Stored fact rows of those decisions.
        audit: Latest audit attempt per (decision, fact).

    Returns:
        One row per (celex_id, fact) with columns ``conditional``,
        ``present``, ``empty_value`` (stored but NA-like), ``n_rows``,
        ``last_audit_ok`` and ``last_error``.
    """
    stored: dict[tuple[str, str], list[str | None]] = {}
    for row in facts:
        stored.setdefault((row.celex_id, row.name), []).append(row.value_md)
    last = {(a.celex_id, a.target_fact_name): a for a in audit}
    records = []
    for celex_id in celex_ids:
        for fact in catalogue.active:
            values = stored.get((celex_id, fact), [])
            attempt = last.get((celex_id, fact))
            records.append(
                {
                    "celex_id": celex_id,
                    "fact": fact,
                    "conditional": fact in catalogue.conditional,
                    "present": bool(values),
                    "empty_value": bool(values)
                    and all((v or "").strip() in EMPTY_FACT_VALUES for v in values),
                    "n_rows": len(values),
                    "last_audit_ok": attempt.success if attempt else None,
                    "last_error": (attempt.errors or "")[:160] if attempt else "",
                }
            )
    return pl.DataFrame(
        records,
        schema={
            "celex_id": pl.String,
            "fact": pl.String,
            "conditional": pl.Boolean,
            "present": pl.Boolean,
            "empty_value": pl.Boolean,
            "n_rows": pl.Int64,
            "last_audit_ok": pl.Boolean,
            "last_error": pl.String,
        },
    )


def summarize_per_decision(frame: pl.DataFrame) -> pl.DataFrame:
    """Count present and missing facts per decision.

    Args:
        frame: Output of :func:`build_completeness_frame`.

    Returns:
        One row per decision with ``have``, ``missing_uncond``,
        ``missing_cond`` and ``empty_values``, gaps first.
    """
    missing = ~pl.col("present")
    return (
        frame.group_by("celex_id", maintain_order=True)
        .agg(
            pl.col("present").sum().alias("have"),
            (missing & ~pl.col("conditional")).sum().alias("missing_uncond"),
            (missing & pl.col("conditional")).sum().alias("missing_cond"),
            pl.col("empty_value").sum().alias("empty_values"),
        )
        .sort(["missing_uncond", "celex_id"], descending=[True, False])
    )


def unconditional_gaps(frame: pl.DataFrame) -> pl.DataFrame:
    """Return the missing facts that are not gated by a condition.

    Args:
        frame: Output of :func:`build_completeness_frame`.

    Returns:
        The gap rows with their latest audit outcome.
    """
    return frame.filter(~pl.col("present") & ~pl.col("conditional")).select(
        "celex_id", "fact", "last_audit_ok", "last_error"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--celex-list-file", type=Path, required=True, help="One decision id per line")
    parser.add_argument("--csv", type=Path, help="Write the full (celex_id, fact) frame to this CSV")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run the completeness check and log the report.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args(argv)
    celex_ids = read_celex_list(args.celex_list_file)
    catalogue = load_catalogue()
    facts, audit = fetch_rows(make_engine(), celex_ids)
    frame = build_completeness_frame(celex_ids, catalogue, facts, audit)

    unknown = sorted(set(celex_ids) - {f.celex_id for f in facts})
    with pl.Config(tbl_rows=-1, tbl_width_chars=250, fmt_str_lengths=160):
        logger.info("decisions: %d  active facts: %d  without any fact: %s", len(celex_ids), len(catalogue.active), unknown)
        logger.info("%s", summarize_per_decision(frame))
        logger.info(
            "missing facts by name:\n%s",
            frame.filter(~pl.col("present"))
            .group_by("fact", "conditional")
            .agg(pl.len().alias("n_missing"), pl.col("last_audit_ok").is_null().sum().alias("never_attempted"))
            .sort("n_missing", descending=True),
        )
        logger.info("unconditional gaps:\n%s", unconditional_gaps(frame))
        logger.info(
            "empty (NA-like) stored values:\n%s",
            frame.filter(pl.col("empty_value")).group_by("fact").agg(pl.col("celex_id")).sort("fact"),
        )
        duplicates = frame.filter(pl.col("n_rows") > 1).select("celex_id", "fact", "n_rows")
        logger.info("duplicate fact rows: %d", duplicates.height)
    if args.csv:
        frame.write_csv(args.csv)
        logger.info("wrote %s", args.csv)


if __name__ == "__main__":
    main()
