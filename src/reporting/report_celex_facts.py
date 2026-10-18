import logging
import os
import polars as pl
from datetime import datetime
from pipelines.rag.pgvector.vector_config import DATABASE_URL, SCHEMA
from config.settings import settings
from reporting.utils.markdown2text import markdown_to_text

log = logging.getLogger(__name__)


def fetch_and_pivot_celex_facts(db_url: str = DATABASE_URL) -> pl.DataFrame:
    """
    Fetches the latest facts for all CELEX documents from the database
    and pivots them so that celex_id is on the Y-axis and fact names
    are on the X-axis.
    
    Args:
        db_url: The database URL connection string.
        
    Returns:
        A Polars DataFrame containing the pivoted data.
    """
    
    # 1. & 2. Query distinct celex_id AND the latest facts
    # We use ROW_NUMBER() over PARTITION BY celex_id, name to get the latest 
    # created_at for each combination of document and fact type.
    query = f"""
    WITH NewestFacts AS (
        SELECT 
            celex_id,
            name, 
            value_json, 
            created_at,
            ROW_NUMBER() OVER (PARTITION BY celex_id, name ORDER BY created_at DESC) as rn
        FROM {SCHEMA}.facts 
    )
    SELECT 
        celex_id,
        name, 
        value_json
    FROM NewestFacts
    WHERE rn = 1
    """
    # connector-x does not support SQLAlchemy dialects like +psycopg or +psycopg2
    # we need to remove them before calling read_database_uri
    clean_db_url = db_url.replace("+psycopg2", "").replace("+psycopg", "")
    df = pl.read_database_uri(query=query, uri=clean_db_url)
    
    if df.is_empty():
        log.warning("No facts found in the database.")
        return df

    # 3. Pivot the table: celex_id on Y-axis (index), name on X-axis (columns), value_json as values
    pivoted_df = df.pivot(
        values="value_json",
        index="celex_id",
        on="name",
        aggregate_function="first",  # We already filtered for the latest, so 'first' is safe
    )

    return pivoted_df


def fetch_celex_facts_markdown(db_url: str = DATABASE_URL) -> pl.DataFrame:
    """
    Fetches the latest value_md for every (celex_id, name) pair from the facts table
    and pivots them so that celex_id is on the Y-axis and fact names are on the X-axis.

    Args:
        db_url: The database URL connection string.

    Returns:
        A Polars DataFrame with ``celex_id`` as the index column and one column per
        distinct fact name containing the corresponding ``value_md`` value.
    """
    query = f"""
    WITH NewestFacts AS (
        SELECT
            celex_id,
            name,
            value_md,
            ROW_NUMBER() OVER (PARTITION BY celex_id, name ORDER BY created_at DESC) AS rn
        FROM {SCHEMA}.facts
    )
    SELECT
        celex_id,
        name,
        value_md
    FROM NewestFacts
    WHERE rn = 1
    """
    clean_db_url = db_url.replace("+psycopg2", "").replace("+psycopg", "")
    df = pl.read_database_uri(query=query, uri=clean_db_url)

    if df.is_empty():
        log.warning("No value_md facts found in the database.")
        return df

    pivoted_df = df.pivot(
        values="value_md",
        index="celex_id",
        on="name",
        aggregate_function="first",
    )

    return pivoted_df


def convert_markdown_to_plaintext(df_markdown: pl.DataFrame) -> pl.DataFrame:
    """
    Converts all fact-value columns in a markdown-pivoted DataFrame to plain text.

    Applies :func:`reporting.utils.markdown2text.markdown_to_text` to every
    column except ``celex_id``.  ``None`` values are passed through unchanged.

    Args:
        df_markdown: DataFrame produced by :func:`fetch_celex_facts_markdown`,
            with ``celex_id`` as the index column and one column per fact name
            containing ``value_md`` strings.

    Returns:
        A new DataFrame with the same shape but all markdown stripped to plain text.
    """
    fact_columns = [c for c in df_markdown.columns if c != "celex_id"]
    return df_markdown.with_columns(
        [
            pl.col(col).map_elements(
                lambda v: markdown_to_text(v) if v is not None else None,
                return_dtype=pl.String,
            ).alias(col)
            for col in fact_columns
        ]
    )


