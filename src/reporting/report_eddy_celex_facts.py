"""Manifest-centred variant of the CELEX facts report.

Joins the downloader loader file (``filtered_final_manifest.xlsx``) with a
facts report workbook written by :mod:`reporting.report_celex_facts` (sheet
``Facts (markdown)``) and explains, per manifest row, why its decision has no
facts in that report.

Join path: ``loader_row`` (1-based position as read by the downloader's
``read_loader``) -> newest download-audit run of that row -> ``decision_id``.
Decisions merged into an owner after the download (``Main_dedupe_registry``,
``Main_decide_source_document``, ``repair_decided_duplicates``) leave a stale
audit id; it is resolved through the ``decision_merge`` ledger (following a
chain to the first registered decision), else through the audited documents,
whose ``document_registry.decision_id`` is the owner.

Usage (from the repo root):
    .venv/bin/python -m reporting.report_eddy_celex_facts \\
        [--manifest PATH] [--facts-report PATH] [--output-dir DIR]

Without --facts-report the standard report (report_celex_facts) is created
first, so the join always sees the current facts.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import polars as pl
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine

from config.settings import settings
from pipelines.download.reader import read_loader
from pipelines.rag.pgvector.session import make_engine
from reporting.report_celex_facts import create_celex_fact_report

log = logging.getLogger(__name__)

FACTS_SHEET = "Facts (markdown)"
OUTPUT_PREFIX = "eddy_celex_fact_report"
SHEET_MANIFEST = "filtered_manifest"
SHEET_NOT_PROCESSED = "not_processed"
SHEET_SUMMARY = "reason_summary"
FACT_COLUMN_SUFFIX = "_fact"
REPORT_ID_COLUMN = "celex_id"

STATUS_PROCESSED = "processed"
STATUS_NOT_PROCESSED = "not_processed"

# Diagnostic columns appended to every manifest row, in sheet order.
DIAGNOSTIC_COLUMNS: tuple[str, ...] = (
    "loader_row", "decision_id", "audit_decision_id", "merged_into",
    "rows_sharing_decision", "report_status", "reason_code", "reason",
)

_LINEAGE_SCHEMA: dict[str, type[pl.DataType]] = {
    "loader_row": pl.Int64, "audit_decision_id": pl.String, "row_failure": pl.String,
    "download_failures": pl.String, "owner_decision_id": pl.String,
}
_STATUS_SCHEMA: dict[str, type[pl.DataType]] = {
    "decision_id": pl.String, "selection_status": pl.String, "review_reasons": pl.String,
    "n_source_docs": pl.Int64, "n_chunks": pl.Int64, "n_facts_db": pl.Int64,
    "n_fact_errors": pl.Int64, "conversion": pl.String, "translation": pl.String,
}

# Per loader row: the audit rows of its newest run, the decision they name,
# the row-level mapper failure, the download failures and the current owner:
# the registered end of the decision's merge chain in decision_merge (nearest
# registered hop; at most _MAX_MERGE_HOPS, which also stops a cycle), else the
# owner of the audited documents (an owner holding the is_source document
# first). A row the planner folded into an earlier row with the same CELEX is
# never audited; its trace is the review reason "merged duplicate row <n> of
# <file> (<id>)" on the surviving decision, which then stands as its audit
# decision.
_MAX_MERGE_HOPS = 16
_LINEAGE_SQL = f"""
WITH RECURSIVE merge_chain AS (
    SELECT merged_decision_id AS start_id, merged_into AS current_id, 1 AS hops
    FROM {settings.DB_SCHEMA}.decision_merge
    UNION ALL
    SELECT c.start_id, m.merged_into, c.hops + 1
    FROM merge_chain AS c
    JOIN {settings.DB_SCHEMA}.decision_merge AS m ON m.merged_decision_id = c.current_id
    WHERE c.hops < {_MAX_MERGE_HOPS}
),
merge_owner AS (
    SELECT DISTINCT ON (c.start_id) c.start_id, c.current_id AS owner_id
    FROM merge_chain AS c
    JOIN {settings.DB_SCHEMA}.decision_registry AS r ON r.decision_id = c.current_id
    ORDER BY c.start_id, c.hops
),
planner_merged AS (
    SELECT DISTINCT ON (loader_row) loader_row, decision_id
    FROM (
        SELECT (regexp_match(r.reason, '^merged duplicate row ([0-9]+) of '))[1]::bigint
                   AS loader_row,
               d.decision_id
        FROM {settings.DB_SCHEMA}.decision_registry AS d,
             unnest(d.review_reasons) AS r(reason)
        WHERE starts_with(r.reason, 'merged duplicate row ')
          AND strpos(r.reason, ' of ' || :loader_file || ' (') > 0
    ) AS m
    ORDER BY loader_row, decision_id
),
row_run AS (
    SELECT DISTINCT ON (loader_row) loader_row, run_id
    FROM {settings.DB_SCHEMA}.document_download_audit
    WHERE loader_file = :loader_file AND loader_row IS NOT NULL
    ORDER BY loader_row, created_at DESC
),
row_audit AS (
    SELECT a.loader_row, a.decision_id, a.document_id, a.outcome, a.failure_reason
    FROM {settings.DB_SCHEMA}.document_download_audit AS a
    JOIN row_run AS r ON r.loader_row = a.loader_row AND r.run_id = a.run_id
    WHERE a.loader_file = :loader_file
),
audited AS (
    SELECT a.loader_row::bigint AS loader_row,
           max(a.decision_id) AS audit_decision_id,
           string_agg(DISTINCT a.failure_reason, ' | ')
               FILTER (WHERE a.decision_id IS NULL) AS row_failure,
           string_agg(DISTINCT a.failure_reason, ' | ')
               FILTER (WHERE a.outcome = 'failed') AS download_failures,
           (array_agg(d.decision_id ORDER BY d.is_source DESC, d.decision_id)
               FILTER (WHERE d.decision_id IS NOT NULL))[1] AS document_owner_id
    FROM row_audit AS a
    LEFT JOIN {settings.DB_SCHEMA}.document_registry AS d ON d.document_id = a.document_id
    GROUP BY a.loader_row
)
SELECT u.loader_row, u.audit_decision_id, u.row_failure, u.download_failures,
       coalesce(o.owner_id, u.document_owner_id) AS owner_decision_id
