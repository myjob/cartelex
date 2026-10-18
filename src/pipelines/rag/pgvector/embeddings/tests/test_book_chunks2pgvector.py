"""Unit tests for BookMarkdownIngestor in book_chunks2pgvector.

Mocks are applied only at system boundaries:
  - The pgvector store (_setup_pgvector / add_documents)
  - The Embeddings provider
No real database or embedding calls are made.
"""

import hashlib
import os
import tempfile
import unittest
from typing import cast
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document

from config.settings import settings
from pipelines.rag.pgvector.embeddings.book_tree import BookNode
from pipelines.rag.pgvector.embeddings.book_chunks2pgvector import BookMarkdownIngestor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODULE = "pipelines.rag.pgvector.embeddings.book_chunks2pgvector"


def _make_ingestor() -> BookMarkdownIngestor:
    """Return a BookMarkdownIngestor with its pgvector store mocked out."""
    mock_store = MagicMock()
    with patch.object(BookMarkdownIngestor, "_setup_pgvector", return_value=mock_store):
        ingestor = BookMarkdownIngestor(embeddings=MagicMock())
    ingestor.vector_store = mock_store
    return ingestor


def _write_temp_md(content: str) -> str:
    """Write *content* to a named temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        os.unlink(path)
        raise
    return path


def _simple_tree() -> BookNode:
    """Build a two-level BookNode tree for testing _persist_md_chapters."""
    section = BookNode(
        title="2.1 Price fixing",
        level=2,
        path="Infringements -> 2 Horizontal restrictions -> 2.1 Price fixing",
        text_line=10,
        raw_text="2.1 Price fixing\nSome body text about price fixing.",
        token_count=12,
    )
    chapter = BookNode(
        title="2 Horizontal restrictions",
        level=1,
        path="Infringements -> 2 Horizontal restrictions",
        text_line=5,
        raw_text="2 Horizontal restrictions\nChapter two body text.",
        token_count=20,
        children=[section],
    )
    return BookNode(
        title="Infringements",
        level=0,
        path="Infringements",
        children=[chapter],
    )


def _empty_tree() -> BookNode:
    """Build a root-only BookNode tree (no children)."""
    return BookNode(title="Infringements", level=0, path="Infringements")


# ---------------------------------------------------------------------------
# _split_markdown
# ---------------------------------------------------------------------------


class TestSplitMarkdown(unittest.TestCase):
    """Tests for BookMarkdownIngestor._split_markdown."""

    def setUp(self) -> None:
        self.ingestor = _make_ingestor()

    def test_missing_file_returns_empty_list_and_zero_tokens(self) -> None:
        """A non-existent file path must return ([], 0) without raising."""
        chunks, tokens = self.ingestor._split_markdown("/nonexistent/path/book.md")
        self.assertEqual(chunks, [])
        self.assertEqual(tokens, 0)

    def test_returns_list_of_documents_for_valid_file(self) -> None:
        """A real markdown file must produce at least one Document chunk."""
        content = (
            "# 2 Horizontal restrictions\n\nChapter body text with enough words.\n"
            "# 2.1 Price fixing\n\nSection body text.\n"
        )
        path = _write_temp_md(content)
        try:
            chunks, _ = self.ingestor._split_markdown(path)
            self.assertIsInstance(chunks, list)
        finally:
            os.unlink(path)

    def test_token_count_is_positive_for_nonempty_file(self) -> None:
        """doc_total_tokens must be > 0 when the file contains text."""
        content = "# Heading\n\nSome paragraph with several words to ensure tokens are counted.\n"
        path = _write_temp_md(content)
        try:
            _, tokens = self.ingestor._split_markdown(path)
            self.assertGreater(tokens, 0)
        finally:
            os.unlink(path)

    def test_empty_file_returns_zero_tokens(self) -> None:
        """An empty markdown file must produce zero tokens."""
        path = _write_temp_md("")
        try:
            _, tokens = self.ingestor._split_markdown(path)
            self.assertEqual(tokens, 0)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# _persist_md_chapters
# ---------------------------------------------------------------------------


class TestPersistMdChapters(unittest.TestCase):
    """Tests for BookMarkdownIngestor._persist_md_chapters."""

    def setUp(self) -> None:
        self.ingestor = _make_ingestor()
        self.mock_store: MagicMock = cast(MagicMock, self.ingestor.vector_store)

    # --- add_documents call ----------------------------------------------------

    def test_add_documents_called_once(self) -> None:
        """vector_store.add_documents must be called exactly once per ingest."""
        self.ingestor._persist_md_chapters("Book", "/f.md", _simple_tree(), 100)
        self.mock_store.add_documents.assert_called_once()

    def test_returns_integer_chunk_token_sum(self) -> None:
        """Return value must be a positive integer (sum of node token counts)."""
        result = self.ingestor._persist_md_chapters(
            "Book", "/f.md", _simple_tree(), 100
        )
        self.assertIsInstance(result, int)
        self.assertGreater(result, 0)

    def test_chunk_token_sum_equals_node_token_count_sum(self) -> None:
        """Return value must equal the sum of token_count across all non-root nodes."""
        tree = _simple_tree()
        # Flatten expected: chapter (level 1) + section (level 2)
        expected = (
            tree.children[0].token_count + tree.children[0].children[0].token_count
        )
        result = self.ingestor._persist_md_chapters("Book", "/f.md", tree, 100)
        self.assertEqual(result, expected)

    # --- Document count and root exclusion ------------------------------------

    def test_root_node_excluded_from_documents(self) -> None:
        """The virtual root node (level=0) must never appear as a Document."""
        self.ingestor._persist_md_chapters("Book", "/f.md", _simple_tree(), 100)
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]
        for doc in docs:
            self.assertNotEqual(doc.metadata["level"], 0)

    def test_document_count_matches_non_root_nodes(self) -> None:
        """Number of Documents must equal the total count of non-root tree nodes."""
        tree = _simple_tree()
        # tree has: root (level 0), chapter (level 1), section (level 2) → 2 non-root
        self.ingestor._persist_md_chapters("Book", "/f.md", tree, 100)
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]
        self.assertEqual(len(docs), 2)

    # --- Metadata correctness -------------------------------------------------

    def test_required_metadata_fields_present(self) -> None:
        """Every Document must carry the expected metadata keys."""
        self.ingestor._persist_md_chapters("Book", "/f.md", _simple_tree(), 100)
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]
        required_keys = {
            "id",
            "book_name",
            "doc_type",
            "path",
            "level",
            "title",
            "text_line",
            "token_count",
            "source",
            "chunk_hash",
        }
        for doc in docs:
            self.assertTrue(
                required_keys.issubset(doc.metadata.keys()),
                f"Missing keys: {required_keys - doc.metadata.keys()}",
            )

    def test_book_name_propagated_to_all_documents(self) -> None:
        """book_name in every Document must match the argument passed."""
        self.ingestor._persist_md_chapters("MyBook", "/f.md", _simple_tree(), 100)
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]
        for doc in docs:
            self.assertEqual(doc.metadata["book_name"], "MyBook")

    def test_doc_type_is_book_section(self) -> None:
        """doc_type must be 'book_section' for every Document."""
        self.ingestor._persist_md_chapters("Book", "/f.md", _simple_tree(), 100)
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]
        for doc in docs:
            self.assertEqual(doc.metadata["doc_type"], "book_section")

    def test_source_metadata_matches_md_file_argument(self) -> None:
        """source metadata must equal the md_file argument."""
        self.ingestor._persist_md_chapters(
            "Book", "/path/to/book.md", _simple_tree(), 100
        )
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]
        for doc in docs:
            self.assertEqual(doc.metadata["source"], "/path/to/book.md")

    def test_chunk_hash_is_md5_of_page_content(self) -> None:
        """chunk_hash must be the MD5 hex digest of the Document's page_content."""
        self.ingestor._persist_md_chapters("Book", "/f.md", _simple_tree(), 100)
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]
        for doc in docs:
            expected = hashlib.md5(doc.page_content.encode("utf-8")).hexdigest()
            self.assertEqual(doc.metadata["chunk_hash"], expected)

    def test_page_content_matches_node_raw_text(self) -> None:
        """page_content of each Document must equal the source node's raw_text."""
        tree = _simple_tree()
        chapter = tree.children[0]
        section = chapter.children[0]

        self.ingestor._persist_md_chapters("Book", "/f.md", tree, 100)
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]
        page_contents = {doc.page_content for doc in docs}

        self.assertIn(chapter.raw_text, page_contents)
        self.assertIn(section.raw_text, page_contents)

    def test_level_metadata_matches_node_level(self) -> None:
        """level metadata value must match the BookNode's level attribute."""
        tree = _simple_tree()
        self.ingestor._persist_md_chapters("Book", "/f.md", tree, 100)
        docs: list[Document] = self.mock_store.add_documents.call_args[0][0]

        levels = {doc.metadata["level"] for doc in docs}
        self.assertIn(1, levels)
        self.assertIn(2, levels)


