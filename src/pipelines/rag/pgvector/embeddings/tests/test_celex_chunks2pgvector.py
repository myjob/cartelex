"""
Unit tests for ``CelexMarkdownIngestor`` document lineage (no live DB).

Run from ``src/``:
    ../.venv/bin/python -m unittest pipelines.rag.pgvector.embeddings.tests.test_celex_chunks2pgvector
"""
import unittest
from unittest.mock import MagicMock

from langchain_core.documents import Document

from pipelines.rag.pgvector.embeddings.celex_chunks2pgvector import CelexMarkdownIngestor


def _ingestor() -> CelexMarkdownIngestor:
    """An ingestor with a mocked store and splitter; verification is a no-op."""
    ingestor = CelexMarkdownIngestor.__new__(CelexMarkdownIngestor)
    ingestor.vector_store = MagicMock()
    chunks = [Document(page_content="alpha", metadata={"token_count": 1}),
              Document(page_content="beta", metadata={"token_count": 1})]
    ingestor._split_markdown = lambda md_file: (chunks, 2)
    ingestor._verify_load_by_token_count = lambda key, total: None
    ingestor._verify_load_by_chunk_count = lambda key, count: None
    return ingestor


class TestIngestDocDocumentId(unittest.TestCase):
    """``ingest_doc`` hands the source document id to the store per chunk."""

    def test_ingest_doc_passes_document_id(self) -> None:
        ingestor = _ingestor()
        ingestor.ingest_doc("CELEX-31990D0645", "/x.md", document_id="CELEX-31990D0645_EN")
        (docs,), _ = ingestor.vector_store.add_documents.call_args
        self.assertEqual(
            [d.metadata["document_id"] for d in docs],
            ["CELEX-31990D0645_EN", "CELEX-31990D0645_EN"],
        )

    def test_ingest_doc_defaults_to_null_document_id(self) -> None:
        ingestor = _ingestor()
        ingestor.ingest_doc("CELEX-31990D0645", "/x.md")
        (docs,), _ = ingestor.vector_store.add_documents.call_args
        self.assertEqual({d.metadata["document_id"] for d in docs}, {None})


class TestIngestDocPosition(unittest.TestCase):
    """``ingest_doc`` records each chunk's position for the ``doc_pos`` column."""

    def test_ingest_doc_sets_doc_pos_in_split_order(self) -> None:
        ingestor = _ingestor()
        ingestor.ingest_doc("CELEX-31990D0645", "/x.md")
        (docs,), _ = ingestor.vector_store.add_documents.call_args
        self.assertEqual([d.metadata["doc_pos"] for d in docs], [0, 1])


if __name__ == "__main__":
    unittest.main()