def fetch_ingestion_errors(db_url: str = DATABASE_URL) -> pl.DataFrame:
    """
    Fetches distinct ingestion errors from the audit trail for failed extractions.

    Returns a flat DataFrame with columns: celex_id, target_fact_name, question,
    output_schema, verify_fact_name, errors, created_at, ordered by celex_id,
    target_fact_name, and created_at descending.

    Args:
        db_url: The database URL connection string.

    Returns:
        A Polars DataFrame with the ingestion error rows.
    """
    query = f"""
    SELECT DISTINCT
        celex_id,
        target_fact_name,
        question,
        output_schema,
        verify_fact_name,
        errors,
        created_at
    FROM {SCHEMA}.fact_ingestion_audit_trail
    WHERE success = false
    ORDER BY celex_id, target_fact_name, created_at DESC
    """
    clean_db_url = db_url.replace("+psycopg2", "").replace("+psycopg", "")
    df = pl.read_database_uri(query=query, uri=clean_db_url)

    if df.is_empty():
        log.warning("No ingestion errors found in the audit trail.")

    return df


def fetch_ingestion_error_pivot(db_url: str = DATABASE_URL) -> pl.DataFrame:
    """
    Fetches a pivot of ingestion outcomes per fact name from the audit trail.

    Returns a DataFrame with columns: target_fact_name, numof_error, numof_success,
    ordered by numof_error descending. Only fact names that have at least one
    failure and one success are included (INNER JOIN semantics).

    Args:
        db_url: The database URL connection string.

    Returns:
        A Polars DataFrame with the error/success counts per fact name.
    """
    query = f"""
    SELECT
        e.target_fact_name,
        e.numof_error,
        s.numof_success
    FROM (
        SELECT target_fact_name, COUNT(1) AS numof_error
        FROM {SCHEMA}.fact_ingestion_audit_trail
        WHERE success = false
        GROUP BY target_fact_name
    ) AS e
    INNER JOIN (
        SELECT target_fact_name, COUNT(1) AS numof_success
        FROM {SCHEMA}.fact_ingestion_audit_trail
        WHERE success = true
        GROUP BY target_fact_name
    ) AS s ON s.target_fact_name = e.target_fact_name
    ORDER BY numof_error DESC
    """
    clean_db_url = db_url.replace("+psycopg2", "").replace("+psycopg", "")
    df = pl.read_database_uri(query=query, uri=clean_db_url)

    if df.is_empty():
        log.warning("No ingestion error pivot data found in the audit trail.")

    return df


def fetch_distinct_fact_names(db_url: str = DATABASE_URL) -> pl.DataFrame:
    """
    Fetches the distinct list of fact names from the facts table, enriched with
    the corresponding question from the question2fact mapping table.

    The result is ordered alphabetically by fact name. Fact names that have no
    entry in ``question2fact`` will have ``NULL`` in the ``question`` column.

    Args:
        db_url: The database URL connection string.

    Returns:
        A Polars DataFrame with columns ``name`` and ``question``.
    """
    query = f"""
    SELECT DISTINCT f.name, q.question
    FROM {SCHEMA}.facts AS f
    LEFT JOIN {SCHEMA}.question2fact AS q ON q.fact_name = f.name
    ORDER BY f.name
    """
    clean_db_url = db_url.replace("+psycopg2", "").replace("+psycopg", "")
    df = pl.read_database_uri(query=query, uri=clean_db_url)

    if df.is_empty():
        log.warning("No fact names found in the database.")

    return df


