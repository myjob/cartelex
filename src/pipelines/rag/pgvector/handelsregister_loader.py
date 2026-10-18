"""
Load FTM JSONL entities from de_offeneregister into handelsregister PostgreSQL tables.

Source file format: FollowTheMoney (FTM) JSONL — one JSON object per line.

Entity schemas handled:
  Company, Organization → handelsregister_companies + handelsregister_previous_names
  Person                → session-scoped temp table (hr_persons_tmp)
  Directorship          → session-scoped temp table (hr_directorships_tmp)
                        → handelsregister_officers (via SQL JOIN after full stream)

All other FTM schema types are skipped.
The handelsregister_company_additional_data table is not populated because the
boolean publication flags (AD, CD, DK, …) are absent from the FTM format.

Strategy:
  A single streaming pass populates the companies / previous-names tables and
  two session-scoped temp tables.  After the stream, one SQL INSERT … SELECT
  JOIN resolves director ids against person data and writes handelsregister_officers.
  Temp tables are dropped when done.
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

_SCHEMA_SQL = Path(__file__).parent / "handelsregister-schema.sql"
_COMPANY_SCHEMAS: frozenset[str] = frozenset({"Company", "Organization"})
_BATCH_SIZE = 2_000

# Matches the German commercial-register type abbreviation inside a string.
_REG_ART_RE = re.compile(r"\b(HRB|HRA|GnR|GenR|VR|PR)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _first(props: dict[str, list[str]], key: str) -> str | None:
    """Return the first element of a property list, or None if absent or empty.

    Args:
        props: FTM entity properties dict (values are always lists).
        key:   Property name to look up.

    Returns:
        First string value, or None.
    """
    vals = props.get(key)
    return vals[0] if vals else None


def _parse_reg_numbers(reg_numbers: list[str]) -> dict[str, str | None]:
    """Decompose FTM registrationNumber values into structured fields.

    FTM typically provides two forms:
      - short: "HRB 16509"
      - long:  "Aachen 73 HRB 16509"  (court name + court code + type + number)

    Args:
        reg_numbers: List of registration-number strings from FTM properties.

    Returns:
        Dict with keys: register_art, register_nummer, registrar,
        native_company_number (the short form).
    """
    empty: dict[str, str | None] = {
        "register_art": None,
        "register_nummer": None,
        "registrar": None,
        "native_company_number": None,
    }
    if not reg_numbers:
        return empty

    short_form: str | None = None
    long_form: str | None = None

    for r in reg_numbers:
        m = _REG_ART_RE.search(r)
        if not m:
            continue
        before = r[: m.start()].strip()
        # Long form has a court name before the reg-art; short form has nothing
        # or only a numeric court code.
        if before and not re.fullmatch(r"\d+", before):
            long_form = r
        else:
            short_form = r

    # Derive short form from long form when only the long form is present.
    if short_form is None and long_form is not None:
        m = _REG_ART_RE.search(long_form)
        if m:
            short_form = long_form[m.start():].strip()

    reg_art: str | None = None
    reg_nummer: str | None = None
    if short_form:
        m = _REG_ART_RE.match(short_form)
        if m:
            reg_art = m.group(0).upper()
            reg_nummer = short_form[m.end():].strip() or None

    registrar: str | None = None
    if long_form is not None:
        m = _REG_ART_RE.search(long_form)
        if m:
            prefix = long_form[: m.start()].strip()
            # Strip trailing court code (e.g. " 73") to isolate the city name.
            registrar = re.sub(r"\s+\d+\s*$", "", prefix).strip() or None

    return {
        "register_art": reg_art,
        "register_nummer": reg_nummer,
        "registrar": registrar,
        "native_company_number": short_form,
    }


def _make_company_row(obj: dict[str, Any]) -> dict[str, Any]:
    """Map an FTM Company/Organization entity to a handelsregister_companies row.

    Args:
        obj: Parsed FTM entity dict (schema must be Company or Organization).

    Returns:
        Dict keyed by handelsregister_companies column names.
    """
    props: dict[str, list[str]] = obj.get("properties", {})
    reg = _parse_reg_numbers(props.get("registrationNumber", []))

    return {
        "company_number": obj["id"],
        "name": _first(props, "name") or obj.get("caption", ""),
        "current_status": _first(props, "status"),
        "jurisdiction_code": _first(props, "jurisdiction"),
        "registered_address": _first(props, "address"),
        "retrieved_at": _first(props, "retrievedAt"),
        "register_art": reg["register_art"],
        "register_nummer": reg["register_nummer"],
        # federal_state is not present in the FTM format for this dataset.
        "federal_state": None,
        "native_company_number": reg["native_company_number"],
        # registered_office and former_registrar are absent from FTM format.
        "registered_office": None,
        "registrar": reg["registrar"],
        "former_registrar": None,
    }


# ---------------------------------------------------------------------------
# Loader class
# ---------------------------------------------------------------------------


class HandelsregisterLoader:
    """Streams a de_offeneregister FTM JSONL file into handelsregister tables.

    Follows the Provisioner pattern: the DB engine is created in __init__ and
    injected into all operations.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Defaults to the project DATABASE_URL.
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)

    # ------------------------------------------------------------------ schema

    def ensure_schema(self) -> None:
        """Execute the handelsregister DDL to create tables if they do not exist."""
        ddl = _SCHEMA_SQL.read_text(encoding="utf-8")
        with self.engine.connect() as conn:
            conn.execute(text(ddl))
            conn.commit()

    def _create_temp_tables(self, conn: Any) -> None:
        """Create session-scoped temp tables for persons and directorships.

        Args:
            conn: Open SQLAlchemy connection within which the tables are created.
        """
        conn.execute(text("""
            CREATE TEMP TABLE IF NOT EXISTS hr_persons_tmp (
                id          TEXT PRIMARY KEY,
                name        TEXT,
                firstname   TEXT,
                lastname    TEXT,
                city        TEXT,
                description TEXT
            ) ON COMMIT PRESERVE ROWS
        """))
        conn.execute(text("""
            CREATE TEMP TABLE IF NOT EXISTS hr_directorships_tmp (
                id              TEXT PRIMARY KEY,
                organization_id TEXT,
                director_id     TEXT,
                role            TEXT,
                description     TEXT,
                summary         TEXT
            ) ON COMMIT PRESERVE ROWS
        """))
        conn.commit()

    def _drop_temp_tables(self, conn: Any) -> None:
        """Drop session-scoped temp tables after the officer JOIN is complete.

        Args:
            conn: Open SQLAlchemy connection that owns the temp tables.
        """
        conn.execute(text("DROP TABLE IF EXISTS hr_persons_tmp"))
        conn.execute(text("DROP TABLE IF EXISTS hr_directorships_tmp"))
        conn.commit()

    def _truncate_main_tables(self, conn: Any) -> None:
        """Truncate all handelsregister tables for an idempotent reload.

        Cascades to child tables (officers, previous_names, additional_data).

        Args:
            conn: Open SQLAlchemy connection.
        """
        conn.execute(text(
            "TRUNCATE handelsregister_companies CASCADE"
        ))
        conn.commit()

    # --------------------------------------------------------------- flush ops

    def _flush_companies(self, conn: Any, batch: list[dict[str, Any]]) -> None:
        """Insert a batch of company rows, skipping duplicates.

        Args:
            conn:  Open SQLAlchemy connection.
            batch: List of row dicts keyed by handelsregister_companies columns.
        """
        conn.execute(
            text("""
                INSERT INTO handelsregister_companies (
                    company_number, name, current_status, jurisdiction_code,
                    registered_address, retrieved_at,
                    register_art, register_nummer,
                    federal_state, native_company_number,
                    registered_office, registrar, former_registrar
                ) VALUES (
                    :company_number, :name, :current_status, :jurisdiction_code,
                    :registered_address, :retrieved_at,
                    :register_art, :register_nummer,
                    :federal_state, :native_company_number,
                    :registered_office, :registrar, :former_registrar
                )
                ON CONFLICT (company_number) DO NOTHING
            """),
            batch,
        )
        conn.commit()

    def _flush_prev_names(self, conn: Any, batch: list[dict[str, Any]]) -> None:
        """Insert a batch of previous-name rows.

        Args:
            conn:  Open SQLAlchemy connection.
            batch: List of row dicts with keys company_number, company_name.
        """
        conn.execute(
            text("""
                INSERT INTO handelsregister_previous_names (company_number, company_name)
                VALUES (:company_number, :company_name)
            """),
            batch,
        )
        conn.commit()

    def _flush_persons_tmp(self, conn: Any, batch: list[dict[str, Any]]) -> None:
        """Insert a batch of person rows into the session temp table.

        Args:
            conn:  Open SQLAlchemy connection.
            batch: List of row dicts with keys id, name, firstname, lastname, city, description.
        """
        conn.execute(
            text("""
                INSERT INTO hr_persons_tmp (id, name, firstname, lastname, city, description)
                VALUES (:id, :name, :firstname, :lastname, :city, :description)
                ON CONFLICT (id) DO NOTHING
            """),
            batch,
        )
        conn.commit()

    def _flush_directorships_tmp(self, conn: Any, batch: list[dict[str, Any]]) -> None:
        """Insert a batch of directorship rows into the session temp table.

        Args:
            conn:  Open SQLAlchemy connection.
            batch: List of row dicts with keys id, organization_id, director_id,
                   role, description, summary.
        """
        conn.execute(
            text("""
                INSERT INTO hr_directorships_tmp
                    (id, organization_id, director_id, role, description, summary)
                VALUES
                    (:id, :organization_id, :director_id, :role, :description, :summary)
                ON CONFLICT (id) DO NOTHING
            """),
            batch,
        )
        conn.commit()

    # ---------------------------------------------------------- officers JOIN

    def _populate_officers(self, conn: Any) -> int:
        """JOIN temp tables to INSERT into handelsregister_officers.

        Only inserts officers where the organization is a known company (INNER
        JOIN on handelsregister_companies) and the director is a known person
        (LEFT JOIN on hr_persons_tmp — officer rows without a matching person
        still land but with NULL name fields).

        Args:
            conn: Open SQLAlchemy connection with both temp tables populated.

        Returns:
            Number of officer rows inserted.
        """
        result = conn.execute(text("""
            INSERT INTO handelsregister_officers
                (company_number, name, position, type, firstname, lastname, city, flag)
            SELECT
                d.organization_id,
                COALESCE(p.name, d.director_id),
                d.role,
                'person',
                p.firstname,
                p.lastname,
                p.city,
                COALESCE(d.summary, d.description)
            FROM hr_directorships_tmp d
            JOIN  handelsregister_companies c ON d.organization_id = c.company_number
            LEFT JOIN hr_persons_tmp        p ON d.director_id     = p.id
        """))
        conn.commit()
        return result.rowcount

    # ------------------------------------------------------------------- load

    def load(self, file_path: str | Path, truncate: bool = True) -> None:
        """Stream FTM JSONL and load all handelsregister tables.

        Args:
            file_path: Path to the FTM JSONL file (one entity JSON per line).
            truncate:  When True, truncate all handelsregister tables before
                       loading.  Set to False for incremental top-up runs.

        Raises:
            FileNotFoundError: If file_path does not exist.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"FTM file not found: {path}")

        self.ensure_schema()

        with self.engine.connect() as conn:
            if truncate:
                logger.info("Truncating handelsregister tables ...")
                self._truncate_main_tables(conn)

            self._create_temp_tables(conn)

            companies: list[dict[str, Any]] = []
            prev_names: list[dict[str, Any]] = []
            persons: list[dict[str, Any]] = []
            directorships: list[dict[str, Any]] = []

            total_lines = 0
            count_companies = 0
            count_prev_names = 0
            count_persons = 0
            count_directorships = 0

            logger.info("Streaming %s ...", path)

            with path.open(encoding="utf-8") as fh:
                for raw_line in fh:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue

                    total_lines += 1
                    if total_lines % 500_000 == 0:
                        logger.info("  %s lines processed ...", f"{total_lines:,}")

                    obj: dict[str, Any] = json.loads(raw_line)
                    schema = obj.get("schema")
                    props: dict[str, list[str]] = obj.get("properties", {})

                    if schema in _COMPANY_SCHEMAS:
                        companies.append(_make_company_row(obj))
                        count_companies += 1

                        for prev in props.get("previousName", []):
                            prev_names.append(
                                {"company_number": obj["id"], "company_name": prev}
                            )
                            count_prev_names += 1

                        if len(companies) >= _BATCH_SIZE:
                            self._flush_companies(conn, companies)
                            companies = []

                        # prev_names FK requires the parent company to be committed
                        # first — always flush pending companies before prev_names.
                        if len(prev_names) >= _BATCH_SIZE:
                            if companies:
                                self._flush_companies(conn, companies)
                                companies = []
                            self._flush_prev_names(conn, prev_names)
                            prev_names = []

                    elif schema == "Person":
                        persons.append(
                            {
                                "id": obj["id"],
                                "name": obj.get("caption") or _first(props, "name"),
                                "firstname": _first(props, "firstName"),
                                "lastname": _first(props, "lastName"),
                                "city": _first(props, "address"),
                                "description": _first(props, "description"),
                            }
                        )
                        count_persons += 1

                        if len(persons) >= _BATCH_SIZE:
                            self._flush_persons_tmp(conn, persons)
                            persons = []

                    elif schema == "Directorship":
                        director_id = _first(props, "director")
                        org_id = _first(props, "organization")
                        if director_id and org_id:
                            directorships.append(
                                {
                                    "id": obj["id"],
                                    "organization_id": org_id,
                                    "director_id": director_id,
                                    "role": _first(props, "role"),
                                    "description": _first(props, "description"),
                                    "summary": _first(props, "summary"),
                                }
                            )
                            count_directorships += 1

                            if len(directorships) >= _BATCH_SIZE:
                                self._flush_directorships_tmp(conn, directorships)
                                directorships = []

            # Flush remaining partial batches.
            if companies:
                self._flush_companies(conn, companies)
            if prev_names:
                self._flush_prev_names(conn, prev_names)
            if persons:
                self._flush_persons_tmp(conn, persons)
            if directorships:
                self._flush_directorships_tmp(conn, directorships)

            logger.info(
                "Stream complete — companies: %s, previous names: %s, "
                "persons: %s, directorships: %s",
                f"{count_companies:,}",
                f"{count_prev_names:,}",
                f"{count_persons:,}",
                f"{count_directorships:,}",
            )

            logger.info("Populating handelsregister_officers via SQL JOIN ...")
            officer_count = self._populate_officers(conn)
            logger.info("Inserted %s officer rows.", f"{officer_count:,}")

            self._drop_temp_tables(conn)

        logger.info("Load complete.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )

    _DEFAULT_FTM = "~/a/corp_index/entities.ftm.json"

    parser = argparse.ArgumentParser(
        description="Load de_offeneregister FTM JSONL into handelsregister tables."
    )
    parser.add_argument(
        "--ftm",
        default=_DEFAULT_FTM,
        help=f"Path to FTM JSONL file (default: {_DEFAULT_FTM})",
    )
    parser.add_argument(
        "--no-truncate",
        action="store_true",
        help="Skip truncating tables before load (for incremental top-up runs).",
    )
    args = parser.parse_args()

    loader = HandelsregisterLoader()
    loader.load(args.ftm, truncate=not args.no_truncate)
