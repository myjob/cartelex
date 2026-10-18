from enum import StrEnum

from pydantic import BaseModel, Field


class SectionType(StrEnum):
    """Structural section of an EU Commission competition decision.

    Every decision follows the same rhetorical skeleton; the classifier batch
    (Main_classify_sections.py) assigns one of these per chunk so retrieval
    can filter and rank by structure instead of raw document position.
    """

    PARTIES = "parties"
    RECITALS_FACTS = "recitals_facts"
    LEGAL_ASSESSMENT = "legal_assessment"
    FINES = "fines"
    OPERATIVE = "operative"
    ANNEX = "annex"
    OTHER = "other"


class ChunkVo(BaseModel):
    """
    A foundational Value Object for passing retrieval results across
    different data domains into universal ranking and extraction pipelines.
    """
    id: str = Field(description="The unique identifier for the document chunk.")
    celex_id: str = Field(default="", description="The CELEX document identifier this chunk belongs to.")
    chunk: str = Field(description="The textual content of the chunk.")
    origin: str = Field(description="The source data origin, e.g., 'sql_keyword', 'pgvector_semantic', 'fact_table'.")
    similarity: float = Field(description="Cosine Similarity based on the retrieve the actual similarity score in your pgvector query")
    doc_pos: int = Field(default=0, description="The structural chunk integer index for reverse-pyramid sorting.")
    section_type: SectionType | None = Field(
        default=None,
        description="Decision section this chunk belongs to; None when unclassified.",
    )

class RankedChunkVo(ChunkVo):
    """
    An augmented ChunkVo containing the scoring metric assigned by a reranker.
    """
    score: float = Field(description="The relevance score assigned to this chunk.")
    base_score: float = Field(default=0.0, description="The raw semantic model score before heuristics")


class FactSearchResult(BaseModel):
    """Value object returned by semantic_search_facts."""

    id: str = Field(description="fk_fact_id from question2fact2embeddings.")
    celex_id: str = Field(description="CELEX document identifier.")
    fact_name: str = Field(description="Name of the extracted fact.")
    question: str = Field(description="Question used during fact extraction.")
    value_txt: str = Field(description="Plain-text value of the fact.")
    hash_q_a: str = Field(description="MD5 hash of (question, value_txt) for change detection.")
    similarity: float = Field(description="Cosine similarity score (1 = identical, 0 = orthogonal).")


class BookChapterVo(BaseModel):
    """Value object for a row of expanded book-chapter text."""

    chapter_title: str = Field(description="The chapter heading from the matched chapter path.")
    sub_chapter_title: str = Field(description="The sub chapter heading from the matched chapter path.")
    level: int = Field(description="Nesting depth of this text segment within the book structure.")
    document: str = Field(description="The textual content of this segment.")
    path: str = Field(description="Full hierarchical path of this segment.")
    token_count: int | None = Field(default=None, description="Token count for this segment.")
    doc_pos: int = Field(default=0, description="The structural chunk integer index for reverse-pyramid sorting.")
