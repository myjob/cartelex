"""DB boundary for the legal-provision ontology (``celex_facts.legal_provisions``).

``pipelines/legal/infringement_concept.py`` stays pure by shipping
:data:`~pipelines.legal.infringement_concept.DEFAULT_PROVISION_MAP`; this
repository loads the same ontology from the database — once per process via
:func:`load_provision_map` — for callers that want the DB as the source of
truth. An integration test asserts the two are equal (drift guard).

Exports:
    ProvisionRepository — reads provision rows
    load_provision_map  — process-wide cached ProvisionMap from the DB
"""

import logging
from functools import lru_cache

from sqlalchemy import text

from pipelines.legal.infringement_concept import (
    InfringementConcept,
    ProvisionMap,
    ProvisionRow,
    TreatyEra,
)
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

# Treaties that participate in runtime concept resolution. EEA rows are
# data-only until deliberately enabled (user decision 2026-07-05): including
# them would make Article 53/54 tokens newly resolve in concepts_for().
RUNTIME_TREATIES: tuple[str, ...] = ("eec", "ec", "tfeu")


class ProvisionRepository:
    """Focused DAO for the ``legal_provisions`` ontology table.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string. Defaults to ``DATABASE_URL`` from
        ``vector_config.py`` / environment.
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)

    def select_provision_rows(
        self, treaties: tuple[str, ...] = RUNTIME_TREATIES
    ) -> list[ProvisionRow]:
        """Fetch ontology rows for the given treaties.

        Parameters
        ----------
        treaties:
            Treaty slugs to include; defaults to the runtime lineage
            (``eec``/``ec``/``tfeu``), excluding data-only EEA rows.

        Returns
        -------
        list[ProvisionRow]
            Typed rows ordered by validity start and article number.
        """
        sql = text("""
            SELECT article_number, treaty, valid_from, valid_to, canonical_concept
            FROM celex_facts.legal_provisions
            WHERE treaty = ANY(:treaties)
            ORDER BY valid_from, article_number
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"treaties": list(treaties)}).fetchall()

        return [
            ProvisionRow(
                article_number=int(row.article_number),
                treaty=TreatyEra(row.treaty),
                valid_from=row.valid_from,
                valid_to=row.valid_to,
                canonical_concept=InfringementConcept(row.canonical_concept),
            )
            for row in rows
        ]


@lru_cache(maxsize=1)
def load_provision_map() -> ProvisionMap:
    """Load the runtime ProvisionMap from the database, once per process.

    Returns
    -------
    ProvisionMap
        Ontology built from the ``eec``/``ec``/``tfeu`` rows of
        ``celex_facts.legal_provisions``.

    Raises
    ------
    ValueError
        If the table has no runtime rows (migration not applied).
    """
    rows = ProvisionRepository().select_provision_rows()
    logger.debug("Loaded %d provision rows from legal_provisions", len(rows))
    return ProvisionMap.from_rows(rows)
