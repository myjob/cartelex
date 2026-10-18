"""
Canonical ORM model registry for all celex_facts and rule_facts business tables.

Every SQLAlchemy model that maps to a business-domain table should be defined
here and registered against the shared `Base`.  This ensures that a single call
to `Base.metadata.create_all(engine)` can materialize the full schema and that
Alembic autogenerate sees all tables in one place.

Note: LangChain vector-store models (`CollectionStore`, `CelexEmbeddingStore`)
are intentionally kept in `celex_pgvector.py` under a separate `VectorBase`
because they interact with LangChain's internal session management and require
`extend_existing=True` to co-exist with LangChain's own table definitions.
"""
import datetime
import uuid
from typing import Any, Dict, Generic, List, TypeVar

from pydantic import BaseModel
from sqlalchemy import ARRAY, CheckConstraint, Column, Date, Index, String, Boolean, Text, Integer, SmallInteger, DateTime, UniqueConstraint, Float, ForeignKey, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import ENUM as PgEnum, JSONB, UUID
from sqlalchemy.sql import func

from pipelines.rag.pgvector.vector_config import SCHEMA

T = TypeVar("T")

Base = declarative_base()


# ---------------------------------------------------------------------------
# Legacy / rule-engine tables
# ---------------------------------------------------------------------------


class RuleFacts(Base):
    __tablename__ = "facts"
    __table_args__ = {"schema": "rule_facts"}
    guid = Column(String, primary_key=True)
    celex_id = Column(String, nullable=False)
    fact = Column(String)
    question = Column(String)
    value = Column(String)


# ---------------------------------------------------------------------------
# celex_facts — core business tables
# ---------------------------------------------------------------------------


class Facts(Base):
    __tablename__ = "facts"
    __table_args__ = (
        UniqueConstraint("celex_id", "name", name="uq_facts_celex_name"),
        {"schema": "celex_facts"},
    )
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    celex_id = Column(String, nullable=False)
    name = Column(String)
    value_md = Column(String)
    value_json = Column(String)
    references_doc_json = Column(JSONB)
    created_at = Column(DateTime, default=datetime.datetime.now)
    # Denormalized evidence-verification verdict (see migrate_fact_evidence.sql).
    verification_status = Column(
        PgEnum(
            "verified_supported", "weak_support", "insufficient_evidence",
            "contradicted", "missing_required_entity", "missing_required_time",
            "manual_approved", "unverified", "manual_rejected",
            name="verification_status_enum",
            schema="celex_facts",
            create_type=False,
        ),
        nullable=False,
        default="unverified",
    )
    verification_score = Column(Float)
    verification_updated_at = Column(DateTime(timezone=True))


class CorpLei(Base):
    __tablename__ = "corp_lei"
    __table_args__ = {"schema": "celex_facts"}

    lei = Column(String, primary_key=True)
    legal_name = Column(String)
    entity_category = Column(String)
    legal_jurisdiction = Column(String)
    entity_status = Column(String)
    registration_status = Column(String)
    country = Column(String)

    legal_address_first_line = Column(String)
    legal_address_additional_line = Column(String)
    legal_address_city = Column(String)
    legal_address_region = Column(String)
    legal_address_postal_code = Column(String)

    headquarters_address_first_line = Column(String)
    headquarters_address_additional_line = Column(String)
    headquarters_address_city = Column(String)
    headquarters_address_region = Column(String)
    headquarters_address_postal_code = Column(String)

    record_json = Column(JSONB)
    created_at = Column(DateTime, default=datetime.datetime.now)


class CelexFactIngestionAuditTrail(Base):
    __tablename__ = "fact_ingestion_audit_trail"
    __table_args__ = {"schema": "celex_facts"}
    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    celex_id = Column(String)
    fact_id = Column(UUID(as_uuid=True))  # FK to facts table id

    # FactExtractorParamVO
    target_fact_name = Column(String)
    question = Column(String)
    output_schema = Column(String)
    verify_fact_name = Column(String)
    force_override = Column(String)

    # New additions
    pipeline = Column(String)
    dspy_module = Column(String)
    predecessors = Column(JSONB)

    # Execution info
    success = Column(Boolean)
    errors = Column(String)
    duration = Column(Integer)
    created_at = Column(DateTime, default=datetime.datetime.now)


# ---------------------------------------------------------------------------
# celex_facts — event extraction tables
# (previously defined inline in their respective pipeline files)
# ---------------------------------------------------------------------------


