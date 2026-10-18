"""
Unified DAO for company / corporate entity persistence.

Consolidates the scattered company-related database access previously in:
  - ``CelexDatabaseToolkit.insert_canonical_company_and_aliases``  (celex_tools.py)
  - ``load_company_standard_alias.py``   (pipelines/utils/)
  - ``load_company_canonical_alias.py``  (pipelines/utils/)

All queries go through the shared engine from ``session.py`` (single
connection pool).

Tables covered:
  - ``celex_facts.company_canonical2celex_id``
  - ``celex_facts.company_canonical2alias``
  - ``celex_facts.company_standard2celex_id2alias``

Exports:
    CompanyRepository — canonical persistence service for company entities
"""

import logging
import uuid as _uuid
from typing import List

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)


class CompanyRepository:
    """Unified DAO for company-related tables in ``celex_facts``.

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
    # Canonical company ↔ CELEX mapping
    # ------------------------------------------------------------------

    def upsert_canonical_company(
        self, company_name: str, celex_id: str
    ) -> _uuid.UUID:
        """Insert or update a canonical company name for a CELEX document.

        Uses ``ON CONFLICT … DO UPDATE`` against the unique constraint
        ``company_canonical2celex_id_name2celexid_uniqu`` so re-runs are
        idempotent.

        Parameters
        ----------
        company_name:
            The canonical (normalized) company name.
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        uuid.UUID
            The primary-key ``id`` of the inserted/updated row.
        """
        sql = text("""
            INSERT INTO celex_facts.company_canonical2celex_id
            (id, company_name, celex_id)
            VALUES (gen_random_uuid(), :company_name, :celex_id)
            ON CONFLICT ON CONSTRAINT company_canonical2celex_id_name2celexid_uniqu
            DO UPDATE SET company_name = EXCLUDED.company_name
            RETURNING id
        """)
        with self.engine.begin() as conn:
            result = conn.execute(
                sql, {"company_name": company_name, "celex_id": celex_id}
            )
            return result.scalar()

    # ------------------------------------------------------------------
    # Canonical company → alias mapping
    # ------------------------------------------------------------------

    def insert_alias(
        self,
        canonical_id: _uuid.UUID,
        company_canonical: str,
        company_alias: str,
    ) -> None:
        """Insert a single alias for a canonical company (idempotent).

        Parameters
        ----------
        canonical_id:
            FK to ``company_canonical2celex_id.id``.
        company_canonical:
            The canonical company name (denormalized for the unique
            constraint).
        company_alias:
            The alias string.
        """
        if company_canonical == company_alias:
            return  # Skip self-referencing aliases

        sql = text("""
            INSERT INTO celex_facts.company_canonical2alias
            (id, company_canonical, company_alias, fk_company_canonical)
            VALUES (gen_random_uuid(), :company_canonical, :company_alias, :fk_company_canonical)
            ON CONFLICT (company_canonical, company_alias) DO NOTHING
        """)
        with self.engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "company_canonical": company_canonical,
                    "company_alias": company_alias,
                    "fk_company_canonical": canonical_id,
                },
            )

    def insert_canonical_company_and_aliases(
        self,
        celex_id: str,
        company_name: str,
        aliases: List[str],
    ) -> str:
        """Insert a canonical company and its aliases in one transaction.

        Direct replacement for
        ``CelexDatabaseToolkit.insert_canonical_company_and_aliases``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        company_name:
            The canonical company name.
        aliases:
            List of alias strings.

        Returns
        -------
        str
            Human-readable success message.
        """
        canonical_id = self.upsert_canonical_company(company_name, celex_id)

        for alias in aliases:
            self.insert_alias(canonical_id, company_name, alias)

        return (
            f"Successfully inserted {company_name} and "
            f"{len(aliases)} aliases for {celex_id}."
        )

    # ------------------------------------------------------------------
    # Standard company alias (legacy rule_facts migration)
    # ------------------------------------------------------------------

    def insert_standard_alias(
        self,
        celex_id: str,
        company_name: str,
        alias: str,
    ) -> None:
        """Insert a standard company alias row.

        Previously inline in ``load_company_standard_alias.py``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        company_name:
            The standard company name.
        alias:
            The alias string.
        """
        sql = text("""
            INSERT INTO celex_facts.company_standard2celex_id2alias
            (id, celex_id, company_name, alias)
            VALUES (gen_random_uuid(), :celex_id, :company_name, :alias)
        """)
        with self.engine.begin() as conn:
            conn.execute(
                sql,
                {"celex_id": celex_id, "company_name": company_name, "alias": alias},
            )