def export_report_to_excel(
    df_pivoted: pl.DataFrame,
    df_markdown: pl.DataFrame,
    df_plaintext: pl.DataFrame,
    df_fact_names: pl.DataFrame,
    df_ingestion_errors: pl.DataFrame,
    df_ingestion_error_pivot: pl.DataFrame,
    output_dir: str = ".",
) -> str | None:
    """
    Exports DataFrames to a single Excel workbook with six worksheets.

    Sheet 1 – ``Facts (json)``: celex_id vs fact names (value_json).
    Sheet 2 – ``Facts (markdown)``: celex_id vs fact names (value_md).
    Sheet 3 – ``Facts (txt)``: celex_id vs fact names (value_md converted to plain text).
    Sheet 4 – ``fact_names``: distinct list of fact names ordered alphabetically.
    Sheet 5 – ``ingestion_errors``: failed extractions from the audit trail.
    Sheet 6 – ``ingestion_error_pivot``: error/success counts per fact name.

    Args:
        df_pivoted: Pivoted DataFrame produced by :func:`fetch_and_pivot_celex_facts`.
        df_markdown: Pivoted DataFrame produced by :func:`fetch_celex_facts_markdown`.
        df_plaintext: Plain-text DataFrame produced by :func:`convert_markdown_to_plaintext`.
        df_fact_names: DataFrame produced by :func:`fetch_distinct_fact_names`.
        df_ingestion_errors: DataFrame produced by :func:`fetch_ingestion_errors`.
        df_ingestion_error_pivot: DataFrame produced by :func:`fetch_ingestion_error_pivot`.
        output_dir: Directory where the file will be saved.

    Returns:
        Absolute path of the generated file, or ``None`` when all frames are empty.
    """
    if (
        df_pivoted.is_empty()
        and df_markdown.is_empty()
        and df_plaintext.is_empty()
        and df_fact_names.is_empty()
        and df_ingestion_errors.is_empty()
        and df_ingestion_error_pivot.is_empty()
    ):
        log.warning("All DataFrames are empty. Skipping export.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"celex_fact_report_{timestamp}.xlsx"
    filepath = os.path.join(output_dir, filename)

    # polars write_excel accepts an xlsxwriter Workbook so we can populate
    # multiple sheets in one pass before closing the file.
    import xlsxwriter  # local import – optional dependency, already pulled by polars

    workbook = xlsxwriter.Workbook(filepath, {"nan_inf_to_errors": True})

    if not df_pivoted.is_empty():
        df_pivoted.write_excel(workbook=workbook, worksheet="Facts (json)")
    if not df_markdown.is_empty():
        df_markdown.write_excel(workbook=workbook, worksheet="Facts (markdown)")
    if not df_plaintext.is_empty():
        df_plaintext.write_excel(workbook=workbook, worksheet="Facts (txt)")
    if not df_fact_names.is_empty():
        df_fact_names.write_excel(workbook=workbook, worksheet="fact_names")
    if not df_ingestion_errors.is_empty():
        df_ingestion_errors.write_excel(workbook=workbook, worksheet="ingestion_errors")
    if not df_ingestion_error_pivot.is_empty():
        df_ingestion_error_pivot.write_excel(workbook=workbook, worksheet="ingestion_error_pivot")

    workbook.close()

    # log.info("Report saved to: %s", filepath)
    return filepath

def main() -> None:
    """Entry point: fetch facts from the database and write the Excel report."""
    log.info("Starting CELEX facts reporting...")

    if not DATABASE_URL:
        log.error("DATABASE_URL is not set.")
        return

    try:
        output_directory = settings.REPORT_PATH
        os.makedirs(output_directory, exist_ok=True)

        log.info("Fetching pivoted facts from the database...")
        df_pivoted = fetch_and_pivot_celex_facts(db_url=DATABASE_URL)
        log.info("Pivoted data: %d CELEX documents.", df_pivoted.height)

        log.info("Fetching markdown facts from the database...")
        df_markdown = fetch_celex_facts_markdown(db_url=DATABASE_URL)
        log.info("Markdown data: %d rows.", df_markdown.height)

        log.info("Converting markdown facts to plain text...")
        df_plaintext = convert_markdown_to_plaintext(df_markdown)
        log.info("Plain-text data: %d rows.", df_plaintext.height)

        log.info("Fetching distinct fact names from the database...")
        df_fact_names = fetch_distinct_fact_names(db_url=DATABASE_URL)
        log.info("Distinct fact names: %d rows.", df_fact_names.height)

        log.info("Fetching ingestion errors from the audit trail...")
        df_ingestion_errors = fetch_ingestion_errors(db_url=DATABASE_URL)
        log.info("Ingestion errors: %d rows.", df_ingestion_errors.height)

        log.info("Fetching ingestion error pivot from the audit trail...")
        df_ingestion_error_pivot = fetch_ingestion_error_pivot(db_url=DATABASE_URL)
        log.info("Ingestion error pivot: %d rows.", df_ingestion_error_pivot.height)

        filepath = export_report_to_excel(
            df_pivoted=df_pivoted,
            df_markdown=df_markdown,
            df_plaintext=df_plaintext,
            df_fact_names=df_fact_names,
            df_ingestion_errors=df_ingestion_errors,
            df_ingestion_error_pivot=df_ingestion_error_pivot,
            output_dir=output_directory,
        )
        if filepath:
            log.info("Report written to: %s", filepath)

    except Exception as e:
        log.exception("An error occurred during reporting: %s", e)

if __name__ == "__main__":
    main()