class CaseCelexId(Base):
    """Maps case identifiers to their CELEX IDs in the case_celex_id table."""

    __tablename__ = "case_celex_id"
    __table_args__ = {"schema": SCHEMA}

    case_id = Column(Text, primary_key=True, nullable=False)
    celex_id = Column(Text, nullable=False)


# ---------------------------------------------------------------------------
# celex_facts — HeidelTime temporal-expression extraction
# ---------------------------------------------------------------------------


class CelexHeidelTimeExtraction(Base):
    """One HeidelTime temporal expression per row, extracted from a document chunk.

    Mirrors the :class:`~pipelines.rag.pgvector.models.HeidelTimeVO` value object so that
    downstream SQL queries can filter on ``timex_type``, ``timex_value``, or
    the character-level ``span_start`` / ``span_end`` offsets without having
    to deserialise ``raw_jsonb``.

    The ``id`` column is a server-generated UUID used as the surrogate primary
    key and also stored inside :attr:`raw_jsonb` under the key ``"id"`` so
    that the row is fully self-describing.
    """

    __tablename__ = "heideltime_extractions"
    __table_args__ = (
        Index("ix_heideltime_celex_chunk", "celex_id", "fk_chunk_id"),
        Index("ix_heideltime_source_hash", "fk_chunk_id", "source_hash"),
        UniqueConstraint(
            "celex_id", "fk_chunk_id", "tid",
            name="uq_heideltime_celex_chunk_tid",
        ),
        {"schema": SCHEMA},
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    celex_id = Column(String, nullable=False, index=True)
    fk_chunk_id = Column(String, nullable=False, index=True)

    # HeidelTime TIMEX3 fields
    tid = Column(String, nullable=False)  # e.g. "t3"
    timex_text = Column(String, nullable=False)  # surface form: "30 June 2013"
    timex_type = Column(String, nullable=False)  # DATE | TIME | DURATION | SET
    # Full calendar dates only.  HeidelTime also emits DURATION ("P3Y"),
    # SET and partial-precision ("1994-12") values; ingest_vos() stores NULL
    # for those, so the column is nullable DATE -- not a NOT NULL String,
    # which is what this model declared while the live column was already
    # ``date``.
    timex_value = Column(Date, nullable=True)  # normalised ISO value: "2013-06-30"

    # Character-level span within the source chunk
    span_start = Column(Integer, nullable=False)
    span_end = Column(Integer, nullable=False)

    # Full VO preserved verbatim for round-trip / JSONB queries
    raw_jsonb = Column(JSONB, nullable=False)

    # md5 of the chunk text this row was extracted from.  A chunk counts as
    # processed only while every one of its rows still matches md5(document);
    # NULL means "written before hashing existed" and is read as stale, so the
    # rows left behind by the in-place text repair re-extract on next touch.
    source_hash = Column(String(32), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# celex_facts — Knowledge-graph tables
# ---------------------------------------------------------------------------


class KnowledgeItem(Base):
    """A distilled knowledge nugget stored in ``celex_facts.knowledge_items``.

    The ``knowledge_tsv`` column is a PostgreSQL ``GENERATED ALWAYS AS … STORED``
    tsvector.  It is intentionally **not** mapped here because SQLAlchemy must
    never attempt to write to a generated column.  Full-text queries should use
    raw SQL / ``text()`` fragments referencing it directly.
    """

    __tablename__ = "knowledge_items"
    __table_args__ = {"schema": SCHEMA}

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    knowledge_text = Column(Text, nullable=False)
    knowledge_type = Column(Text, nullable=False)
    scope = Column(Text, nullable=False, server_default="global")
    jurisdiction = Column(Text, nullable=True)
    status = Column(Text, nullable=False, server_default="draft")
    confidence = Column(Float, nullable=True)
    created_by = Column(Text, nullable=False)
    agent_run_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeQuestionEmbedding(Base):
    """Question-variant embedding linked to a ``KnowledgeItem``.

    Stored in ``celex_facts.knowledge_question_embeddings``.
    The ``embedding`` column uses the ``pgvector`` Vector type.
    """

    __tablename__ = "knowledge_question_embeddings"
    __table_args__ = {"schema": SCHEMA}

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    knowledge_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.knowledge_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_variant = Column(Text, nullable=False)
    # Vector dimension is intentionally unspecified so the column accepts any
    # dimension — matching the DDL which declares ``public.vector`` without a
    # fixed size.
    embedding = Column("embedding", Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class KnowledgeEdge(Base):
    """Typed edge in the knowledge graph, stored in ``celex_facts.knowledge_edges``.

    Exactly one of ``dst_source_id``, ``dst_fact_id``, or ``dst_knowledge_id``
    must be non-NULL.  This invariant is enforced by a PostgreSQL ``CHECK``
    constraint and additionally validated in ``KnowledgeRepository.insert_edge``.

    The destination encodes the layered provenance model:

    * ``dst_source_id``    — raw evidence (chunk, csv, book, lei, heideltime),
      routed through ``fact_source_registry`` so facts and knowledge items ground
      against one canonical evidence catalog.
    * ``dst_fact_id``      — a derived fact (Tier 1).
    * ``dst_knowledge_id`` — another knowledge item (Tier 2), used for the
      ``supports`` / ``contradicts`` reasoning graph.
    """

    __tablename__ = "knowledge_edges"
    __table_args__ = {"schema": SCHEMA}

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    src_knowledge_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.knowledge_items.id"),
        nullable=False,
    )

    # Destination — exactly one must be set (DB CHECK constraint).
    # Raw evidence grounding delegates to the canonical fact_source_registry,
    # so any source_type the registry knows is reachable without schema churn.
    dst_source_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.fact_source_registry.source_id"),
        nullable=True,
    )
    dst_fact_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.facts.id"),
        nullable=True,
    )
    dst_knowledge_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.knowledge_items.id"),
        nullable=True,
    )

    relation_type = Column(Text, nullable=False)
    confidence = Column(Float, nullable=True)
    created_by = Column(Text, nullable=True)
    agent_run_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# celex_facts — Knowledge-ingestion pipeline tables
