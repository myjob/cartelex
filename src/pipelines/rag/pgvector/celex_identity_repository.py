"""
Unified DAO for CELEX / case identity lookups.

Consolidates the scattered ``celex_case_id`` access previously split across:
  - ``CelexCheckerToolkit``     (tools/celex_checker.py)
  - ``FactRepository``          (select_case_ids_for_celex)
  - ``loader_celex_id2case_id`` (pipelines/utils/)

All queries go through the shared engine from ``session.py`` (single
connection pool).

Exports:
    CelexIdentityRepository — canonical persistence service for case/celex mapping
"""

import logging
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from pipelines.rag.pgvector.models import CaseCelexId
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)


class CelexIdentityRepository:
    """Unified DAO for the ``celex_facts.case_celex_id`` table.

    Satisfies both the lookup / validation use-case (``check_celex_or_case_id``)
    and the bulk-insert use-case (``loader_celex_id2case_id``).

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Defaults to ``DATABASE_URL`` from
        ``vector_config.py`` / environment.
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def check_celex_or_case_id(self, identifier: str) -> Optional[str]:
        """Return the ``celex_id`` for *identifier*, or ``None`` if not found.

        Checks whether *identifier* matches either a ``celex_id`` or
        ``case_id`` column in ``case_celex_id``.

        Previously implemented independently in ``tools/celex_checker.py``
        with its own ``create_engine`` call.

        Parameters
        ----------
        identifier:
            A CELEX ID (e.g. ``"39226_8310_11"``) or a case ID
            (e.g. ``"COMP/35.587"``).

        Returns
        -------
        Optional[str]
            The matching ``celex_id``, or ``None`` when the identifier is
            unknown.
        """
        sql = text("""
            SELECT celex_id
            FROM celex_facts.case_celex_id
            WHERE celex_id = :id OR case_id = :id
            LIMIT 1
        """)
        try:
            with self.engine.connect() as conn:
                result = conn.execute(sql, {"id": identifier}).scalar()
                return result if result else None
        except Exception as exc:
            logger.error("Database error in check_celex_or_case_id: %s", exc)
            return None

    def select_identifier_index(self) -> List[tuple[str, Optional[str]]]:
        """Return every ``(celex_id, case_id)`` pair from the identity registry.

        Executes::

            SELECT celex_id, case_id
            FROM celex_facts.celex_case_id

        This is the bulk read that backs the process-lifetime identifier index
        in ``tools.celex_checker``: one query replaces a per-token round trip
        and supplies the vocabulary used to tell a mistyped case reference
        apart from ordinary prose.

        Reads ``celex_case_id`` (the authoritative CSV-loaded mapping), not
        ``case_celex_id`` (the noisier regex-scanned one) -- see the tech-debt
        note in ISSUES.md about precedence between the two.

        Returns
        -------
        List[tuple[str, Optional[str]]]
            One ``(celex_id, case_id)`` tuple per row; ``case_id`` may be None.
        """
        sql = text(f"""
            SELECT celex_id, case_id
            FROM {settings.DB_SCHEMA}.{settings.CELEX_CASE_ID_TABLE}
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        logger.info(
            "Loaded %d rows from %s.%s for the identifier index.",
            len(rows),
            settings.DB_SCHEMA,
            settings.CELEX_CASE_ID_TABLE,
        )
        return [(row[0], row[1]) for row in rows]

    def select_scanned_identifier_index(self) -> List[dict]:
        """Return every ``(celex_id, case_id)`` pair from the scanned mapping.

        Reads ``case_celex_id`` -- the wider, regex-discovered table.  Used only
        as a recall supplement behind :meth:`select_identifier_index`, because
        ``celex_case_id`` holds no COMP/ case numbers and misses part of the
        served corpus.

        Returns
        -------
        List[dict]
            One dict per row with keys ``"celex_id"`` and ``"case_id"``.
        """
        sql = text(f"""
            SELECT DISTINCT celex_id, case_id
            FROM {settings.DB_SCHEMA}.case_celex_id
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [{"celex_id": row[0], "case_id": row[1]} for row in rows]

    def select_case_ids_for_celex(self, celex_id: str) -> List[str]:
        """Return distinct case IDs associated with a CELEX ID.

        Previously in ``FactRepository.select_case_ids_for_celex``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        List[str]
            Distinct ``case_id`` strings.
        """
        with self.Session() as session:
            rows = (
                session.query(CaseCelexId.case_id)
                .filter(CaseCelexId.celex_id == celex_id)
                .distinct()
                .all()
            )
            return [row.case_id for row in rows]

    def select_distinct_book_cases(self) -> List[dict]:
        """Return all rows from ``celex_facts.celex_case_id`` for book cross-check.

        Executes::

            SELECT DISTINCT celex_id, case_x, case_id
            FROM celex_facts.celex_case_id

        Note: reads from ``celex_case_id`` (the book-case mapping loaded from the
        Russo-Schinkel CSV), **not** from ``case_celex_id`` (the reverse mapping
        populated from CELEX document text).  See tech-debt #1 in ISSUES.md.

        Returns
        -------
        List[dict]
            One dict per distinct row with keys ``"celex_id"``, ``"case_x"``,
            ``"case_id"``.  Rows where all three are NULL are included as-is.
        """
        sql = text("""
            SELECT DISTINCT celex_id, case_x, case_id
            FROM celex_facts.celex_case_id
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [
            {"celex_id": row[0], "case_x": row[1], "case_id": row[2]}
            for row in rows
        ]

    def select_celex_ids_for_case(self, case_id: str) -> List[str]:
        """Return distinct CELEX IDs associated with a case ID.

        Parameters
        ----------
        case_id:
            The case identifier (e.g. ``"COMP/35.587"``).

        Returns
        -------
        List[str]
            Distinct ``celex_id`` strings.
        """
        with self.Session() as session:
            rows = (
                session.query(CaseCelexId.celex_id)
                .filter(CaseCelexId.case_id == case_id)
                .distinct()
                .all()
            )
            return [row.celex_id for row in rows]

    # ------------------------------------------------------------------
    # Bulk insert (used by loader scripts)
    # ------------------------------------------------------------------

    def upsert_case_celex_mapping(
        self, case_id: str, celex_id: str
    ) -> None:
        """Insert a ``(case_id, celex_id)`` row, skipping on conflict.

        Parameters
        ----------
        case_id:
            The case identifier.
        celex_id:
            The CELEX document identifier.
        """
        sql = text("""
            INSERT INTO celex_facts.case_celex_id (case_id, celex_id)
            VALUES (:case_id, :celex_id)
            ON CONFLICT (case_id, celex_id) DO NOTHING
        """)
        with self.engine.begin() as conn:
            conn.execute(sql, {"case_id": case_id, "celex_id": celex_id})

    def bulk_upsert_case_celex_mappings(
        self, records: list[dict]
    ) -> int:
        """Bulk insert ``(case_id, celex_id)`` rows, skipping conflicts.

        Parameters
        ----------
        records:
            List of dicts with keys ``"case_id"`` and ``"celex_id"``.

        Returns
        -------
        int
            Number of records processed (not necessarily inserted due to
            ON CONFLICT DO NOTHING).
        """
        if not records:
            return 0

        sql = text("""
            INSERT INTO celex_facts.case_celex_id (case_id, celex_id)
            VALUES (:case_id, :celex_id)
            ON CONFLICT (case_id, celex_id) DO NOTHING
        """)
        with self.engine.begin() as conn:
            for record in records:
                conn.execute(
                    sql,
                    {"case_id": record["case_id"], "celex_id": record["celex_id"]},
                )
        logger.info("Processed %d case-celex mapping records.", len(records))
        return len(records)

    def exists(self, identifier: str) -> bool:
        """Return ``True`` when *identifier* exists as celex_id or case_id.

        Parameters
        ----------
        identifier:
            A CELEX ID or case ID.
        """
        return self.check_celex_or_case_id(identifier) is not None
