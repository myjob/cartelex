"""
Bulk-load ``celex_facts.celex_case_id`` from a CSV file.

This is a one-off data-load script. The connection string comes from
``settings.DATABASE_URL`` via ``make_engine`` in ``session.py``, which also
supplies the shared connection pool and the project search_path.
"""

import logging

import polars as pl
from sqlalchemy import text
from pathlib import Path

from config.settings import settings
from pipelines.rag.pgvector.session import make_engine

logger = logging.getLogger(__name__)


def main() -> None:
    """Truncate and reload ``celex_facts.celex_case_id`` from the CSV export.

    Reads ``celex2case_id.csv`` from ``settings.CELEX_DOCS_DIR`` and replaces
    the whole table contents in a single transaction, then verifies the row
    count against the CSV.

    Returns:
        None.
    """
    engine = make_engine()

    csv_file = str(Path(settings.CELEX_DOCS_DIR) / "celex2case_id.csv")

    try:
        # dropping any leading zeros and breaking the join against varchar
        # columns downstream.
        df = pl.read_csv(csv_file, infer_schema_length=0)

        # Rename columns to match database schema if necessary
        # CSV header: celex,case.x,case_id
        # DB columns: celex_id, case_x, case_id
        df = df.rename({
            "celex": "celex_id",
            "case.x": "case_x"
        })

        # Ensure we only have the columns we want
        df = df.select(["celex_id", "case_x", "case_id"])

        # Insert data into the database
        insert_stmt = text(
            "INSERT INTO celex_facts.celex_case_id (celex_id, case_x, case_id) "
            "VALUES (:celex_id, :case_x, :case_id)"
        )
        with engine.begin() as connection:
            # Clear table first
            connection.execute(text("TRUNCATE TABLE celex_facts.celex_case_id"))

            connection.execute(insert_stmt, df.to_dicts())

        logger.info(
            "Successfully loaded %d rows into celex_facts.celex_case_id", len(df)
        )

        # Validate the load
        with engine.connect() as connection:
            result = connection.execute(
                text("SELECT COUNT(*) FROM celex_facts.celex_case_id")
            ).scalar()
            logger.info("Validation: table row count is %d", result)

            if result == len(df):
                logger.info("Validation successful: row count matches CSV.")
            else:
                logger.error(
                    "Validation failed: expected %d, found %d", len(df), result
                )

    except Exception as e:
        logger.error("Error loading data: %s", e)


if __name__ == "__main__":
    main()