# ---------------------------------------------------------------------------


class KnowledgeExtractionQueue(Base):
    """Staging table: agent writes pending extraction payloads, batch consumes them.

    Lifecycle:  ``pending`` → ``processing`` → ``done`` | ``failed``
    """

    __tablename__ = "knowledge_extraction_queue"
    __table_args__ = {"schema": SCHEMA}

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    user_prompt = Column(Text, nullable=False)
    agent_answer = Column(Text, nullable=False)
    supporting_facts = Column(Text, nullable=True)
    fact_ids = Column(JSONB, nullable=True)
    chunk_ids = Column(JSONB, nullable=True)
    status = Column(Text, nullable=False, server_default="pending")
    error_message = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at = Column(DateTime(timezone=True), nullable=True)


class KnowledgeIngestAuditTrail(Base):
    """Audit row for each knowledge-ingestion batch run.

    Mirrors :class:`CelexFactIngestionAuditTrail` but with columns specific
    to knowledge ingestion (``queue_item_id``, ``knowledge_id`` instead of
    ``celex_id``, ``target_fact_name``).
    """

    __tablename__ = "knowledge_ingest_audit_trail"
    __table_args__ = {"schema": SCHEMA}

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    queue_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.knowledge_extraction_queue.id"),
        nullable=True,
    )
    knowledge_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.knowledge_items.id"),
        nullable=True,
    )
    user_prompt = Column(Text, nullable=True)
    agent_answer = Column(Text, nullable=True)
    pipeline = Column(Text, server_default="knowledge_ingest")
    dspy_module = Column(Text, nullable=True)
    predecessors = Column(JSONB, nullable=True)
    success = Column(Boolean, nullable=True)
    errors = Column(Text, nullable=True)
    duration = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# celex_facts — Data Lineage Registry (Option 2 + selective Option 3)
# ---------------------------------------------------------------------------


