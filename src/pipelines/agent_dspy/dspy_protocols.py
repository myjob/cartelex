from typing import Any, List, Optional, Dict, Protocol, Sequence

from pydantic import BaseModel, Field

from extract.rag.models.chunk_vo import BookChapterVo, ChunkVo, RankedChunkVo, FactSearchResult
from pipelines.graph.entity_graph_models import EdgeRelation, GraphNeighbourhood

# --- Protocols & Models (Rule 3) ---


class KBFactSearcher(Protocol):
    """Semantic search over the structured knowledge base (question2fact2embeddings)."""

    def semantic_search_facts(
        self, query: str, hits: int = 10, celex_id: Optional[str] = None
    ) -> List[FactSearchResult]: ...


class KnowledgeSearcher(Protocol):
    """Full-text search over the distilled knowledge-item store (knowledge_items table).

    Satisfied by ``pipelines.rag.pgvector.knowledge_repository.KnowledgeRepository``.
    Used by the ``knowledge_search`` retrieval tool to surface cross-case nuggets
    that were distilled from previous Q&A interactions.
    """

    def search_knowledge_fulltext(self, query: str, *, limit: int = 20) -> list: ...

    def search_knowledge_any_terms(self, query: str, *, limit: int = 20) -> list: ...


class KnowledgeQuestionMatcher(Protocol):
    """Nearest stored question variants for a question embedding.

    Satisfied by ``pipelines.rag.pgvector.knowledge_repository.KnowledgeRepository``.
    Backs the pre-plan knowledge probe: the question itself is matched against
    the phrasings each knowledge item was ingested under.
    """

    def search_knowledge_by_question(
        self, embedding: list, *, max_distance: float, limit: int
    ) -> list[dict]: ...

    def get_knowledge_sources(self, item_id: Any) -> list[dict]: ...


class GraphSearcher(Protocol):
    """Multi-hop traversal over the typed entity graph.

    Satisfied by ``pipelines.rag.pgvector.entity_graph_repository
    .EntityGraphRepository``.  Backs the ``graph_search`` retrieval tool, which
    answers ADJACENCY questions the flat graph views cannot express: which
    companies were in cartels together, who is connected to whom through shared
    cases.  The views stay the right surface for projecting, filtering and
    counting company/case relations through ``kb_sql``.
    """

    def co_participants_by_name(
        self,
        name_stem: str,
        relation: EdgeRelation = EdgeRelation.PARTICIPATED_IN,
        max_hops: Optional[int] = None,
        max_seeds: Optional[int] = None,
        max_peers: Optional[int] = None,
    ) -> GraphNeighbourhood: ...


class CelexValidator(Protocol):
    """Detects and validates celex_id / case_id references in a prompt.

    ``check_celex_or_case_id`` returns the canonical ``celex_id`` (e.g.
    ``CELEX-31969D0216``) on success or the literal string ``"NA"`` when the
    identifier is unknown.  ``find_candidates`` reports which prompt tokens were
    *meant* to be case references, so a mistyped id is rejected at the gate
    rather than mistaken for prose.  ``suggest`` offers near-miss corrections
    for an unresolved token.

    Satisfied by ``tools.celex_checker.CelexCheckerToolkit``, whose
    implementations are backed by the cached identifier index.
    """

    def check_celex_or_case_id(self, identifier: str) -> str: ...

    def find_candidates(self, question: str) -> List[str]: ...

    def suggest(self, identifier: str, limit: int = 3) -> Sequence[str]: ...


class CrossCaseAggregator(Protocol):
    """Read-only SQL aggregation over celex_facts.facts for macroscopic questions."""

    def aggregate_facts(self, sql: str) -> List[Dict[str, Any]]: ...


class ChunkLoader(Protocol):
    """Resolves a stable chunk id to its raw source text for provenance display.

    Satisfied by ``pipelines.rag.pgvector.chunk_repository.ChunkRepository``;
    used to embed the verbatim source text behind a fact's lineage reference
    inline in the answer.
    """

    def get_chunk_by_id(self, chunk_id: str) -> Optional[Dict[str, Any]]: ...


