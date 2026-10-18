"""
Agno tool-registration facade — central toolkit for CELEX document access.

All database access is delegated to canonical repositories:
  - :class:`~pipelines.rag.pgvector.fact_repository.FactRepository`
  - :class:`~pipelines.rag.pgvector.chunk_repository.ChunkRepository`
  - :class:`~pipelines.rag.pgvector.query_embedding_repository.QueryEmbeddingRepository`

The *Provisioner* pattern is applied: repositories are created once (or
injected) and shared across all registered tool methods.
"""

from config.settings import settings
import json
from pipelines.rag.pgvector.celex_pgvector import CelexPGVectorPgQwen
from typing import Any, Optional, Tuple, List
from extract.rag.models.chunk_vo import ChunkVo, FactSearchResult
from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.chunk_repository import ChunkRepository
from pipelines.rag.pgvector.query_embedding_repository import QueryEmbeddingRepository
from pipelines.rag.pgvector.embeddings.qwen3_embedding import (
    Qwen3OllamaEmbeddings,
)

class CelexDatabaseToolkit():
    """Agno tool-registration facade that delegates all DB access to injected repositories.

    The *Provisioner* pattern is applied: ``FactRepository``, ``ChunkRepository``,
    and ``QueryEmbeddingRepository`` are created once (or injected) and shared
    across all registered tool methods.  This eliminates the
    duplicate ``create_engine`` call that previously gave the toolkit its own
    disconnected connection pool.
    """

    def __init__(
        self,
        db_url: str = settings.DATABASE_URL,
        collection_name: str = settings.COLLECTION_NAME,
        embedding_model: str = settings.EMBEDDING_MODEL_qwen3,
        fact_repo: Optional[FactRepository] = None,
    ):
        self.db_url = db_url

        # --- Fact persistence (single engine via session.py) ---
        self.fact_repo = fact_repo if fact_repo is not None else FactRepository(db_url)
        # Expose the shared engine (single connection pool) for consumers.
        self.engine = self.fact_repo.engine
        self.embedding_table = settings.EMBEDDING_TABLE

        # --- Embedding provider (Qwen3 is the only one) ---
        self.embeddings = Qwen3OllamaEmbeddings()
        self.query_cache_table = "query_embeddings_qwen"
        self.vector_store = CelexPGVectorPgQwen(
            embeddings=self.embeddings,
            collection_name=collection_name,
            connection=db_url,
            use_jsonb=True,
            engine_args={"connect_args": {"options": "-csearch_path=celex_facts,public"}},
        )

        # --- Repository layer (Phase 2 consolidation) ---
        self.chunk_repo = ChunkRepository(db_url=db_url, embedding_table=self.embedding_table)
        self._query_repo = QueryEmbeddingRepository(db_url=db_url, cache_table=self.query_cache_table)

        # Public, cached embedding function for search orchestrators that keep
        # the embedding concern external (HybridSearch, EnumerationSearch).
        self.embed_query = self._get_or_cache_embedding

      

    # ------------------------------------------------------------------
    # Embedding cache — delegates to QueryEmbeddingRepository
    # ------------------------------------------------------------------

    def _get_or_cache_embedding(self, query: str) -> list:
        """Return the embedding for *query*, using the provider-specific cache table.

        Delegates to ``QueryEmbeddingRepository.get_or_cache`` so that the
        read-through caching logic is not duplicated.
        """
        return self._query_repo.get_or_cache(query, self.embeddings.embed_query)

    # ------------------------------------------------------------------
    # Chunk search — delegates to ChunkRepository
    # ------------------------------------------------------------------

    def search_document_chunks_with_meta_coordinates(self, query: str, celex_id: Optional[str] = None) -> str:
        """Searches and returns excerpts from the CELEX document facts with coordinates (chunk_hash, id)."""
        query_embedding = self._get_or_cache_embedding(query)
        results = self.chunk_repo.semantic_search_with_meta(
            query_embedding=query_embedding,
            celex_id=celex_id,
            limit=3,
        )
        # Backward compat: old API returned "content" key, new repo returns "document"
        for item in results:
            if "document" in item and "content" not in item:
                item["content"] = item.pop("document")
        return json.dumps(results, indent=2)

    def search_document_chunk4celex_id(self, celex_id: str, query: str, hits: int = settings.VECTOR_STORE_K) -> list[str]:
        """Searches and returns excerpts from the CELEX document facts for a given celex_id."""
        query_embedding = self._get_or_cache_embedding(query)
        chunks = self.chunk_repo.semantic_search_in_celex(celex_id, query_embedding, hits)
        return [c.chunk for c in chunks]

    def search_document_chunk(self, query: str) -> str:
        """Searches and returns excerpts from the CELEX document facts."""
        query_embedding = self._get_or_cache_embedding(query)
        chunks = self.chunk_repo.semantic_search(query_embedding, hits=3)
        return "\n\n".join([c.chunk for c in chunks])

    def keyword_search_chunks_all_cases(
        self, keyword: str, hits: int = 20
    ) -> List[ChunkVo]:
        """Execute a cross-case ILIKE keyword search over all document chunks.

        Returns one representative chunk per distinct ``celex_id`` so the result
        set answers "which cases mention X" entity lookups. This is the reliable
        path for proper nouns (company names, towns), which embed poorly as
        short vector queries and are easily out-ranked by tiny header chunks.

        Args:
            keyword: Substring matched case-insensitively against chunk text.
            hits: Maximum number of distinct cases to return.

        Returns:
            List of ChunkVo with ``origin="sql_keyword"``, one per celex_id,
            each carrying the lowest-doc_pos chunk that contains the keyword.
        """
        return self.chunk_repo.keyword_search_all_cases(keyword, hits)

    def fulltext_search_chunks_all_cases(
        self, query: str, hits: int = 20
    ) -> List[ChunkVo]:
        """Cross-case full-text search returning one best chunk per case.

        The multi-word counterpart to ``keyword_search_chunks_all_cases``: an
        ILIKE ``'%multi word query%'`` matches nothing, so a multi-word
        ``doc_search`` query previously fell back to semantic-only.
        ``websearch_to_tsquery`` AND's the stemmed lexemes so the cases that
        mention the terms are surfaced.

        Args:
            query: Raw natural-language query (no keyword extraction).
            hits: Maximum number of distinct cases to return.

        Returns:
            List of ChunkVo with ``origin="pgvector_fts"``, one per celex_id.
        """
        return self.chunk_repo.fulltext_search_all_cases(query, hits)

    def keyword_count_chunks_all_cases(
        self, keyword: str
    ) -> list[dict[str, Any]]:
        """Return per-case keyword hit counts across all document chunks.

        Unlike ``keyword_search_chunks_all_cases`` (which returns one
        representative chunk per case), this method returns the *count* of
        matching chunks per case — essential for answering "how many chunks
        mention X" aggregation questions.

        Args:
            keyword: Substring matched case-insensitively against chunk text.

        Returns:
            List of dicts ``{"celex_id": str, "count": int}`` ordered by
            count descending, then celex_id ascending.
        """
        return self.chunk_repo.keyword_count_all_cases(keyword)

    def semantic_search_chunks(self, query: str, hits: int = settings.VECTOR_STORE_K) -> List[ChunkVo]:
        """
        Executes a vector similarity search against the document chunks
        and returns structured ChunkVo models. Utilises the query embedding cache.
        """
        query_embedding = self._get_or_cache_embedding(query)
        return self.chunk_repo.semantic_search(query_embedding, hits)

    def semantic_search_chunks_in_celex(self, celex_id: str, query: str, hits: int = settings.VECTOR_STORE_K) -> List[ChunkVo]:
        """Executes a vector similarity search against the document chunks of a specific
        CELEX document and returns structured ChunkVo models.
        """
        query_embedding = self._get_or_cache_embedding(query)
        return self.chunk_repo.semantic_search_in_celex(celex_id, query_embedding, hits)

    def semantic_search_facts(
        self, query: str, hits: int = settings.VECTOR_STORE_K, celex_id: Optional[str] = None
    ) -> List[FactSearchResult]:
        """
        Executes a Qwen3 vector similarity search against question-fact embeddings
        and returns structured FactSearchResult models.
        Utilises the query_embeddings_qwen cache to avoid redundant embedding calls.

        Args:
            query: Natural-language query to embed and search against.
            hits: Maximum number of results to return.
            celex_id: Optional CELEX document identifier; omit to search all documents.

        Returns:
            List of FactSearchResult ordered by descending cosine similarity.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string")

        query_embedding = self._get_or_cache_embedding(query)
        return self.chunk_repo.semantic_search_facts(query_embedding, hits, celex_id)

    def select_distinct_celex_id4doc_chunk_query(self, query: str) -> str:
        """
        Retrieves a list of distinct celex_ids and their chunk counts where the document content matches the query.
        Returns a JSON string list of dicts: [{"celex_id": "...", "numof": ...}, ...].
        """
        results = self.chunk_repo.select_distinct_celex_for_keyword(query)
        return json.dumps(results, indent=2)

    # ------------------------------------------------------------------
    # Fact retrieval — delegates to FactRepository
    # ------------------------------------------------------------------

    def select_fact(self, celex_id: str, name: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Retrieves a single fact from celex_facts.facts.
        Returns a tuple of (value_md, value_json) or (None, None) if not found.

        Delegates to FactRepository — uses the shared ORM session/engine.
        """
        rows = self.fact_repo.select_fact4celex(celex_id, name)
        if rows:
            return (rows[-1].value_md, rows[-1].value_json)
        return (None, None)

    def select_available_fact_names(self, celex_id: str) -> str:
        """
        Retrieves the list of distinct fact names available for a given celex_id.
        Returns a JSON string list of names.

        Delegates to FactRepository — uses the shared ORM session/engine.
        """
        names = self.fact_repo.select_available_fact_names(celex_id)
        return json.dumps(names)

    # ------------------------------------------------------------------
    # Metadata-rich semantic search
    # ------------------------------------------------------------------

    def search_semantic_chunks(
        self, 
        query: str, 
        celex_id: Optional[str] = None, 
        similarity_threshold: Optional[float] = 0.0, 
        limit: int = 5
    ) -> str:
        """
        Searches semantically for document chunks.
        
        Args:
            query: The text to search for (will be embedded).
            celex_id: Optional filter for a specific CELEX ID.
            similarity_threshold: Minimum similarity score (0 to 1).
            limit: Maximum number of hits to return. Defaults to 5.
            
        Returns:
            JSON string containing list of objects with:
            id, collection_id, document, doc_pos, token_count, path, chunk_hash
        """
        query_embedding = self._get_or_cache_embedding(query)
        results = self.chunk_repo.semantic_search_with_meta(
            query_embedding=query_embedding,
            celex_id=celex_id,
            limit=limit,
            similarity_threshold=similarity_threshold or 0.0,
        )
        return json.dumps(results, indent=2)
