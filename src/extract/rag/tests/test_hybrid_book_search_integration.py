"""Integration tests for HybridBookSearchToolkit against the live database."""

import unittest

from extract.rag.hybrid_book_search import HybridBookSearchToolkit
from extract.rag.models.chunk_vo import BookChapterVo
from tools.celex_tools import CelexDatabaseToolkit


class TestHybridBookSearchToolkitIntegration(unittest.TestCase):
    """Live-database integration tests for HybridBookSearchToolkit.search()."""

    @classmethod
    def setUpClass(cls) -> None:
        """Initialise shared toolkit once for the test class."""
        tools = CelexDatabaseToolkit()
        cls.toolkit = HybridBookSearchToolkit(tools=tools)

    def test_search_comp_case_reference_returns_results(self) -> None:
        """search('COMP/E-2/37.784') returns at least one BookChapterVo."""
        results = self.toolkit.search_keyword(keyword="COMP/E-2/37.784")

        self.assertGreater(len(results), 0, "Expected non-empty results for COMP/E-2/37.784")

    def test_search_comp_case_reference_returns_book_chapter_vos(self) -> None:
        """Every result is a well-formed BookChapterVo with non-empty fields."""
        results = self.toolkit.search_keyword(keyword="COMP/E-2/37.784")

        for row in results:
            self.assertIsInstance(row, BookChapterVo)
            self.assertTrue(row.chapter_title, "chapter_title must not be empty")
            self.assertTrue(row.document, "document must not be empty")
            self.assertTrue(row.path, "path must not be empty")

    def test_search_comp_case_reference_mentions_case_id(self) -> None:
        """The aggregated chapter text must contain the queried case reference."""
        results = self.toolkit.search_keyword("COMP/E-2/37.784")

        full_text = " ".join(row.document for row in results)
        self.assertIn(
            "37.784",
            full_text,
            "Expected case reference '37.784' to appear in the returned chapter text",
        )


class TestHybridBookSearchToolkitSearchKeywordIntegration(unittest.TestCase):
    """Live-database integration tests for HybridBookSearchToolkit.search_keyword()."""

    @classmethod
    def setUpClass(cls) -> None:
        """Initialise shared toolkit once for the test class."""
        tools = CelexDatabaseToolkit()
        cls.toolkit = HybridBookSearchToolkit(tools=tools)

    def test_search_keyword_returns_results(self) -> None:
        # .venv/bin/python -m unittest extract.rag.tests.test_hybrid_book_search_integration.TestHybridBookSearchToolkitSearchKeywordIntegration.test_search_keyword_returns_results
        """search_keyword('CELEX-31975D0781') returns at least one BookChapterVo."""
        results = self.toolkit.search_keyword('IV/256')

        self.assertGreater(len(results), 0, "Expected non-empty results for CELEX-31975D0781")

    def test_search_keyword_returns_book_chapter_vos(self) -> None:
        """Every result is a well-formed BookChapterVo with non-empty fields."""
        results = self.toolkit.search_keyword("IV/256")

        for row in results:
            self.assertIsInstance(row, BookChapterVo)
            self.assertTrue(row.chapter_title, "chapter_title must not be empty")
            self.assertTrue(row.document, "document must not be empty")
            self.assertTrue(row.path, "path must not be empty")

    def test_search_keyword_chapter_title_trade_associations(self) -> None:
        """search_keyword('IV/256') resolves to the '2.6 Trade associations' chapter."""
        results = self.toolkit.search_keyword("IV/256") #-- CELEX-31975D0781

        self.assertGreater(len(results), 0, "Expected non-empty results for IV/256")
        self.assertIn(
            "2.6 Trade associations",
            results[0].chapter_title,
            f"Expected chapter_title to contain '2.6 Trade associations', got: {results[0].chapter_title!r}",
        )


if __name__ == "__main__":
    unittest.main()
