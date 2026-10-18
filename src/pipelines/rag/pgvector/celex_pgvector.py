"""
LangChain-derived vector store for Celex document embeddings.

This module owns `CelexPGVector` (a PGVector subclass) and its two companion
ORM models — `CollectionStore` and `CelexEmbeddingStore` — which are registered
under a dedicated `VectorBase`.


Session management: `CelexPGVector` inherits `_make_sync_session()` from
LangChain's `PGVector` and does not use the shared `session.py` factory.
"""

from __future__ import annotations

import uuid
import logging
from typing import (
    Any,
    List,
    Optional,
    Tuple,
    Sequence
)
from typing import cast as typing_cast

import sqlalchemy
from langchain_core.documents import Document
from sqlalchemy import String, Integer, DateTime, select, func
from sqlalchemy.dialects.postgresql import JSONB, UUID, JSON
from sqlalchemy.orm import relationship, Session, declarative_base
from pgvector.sqlalchemy import Vector

from langchain_postgres.vectorstores import (
    PGVector
)
from sqlalchemy.dialects.postgresql import insert

logger = logging.getLogger(__name__)

# Separate declarative base for LangChain vector-store models.
# See module docstring for why this is distinct from models.Base.
VectorBase = declarative_base()

class CollectionStore(VectorBase):
    """Collection store."""

    __tablename__ = "langchain_pg_collection"
    __table_args__ = {"extend_existing": True}

    uuid = sqlalchemy.Column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name = sqlalchemy.Column(sqlalchemy.String, nullable=False, unique=True)
    cmetadata = sqlalchemy.Column(JSON)

    embeddings = relationship(
        "CelexEmbeddingStore",
        back_populates="collection",
        passive_deletes=True,
    )



    @classmethod
    def get_by_name(
        cls, session: Session, name: str
    ) -> Optional["CollectionStore"]:
        return (
            session.query(cls)
            .filter(typing_cast(sqlalchemy.Column, cls.name) == name)
            .first()
        )

    @classmethod
    def get_or_create(
        cls,
        session: Session,
        name: str,
        cmetadata: Optional[dict] = None,
    ) -> Tuple["CollectionStore", bool]:
        created = False
        collection = cls.get_by_name(session, name)
        if collection:
            return collection, created

        collection = cls(name=name, cmetadata=cmetadata)
        session.add(collection)
        session.commit()
        created = True
        return collection, created