class DocChunkSearcher(Protocol):
    """Cross-case search over raw document chunks (pg_embedding_qwen).

    Use for entity-specific lookups (company names, commissioner names,
    countries, towns) where the answer may not be captured in structured facts.

    Three complementary surfaces, all satisfied by ``CelexDatabaseToolkit``:
      - ``keyword_search_chunks_all_cases`` — ILIKE substring match; reliable
        for single proper nouns, which embed poorly as short vector queries.
      - ``fulltext_search_chunks_all_cases`` — Postgres FTS; multi-word
        queries (where a single ILIKE ``'%multi word%'`` matches nothing) hit
        the stemmed lexemes instead.
      - ``semantic_search_chunks`` — vector similarity; covers paraphrase /
        concept matches the lexical passes miss.
    """

    def semantic_search_chunks(self, query: str, hits: int = 5) -> List[ChunkVo]: ...

    def keyword_search_chunks_all_cases(
        self, keyword: str, hits: int = 20
    ) -> List[ChunkVo]: ...

    def fulltext_search_chunks_all_cases(
        self, query: str, hits: int = 20
    ) -> List[ChunkVo]:
        """Cross-case FTS returning one best chunk per case for *query*.

        The multi-word counterpart to ``keyword_search_chunks_all_cases``:
        ``websearch_to_tsquery`` AND's the stemmed lexemes, so a multi-word
        ``doc_search`` query surfaces the cases mentioning the terms instead
        of falling back to semantic-only. Optional — ``_run_doc_search``
        degrades gracefully when a searcher does not implement it.
        """
        ...

    def keyword_count_chunks_all_cases(
        self, keyword: str
    ) -> list[dict[str, Any]]:
        """Return per-case keyword hit counts across all document chunks.

        Each dict has ``celex_id`` (str) and ``count`` (int) — the number of
        chunks in that case whose text contains *keyword* (case-insensitive).
        Results are ordered by count descending.  Used by ``doc_search`` to
        report aggregate counts so the synthesizer can answer "how many …"
        questions about document chunks.
        """
        ...



class FirstPageVO(BaseModel):
    """First-page (``doc_pos = 0``) chunk of a CELEX document with its identity.

    Carries the immutable chunk ``id`` and structural ``doc_pos`` alongside the
    raw text so callers can record exact chunk lineage instead of assuming a
    positional reference.

    Attributes
    ----------
    chunk_id:
        Stable UUID of the ``pg_embedding_qwen`` row, as a string.
    doc_pos:
        Structural chunk index of the first page (always ``0`` today).
    document:
        Raw chunk text of the first page.
    """

    chunk_id: str = Field(description="Stable UUID of the first-page chunk row.")
    doc_pos: int = Field(description="Structural chunk index of the first page.")
    document: str = Field(description="Raw text of the first-page chunk.")


class SQLFactStore(Protocol):
    def select_fact4celex(self, celex_id: str, name: str) -> list: ...
    def select_legacy_fact4celex(self, celex_id: str, name: str) -> list: ...
    def select_case_ids_for_celex(self, celex_id: str) -> list[str]: ...
    def select_facts4celex(self, celex_id: str) -> dict[str, str]: ...
    def select_facts4celex_with_ids(
        self, celex_id: str
    ) -> list[tuple[str, str, str]]: ...
    def select_named_facts4celex(self, celex_id: str, names: list[str]) -> list: ...
    def select_first_page4celex(self, celex_id: str) -> Optional["FirstPageVO"]: ...

class HeidelTimeEventRetriever(Protocol):
    """Protocol for data-access objects that retrieve heideltime dates per chunk."""

    def select_chunks_with_dates(self, celex_id: str) -> list[dict[str, Any]]:
        """Return chunks with their HeidelTime dates and the sources of each date.

        Each dict has keys ``fk_chunk_id`` (str), ``chunk_text`` (str),
        ``canonical_dates`` (list[str]) and ``date_sources``, mapping each
        canonical date to the ``heideltime_extractions`` rows that produced it
        as ``{"id", "timex_text"}`` dicts.
        """
        ...