FROM audited AS u
LEFT JOIN merge_owner AS o ON o.start_id = u.audit_decision_id
UNION ALL
SELECT p.loader_row, p.decision_id, NULL, NULL, coalesce(o.owner_id, p.decision_id)
FROM planner_merged AS p
LEFT JOIN merge_owner AS o ON o.start_id = p.decision_id
WHERE NOT EXISTS (SELECT 1 FROM row_run AS r WHERE r.loader_row = p.loader_row)
"""

# Per registered decision: selection, source documents, chunks, facts, fact
# ingestion failures and the newest conversion / translation outcome.
_STATUS_SQL = f"""
WITH src AS (
    SELECT decision_id, count(*) FILTER (WHERE is_source) AS n_source_docs
    FROM {settings.DB_SCHEMA}.document_registry GROUP BY decision_id
),
chunks AS (
    SELECT celex_id, count(*) AS n_chunks
    FROM {settings.DB_SCHEMA}.{settings.EMBEDDING_TABLE} GROUP BY celex_id
),
facts AS (
    SELECT celex_id, count(DISTINCT name) AS n_facts_db
    FROM {settings.DB_SCHEMA}.facts GROUP BY celex_id
),
fact_errors AS (
    SELECT celex_id, count(*) AS n_fact_errors
    FROM {settings.DB_SCHEMA}.fact_ingestion_audit_trail
    WHERE success = false GROUP BY celex_id
),
conv AS (
    SELECT DISTINCT ON (decision_id) decision_id,
           concat_ws(' ', outcome, failure_reason) AS conversion
    FROM {settings.DB_SCHEMA}.document_conversion_audit
    WHERE decision_id IS NOT NULL
    ORDER BY decision_id, created_at DESC
),
trans AS (
    SELECT DISTINCT ON (decision_id) decision_id,
           concat_ws(' ', outcome, reason) AS translation
    FROM {settings.DB_SCHEMA}.document_translation_audit
    WHERE decision_id IS NOT NULL
    ORDER BY decision_id, created_at DESC
)
SELECT d.decision_id,
       d.selection_status,
       NULLIF(array_to_string(d.review_reasons, '; '), '') AS review_reasons,
       COALESCE(src.n_source_docs, 0)::bigint AS n_source_docs,
       COALESCE(chunks.n_chunks, 0)::bigint AS n_chunks,
       COALESCE(facts.n_facts_db, 0)::bigint AS n_facts_db,
       COALESCE(fact_errors.n_fact_errors, 0)::bigint AS n_fact_errors,
       conv.conversion,
       trans.translation