class SourceRegistry(Base):
    """Normalized catalog of all evidence sources that may support a fact.

    One row per distinct piece of evidence regardless of type.  The
    ``source_type`` column determines which foreign-key columns are populated:

    * ``chunk``         — ``celex_id``, ``chunk_id`` (→ pg_embedding_qwen.id),
                          ``chunk_seq`` (positional doc_pos values)
    * ``book_case``     — ``celex_id``, ``chunk_id`` (→ pg_embedding_book_qwen.id),
                          ``chunk_seq``
    * ``heideltime``    — ``celex_id``, ``heideltime_event_id``
                          (→ heideltime_extractions.id)
    * ``lei_corporate`` — ``lei_id`` (→ corp_lei.lei)
    * ``csv_human``     — ``celex_id``, ``csv_row``
    * ``fact``          — ``celex_id``, ``source_fact_id`` (→ facts.id); a
                          predecessor fact that a derived fact was computed from
    """

    __tablename__ = "fact_source_registry"
    __table_args__ = (
        Index("ix_source_registry_celex", "celex_id"),
        Index("ix_source_registry_type", "source_type"),
        # Normalization: one row per distinct piece of evidence. Partial unique
        # indexes enforce the natural key per source type so register_* upserts
        # converge on a single shared source_id.
        Index(
            "uq_source_registry_chunk",
            "source_type",
            "chunk_id",
            unique=True,
            postgresql_where=text("chunk_id IS NOT NULL"),
        ),
        Index(
            "uq_source_registry_heideltime",
            "heideltime_event_id",
            unique=True,
            postgresql_where=text("heideltime_event_id IS NOT NULL"),
        ),
        Index(
            "uq_source_registry_lei",
            "lei_id",
            unique=True,
            postgresql_where=text("lei_id IS NOT NULL"),
        ),
        Index(
            "uq_source_registry_csv",
            "celex_id",
            "csv_row",
            unique=True,
            postgresql_where=text("csv_row IS NOT NULL"),
        ),
        Index(
            "uq_source_registry_fact",
            "source_fact_id",
            unique=True,
            postgresql_where=text("source_fact_id IS NOT NULL"),
        ),
        {"schema": SCHEMA},
    )

    source_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    source_type = Column(
        PgEnum(
            "chunk", "csv_human", "book_case", "lei_corporate", "heideltime", "fact",
            name="source_type_enum",
            schema=SCHEMA,
            create_type=False,
        ),
        nullable=False,
    )
    celex_id = Column(String, nullable=True)
    # Stable identifier inside pg_embedding_qwen / pg_embedding_book_qwen.
    # varchar (not uuid) because those tables are LangChain-owned and key on a
    # varchar id holding UUID-format strings; matching the type avoids casts.
    chunk_id = Column(String, nullable=True)
    # doc_pos values — used for backfill and human-readable display only
    chunk_seq = Column(ARRAY(Integer), nullable=True)
    # FK to heideltime_extractions.id (kept loose to match existing style)
    heideltime_event_id = Column(UUID(as_uuid=True), nullable=True)
    # FK to corp_lei.lei
    lei_id = Column(String, nullable=True)
    csv_row = Column(Integer, nullable=True)
    # FK to facts.id — the predecessor fact a derived fact was computed from
    source_fact_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.facts.id", ondelete="CASCADE"),
        nullable=True,
    )
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FactSourceLink(Base):
    """Join table linking a fact to its evidence sources with lineage metadata.

    Composite PK on ``(fact_id, source_id)`` — one row per fact/source pair.

    The ``relation_type`` column encodes Option 3 edge semantics selectively:
      * ``extracted_from``      — primary extraction (chunk or heideltime → fact)
      * ``corroborated_by``     — secondary confirmation from another source
      * ``entity_resolved_via`` — entity-resolution step (e.g. LEI lookup)
      * ``derived_from``        — computed / transformed value
      * ``gated_by``            — control-flow precondition; excluded from
                                  provenance traversal

    ``trust_rank`` (1–10) defaults by source type:
      csv_human=9, book_case=8, lei_corporate=7, heideltime=6, chunk=5, fact=4
    """

    __tablename__ = "fact_source_link"
    __table_args__ = (
        Index("ix_fact_source_link_fact", "fact_id"),
        Index("ix_fact_source_link_source", "source_id"),
        {"schema": SCHEMA},
    )

    fact_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.facts.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    source_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.fact_source_registry.source_id"),
        primary_key=True,
        nullable=False,
    )
    # Pipeline or module that produced this link (e.g. 'universal', 'heideltime_raw')
    extraction_method = Column(String, nullable=False)
    # Structured producer provenance (mirrors fact_ingestion_audit_trail)
    pipeline = Column(String, nullable=True)
    dspy_module = Column(String, nullable=True)
    # Option 3 edge type
    relation_type = Column(
        PgEnum(
            "extracted_from", "corroborated_by", "entity_resolved_via",
            "derived_from", "gated_by",
            name="relation_type_enum",
            schema=SCHEMA,
            create_type=False,
        ),
        nullable=False,
        default="extracted_from",
    )
    trust_rank = Column(SmallInteger, nullable=False, default=5)
    conflict_flag = Column(Boolean, nullable=False, default=False)
    conflict_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FactEvidence(Base):
    """Verification ledger: the best (or summary) evidence row per fact.

    Written by the evidence verifier through
    ``FactEvidenceRepository.upsert_evidence``, which replaces a fact's rows
    with the single current verdict: the best supporting chunk, or a chunk-less
    summary row for human / derived / no-provenance facts. Every non-excluded
    fact therefore carries exactly one ledger entry and the table is never
    sparse — see migrate_fact_evidence.sql.

    ``chunk_id`` is the stable ``pg_embedding_qwen.id`` (a LangChain-owned
    varchar holding a UUID-format string), or ``NULL`` for the summary row.
    The verdict label/score mirror the LLM/​deterministic support decision.
    """

    __tablename__ = "fact_evidence"
    __table_args__ = (
        Index("ix_fact_evidence_fact", "fact_id"),
        Index(
            "uq_fact_evidence_chunk",
            "fact_id",
            "chunk_id",
            unique=True,
            postgresql_where=text("chunk_id IS NOT NULL"),
        ),
        Index(
            "uq_fact_evidence_summary",
            "fact_id",
            unique=True,
            postgresql_where=text("chunk_id IS NULL"),
        ),
        {"schema": SCHEMA},
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    fact_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.facts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # varchar (not uuid) to match the LangChain-owned pg_embedding_qwen.id.
    # NULL for the per-fact summary row.
    chunk_id = Column(String, nullable=True)

    support_label = Column(Text, nullable=True)
    final_score = Column(Float, nullable=True)

    entity_score = Column(Float, nullable=True)
    keyword_score = Column(Float, nullable=True)
    semantic_score = Column(Float, nullable=True)
    temporal_score = Column(Float, nullable=True)
    provenance_score = Column(Float, nullable=True)

    evidence_quote = Column(Text, nullable=True)
    missing_elements = Column(JSONB, nullable=True)

    verified_at = Column(DateTime(timezone=True), server_default=func.now())


class Fact2BookCheck(Base):
    """Check ledger: every cross-check of an extracted fact against the book.

    Written by ``BookCrossCheckModule`` for each fact it judges against the
    book chapter. The ``action`` column records the routing decision
    (``corroborated`` / ``mismatch`` / ``skipped``), so the table is a complete
    per-fact check ledger rather than a contradiction-only one. One row per
    ``(fact_id, celex_id)`` pair; upserted on that pair so re-runs heal rather
    than duplicate.

    ``book_chunk_id`` is the stable ``pg_embedding_book_qwen.id`` (a
    LangChain-owned varchar holding a UUID-format string) of the chunk whose
    chapter context was judged against the fact. It is a payload column, not
    part of the key: the reranker re-selects the anchor on every run, so keying
    on it would insert a second row whenever the anchor drifted.
    """

    __tablename__ = "fact2book_checks"
    __table_args__ = (
        Index("ix_fact2book_checks_fact", "fact_id"),
        Index("ix_fact2book_checks_celex", "celex_id"),
        Index(
            "uq_fact2book_checks",
            "fact_id",
            "celex_id",
            unique=True,
        ),
        CheckConstraint(
            "action IN ('corroborated', 'mismatch', 'skipped')",
            name="ck_fact2book_checks_action",
        ),
        {"schema": SCHEMA},
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    fact_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.facts.id", ondelete="CASCADE"),
        nullable=False,
    )
    celex_id = Column(Text, nullable=False)
    case_id = Column(Text, nullable=True)
    # varchar to match the LangChain-owned pg_embedding_book_qwen.id type.
    book_chunk_id = Column(String, nullable=False)
    book_path = Column(Text, nullable=True)
    explanation = Column(Text, nullable=False)
    support_label = Column(Text, nullable=True)
    final_score = Column(Float, nullable=True)
    # Routing decision: 'corroborated' | 'mismatch' | 'skipped'.
    action = Column(String, nullable=False)
    # How the book references the case: 'landmark' | 'simple'.
    reference_type = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ListOutput(BaseModel, Generic[T]):
    items: List[T]


class DictOutput(BaseModel, Generic[T]):
    mapping: Dict[str, T]


# ---------------------------------------------------------------------------
# HeidelTime value object
# ---------------------------------------------------------------------------


class HeidelTimeVO(BaseModel):
    """Immutable value object representing a single temporal expression
    returned by HeidelTime / py_heideltime.

    Source dict shape::

        {'text': '30 June 2013', 'tid': 't3', 'type': 'DATE',
         'value': '2013-06-30', 'span': [117, 129]}

    The ``span`` list is decomposed into :attr:`span_start` / :attr:`span_end`
    for direct mapping to integer columns.  :attr:`raw_jsonb` preserves the
    original dict verbatim for round-trip storage in a PostgreSQL ``JSONB``
    column.
    """

    model_config = {"frozen": True}  # true value object — immutable after construction

    celex_id: str  # fk for documents table
    fk_chunk_id: str  # fk for chunks table
    text: str  # text of the temporal expression
    tid: str  # unique identifier for the temporal expression
    type: str  # type of the temporal expression
    value: str  # value of the temporal expression
    span_start: int  # start index of the temporal expression in the chunk
    span_end: int  # end index of the temporal expression in the chunk
    raw_jsonb: Dict[str, Any]  # raw json object of the temporal expression

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HeidelTimeVO":
        """Construct from a raw HeidelTime output dict.

        Example::

            vo = HeidelTimeVO.from_dict(
                {'text': '30 June 2013', 'tid': 't3', 'type': 'DATE',
                 'value': '2013-06-30', 'span': [117, 129]}
            )
        """
        span: List[int] = d["span"]
        return cls(
            celex_id=d["celex_id"],
            fk_chunk_id=d["fk_chunk_id"],
            text=d["text"],
            tid=d["tid"],
            type=d["type"],
            value=d["value"],
            span_start=span[0],
            span_end=span[1],
            raw_jsonb=dict(d),  # shallow copy — safe for JSON-serialisable values
        )


# ---------------------------------------------------------------------------
# celex_facts — typed entity graph (Phase 5.1)
# ---------------------------------------------------------------------------


class Entity(Base):
    """A typed node in the entity graph, deduplicated per ``(kind, natural_key)``.

    Materialized from ``celex_facts.facts`` by
    ``src/batch/Main_materialize_entity_graph.py``.  ``natural_key`` encodes the
    dedup identity per kind: a company's LEI (or ``'name:'<normalized>`` when
    unresolved), a case's ``celex_id``, or a provision's canonical concept — so
    the same company across many decisions collapses to one row.
    """

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("kind", "natural_key", name="uq_entities_kind_natural"),
        Index(
            "ix_entities_lei",
            "lei_id",
            postgresql_where=text("lei_id IS NOT NULL"),
        ),
        Index(
            "ix_entities_celex",
            "celex_id",
            postgresql_where=text("celex_id IS NOT NULL"),
        ),
        Index("ix_entities_kind", "kind"),
        {"schema": SCHEMA},
    )

    entity_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    kind = Column(
        PgEnum(
            "company", "case", "market", "provision",
            name="entity_kind_enum",
            schema=SCHEMA,
            create_type=False,
        ),
        nullable=False,
    )
    canonical_name = Column(Text, nullable=False)
    natural_key = Column(Text, nullable=False)
    lei_id = Column(Text, nullable=True)
    nace_code = Column(Text, nullable=True)
    celex_id = Column(Text, nullable=True)
    article_number = Column(Integer, nullable=True)
    attrs_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EntityEdge(Base):
    """A typed, lineage-grounded relation between two ``Entity`` nodes.

    Edge identity is ``(src_entity_id, dst_entity_id, relation, source_fact_id)``:
    the amount / payload lives in ``value_json`` and is refreshed in place on
    re-materialization.  ``source_fact_id`` (FK to ``facts.id``, ``ON DELETE
    CASCADE``) keeps every edge resolvable through ``fact_provenance()``.
    """

    __tablename__ = "entity_edges"
    __table_args__ = (
        UniqueConstraint(
            "src_entity_id", "dst_entity_id", "relation", "source_fact_id",
            name="uq_entity_edges",
        ),
        Index("ix_edges_src_rel", "src_entity_id", "relation"),
        Index("ix_edges_dst_rel", "dst_entity_id", "relation"),
        Index("ix_edges_celex", "celex_id"),
        Index("ix_edges_source_fact", "source_fact_id"),
        {"schema": SCHEMA},
    )

    edge_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    src_entity_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    )
    dst_entity_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    )
    relation = Column(
        PgEnum(
            "participated_in", "fined", "leniency_applied", "leniency_granted",
            "leniency_rejected", "parent_of", "active_in", "violated", "dated_in",
            name="edge_relation_enum",
            schema=SCHEMA,
            create_type=False,
        ),
        nullable=False,
    )
    celex_id = Column(Text, nullable=False)
    value_json = Column(Text, nullable=True)
    source_fact_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.facts.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