class ChunkRetriever(Protocol):
    """Protocol for data-access objects that retrieve document chunks."""

    def keyword_search(self, celex_id: str, keyword: str) -> list[ChunkVo]: ...
    def semantic_search_in_celex(
        self,
        celex_id: str,
        query_embedding: list,
        hits: int,
        section_types: Optional[list[str]] = None,
    ) -> list[ChunkVo]: ...
    def fulltext_search_in_celex(
        self,
        celex_id: str,
        query: str,
        hits: int,
        section_types: Optional[list[str]] = None,
    ) -> list[ChunkVo]: ...
    def websearch_search_in_celex(
        self,
        celex_id: str,
        query: str,
        hits: int,
        use_or: bool = True,
        section_types: Optional[list[str]] = None,
    ) -> list[ChunkVo]: ...
    def trigram_search(
        self, celex_id: str, term: str, hits: int
    ) -> list[ChunkVo]: ...
    def select_closing_chunks(self, celex_id: str, count: int) -> list[ChunkVo]: ...


class KeywordSearch(Protocol):
    def search(
        self,
        celex_id: str,
        query: str,
        top_k: Optional[int] = None,
        section_types: Optional[list[str]] = None,
    ) -> list[RankedChunkVo]: ...


class BookKeywordSearch(Protocol):
    def keyword_exists(self, keyword: str) -> bool: ...


class BookSearcher(Protocol):
    def search_keyword(self, keyword: str) -> list[BookChapterVo]: ...
    def search_semantic(self, query: str) -> list[BookChapterVo]: ...


class BookSearchToolkit(BookKeywordSearch, BookSearcher, Protocol):
    """Combined protocol for toolkits that support both keyword existence checks and hybrid search."""

    ...


class HybridSearcher(Protocol):
    def search(
        self,
        celex_id: str,
        query: str,
        top_k: int,
        section_types: Optional[list[str]] = None,
    ) -> list[RankedChunkVo]: ...


class RegexRetriever(Protocol):
    def regex_retriever(
        self,
        celex_id: str,
        regex: str,
        section_types: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]: ...


class FactOutput(BaseModel):
    items: List[Dict[str, Any]] = Field(
        description="A list of extracted dictionaries matching the requested schema. STRICTLY ONLY use the requested keys. Do NOT add 'Role', 'source', or any other extra keys."
    )
    source: str = Field(description="The data origin (sql/vector/verified)")
    references_doc: Optional[List[int]] = Field(
        default=None,
        description="The list of chunk doc_pos that support the extracted fact.",
    )
    lineage_sources: Optional[List[Any]] = Field(
        default=None,
        description=(
            "Optional explicit lineage sources (LineageSourceVO instances) "
            "emitted by a module. When present, the ingestor persists these "
            "directly instead of deriving chunk lineage from references_doc. "
            "Typed as Any to avoid a circular import with the ingestor layer."
        ),
    )


class VerificationAudit(BaseModel):
    verified_items: List[Dict[str, str]] = Field(
        description="Items confirmed by context and golden list. MUST preserve exact original keys. DO NOT add 'Role' or 'source'."
    )
    hallucinations: List[Dict[str, str]] = Field(
        description="Items rejected or missing fine/value"
    )
    verdict: bool = Field(
        description="True if all extracted items passed verification, False otherwise."
    )


class FactComparisonOutput(BaseModel):
    is_match: bool = Field(
        description="True if the facts represent the same fundamental information."
    )
    reasoning: str = Field(
        description="A brief explanation of why the facts do or do not match."
    )


class ChunkReferenceOutput(BaseModel):
    references_doc: List[int] = Field(
        description="List of integer doc_pos representing the chunks that explicitly support the fact. Max length 3."
    )
