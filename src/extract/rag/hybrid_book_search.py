"""
Hybrid search for book-section embeddings in ``pg_embedding_book_qwen``.

Two-step retrieval:
  1. Hybrid keyword (ILIKE) + vector (cosine) search to find the single
     best-matching chunk (top_k=1).
  2. Expand that chunk's chapter prefix to return **all** text from the
     matching chapter, ordered by ``doc_pos``.

Database access is delegated to:
  - :class:`~pipelines.rag.pgvector.book_chunk_repository.BookChunkRepository`
  - :class:`~pipelines.rag.pgvector.query_embedding_repository.QueryEmbeddingRepository`
"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_core.embeddings import Embeddings

from extract.rag.models.chunk_vo import BookChapterVo, ChunkVo
from extract.rag.ranker.ranker_factory import RankerProtocol
from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository
from pipelines.rag.pgvector.query_embedding_repository import QueryEmbeddingRepository
from pipelines.rag.pgvector.vector_config import DATABASE_URL

from tools.celex_tools import CelexDatabaseToolkit

logger = logging.getLogger(__name__)


class HybridBookSearchToolkit:
    """Toolkit for hybrid book search, providing necessary database access and embedding support."""

    def __init__(self, tools: CelexDatabaseToolkit):
        self.tools = tools
        self.engine = tools.engine
        self.embeddings = tools.embeddings
        self.search_engine = HybridBookSearch(
            embeddings=self.embeddings,
            db_url=DATABASE_URL,
        )

    def keyword_exists(self, keyword: str) -> bool:
        lst = self.search_engine._keyword_search(keyword, limit=1)
        return len(lst) > 0
   
    def search_keyword(self, keyword: str) -> list[BookChapterVo]:
        """Search by keyword and expand the best hit to full chapter text.

        Args:
            keyword: Search term for ILIKE matching.

        Returns:
            List of ``BookChapterVo`` for the chapter containing the best
            keyword hit, ordered by ``doc_pos``. Empty list if no match.
        """
        hits = self.search_engine._keyword_search(keyword, limit=1)
        if not hits:
            return []
        return self.search_engine._fetch_chapter_text(hits[0].id)
    
    def search_semantic(self, query: str) -> list[BookChapterVo]:
        
        hits = self.search_engine._semantic_search(query)
        if not hits:
            return []
        return self.search_engine._fetch_chapter_text(hits[0].id)
    
    def search(self, query: str, keyword: str = '') -> list[BookChapterVo]:
        """Hybrid (semantic + optional keyword) search expanded to chapter text.

        Args:
            query: Natural-language query for the vector leg.
            keyword: Optional ILIKE term for the keyword leg; empty disables it.

        Returns:
            The chapter rows of the best hit, ordered by ``doc_pos``.
        """
        return self.search_engine.search(query=query, keyword=keyword)

class HybridBookSearch:
    """Hybrid search over book-section embeddings with chapter expansion.

    Combines ILIKE keyword search and pgvector cosine similarity to locate
    the single most relevant chunk, then expands the result to the full
    chapter text.

    All database access is delegated to ``BookChunkRepository`` and
    ``QueryEmbeddingRepository``.

    Args:
        embeddings: LangChain-compatible embedding provider for query vectors.
        db_url: PostgreSQL connection string (forwarded to repositories).
        ranker: Optional reranker. When provided the merged keyword + vector
            hits are reranked and the top-1 is selected. When ``None`` the
            first semantic hit is used directly.
        engine: **Deprecated** — accepted for backward compatibility with
            existing tests that construct ``HybridBookSearch(engine=..., ...)``.
            Ignored when ``db_url`` is provided.
    """
  
    def __init__(
        self,
        embeddings: Embeddings,
        ranker: RankerProtocol | None = None,
        db_url: str = DATABASE_URL,
        engine=None,  # backward compat for tests
    ) -> None:
        self.embeddings = embeddings
        self.ranker = ranker
        # Keep engine attr for backward compat (tests reference it)
        self.engine = engine
        self._book_repo = BookChunkRepository(db_url=db_url)
        self._query_repo = QueryEmbeddingRepository(db_url=db_url)

    # ------------------------------------------------------------------
    # Embedding cache (delegates to QueryEmbeddingRepository)
    # ------------------------------------------------------------------

    def _get_or_cache_embedding(self, query: str) -> list:
        """Return the embedding for *query*, using the query cache table.

        Looks up by the raw ``query`` text. On a cache miss the embedding
        provider is called and the result is stored for future reuse.

        Args:
            query: Natural-language query string.

        Returns:
            Embedding vector as a list of floats.
        """
        return self._query_repo.get_or_cache(
            query, self.embeddings.embed_query
        )

    # ------------------------------------------------------------------
    # Step 1 helpers (delegate to BookChunkRepository)
    # ------------------------------------------------------------------

    def _keyword_search(self, keyword: str, limit: int = 15) -> list[ChunkVo]:
        """Run an ILIKE keyword search on the book embedding table.

        Args:
            keyword: Search term (wrapped in ``%`` wildcards internally).
            limit: Maximum number of rows to return.

        Returns:
            List of matching chunks ordered by ``doc_pos``.
        """
        return self._book_repo.keyword_search(keyword, limit)

    def _semantic_search(self, query: str, hits: int = 15) -> list[ChunkVo]:
        """Run a vector cosine-similarity search on the book embedding table.

        Args:
            query: Natural-language query to embed and search against.
            hits: Maximum number of rows to return.

        Returns:
            List of matching chunks ordered by cosine distance (ascending).
        """
        query_embedding = self._get_or_cache_embedding(query)
        return self._book_repo.semantic_search(query_embedding, hits)

    @staticmethod
    def _deduplicate(*chunk_lists: list[ChunkVo]) -> list[ChunkVo]:
        """Deduplicate chunks across multiple lists by their ID.

        Args:
            *chunk_lists: Variable number of chunk lists to merge.

        Returns:
            Deduplicated list preserving first-seen order.
        """
        seen_ids: set[str] = set()
        deduped: list[ChunkVo] = []
        for chunk_list in chunk_lists:
            for chunk in chunk_list:
                if chunk.id not in seen_ids:
                    seen_ids.add(chunk.id)
                    deduped.append(chunk)
        return deduped

    def _find_best_match(self, query: str, keyword: str) -> Optional[str]:
        """Locate the single best-matching row via hybrid search.

        Merges keyword and semantic results, optionally reranks with
        ``FlashRanker``, and returns the ID of the top-1 result.

        Args:
            query: Natural-language query for semantic search.
            keyword: Keyword string for ILIKE matching.

        Returns:
            The row ID of the best match, or ``None`` if no results.
        """
        logger.debug(
            "Finding best book match — keyword=%r, query=%r", keyword, query
        )

        is_single_unsplittable_word = len(query.split()) == 1 and "_" not in query
        if is_single_unsplittable_word:
            logger.debug("Single unsplittable word — skipping keyword search.")
            keyword_chunks: list[ChunkVo] = []
        else:
            keyword_chunks = self._keyword_search(keyword)
            logger.debug("Keyword search yielded %d chunks.", len(keyword_chunks))

        semantic_chunks = self._semantic_search(query)
        logger.debug("Semantic search yielded %d chunks.", len(semantic_chunks))

        unique_chunks = self._deduplicate(keyword_chunks, semantic_chunks)

        if not unique_chunks:
            logger.warning("No chunks found for query=%r, keyword=%r", query, keyword)
            return None

        if self.ranker is not None:
            ranked = self.ranker.rank_chunkvo(query, unique_chunks, top_k=1)
            if ranked:
                logger.debug("Ranker selected chunk id=%s (score=%.4f)", ranked[0].id, ranked[0].score)
                return ranked[0].id

        # Fallback: first semantic hit (already ordered by cosine distance)
        best = semantic_chunks[0] if semantic_chunks else unique_chunks[0]
        logger.debug("Using first hit id=%s (no ranker).", best.id)
        return best.id

    # ------------------------------------------------------------------
    # Step 2: chapter expansion (delegates to BookChunkRepository)
    # ------------------------------------------------------------------

    def _fetch_chapter_text(self, row_id: str) -> list[BookChapterVo]:
        """Expand a matched row ID into the full chapter text.

        Uses the row's path to derive the chapter prefix (first 3 path
        segments) and then retrieves every row whose path starts with
        that prefix, ordered by ``doc_pos``.

        Args:
            row_id: Primary key of the matched row in ``pg_embedding_book_qwen``.

        Returns:
            List of ``BookChapterVo`` representing the full chapter content.
        """
        return self._book_repo.fetch_chapter_text(row_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: str, keyword: str) -> list[BookChapterVo]:
        """Execute a hybrid search and return the full chapter text.

        Step 1: hybrid keyword + vector search to find the best chunk.
        Step 2: expand that chunk's chapter prefix to retrieve every row
        in the same chapter, ordered by ``doc_pos``.

        Args:
            query: Natural-language query for semantic search.
            keyword: Keyword string for ILIKE matching.

        Returns:
            List of ``BookChapterVo`` containing the full chapter text,
            ordered by ``doc_pos``. Empty list if no match is found.
        """
        best_id = self._find_best_match(query, keyword)

        if best_id is None:
            logger.warning("No match found — returning empty results.")
            return []

        logger.debug("Expanding chapter for matched id=%s", best_id)
        chapter_rows = self._fetch_chapter_text(best_id)
        logger.debug("Chapter expansion returned %d rows.", len(chapter_rows))
        return chapter_rows
