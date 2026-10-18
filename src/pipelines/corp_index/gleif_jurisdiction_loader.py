"""
Load the GLEIF Accepted Legal Jurisdictions code list into
``celex_facts.gleif_jurisdication2countrycode``.

The source is GLEIF's official code list (see
``docs/Data Model Country Code and Jurisdication.md``), whose ``Code`` column is
plain ISO 3166: an ISO 3166-1 alpha-2 country code for national jurisdictions
(``AT``), or an ISO 3166-2 subdivision code for sub-national legal
jurisdictions (``AE-DU``, ``US-DE``). The loader therefore derives the parent
country code by splitting on the hyphen -- no third-party mapping file is
needed -- and stores both parts so the roster's ``iso_country_code`` can join
this table directly.

Self-contained persistence layer, mirroring ``opencorp_repository.py``: this
module owns its declarative ``Base`` and the single ORM model, so creating this
reference table never drags in the OpenCorporates cache tables. The engine is
injectable for tests.

It is a plain CSV -> DB reference-data load: no LLM or DSPy pipeline is
involved, so the DSPy signature rules do not apply.

Exports:
    GleifJurisdiction -- validated row VO that derives the ISO country /
    subdivision parts from a GLEIF jurisdiction code.
    GleifJurisdictionRepository -- DDL + idempotent upsert for the table.
    load_jurisdictions -- parse the CSV into validated VOs.
"""

import argparse
import logging
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config.settings import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

_SCHEMA = settings.DB_SCHEMA
TABLE_NAME = "gleif_jurisdication2countrycode"

# The hyphen that separates the ISO 3166-1 country part from the ISO 3166-2
# subdivision part in a GLEIF jurisdiction code (``AE-DU`` -> ``AE`` + ``DU``).
SUBDIVISION_SEPARATOR = "-"

# A GLEIF jurisdiction code: two uppercase letters, optionally followed by a
# hyphen and a 1-3 character alphanumeric subdivision (subdivisions such as
# PT-20 are numeric). Verified against the whole v1.5 list.
JURISDICTION_CODE_PATTERN = r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$"

# CSV header -> model field. The file ships as UTF-8 with a BOM; polars strips
# it, so the first column name needs no special handling.
CSV_COLUMN_MAP: dict[str, str] = {
    "Jurisdiction": "jurisdiction_name",
    "Code": "jurisdiction_code",
    "Type": "jurisdiction_type",
}