class CelexEmbeddingStore(VectorBase):
    """Custom Embedding store with specific columns."""

    __tablename__ = "langchain_pg_embedding"
    
    # Override table args to drop existing constraints if needed or add new ones
    # We might need to handle migration manually if the table already exists with different schema
    __table_args__ = {"extend_existing": True}

    id = sqlalchemy.Column(sqlalchemy.String, primary_key=True)

    collection_id = sqlalchemy.Column(
        UUID(as_uuid=True),
        sqlalchemy.ForeignKey(
            f"{CollectionStore.__tablename__}.uuid",
            ondelete="CASCADE",
        ),
    )
    collection = relationship(CollectionStore, back_populates="embeddings")

    # Use generic Vector type to allow class to be defined at module level without fixed dimension
    embedding = sqlalchemy.Column(Vector)
    document = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    cmetadata = sqlalchemy.Column(JSONB, nullable=True)

    # Custom Columns
    celex_id = sqlalchemy.Column(String, nullable=True, index=True)
    doc_type = sqlalchemy.Column(String, nullable=True)
    token_count = sqlalchemy.Column(Integer, nullable=True)
    path = sqlalchemy.Column(String, nullable=True)
    chunk_hash = sqlalchemy.Column(String, nullable=True, index=True)
    doc_pos = sqlalchemy.Column(Integer, nullable=True)
    ingestion_timestamp = sqlalchemy.Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class PgEmbeddingQwenStore(VectorBase):
    """Embedding store for Qwen3 vectors in ``pg_embedding_qwen`` (no collection FK)."""

    __tablename__ = "pg_embedding_qwen"
    __table_args__ = {"extend_existing": True}

    id = sqlalchemy.Column(sqlalchemy.String, primary_key=True)

    # Kept for schema compatibility but NOT a FK to langchain_pg_collection.
    collection_id = sqlalchemy.Column(UUID(as_uuid=True), nullable=True)

    embedding = sqlalchemy.Column(Vector)
    document = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    cmetadata = sqlalchemy.Column(JSONB, nullable=True)

    celex_id = sqlalchemy.Column(String, nullable=True, index=True)
    doc_type = sqlalchemy.Column(String, nullable=True)
    token_count = sqlalchemy.Column(Integer, nullable=True)
    path = sqlalchemy.Column(String, nullable=True)
    chunk_hash = sqlalchemy.Column(String, nullable=True, index=True)
    doc_pos = sqlalchemy.Column(Integer, nullable=True)
    # Written by the backfill batches (Main_classify_sections /
    # Main_enrich_chunk_context), never by ingest; deliberately excluded from
    # the add_embeddings upsert set_ so a re-ingest cannot null them out.
    context_prefix = sqlalchemy.Column(sqlalchemy.Text, nullable=True)
    section_type = sqlalchemy.Column(String, nullable=True, index=True)
    ingestion_timestamp = sqlalchemy.Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PgEmbeddingBookQwen3Store(VectorBase):
    """Embedding store for book-section vectors in ``pg_embedding_book_qwen``.

    Promoted columns mirror ``BookNode`` fields written into ``Document.metadata``
    by ``BookMarkdownIngestor``.  There is no ``celex_id``; books are identified
    by ``book_name``.
    """

    __tablename__ = "pg_embedding_book_qwen"
    __table_args__ = {"extend_existing": True}

    id = sqlalchemy.Column(String, primary_key=True)
    collection_id = sqlalchemy.Column(
        UUID(as_uuid=True),
        sqlalchemy.ForeignKey(
            f"{CollectionStore.__tablename__}.uuid",
            ondelete="CASCADE",
        ),
        nullable=True,
    )
    collection = relationship(CollectionStore, foreign_keys=[collection_id])
    embedding = sqlalchemy.Column(Vector)
    document = sqlalchemy.Column(String, nullable=True)
    cmetadata = sqlalchemy.Column(JSONB, nullable=True)
    book_name = sqlalchemy.Column(String, nullable=True, index=True)
    doc_type = sqlalchemy.Column(String, nullable=True)
    token_count = sqlalchemy.Column(Integer, nullable=True)
    path = sqlalchemy.Column(String, nullable=True)
    level = sqlalchemy.Column(Integer, nullable=True, index=True)
    title = sqlalchemy.Column(String, nullable=True)
    text_line = sqlalchemy.Column(Integer, nullable=True)
    source = sqlalchemy.Column(String, nullable=True)
    chunk_hash = sqlalchemy.Column(String, nullable=True, index=True)
    ingestion_timestamp = sqlalchemy.Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    doc_pos = sqlalchemy.Column(Integer, nullable=True)


def _get_celex_embedding_collection_store(vector_dimension: Optional[int] = None) -> Any:
    """
    Returns the custom models for the Celex vector store.
    Arguments are ignored as we use static class definitions for better IDE support.
    """
    return CelexEmbeddingStore, CollectionStore


def _get_pg_embedding_qwen_store(vector_dimension: Optional[int] = None) -> Any:
    """
    Returns the pg_embedding_qwen models for the Qwen3 vector store.
    Arguments are ignored as we use static class definitions for better IDE support.
    """
    return PgEmbeddingQwenStore, CollectionStore


def _get_pg_embedding_book_qwen_store(vector_dimension: Optional[int] = None) -> Any:
    """
    Returns the pg_embedding_book_qwen models for the book-section vector store.
    Arguments are ignored as we use static class definitions for better IDE support.
    """
    return PgEmbeddingBookQwen3Store, CollectionStore