# ---------------------------------------------------------------------------
# ingest_book
# ---------------------------------------------------------------------------


class TestIngestBook(unittest.TestCase):
    """Tests for BookMarkdownIngestor.ingest_book."""

    def setUp(self) -> None:
        self.ingestor = _make_ingestor()

    @patch(f"{_MODULE}.InfringementTreeBuilder")
    def test_persist_called_when_tree_has_children(
        self, mock_builder_cls: MagicMock
    ) -> None:
        """_persist_md_chapters must be called when the tree has at least one child."""
        mock_builder_cls.return_value.build.return_value = _simple_tree()

        with patch.object(self.ingestor, "_split_markdown", return_value=([], 100)):
            with patch.object(
                self.ingestor, "_persist_md_chapters", return_value=32
            ) as mock_persist:
                with patch.object(self.ingestor, "_verify_load_by_token_count"):
                    self.ingestor.ingest_book("TestBook", "/some/file.md")
                    mock_persist.assert_called_once()

    @patch(f"{_MODULE}.InfringementTreeBuilder")
    def test_persist_skipped_when_tree_has_no_children(
        self, mock_builder_cls: MagicMock
    ) -> None:
        """_persist_md_chapters must NOT be called when the tree is empty."""
        mock_builder_cls.return_value.build.return_value = _empty_tree()

        with patch.object(self.ingestor, "_split_markdown", return_value=([], 0)):
            with patch.object(self.ingestor, "_persist_md_chapters") as mock_persist:
                with patch.object(self.ingestor, "_verify_load_by_token_count"):
                    self.ingestor.ingest_book("TestBook", "/some/file.md")
                    mock_persist.assert_not_called()

    @patch(f"{_MODULE}.InfringementTreeBuilder")
    def test_persist_receives_correct_book_name(
        self, mock_builder_cls: MagicMock
    ) -> None:
        """book_name passed to ingest_book must be forwarded to _persist_md_chapters."""
        mock_builder_cls.return_value.build.return_value = _simple_tree()

        with patch.object(self.ingestor, "_split_markdown", return_value=([], 50)):
            with patch.object(
                self.ingestor, "_persist_md_chapters", return_value=32
            ) as mock_persist:
                with patch.object(self.ingestor, "_verify_load_by_token_count"):
                    self.ingestor.ingest_book("SpecialBook", "/some/file.md")
                    call_book_name = mock_persist.call_args[0][0]
                    self.assertEqual(call_book_name, "SpecialBook")

    @patch(f"{_MODULE}.InfringementTreeBuilder")
    def test_persist_receives_correct_md_file(
        self, mock_builder_cls: MagicMock
    ) -> None:
        """md_file passed to ingest_book must be forwarded to _persist_md_chapters."""
        mock_builder_cls.return_value.build.return_value = _simple_tree()

        with patch.object(self.ingestor, "_split_markdown", return_value=([], 50)):
            with patch.object(
                self.ingestor, "_persist_md_chapters", return_value=32
            ) as mock_persist:
                with patch.object(self.ingestor, "_verify_load_by_token_count"):
                    self.ingestor.ingest_book("Book", "/exact/path/book.md")
                    call_md_file = mock_persist.call_args[0][1]
                    self.assertEqual(call_md_file, "/exact/path/book.md")

    @patch(f"{_MODULE}.InfringementTreeBuilder")
    def test_split_markdown_called_with_md_file(
        self, mock_builder_cls: MagicMock
    ) -> None:
        """_split_markdown must be called with the md_file argument."""
        mock_builder_cls.return_value.build.return_value = _simple_tree()

        with patch.object(
            self.ingestor, "_split_markdown", return_value=([], 50)
        ) as mock_split:
            with patch.object(self.ingestor, "_persist_md_chapters", return_value=32):
                with patch.object(self.ingestor, "_verify_load_by_token_count"):
                    self.ingestor.ingest_book("Book", "/target/file.md")
                    mock_split.assert_called_once_with("/target/file.md")


