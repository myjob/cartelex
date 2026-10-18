"""
Unified DAO for the query-embedding cache table (``query_embeddings_qwen``).

Consolidates the duplicate ``_get_or_cache_embedding`` logic previously
copy-pasted across:
  - ``CelexDatabaseToolkit`` (celex_tools.py)
  - ``HybridBookSearch``     (hybrid_book_search.py)

All queries go through the shared engine from ``session.py`` (single
connection pool).  The class follows the Provisioner pattern: database
client injected at ``__init__``.

Exports:
    QueryEmbeddingRepository — canonical persistence service for query embeddings
"""

import json as _json
import logging
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)


class QueryEmbeddingRepository:
    """Unified DAO for query-embedding cache tables.

    Eliminates the duplicate ``_get_or_cache_embedding`` implementations by
    providing a single, authoritative read-through cache backed by any
    ``celex_facts.query_embeddings_*`` table.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Defaults to ``DATABASE_URL`` from
        ``vector_config.py`` / environment.
    cache_table:
        Unqualified table name within the ``celex_facts`` schema (e.g.
        ``"query_embeddings_qwen"``).  The schema prefix is applied
        automatically.
    """

    def __init__(
        self,
        db_url: str = DATABASE_URL,
        cache_table: str = "query_embeddings_qwen",
    ) -> None:
        self.engine = make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)
        self._fq_table = f"celex_facts.{cache_table}"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_cached(self, query: str) -> Optional[list]:
        """Return the cached embedding for *query*, or ``None`` on miss.

        Parameters
        ----------
        query:
            Natural-language query string (exact match).

        Returns
        -------
        Optional[list]
            The embedding vector as a Python list of floats, or ``None``
            when the query has not been cached yet.
        """
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT embedding::text FROM {self._fq_table}"
                    " WHERE query = :query"
                ),
                {"query": query},
            ).fetchone()

        if row:
            return _json.loads(row[0])
        return None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def cache(self, query: str, embedding: list) -> None:
        """Store an embedding for *query* (idempotent — ON CONFLICT DO NOTHING).

        Parameters
        ----------
        query:
            Natural-language query string.
        embedding:
            The embedding vector as a Python list of floats.
        """
        with self.engine.begin() as tx:
            tx.execute(
                text(f"""
                    INSERT INTO {self._fq_table} (query, embedding)
                    VALUES (:query, :embedding)
                    ON CONFLICT (query) DO NOTHING
                """),
                {"query": query, "embedding": str(embedding)},
            )

    # ------------------------------------------------------------------
    # Convenience: read-through cache
    # ------------------------------------------------------------------

    def get_or_cache(
        self, query: str, embed_fn: Callable[[str], list]
    ) -> list:
        """Return the embedding for *query*, computing and caching on miss.

        This is the direct replacement for the duplicate
        ``_get_or_cache_embedding`` methods that previously lived in
        ``CelexDatabaseToolkit`` and ``HybridBookSearch``.

        Parameters
        ----------
        query:
            Natural-language query string.
        embed_fn:
            A callable ``(str) -> list[float]`` that computes the embedding.
            Only called on a cache miss.

        Returns
        -------
        list
            The embedding vector as a Python list of floats.
        """
        cached = self.get_cached(query)
        if cached is not None:
            return cached

        embedding = embed_fn(query)
        self.cache(query, embedding)
        return embedding
