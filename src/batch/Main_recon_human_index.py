"""
Reconcile the human case index against the extracted facts.

Reads the hand-maintained workbook (``settings.HUMAN_INDEX_XLSX_PATH``), compares
four of its columns with their extracted counterparts
(:mod:`pipelines.legal.human_index_recon`) and stores one row per
(decision, target) in ``celex_facts.human_index_recon``. After a run the table
holds the human value, the extracted value and the outcome side by side, so the
workbook does not have to be opened again.

No LLM is involved: the run is deterministic and takes a few seconds.

Idempotency
-----------
Rows upsert on ``(celex_id, target)``. A human ``verdict`` already recorded on a
row is kept while both compared values are unchanged.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/Main_recon_human_index.py --dry-run
    PYTHONPATH=src .venv/bin/python src/batch/Main_recon_human_index.py
    PYTHONPATH=src .venv/bin/python src/batch/Main_recon_human_index.py --celex-id 39125_2726_3
    PYTHONPATH=src .venv/bin/python src/batch/Main_recon_human_index.py --xlsx /path/to/index.xlsx
"""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

import openpyxl
import polars as pl

from config.settings import settings
from pipelines.legal.human_index_recon import (
    FACT_COMPANIES,
    RECON_FACT_NAMES,
    ExtractedFacts,
    HumanIndexRow,
    reconcile_row,
    size_band,
)
from pipelines.legal.undertaking_count import parse_roster
from pipelines.rag.pgvector.human_index_recon_repository import (
    HumanIndexReconRepository,
    ReconRecord,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("recon_human_index")

# Worksheet header of each HumanIndexRow field.
_COLUMNS: dict[str, str] = {
    "celex_id": "celex",
    "title": "title",
    "decision_year": "year",
    "formal_decision": "Formal Decision",
    "nature_of_conduct": "Nature of Conduct",
    "conduct_indicator": "Indicator_Conduct",
    "responsible_commissioner": "Responsible Commissioner",
    "firms": "firms",
}
_ROW_NUMBER = "source_row"
_FIRST_DATA_ROW = 2


def read_human_index(path: str | Path) -> pl.DataFrame:
    """Read the columns the reconciliation needs from the index workbook.

    The workbook is read with openpyxl in streaming mode: its stored dimension
    record is wrong (it claims a single cell) and it references a drawing part
    that is missing from the archive, which the eager readers refuse.

    Parameters
    ----------
    path:
        The ``.xlsx`` file; its first worksheet is read.

    Returns
    -------
    pl.DataFrame
        One row per worksheet row that has an identifier, all values as
        strings, with the columns of ``HumanIndexRow`` plus ``source_row``.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the worksheet lacks one of the required headers.
    """
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        sheet.reset_dimensions()
        rows = sheet.iter_rows(values_only=True)
        header = [str(cell) if cell is not None else "" for cell in next(rows, ())]
        missing = sorted(set(_COLUMNS.values()) - set(header))
        if missing:
            raise ValueError(f"{path}: worksheet lacks the column(s) {missing}")
        position = {field: header.index(column) for field, column in _COLUMNS.items()}
        records = [
            {
                _ROW_NUMBER: number,
                **{
                    field: None if index >= len(row) or row[index] is None else str(row[index])
                    for field, index in position.items()
                },
            }
            for number, row in enumerate(rows, start=_FIRST_DATA_ROW)
        ]
    finally:
        workbook.close()
    schema = {_ROW_NUMBER: pl.Int64, **dict.fromkeys(_COLUMNS, pl.Utf8)}
    frame = pl.DataFrame(records, schema=schema)
    return frame.filter(pl.col("celex_id").str.strip_chars().str.len_chars() > 0)


def to_index_rows(frame: pl.DataFrame) -> tuple[list[HumanIndexRow], list[str]]:
    """Validate the worksheet rows and drop repeated identifiers.

    The index lists one decision twice under two case numbers; the first row
    wins so the result has one row per decision.

    Parameters
    ----------
    frame:
        Output of :func:`read_human_index`.

    Returns
    -------
    tuple[list[HumanIndexRow], list[str]]
        The validated rows in sheet order, and the identifiers that occurred
        more than once.
    """
    duplicates = (
        frame.group_by("celex_id").len().filter(pl.col("len") > 1).get_column("celex_id").to_list()
    )
    unique = frame.unique(subset="celex_id", keep="first", maintain_order=True)
    year = pl.col("decision_year").cast(pl.Int64, strict=False)
    rows = [HumanIndexRow(**record) for record in unique.with_columns(year).to_dicts()]
    return rows, sorted(duplicates)


def build_records(
    rows: list[HumanIndexRow],
    fact_values: dict[str, dict[str, tuple[str | None, str | None]]],
    content_tokens: dict[str, int],
    source_file: str,
) -> list[ReconRecord]:
    """Reconcile every index row and attach the decision context.

    Parameters
    ----------
    rows:
        Validated human index rows.
    fact_values:
        ``celex_id -> fact name -> (value_md, value_json)``.
    content_tokens:
        ``celex_id -> stored token total``.
    source_file:
        File name of the workbook, stored as provenance.

    Returns
    -------
    list[ReconRecord]
        One record per (decision, target).
    """
    records: list[ReconRecord] = []
    for row in rows:
        stored = fact_values.get(row.celex_id, {})
        roster_json = stored.get(FACT_COMPANIES, (None, None))[1]
        facts = ExtractedFacts(
            values={
                name: value_md
                for name, (value_md, _) in stored.items()
                if name != FACT_COMPANIES and value_md is not None
            },
            roster=parse_roster(roster_json),
        )
        tokens = content_tokens.get(row.celex_id)
        band = size_band(
            tokens, settings.SIZE_BAND_MEDIUM_FROM_TOKENS, settings.SIZE_BAND_LARGE_FROM_TOKENS
        )
        records.extend(
            ReconRecord(
                result=result,
                title=row.title,
                decision_year=row.decision_year,
                content_tokens=tokens,
                size_band=band,
                source_file=source_file,
                source_row=row.source_row,
            )
            for result in reconcile_row(row, facts)
        )
    return records


def _log_summary(records: list[ReconRecord]) -> None:
    """Log the number of records per target and status."""
    counts = Counter((r.result.target.value, r.result.status.value) for r in records)
    for target in sorted({target for target, _ in counts}):
        per_status = {status: n for (t, status), n in sorted(counts.items()) if t == target}
        logger.info("%-26s %s", target, per_status)


def run(
    xlsx_path: str = settings.HUMAN_INDEX_XLSX_PATH,
    dry_run: bool = False,
    celex_filter: str | None = None,
    repository: HumanIndexReconRepository | None = None,
) -> list[ReconRecord]:
    """Reconcile the human index with the extracted facts and store the outcome.

    Parameters
    ----------
    xlsx_path:
        The index workbook.
    dry_run:
        When ``True``, compare and log the summary but write nothing; the
        target table need not exist.
    celex_filter:
        If set, only this decision is reconciled.
    repository:
        Injected repository (Provisioner pattern); built from settings when
        omitted.

    Returns
    -------
    list[ReconRecord]
        The reconciled records, whether or not they were written.

    Raises
    ------
    FileNotFoundError
        If the workbook does not exist.
    RuntimeError
        If the target table is missing on a non-dry run.
    """
    repository = repository or HumanIndexReconRepository()
    if not dry_run:
        repository.probe_table()

    rows, duplicates = to_index_rows(read_human_index(xlsx_path))
    if duplicates:
        logger.warning("Identifier listed more than once, first row kept: %s", duplicates)
    if celex_filter:
        rows = [row for row in rows if row.celex_id == celex_filter]
    logger.info("Reconciling %d decision(s) from %s", len(rows), xlsx_path)

    records = build_records(
        rows,
        repository.select_fact_values(RECON_FACT_NAMES),
        repository.select_content_tokens(),
        Path(xlsx_path).name,
    )
    _log_summary(records)

    if dry_run:
        logger.info("[DRY-RUN] %d row(s) compared, nothing written", len(records))
        return records
    written = repository.upsert_records(records)
    logger.info("Wrote %d row(s) to celex_facts.human_index_recon", written)
    return records


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Reconcile the human case index against the extracted facts."
    )
    parser.add_argument(
        "--xlsx",
        metavar="PATH",
        default=settings.HUMAN_INDEX_XLSX_PATH,
        help="Index workbook (default: settings.HUMAN_INDEX_XLSX_PATH)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Compare and log the summary; write nothing"
    )
    parser.add_argument(
        "--celex-id", metavar="CELEX_ID", default=None, help="Reconcile a single decision"
    )
    args = parser.parse_args()
    run(xlsx_path=args.xlsx, dry_run=args.dry_run, celex_filter=args.celex_id)


if __name__ == "__main__":
    main()