# ---------------------------------------------------------------------------
# Celex Case Markdown Integration
# ---------------------------------------------------------------------------


class TestCelexIntegration(unittest.TestCase):
    """Tests behaviour when given a generic Celex case markdown document."""

    def setUp(self) -> None:
        self.ingestor = _make_ingestor()

    def test_split_markdown_processes_celex_markdown(self) -> None:
        """_split_markdown must successfully chunk a generic Celex markdown document."""
        path = os.path.join(
            settings.CELEX_DOCS_DIR, "32450_35_6/mineru/auto/32450_35_6.md"
        )

        chunks, tokens = self.ingestor._split_markdown(path)
        self.assertGreater(len(chunks), 0)
        self.assertGreater(tokens, 0)
        # Verify the splitter parsed the Headers properly
        metadata_headers = [
            chunk.metadata.get("Header 1")
            for chunk in chunks
            if "Header 1" in chunk.metadata
        ]

        self.assertTrue(
            any(
                "HAS ADOPTED THIS DECISION:" in str(header)
                for header in metadata_headers
            )
        )

    def test_infringement_tree_builder_rejects_celex_markdown(self) -> None:
        """
        InfringementTreeBuilder (and thus ingest_book) is hard-coded for the
        Russo/Schinkel book. A normal Celex markdown file lacking the expected
        start heading will cause a ValueError.
        """
        path = os.path.join(
            settings.CELEX_DOCS_DIR, "32450_35_6/mineru/auto/32450_35_6.md"
        )

        # We do NOT mock InfringementTreeBuilder here, so it executes the real build()
        with self.assertRaisesRegex(
            ValueError, "Start heading '2 Horizontal restrictions' not found"
        ):
            self.ingestor.ingest_book("CelexCase_32450_35_6", path)


if __name__ == "__main__":
    unittest.main()