class CelexPGVector(PGVector):
    def __post_init__(self) -> None:
        """Initialize the store."""
        if self.create_extension:
            self.create_vector_extension()

        # Use our custom store generator
        # Note: vector_dimension is ignored by our static implementation
        EmbeddingStore, CollectionStore = _get_celex_embedding_collection_store(
            self._embedding_length
        )
        self.CollectionStore = CollectionStore
        self.EmbeddingStore = EmbeddingStore
        self.create_tables_if_not_exists()
        self.create_collection()

    def create_tables_if_not_exists(self) -> None:
        with self._make_sync_session() as session:
            VectorBase.metadata.create_all(session.get_bind())
            session.commit()

    async def acreate_tables_if_not_exists(self) -> None:
        assert self._async_engine, "This method must be called with async_mode"
        async with self._async_engine.begin() as conn:
            await conn.run_sync(VectorBase.metadata.create_all)

    def drop_tables(self) -> None:
        with self._make_sync_session() as session:
            VectorBase.metadata.drop_all(session.get_bind())
            session.commit()

    async def adrop_tables(self) -> None:
        assert self._async_engine, "This method must be called with async_mode"
        await self.__apost_init__()  # Lazy async init
        async with self._async_engine.begin() as conn:
            await conn.run_sync(VectorBase.metadata.drop_all)

    #: Metadata key (and promoted column) that, together with ``chunk_hash``,
    #: identifies a chunk for deduplication: ``celex_id`` for decision chunks,
    #: ``book_name`` for book sections (see ``CelexPGVectorPgBookQwen3``).
    _DEDUP_KEY_FIELD: str = "celex_id"
    #: Metadata keys promoted to their own columns on insert and copied back
    #: into ``Document.metadata`` on read. ``section_type`` / ``context_prefix``
    #: are deliberately absent: the backfill batches write them after ingest,
    #: and an id-collision re-ingest must not reset them to NULL.
    _PROMOTED_COLUMNS: tuple[str, ...] = (
        "doc_type", "token_count", "path", "chunk_hash", "doc_pos",
    )
    #: Columns a metadata filter may address directly instead of via JSONB.
    _FILTER_COLUMNS: frozenset[str] = frozenset({
        "celex_id", "doc_type", "token_count", "path", "chunk_hash",
        "doc_pos", "section_type", "context_prefix",
    })

    def add_embeddings(
        self,
        texts: Sequence[str],
        embeddings: List[List[float]],
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> List[str]:
        """Add embeddings, promoting metadata to columns and skipping duplicates.

        A chunk whose ``(_DEDUP_KEY_FIELD, chunk_hash)`` signature already
        exists in the collection is skipped rather than re-inserted; an id
        collision on a new chunk upserts the row.

        Args:
            texts: Document text chunks.
            embeddings: Pre-computed embedding vectors.
            metadatas: Per-chunk metadata dicts; the dedup key and the
                promoted columns are read from here.
            ids: Optional explicit ids; UUIDs are generated for any ``None``.

        Returns:
            The ids of every input chunk (skipped ones included).

        Raises:
            ValueError: If the collection does not exist.
        """
        key_field = self._DEDUP_KEY_FIELD
        if ids is None:
            ids_ = [str(uuid.uuid4()) for _ in texts]
        else:
            ids_ = [id_ if id_ is not None else str(uuid.uuid4()) for id_ in ids]
        if not metadatas:
            metadatas = [{} for _ in texts]

        with self._make_sync_session() as session:
            collection = self.get_collection(session)
            if not collection:
                raise ValueError("Collection not found")

            input_records = [
                {
                    "text": text,
                    "metadata": metadata or {},
                    "embedding": embedding,
                    "id": id_,
                    "key": (metadata or {}).get(key_field),
                    "chunk_hash": (metadata or {}).get("chunk_hash"),
                }
                for text, metadata, embedding, id_ in zip(texts, metadatas, embeddings, ids_)
            ]

            # Existing signatures for every key value involved, in one query.
            existing_signatures: set[tuple[Any, str]] = set()
            involved_keys = {r["key"] for r in input_records if r["key"] and r["chunk_hash"]}
            if involved_keys:
                key_column = getattr(self.EmbeddingStore, key_field)
                stmt_exist = select(key_column, self.EmbeddingStore.chunk_hash).where(
                    self.EmbeddingStore.collection_id == collection.uuid,
                    key_column.in_(involved_keys),
                )
                for row in session.execute(stmt_exist).all():
                    if row.chunk_hash:
                        existing_signatures.add((row[0], row.chunk_hash))

            data = []
            skipped_count = 0
            for r in input_records:
                if (
                    r["key"]
                    and r["chunk_hash"]
                    and (r["key"], r["chunk_hash"]) in existing_signatures
                ):
                    skipped_count += 1
                    continue
                md = r["metadata"]
                record = {
                    "id": r["id"],
                    "collection_id": collection.uuid,
                    "embedding": r["embedding"],
                    "document": r["text"],
                    "cmetadata": md,
                    key_field: r["key"],
                }
                record.update({column: md.get(column) for column in self._PROMOTED_COLUMNS})
                data.append(record)

            if skipped_count > 0:
                logger.info(
                    "Skipping %d existing chunks based on (%s, chunk_hash).",
                    skipped_count, key_field,
                )

            if data:
                stmt = insert(self.EmbeddingStore).values(data)
                refreshed = ["embedding", "document", "cmetadata", key_field, *self._PROMOTED_COLUMNS]
                on_conflict_stmt = stmt.on_conflict_do_update(
                    index_elements=["id"],
                    set_={column: getattr(stmt.excluded, column) for column in refreshed},
                )
                session.execute(on_conflict_stmt)

            session.commit()

        return ids_

    def _results_to_docs_and_scores(self, results: Any) -> List[Tuple[Document, float]]:
        """Return docs and scores, with the promoted columns copied into metadata.

        Args:
            results: Raw query results from the vector store.

        Returns:
            List of ``(Document, score)`` tuples.
        """
        store_name = self.EmbeddingStore.__name__
        docs = []
        for result in results:
            store = getattr(result, store_name)
            metadata = store.cmetadata or {}
            metadata["collection_id"] = str(store.collection_id)
            for column in (self._DEDUP_KEY_FIELD, *self._PROMOTED_COLUMNS):
                metadata[column] = getattr(store, column)
            doc = Document(
                id=str(store.id),
                page_content=store.document,
                metadata=metadata,
            )
            score = result.distance if self.embeddings is not None else None
            docs.append((doc, score))
        return docs

    def _handle_field_filter(self, field: str, value: Any) -> Any:
        """Filter on a promoted column directly, else fall back to JSONB.

        Args:
            field: Metadata field name.
            value: Scalar value or an operator dict (``{"$eq": v}`` /
                ``{"$in": [...]}``); other operators fall back to the default.

        Returns:
            SQLAlchemy filter expression.
        """
        if field in self._FILTER_COLUMNS and hasattr(self.EmbeddingStore, field):
            col_attr = getattr(self.EmbeddingStore, field)
            if isinstance(value, dict):
                op, val = list(value.items())[0]
                if op == "$eq":
                    return col_attr == val
                if op == "$in":
                    return col_attr.in_(val)
            else:
                return col_attr == value
        return super()._handle_field_filter(field, value)


class CelexPGVectorPgQwen(CelexPGVector):
    """
    ``CelexPGVector`` backed by the ``pg_embedding_qwen`` table.

    Use this store for Qwen3/Ollama embeddings.  The ``pg_embedding_qwen``
    table has no FK to ``langchain_pg_collection``, but the ``collection_id``
    column is retained for schema compatibility.
    """

    def __post_init__(self) -> None:
        """Initialise the store using the pg_embedding_qwen table."""
        if self.create_extension:
            self.create_vector_extension()

        EmbeddingStore, CollectionStore = _get_pg_embedding_qwen_store(
            self._embedding_length
        )
        self.CollectionStore = CollectionStore
        self.EmbeddingStore = EmbeddingStore
        self.create_tables_if_not_exists()
        self.create_collection()


class CelexPGVectorPgBookQwen3(CelexPGVector):
    """
    ``CelexPGVector`` backed by the ``pg_embedding_book_qwen`` table.

    Use this store for book-section embeddings produced by
    ``BookMarkdownIngestor``.  Deduplication is keyed on ``(book_name,
    chunk_hash)`` instead of ``(celex_id, chunk_hash)``.
    """

    def __post_init__(self) -> None:
        """Initialise the store using the pg_embedding_book_qwen table."""
        if self.create_extension:
            self.create_vector_extension()

        EmbeddingStore, CollectionStore = _get_pg_embedding_book_qwen_store(
            self._embedding_length
        )
        self.CollectionStore = CollectionStore
        self.EmbeddingStore = EmbeddingStore
        self.create_tables_if_not_exists()
        self.create_collection()

    _DEDUP_KEY_FIELD = "book_name"
    _PROMOTED_COLUMNS = (
        "doc_type", "token_count", "path", "level", "title", "text_line",
        "source", "chunk_hash", "doc_pos",
    )
    _FILTER_COLUMNS = frozenset({
        "book_name", "doc_type", "token_count", "path", "level", "title",
        "text_line", "source", "chunk_hash", "doc_pos",
    })
