"""
Unit tests for ``CelexPGVector.add_embeddings`` document lineage.

The ``pg_embedding_qwen.document_id`` column is added by
``migrate_document_lineage.sql``; the writer must work both before and after
that migration. No live DB: the session is a fake that records statements.

Run from ``src/``:
    ../.venv/bin/python -m unittest pipelines.rag.pgvector.tests.test_celex_pgvector_lineage
"""
import unittest
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import TextClause

from pipelines.rag.pgvector import session as session_module
from pipelines.rag.pgvector.celex_pgvector import (
    CelexPGVectorPgBookQwen3,
    CelexPGVectorPgQwen,
    PgEmbeddingBookQwen3Store,
    PgEmbeddingQwenStore,
)


class _FakeSession:
    """Session stand-in: answers the column probe, records the insert."""

    def __init__(self, column_present: bool, probe_error: Exception | None = None) -> None:
        self.column_present = column_present
        self.probe_error = probe_error
        self.probes = 0
        self.rollbacks = 0
        self.inserts: list[Insert] = []

    def execute(self, statement: object, params: dict | None = None) -> SimpleNamespace:
        if isinstance(statement, TextClause):
            self.probes += 1
            if self.probe_error is not None:
                raise self.probe_error
            row = (1,) if self.column_present else None
            return SimpleNamespace(first=lambda: row)
        if isinstance(statement, Insert):
            self.inserts.append(statement)
            return SimpleNamespace()
        return SimpleNamespace(all=lambda: [])

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        self.rollbacks += 1


def _store(cls: type, store_model: type, fake: _FakeSession) -> object:
    """Build a store without touching the DB; every session is ``fake``."""
    store = cls.__new__(cls)
    store.EmbeddingStore = store_model
    collection = SimpleNamespace(uuid=uuid.uuid4())
    store.get_collection = lambda session: collection

    @contextmanager
    def _session() -> Iterator[_FakeSession]:
        yield fake

    store._make_sync_session = _session
    return store


def _params(insert_stmt: Insert) -> dict:
    """Bound parameters of the compiled multi-row insert."""
    return insert_stmt.compile(dialect=postgresql.dialect()).params


def _meta(document_id: str | None, chunk_hash: str = "h1") -> dict:
    """Chunk metadata as ``CelexMarkdownIngestor`` builds it."""
    return {
        "celex_id": "CELEX-31990D0645",
        "token_count": 3,
        "doc_type": "full_md",
        "path": "/x.md",
        "chunk_hash": chunk_hash,
        "document_id": document_id,
    }


class TestAddEmbeddingsDocumentId(unittest.TestCase):
    """``document_id`` goes to its column, never into ``cmetadata``."""

    def setUp(self) -> None:
        session_module._WARNED_ONCE.clear()

    def test_add_embeddings_writes_document_id_column(self) -> None:
        fake = _FakeSession(column_present=True)
        store = _store(CelexPGVectorPgQwen, PgEmbeddingQwenStore, fake)
        store.add_embeddings(["t"], [[0.1]], metadatas=[_meta("CELEX-31990D0645_EN")])
        (stmt,) = fake.inserts
        params = _params(stmt)
        self.assertEqual(params["document_id_m0"], "CELEX-31990D0645_EN")
        self.assertNotIn("document_id", params["cmetadata_m0"])
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.assertIn("coalesce(excluded.document_id, pg_embedding_qwen.document_id)", sql)

    def test_probe_runs_once_per_store(self) -> None:
        fake = _FakeSession(column_present=True)
        store = _store(CelexPGVectorPgQwen, PgEmbeddingQwenStore, fake)
        store.add_embeddings(["t"], [[0.1]], metadatas=[_meta("D", "h1")])
        store.add_embeddings(["u"], [[0.2]], metadatas=[_meta("D", "h2")])
        self.assertEqual(fake.probes, 1)

    def test_works_without_document_id_column(self) -> None:
        fake = _FakeSession(column_present=False)
        store = _store(CelexPGVectorPgQwen, PgEmbeddingQwenStore, fake)
        with self.assertLogs("pipelines.rag.pgvector.celex_pgvector", "WARNING") as logs:
            store.add_embeddings(["t"], [[0.1]], metadatas=[_meta("D", "h1")])
            store.add_embeddings(["u"], [[0.2]], metadatas=[_meta("D", "h2")])
        self.assertEqual(len(logs.records), 1, "one warning per process")
        self.assertIn("migrate_document_lineage.sql", logs.output[0])
        self.assertEqual(len(fake.inserts), 2, "chunks are still written")
        for stmt in fake.inserts:
            self.assertNotIn("document_id", str(stmt.compile(dialect=postgresql.dialect())))
            self.assertNotIn("document_id", _params(stmt)["cmetadata_m0"])

    def test_probe_error_rolls_back_and_inserts_without_the_column(self) -> None:
        fake = _FakeSession(column_present=True, probe_error=RuntimeError("probe boom"))
        store = _store(CelexPGVectorPgQwen, PgEmbeddingQwenStore, fake)
        with self.assertLogs("pipelines.rag.pgvector.celex_pgvector", "WARNING") as logs:
            store.add_embeddings(["t"], [[0.1]], metadatas=[_meta("D")])
        self.assertEqual(fake.rollbacks, 1)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("lineage probe failed, writing NULL", logs.output[0])
        self.assertIn("probe boom", logs.output[0])
        self.assertNotIn("is missing", logs.output[0])
        (stmt,) = fake.inserts
        self.assertNotIn("document_id", str(stmt.compile(dialect=postgresql.dialect())))

    def test_no_document_id_skips_probe_and_column(self) -> None:
        fake = _FakeSession(column_present=True)
        store = _store(CelexPGVectorPgQwen, PgEmbeddingQwenStore, fake)
        store.add_embeddings(["t"], [[0.1]], metadatas=[_meta(None)])
        self.assertEqual(fake.probes, 0)
        (stmt,) = fake.inserts
        self.assertNotIn("document_id", str(stmt.compile(dialect=postgresql.dialect())))

    def test_store_without_lineage_column_drops_the_key(self) -> None:
        fake = _FakeSession(column_present=True)
        store = _store(CelexPGVectorPgBookQwen3, PgEmbeddingBookQwen3Store, fake)
        meta = {"book_name": "B", "chunk_hash": "h1", "document_id": "D"}
        store.add_embeddings(["t"], [[0.1]], metadatas=[meta])
        self.assertEqual(fake.probes, 0)
        (stmt,) = fake.inserts
        self.assertNotIn("document_id", _params(stmt)["cmetadata_m0"])

    def test_orm_select_does_not_load_document_id(self) -> None:
        """Pre-migration similarity searches select the entity; it must not name the column."""
        self.assertNotIn("document_id", str(select(PgEmbeddingQwenStore)))


if __name__ == "__main__":
    unittest.main()