FROM {settings.DB_SCHEMA}.decision_registry AS d
LEFT JOIN src ON src.decision_id = d.decision_id
LEFT JOIN chunks ON chunks.celex_id = d.decision_id
LEFT JOIN facts ON facts.celex_id = d.decision_id
LEFT JOIN fact_errors ON fact_errors.celex_id = d.decision_id
LEFT JOIN conv ON conv.decision_id = d.decision_id
LEFT JOIN trans ON trans.decision_id = d.decision_id
"""


class EddyReportInputs(BaseModel):
    """Validated input and output locations of one report run."""

    manifest_path: Path = Field(description="Loader file (filtered_final_manifest.xlsx).")
    facts_report_path: Path = Field(description="Facts report workbook with a 'Facts (markdown)' sheet.")
    output_dir: Path = Field(description="Directory the report workbook is written to.")

    @field_validator("manifest_path", "facts_report_path")
    @classmethod
    def _must_exist(cls, value: Path) -> Path:
        """Reject an input file that does not exist.

        Args:
            value: Input path.

        Returns:
            The path unchanged.

        Raises:
            ValueError: If the file does not exist.
        """
        if not value.is_file():
            raise ValueError(f"file not found: {value}")
        return value


def resolve_facts_report(facts_report: Path | None, output_dir: Path) -> Path:
    """Return the given facts report, or create a fresh standard one first.

    Without an explicit workbook the standard report
    (:func:`reporting.report_celex_facts.create_celex_fact_report`) runs
    against the database, so the join sees the current facts.

    Args:
        facts_report: Explicit facts report workbook, or ``None``.
        output_dir: Directory the standard report is written to.

    Returns:
        Path of the facts report workbook to join.

    Raises:
        RuntimeError: If the standard report wrote no workbook (no facts at all).
    """
    if facts_report is not None:
        return facts_report
    log.info("no --facts-report given: creating the standard facts report first")
    created = create_celex_fact_report(output_dir=str(output_dir))
    if created is None:
        raise RuntimeError("standard facts report wrote no workbook (database has no facts)")
    return Path(created)


def read_manifest(path: Path) -> pl.DataFrame:
    """Read the loader file exactly as the downloader numbered its rows.

    Args:
        path: Loader file.

    Returns:
        The loader frame (all strings) with a leading 1-based ``loader_row``.
    """
    return read_loader(path).with_row_index("loader_row", offset=1).with_columns(
        pl.col("loader_row").cast(pl.Int64))


def read_facts_report(path: Path, sheet: str = FACTS_SHEET) -> pl.DataFrame:
    """Read the facts sheet of a facts report workbook as strings.

    Args:
        path: Facts report workbook.
        sheet: Sheet name (default ``Facts (markdown)``).

    Returns:
        Frame with ``celex_id`` and one string column per fact name.

    Raises:
        ValueError: If the sheet lacks the ``celex_id`` column.
    """
    df = pl.read_excel(path, sheet_name=sheet, engine="openpyxl", infer_schema_length=0)
    if REPORT_ID_COLUMN not in df.columns:
        raise ValueError(f"sheet {sheet!r} of {path.name} has no {REPORT_ID_COLUMN} column")
    return df.filter(pl.col(REPORT_ID_COLUMN).is_not_null())


def fetch_row_lineage(engine: Engine, loader_file: str) -> pl.DataFrame:
    """Fetch the download lineage of every audited row of a loader file.

    Args:
        engine: Database engine.
        loader_file: Loader file name as recorded in the audit (basename).

    Returns:
        Frame with loader_row, audit_decision_id, row_failure,
        download_failures and owner_decision_id.
    """
    with engine.connect() as conn:
        return pl.read_database(text(_LINEAGE_SQL), conn, schema_overrides=_LINEAGE_SCHEMA,
                                execute_options={"parameters": {"loader_file": loader_file}})


def fetch_decision_status(engine: Engine) -> pl.DataFrame:
    """Fetch the pipeline status of every registered decision.

    Args:
        engine: Database engine.

    Returns:
        Frame keyed by decision_id with selection_status, review_reasons,
        n_source_docs, n_chunks, n_facts_db, n_fact_errors, conversion and
        translation.
    """
    with engine.connect() as conn:
        return pl.read_database(text(_STATUS_SQL), conn, schema_overrides=_STATUS_SCHEMA)


def _detail(label: str, column: str) -> pl.Expr:
    """Return ``'; <label>: <value>'`` or an empty string when the value is null.

    Args:
        label: Detail label.
        column: Column holding the value.

    Returns:
        String expression.
    """
    return pl.when(pl.col(column).is_not_null()).then(
        pl.lit(f"; {label}: ") + pl.col(column).cast(pl.String)).otherwise(pl.lit(""))


def _reason_code() -> pl.Expr:
    """Return the first failing pipeline stage of a row (the reason ladder).

    Returns:
        String expression naming the reason code.
    """
    sel = pl.col("selection_status")
    return (
        pl.when(pl.col("in_report")).then(pl.lit(STATUS_PROCESSED))
        .when(pl.col("audit_decision_id").is_null()).then(pl.lit("no_decision"))
        .when(sel.is_null()).then(pl.lit("decision_missing"))
        .when(sel == "no_pdf_source").then(pl.lit("no_pdf_source"))
        .when(sel == "undecided").then(pl.lit("undecided"))
        .when(pl.col("n_source_docs") == 0).then(pl.lit("no_source_document"))
        .when(pl.col("n_chunks") == 0).then(pl.lit("not_embedded"))
        .when(pl.col("n_facts_db") > 0).then(pl.lit("facts_not_in_report"))
        .otherwise(pl.lit("no_facts"))
    )


def _reason_text() -> pl.Expr:
    """Return the human-readable reason for the row's reason code.

    Returns:
        String expression; null for processed rows.
    """
    code = pl.col("reason_code")
    merged = pl.when(pl.col("merged_into").is_not_null()).then(
        pl.lit("merged ") + pl.col("audit_decision_id") + pl.lit(" into ")
        + pl.col("merged_into") + pl.lit("; ")).otherwise(pl.lit(""))
    body = (
        pl.when(code == "no_decision").then(
            pl.when(pl.col("row_failure").is_not_null())
            .then(pl.lit("download mapper found no decision: ") + pl.col("row_failure"))
            .otherwise(pl.lit("row absent from the download audit (never run)")))
        .when(code == "decision_missing").then(
            pl.lit("decision ") + pl.col("audit_decision_id")
            + pl.lit(" is no longer registered and none of its documents has an owner"))
        .when(code == "no_pdf_source").then(
            pl.lit("no PDF source document") + _detail("review", "review_reasons"))
        .when(code == "undecided").then(
            pl.lit("source document not decided") + _detail("review", "review_reasons"))
        .when(code == "no_source_document").then(
            pl.lit("no source document registered")
            + _detail("download failures", "download_failures"))
        .when(code == "not_embedded").then(
            pl.lit("no chunks in ") + pl.lit(settings.EMBEDDING_TABLE)
            + _detail("conversion", "conversion") + _detail("translation", "translation"))
        .when(code == "facts_not_in_report").then(
            pl.col("n_facts_db").cast(pl.String)
            + pl.lit(" facts in the database, but the facts report snapshot predates them"))
        .when(code == "no_facts").then(
            pl.lit("embedded (") + pl.col("n_chunks").cast(pl.String)
            + pl.lit(" chunks) but no facts extracted; failed fact ingestions: ")
            + pl.col("n_fact_errors").cast(pl.String))
    )
    return pl.when(code == STATUS_PROCESSED).then(
        pl.when(merged != "").then(merged.str.strip_suffix("; ")))\
        .otherwise(merged + body)


def explain_rows(
    manifest: pl.DataFrame,
    lineage: pl.DataFrame,
    decisions: pl.DataFrame,
    report_ids: pl.Series,
) -> pl.DataFrame:
    """Resolve each manifest row to its current decision and explain its status.

    Args:
        manifest: Frame from :func:`read_manifest`.
        lineage: Frame from :func:`fetch_row_lineage`.
        decisions: Frame from :func:`fetch_decision_status`.
        report_ids: ``celex_id`` values of the facts report.

    Returns:
        ``manifest`` with the :data:`DIAGNOSTIC_COLUMNS` (``loader_row`` kept
        in place) plus the internal status columns used by the reason.
    """
    registered = decisions["decision_id"].implode()
    df = manifest.join(lineage, on="loader_row", how="left").with_columns(
        decision_id=pl.when(pl.col("audit_decision_id").is_in(registered))
        .then(pl.col("audit_decision_id")).otherwise(pl.col("owner_decision_id")),
    ).with_columns(
        merged_into=pl.when(pl.col("decision_id") != pl.col("audit_decision_id"))
        .then(pl.col("decision_id")),
        rows_sharing_decision=pl.when(pl.col("decision_id").is_not_null())
        .then(pl.len().over("decision_id")).cast(pl.Int64),
        in_report=pl.col("decision_id").is_in(report_ids.implode()).fill_null(False),
    ).join(decisions, on="decision_id", how="left")
    df = df.with_columns(reason_code=_reason_code())
    return df.with_columns(
        report_status=pl.when(pl.col("reason_code") == STATUS_PROCESSED)
        .then(pl.lit(STATUS_PROCESSED)).otherwise(pl.lit(STATUS_NOT_PROCESSED)),
        reason=_reason_text(),
    )


def build_extended_manifest(explained: pl.DataFrame, facts: pl.DataFrame,
                            manifest_columns: list[str]) -> pl.DataFrame:
    """Build the extended ``filtered_manifest`` sheet.

    Args:
        explained: Frame from :func:`explain_rows`.
        facts: Frame from :func:`read_facts_report`.
        manifest_columns: Original manifest column names, in order.

    Returns:
        Manifest columns, then the diagnostic columns, then one column per
        fact (suffixed with ``_fact`` where a name collides).
    """
    base = explained.select(*manifest_columns, *[c for c in DIAGNOSTIC_COLUMNS if c != "loader_row"])
    taken = set(base.columns)
    facts = facts.rename({REPORT_ID_COLUMN: "decision_id"} | {
        c: f"{c}{FACT_COLUMN_SUFFIX}" for c in facts.columns
        if c != REPORT_ID_COLUMN and c in taken})
    out = base.join(facts, on="decision_id", how="left", maintain_order="left")
    return out.select(pl.col("loader_row"), pl.exclude("loader_row"))


def build_not_processed(explained: pl.DataFrame) -> pl.DataFrame:
    """Build the ``not_processed`` sheet: one commented line per missing row.

    Args:
        explained: Frame from :func:`explain_rows`.

    Returns:
        Identifying manifest columns, the decision ids, the reason and the
        stage counters of every row whose decision is not in the report.
    """
    keep = ["loader_row", "manifest_row_id", "celex", "case_numbers", "date", "title",
            "decision_id", "audit_decision_id", "merged_into", "reason_code", "reason",
            "selection_status", "n_source_docs", "n_chunks", "n_facts_db", "n_fact_errors"]
    return explained.filter(pl.col("report_status") == STATUS_NOT_PROCESSED).select(
        [c for c in keep if c in explained.columns])


def build_reason_summary(explained: pl.DataFrame) -> pl.DataFrame:
    """Count manifest rows per reason code.

    Args:
        explained: Frame from :func:`explain_rows`.

    Returns:
        Frame with reason_code and rows, largest first.
    """
    return explained.group_by("reason_code").agg(rows=pl.len()).sort(
        ["rows", "reason_code"], descending=[True, False])


def export_eddy_report(sheets: dict[str, pl.DataFrame], output_dir: Path) -> Path:
    """Write the report sheets to one timestamped workbook.

    Args:
        sheets: Sheet name -> frame, in sheet order; empty frames are skipped.
        output_dir: Target directory (created if missing).

    Returns:
        Path of the written workbook.
    """
    import xlsxwriter  # optional dependency of polars write_excel

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{OUTPUT_PREFIX}_{datetime.now():%Y%m%d%H%M%S}.xlsx"
    # strings_to_urls off: pipe-joined source_url cells exceed Excel's URL limit.
    workbook = xlsxwriter.Workbook(str(path), {"nan_inf_to_errors": True, "strings_to_urls": False})
    try:
        for name, frame in sheets.items():
            if not frame.is_empty():
                frame.write_excel(workbook=workbook, worksheet=name)
    finally:
        workbook.close()
    return path


def build_report(inputs: EddyReportInputs, engine: Engine) -> Path:
    """Read the inputs, resolve and explain every manifest row, write the workbook.

    Args:
        inputs: Validated input/output locations.
        engine: Database engine for the lineage and status queries.

    Returns:
        Path of the written workbook.
    """
    manifest = read_manifest(inputs.manifest_path)
    facts = read_facts_report(inputs.facts_report_path)
    lineage = fetch_row_lineage(engine, inputs.manifest_path.name)
    decisions = fetch_decision_status(engine)
    log.info("manifest %d rows, facts report %d decisions, audit %d rows, registry %d decisions",
             manifest.height, facts.height, lineage.height, decisions.height)
    explained = explain_rows(manifest, lineage, decisions, facts[REPORT_ID_COLUMN])
    summary = build_reason_summary(explained)
    for code, rows in summary.iter_rows():
        log.info("%-22s %d", code, rows)
    return export_eddy_report({
        SHEET_MANIFEST: build_extended_manifest(explained, facts, manifest.columns),
        SHEET_NOT_PROCESSED: build_not_processed(explained),
        SHEET_SUMMARY: summary,
    }, inputs.output_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        Namespace with manifest, facts_report and output_dir.
    """
    p = argparse.ArgumentParser(description="Join the loader manifest with a facts report.")
    p.add_argument("--manifest", type=Path, default=Path(settings.REPORT_EDDY_MANIFEST_PATH),
                   help="Loader file (default settings.REPORT_EDDY_MANIFEST_PATH).")
    p.add_argument("--facts-report", type=Path, default=None,
                   help="Facts report workbook (default: create the standard report first, "
                        "into --output-dir).")
    p.add_argument("--output-dir", type=Path, default=Path(settings.REPORT_PATH),
                   help="Output directory (default settings.REPORT_PATH).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: build the manifest-centred facts report.

    Args:
        argv: Command-line arguments.

    Returns:
        Process exit code (0 on success).
    """
    args = parse_args(argv)
    inputs = EddyReportInputs(
        manifest_path=args.manifest,
        facts_report_path=resolve_facts_report(args.facts_report, args.output_dir),
        output_dir=args.output_dir,
    )
    log.info("manifest %s, facts report %s", inputs.manifest_path, inputs.facts_report_path)
    engine = make_engine()
    try:
        path = build_report(inputs, engine)
    finally:
        engine.dispose()
    log.info("Report written to: %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
