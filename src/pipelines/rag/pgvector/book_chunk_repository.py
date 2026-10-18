"""
Unified DAO for book-section chunk retrieval (``pg_embedding_book_qwen``).

Consolidates the scattered book-chunk database access previously in:
  - ``HybridBookSearch._keyword_search``    (extract/rag/hybrid_book_search.py)
  - ``HybridBookSearch._semantic_search``   (same)
  - ``HybridBookSearch._fetch_chapter_text`` (same)

All queries go through the shared engine from ``session.py`` (single
connection pool).  Semantic search methods accept pre-computed embeddings.

Exports:
    BookChunkRepository — canonical persistence service for book-section chunks
"""

import logging
from typing import List

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from extract.rag.models.chunk_vo import BookChapterVo, ChunkVo
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

_EMBEDDING_TABLE = "celex_facts.pg_embedding_book_qwen"


class BookChunkRepository:
    """Focused DAO for keyword and semantic retrieval against book-section embeddings.

    Mirror of :class:`ChunkRepository` but targeting
    ``pg_embedding_book_qwen`` (book-section vectors instead of
    per-document chunks).

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Defaults to ``DATABASE_URL``.
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)

    # ------------------------------------------------------------------
    # Keyword search
    # ------------------------------------------------------------------

    def keyword_search(
        self, keyword: str, limit: int = 15
    ) -> List[ChunkVo]:
        """Run an ILIKE keyword search on the book embedding table.

        Parameters
        ----------
        keyword:
            Search term (wrapped in ``%`` wildcards internally).
        limit:
            Maximum number of rows to return.

        Returns
        -------
        List[ChunkVo]
            Matching chunks ordered by ``doc_pos``.
        """
        search_term = f"%{keyword}%"
        sql = text(f"""
            SELECT id, document, doc_pos
            FROM {_EMBEDDING_TABLE}
            WHERE document ILIKE :search_term
            ORDER BY doc_pos
            LIMIT :limit
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"search_term": search_term, "limit": limit}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                chunk=row[1],
                origin="sql_keyword",
                similarity=0.5,
                doc_pos=int(row[2]) if row[2] is not None else 0,
            )
            for row in rows
        ]

    def select_reference_paths(
        self, keyword: str, limit: int = 200
    ) -> List[str]:
        """Return the distinct section paths where *keyword* is named.

        Used to classify how the book references a case (landmark subsection,
        chapter prose, or pure index listing) from the section hierarchy encoded
        in ``path``. Mirrors the ILIKE term wrapping of :meth:`keyword_search`.

        Parameters
        ----------
        keyword:
            Search term (a case identifier); wrapped in ``%`` wildcards.
        limit:
            Safety cap on the number of distinct paths returned.

        Returns
        -------
        List[str]
            Distinct ``path`` values of chunks whose text contains *keyword*.
        """
        sql = text(f"""
            SELECT DISTINCT path
            FROM {_EMBEDDING_TABLE}
            WHERE document ILIKE :search_term
            LIMIT :limit
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"search_term": f"%{keyword}%", "limit": limit}
            ).fetchall()

        return [row[0] for row in rows if row[0] is not None]

    def select_reference_chunks(
        self, keyword: str, limit: int = 200
    ) -> List[tuple[str, str]]:
        """Return ``(path, document)`` of every chunk whose text names *keyword*.

        The text-bearing sibling of :meth:`select_reference_paths`: a path can
        hold several chunks, and each is returned so a caller can quote where
        the case is named. Ordered by path, then document position.

        Parameters
        ----------
        keyword:
            Search term (a case identifier); wrapped in ``%`` wildcards.
        limit:
            Safety cap on the number of chunks returned.

        Returns
        -------
        List[tuple[str, str]]
            ``(path, document)`` pairs; chunks without a path are dropped.
        """
        sql = text(f"""
            SELECT path, document
            FROM {_EMBEDDING_TABLE}
            WHERE document ILIKE :search_term
              AND path IS NOT NULL
            ORDER BY path, doc_pos
            LIMIT :limit
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"search_term": f"%{keyword}%", "limit": limit}
            ).fetchall()

        return [(row[0], row[1] or "") for row in rows]

    # ------------------------------------------------------------------
    # Semantic search
    # ------------------------------------------------------------------

    def semantic_search(
        self, query_embedding: list, hits: int = 15
    ) -> List[ChunkVo]:
        """Run a vector cosine-similarity search on the book embedding table.

        Parameters
        ----------
        query_embedding:
            Pre-computed embedding vector as a Python list of floats.
        hits:
            Maximum number of rows to return.

        Returns
        -------
        List[ChunkVo]
            Matching chunks ordered by cosine distance (ascending).
        """
        sql = text(f"""
            SELECT id, document, doc_pos
            FROM {_EMBEDDING_TABLE}
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:qemb AS vector)
            LIMIT :hits
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"qemb": str(query_embedding), "hits": hits}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                chunk=row[1],
                origin="pgvector_semantic",
                similarity=0.5,
                doc_pos=int(row[2]) if row[2] is not None else 0,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Chapter expansion
    # ------------------------------------------------------------------

    def fetch_chapter_text(self, row_id: str) -> List[BookChapterVo]:
        """Expand a matched row ID into the full chapter text.

        Uses the row's path to derive the chapter prefix (first 3 path
        segments) and then retrieves every row whose path starts with
        that prefix, ordered by ``doc_pos``.

        Parameters
        ----------
        row_id:
            Primary key of the matched row in ``pg_embedding_book_qwen``.

        Returns
        -------
        List[BookChapterVo]
            Representing the full chapter content.
        """
        sql = text(f"""
            WITH found_matches AS (
                SELECT
                    path,
                    array_to_string(
                        (string_to_array(path, ' -> '))[1:3], ' -> '
                    ) AS chapter_prefix,
                    (string_to_array(path, ' -> '))[3]  AS sub_chapter_title
                FROM {_EMBEDDING_TABLE}
                WHERE id = :row_id
            ),
            chapters AS (
                SELECT DISTINCT ON (c.path)
                    c.path   AS chapter_path,
                    c.document AS chapter_title,
                    f.sub_chapter_title
                FROM {_EMBEDDING_TABLE} c
                JOIN found_matches f ON c.path = f.chapter_prefix
            )
            SELECT
                ch.chapter_title,
                ch.sub_chapter_title,
                t.level,
                t.document,
                t.path,
                t.token_count,
                t.doc_pos
            FROM chapters ch
            JOIN {_EMBEDDING_TABLE} t
              ON t.path LIKE ch.chapter_path || '%'
            ORDER BY t.doc_pos
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"row_id": row_id}).fetchall()

        return [
            BookChapterVo(
                chapter_title=row[0],
                sub_chapter_title=row[1],
                level=int(row[2]) if row[2] is not None else 0,
                document=row[3],
                path=row[4],
                token_count=int(row[5]) if row[5] is not None else None,
                doc_pos=int(row[6]) if row[6] is not None else 0,
            )
            for row in rows
        ]

    def fetch_chapters_for_keyword(
        self, keyword: str, limit: int = 80
    ) -> List[ChunkVo]:
        """Return all chunks of every chapter that mentions *keyword*.

        Unlike :meth:`keyword_search` (which returns only the rows whose text
        literally contains *keyword* — often a citation/index entry), this
        expands each matching row to its chapter (first three path segments) and
        returns **all** chunks of those chapters as citable ``ChunkVo`` rows.
        This is the case-scoped candidate set the book cross-check reranks per
        fact: the analytical prose discussing a case, not just the index line
        that names it.

        Parameters
        ----------
        keyword:
            ILIKE search term (a case identifier); wrapped in ``%`` internally.
        limit:
            Safety cap on the number of chunks returned (ordered by ``doc_pos``).

        Returns
        -------
        List[ChunkVo]
            Chapter chunks (with citable ids) ordered by ``doc_pos``; empty when
            the keyword matches no chunk.
        """
        sql = text(f"""
            WITH seeds AS (
                SELECT DISTINCT array_to_string(
                    (string_to_array(path, ' -> '))[1:3], ' -> '
                ) AS chapter_prefix
                FROM {_EMBEDDING_TABLE}
                WHERE document ILIKE :search_term
            )
            SELECT t.id, t.document, t.doc_pos
            FROM {_EMBEDDING_TABLE} t
            JOIN seeds s ON t.path LIKE s.chapter_prefix || '%'
            ORDER BY t.doc_pos
            LIMIT :limit
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(
                sql, {"search_term": f"%{keyword}%", "limit": limit}
            ).fetchall()

        return [
            ChunkVo(
                id=str(row[0]),
                chunk=row[1],
                origin="sql_chapter",
                similarity=0.5,
                doc_pos=int(row[2]) if row[2] is not None else 0,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Existence check
    # ------------------------------------------------------------------

    def keyword_exists(self, keyword: str) -> bool:
        """Return ``True`` when at least one book chunk matches *keyword*.

        Parameters
        ----------
        keyword:
            ILIKE search term.
        """
        hits = self.keyword_search(keyword, limit=1)
        return len(hits) > 0