class GleifJurisdiction(BaseModel):
    """One GLEIF-accepted legal jurisdiction with its ISO parts split out.

    Validation boundary for the CSV: the code is normalized and pattern-checked,
    and ``iso_country_code`` / ``iso_subdivision_code`` are derived from it
    rather than supplied, so the two can never disagree with the code.

    Attributes
    ----------
    jurisdiction_code:
        The GLEIF code, uppercased (``AT``, ``AE-DU``).
    jurisdiction_name:
        GLEIF's jurisdiction label (``Dubai (United Arab Emirates)``).
    jurisdiction_type:
        GLEIF's classification: ``COUNTRY_ONLY``,
        ``COUNTRY_AND_SUBDIVISION``, ``EXCEPTIONALLY_RESERVED`` (``EU``,
        ``UN``) or ``USER_ASSIGNED_CODE`` (``XX``).
    iso_country_code:
        The ISO 3166-1 alpha-2 parent country part of the code.
    iso_subdivision_code:
        The ISO 3166-2 subdivision part, or None for a country-level row.
    """

    model_config = {"frozen": True}

    jurisdiction_code: str = Field(
        pattern=JURISDICTION_CODE_PATTERN,
        max_length=6,
        description="GLEIF jurisdiction code, uppercased (AT, AE-DU).",
    )
    jurisdiction_name: str = Field(
        min_length=1, description="GLEIF jurisdiction label."
    )
    jurisdiction_type: str = Field(
        min_length=1,
        description=(
            "GLEIF classification: COUNTRY_ONLY, COUNTRY_AND_SUBDIVISION, "
            "EXCEPTIONALLY_RESERVED or USER_ASSIGNED_CODE."
        ),
    )
    iso_country_code: str = Field(
        min_length=2,
        max_length=2,
        description="ISO 3166-1 alpha-2 parent country part of the code.",
    )
    iso_subdivision_code: str | None = Field(
        default=None,
        max_length=3,
        description="ISO 3166-2 subdivision part, or None at country level.",
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_iso_parts(cls, values: Any) -> Any:
        """Normalize the code and split it into its ISO country / subdivision parts.

        Runs before field validation so the derived values are validated like
        any other field. Non-dict input is passed through untouched for
        pydantic's own error handling.

        Args:
            values: Raw field values, normally a dict from the CSV.

        Returns:
            The values with ``jurisdiction_code`` uppercased/trimmed and the two
            ISO parts filled in.
        """
        if not isinstance(values, dict):
            return values

        code = str(values.get("jurisdiction_code", "")).strip().upper()
        country_part, _, subdivision_part = code.partition(SUBDIVISION_SEPARATOR)
        return {
            **values,
            "jurisdiction_code": code,
            "iso_country_code": country_part,
            "iso_subdivision_code": subdivision_part or None,
        }

    @property
    def is_subdivision(self) -> bool:
        """Whether this row is a sub-national jurisdiction rather than a country."""
        return self.iso_subdivision_code is not None


class GleifJurisdictionRow(Base):
    """ORM mapping for ``celex_facts.gleif_jurisdication2countrycode``.

    ``jurisdiction_code`` is the natural primary key (varchar, per the project's
    identifier convention for keys sourced from an external register).
    """

    __tablename__ = TABLE_NAME
    __table_args__ = (
        Index("ix_gleif_juris_country_code", "iso_country_code"),
        Index("ix_gleif_juris_type", "jurisdiction_type"),
        {"schema": _SCHEMA},
    )

    jurisdiction_code = Column(String(6), primary_key=True)
    jurisdiction_name = Column(Text, nullable=False)
    jurisdiction_type = Column(String(32), nullable=False)
    iso_country_code = Column(String(2), nullable=False)
    iso_subdivision_code = Column(String(3), nullable=True)
    is_subdivision = Column(Boolean, nullable=False)
    source_version = Column(String(16), nullable=False)
    loaded_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def load_jurisdictions(csv_path: str | Path) -> list[GleifJurisdiction]:
    """Parse the GLEIF code-list CSV into validated jurisdiction records.

    Args:
        csv_path: Path to the GLEIF ``acceptedjurisdictions`` CSV. Expected
            header: ``Jurisdiction,Code,Type`` (UTF-8, BOM tolerated).

    Returns:
        One validated :class:`GleifJurisdiction` per data row, in file order.

    Raises:
        FileNotFoundError: If ``csv_path`` does not exist.
        ValueError: If the expected columns are missing.
        pydantic.ValidationError: If a row fails validation (e.g. a code that
            is not ISO 3166 shaped).
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"GLEIF jurisdictions CSV not found: {path}")

    frame = pl.read_csv(path)
    missing = [column for column in CSV_COLUMN_MAP if column not in frame.columns]
    if missing:
        raise ValueError(
            f"GLEIF jurisdictions CSV {path} is missing column(s): {missing}; "
            f"found {frame.columns}"
        )

    records = frame.select(list(CSV_COLUMN_MAP)).rename(CSV_COLUMN_MAP).to_dicts()
    return [GleifJurisdiction(**record) for record in records]


class GleifJurisdictionRepository:
    """DDL and idempotent upsert for the GLEIF jurisdiction reference table.

    Follows the Provisioner pattern: the engine is injected (or built from
    ``settings.DATABASE_URL``) in ``__init__`` rather than created per call.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string used when ``engine`` is not supplied.
    engine:
        Pre-built SQLAlchemy engine (used by tests). When None, an engine is
        created from ``db_url`` with the project search path applied.
    """

    def __init__(
        self, db_url: str = settings.DATABASE_URL, engine: Engine | None = None
    ) -> None:
        self.engine: Engine = engine or create_engine(
            db_url,
            pool_pre_ping=True,
            connect_args={"options": f"-csearch_path={settings.DB_SEARCH_PATH}"},
        )
        self._session_factory = sessionmaker(bind=self.engine)

    def ensure_tables(self) -> None:
        """Create the jurisdiction table if it does not yet exist.

        Idempotent: delegates to ``Base.metadata.create_all``, which issues
        ``CREATE TABLE IF NOT EXISTS``.
        """
        Base.metadata.create_all(self.engine)

    def upsert(
        self,
        jurisdictions: list[GleifJurisdiction],
        source_version: str = settings.GLEIF_JURISDICTIONS_VERSION,
    ) -> int:
        """Insert or refresh one row per jurisdiction, in a single transaction.

        Uses ``INSERT ... ON CONFLICT (jurisdiction_code) DO UPDATE`` so
        re-running the loader (or loading a newer code-list version) refreshes
        names, types and derived ISO parts in place instead of duplicating rows.

        Args:
            jurisdictions: Validated records to write. An empty list is a no-op.
            source_version: GLEIF code-list version recorded on every written
                row for provenance.

        Returns:
            The number of rows submitted.
        """
        if not jurisdictions:
            return 0

        rows = [
            {
                "jurisdiction_code": jurisdiction.jurisdiction_code,
                "jurisdiction_name": jurisdiction.jurisdiction_name,
                "jurisdiction_type": jurisdiction.jurisdiction_type,
                "iso_country_code": jurisdiction.iso_country_code,
                "iso_subdivision_code": jurisdiction.iso_subdivision_code,
                "is_subdivision": jurisdiction.is_subdivision,
                "source_version": source_version,
            }
            for jurisdiction in jurisdictions
        ]
        statement = pg_insert(GleifJurisdictionRow).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=[GleifJurisdictionRow.jurisdiction_code],
            set_={
                "jurisdiction_name": statement.excluded.jurisdiction_name,
                "jurisdiction_type": statement.excluded.jurisdiction_type,
                "iso_country_code": statement.excluded.iso_country_code,
                "iso_subdivision_code": statement.excluded.iso_subdivision_code,
                "is_subdivision": statement.excluded.is_subdivision,
                "source_version": statement.excluded.source_version,
                "loaded_at": func.now(),
            },
        )
        session = self._session_factory()
        try:
            session.execute(statement)
            session.commit()
            return len(rows)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def prune_absent(self, keep_codes: list[str]) -> int:
        """Delete rows whose code is absent from ``keep_codes``.

        For the case where GLEIF retires a jurisdiction code between code-list
        versions; without this a withdrawn code would linger indefinitely.

        Args:
            keep_codes: The jurisdiction codes present in the loaded file. An
                empty list is a no-op (never truncates the table).

        Returns:
            The number of rows deleted.
        """
        if not keep_codes:
            return 0

        session = self._session_factory()
        try:
            deleted = session.query(GleifJurisdictionRow).filter(
                GleifJurisdictionRow.jurisdiction_code.notin_(keep_codes)
            ).delete(synchronize_session=False)
            session.commit()
            return deleted
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def count(self) -> int:
        """Return the number of rows currently in the table."""
        session = self._session_factory()
        try:
            return session.execute(
                select(func.count()).select_from(GleifJurisdictionRow)
            ).scalar_one()
        finally:
            session.close()


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for the GLEIF jurisdiction load.

    Returns:
        The configured :class:`argparse.ArgumentParser`.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Load the GLEIF Accepted Legal Jurisdictions code list into "
            f"{_SCHEMA}.{TABLE_NAME} (idempotent upsert)."
        )
    )
    parser.add_argument(
        "--csv-path",
        default=settings.GLEIF_JURISDICTIONS_CSV_PATH,
        help=(
            "Path to the GLEIF acceptedjurisdictions CSV (default: "
            "settings.GLEIF_JURISDICTIONS_CSV_PATH)."
        ),
    )
    parser.add_argument(
        "--source-version",
        default=settings.GLEIF_JURISDICTIONS_VERSION,
        help=(
            "GLEIF code-list version recorded on every row (default: "
            "settings.GLEIF_JURISDICTIONS_VERSION)."
        ),
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Also delete stored codes that are absent from the CSV.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate the CSV but make no DDL and no writes.",
    )
    return parser


def main() -> None:
    """Entry point: parse the GLEIF code list and upsert it into the table."""
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args()

    jurisdictions = load_jurisdictions(args.csv_path)
    subdivisions = sum(1 for row in jurisdictions if row.is_subdivision)
    logger.info(
        "Parsed %d GLEIF jurisdiction(s) from %s: %d country-level, %d subdivision.",
        len(jurisdictions),
        args.csv_path,
        len(jurisdictions) - subdivisions,
        subdivisions,
    )

    if args.dry_run:
        logger.info("[DRY RUN] No DDL and no writes issued.")
        return

    repository = GleifJurisdictionRepository()
    repository.ensure_tables()
    written = repository.upsert(jurisdictions, source_version=args.source_version)
    logger.info("Upserted %d row(s) into %s.%s", written, _SCHEMA, TABLE_NAME)

    if args.prune:
        pruned = repository.prune_absent(
            [row.jurisdiction_code for row in jurisdictions]
        )
        logger.info("Pruned %d stored code(s) absent from the CSV.", pruned)

    logger.info("%s.%s now holds %d row(s).", _SCHEMA, TABLE_NAME, repository.count())


if __name__ == "__main__":
    main()


"""
# Validate the CSV without touching the database:
PYTHONPATH=src .venv/bin/python \
    src/pipelines/corp_index/gleif_jurisdiction_loader.py --dry-run

# Create the table (if absent) and load the code list:
PYTHONPATH=src .venv/bin/python \
    src/pipelines/corp_index/gleif_jurisdiction_loader.py

# Load a newer code-list version and drop codes GLEIF has retired:
PYTHONPATH=src .venv/bin/python \
    src/pipelines/corp_index/gleif_jurisdiction_loader.py \
    --csv-path ~/a/celex_docs/gleif_acceptedjurisdictions_v1.6.csv \
    --source-version 1.6 --prune
"""
