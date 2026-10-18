"""Unit tests for HybridBookSearch."""

import unittest
from unittest.mock import MagicMock, patch

from extract.rag.hybrid_book_search import HybridBookSearch, HybridBookSearchToolkit
from extract.rag.models.chunk_vo import BookChapterVo, ChunkVo, RankedChunkVo


class TestDeduplicate(unittest.TestCase):
    """Tests for the static _deduplicate method."""

    def test_deduplicates_across_lists(self):
        """Shared IDs across keyword and semantic lists are merged."""
        list_a = [
            ChunkVo(id="a1", celex_id="TEST", chunk="chunk a1", origin="sql_keyword", similarity=0.5, doc_pos=0),
            ChunkVo(id="a2", celex_id="TEST", chunk="chunk a2", origin="sql_keyword", similarity=0.5, doc_pos=1),
        ]
        list_b = [
            ChunkVo(id="a1", celex_id="TEST", chunk="chunk a1 dup", origin="pgvector_semantic", similarity=0.5, doc_pos=0),
            ChunkVo(id="b1", celex_id="TEST", chunk="chunk b1", origin="pgvector_semantic", similarity=0.5, doc_pos=2),
        ]

        result = HybridBookSearch._deduplicate(list_a, list_b)

        self.assertEqual(len(result), 3)
        ids = [c.id for c in result]
        self.assertEqual(ids, ["a1", "a2", "b1"])

    def test_empty_lists(self):
        """Empty inputs produce an empty result."""
        result = HybridBookSearch._deduplicate([], [])
        self.assertEqual(result, [])


class TestFindBestMatch(unittest.TestCase):
    """Tests for _find_best_match orchestration."""

    def _make_search(self, ranker=None):
        """Create a HybridBookSearch with a mocked engine and embeddings."""
        engine = MagicMock()
        embeddings = MagicMock()
        return HybridBookSearch(engine=engine, embeddings=embeddings, ranker=ranker)

    @patch.object(HybridBookSearch, "_semantic_search")
    @patch.object(HybridBookSearch, "_keyword_search")
    def test_with_ranker_selects_top1(self, mock_kw, mock_sem):
        """When a ranker is provided, the top-ranked chunk ID is returned."""
        mock_kw.return_value = [
            ChunkVo(id="kw-1", celex_id="TEST", chunk="keyword hit", origin="sql_keyword", similarity=0.5, doc_pos=0),
        ]
        mock_sem.return_value = [
            ChunkVo(id="sem-1", celex_id="TEST", chunk="semantic hit", origin="pgvector_semantic", similarity=0.5, doc_pos=1),
        ]

        mock_ranker = MagicMock()
        mock_ranker.rank_chunkvo.return_value = [
            RankedChunkVo(id="kw-1", celex_id="TEST", chunk="keyword hit", origin="sql_keyword", similarity=0.5, doc_pos=0, score=0.95, base_score=0.9),
        ]

        search = self._make_search(ranker=mock_ranker)
        best_id = search._find_best_match(query="cartel fines overview", keyword="fine")

        self.assertEqual(best_id, "kw-1")
        mock_ranker.rank_chunkvo.assert_called_once()
        call_args = mock_ranker.rank_chunkvo.call_args
        self.assertEqual(call_args.kwargs["top_k"], 1)

    @patch.object(HybridBookSearch, "_semantic_search")
    @patch.object(HybridBookSearch, "_keyword_search")
    def test_no_ranker_uses_first_semantic(self, mock_kw, mock_sem):
        """Without a ranker, the first semantic hit is selected."""
        mock_kw.return_value = [
            ChunkVo(id="kw-1", chunk="keyword hit", origin="sql_keyword", similarity=0.5, doc_pos=0),
        ]
        mock_sem.return_value = [
            ChunkVo(id="sem-1", chunk="semantic hit", origin="pgvector_semantic", similarity=0.5, doc_pos=1),
            ChunkVo(id="sem-2", chunk="second semantic", origin="pgvector_semantic", similarity=0.5, doc_pos=2),
        ]

        search = self._make_search(ranker=None)
        best_id = search._find_best_match(query="cartel fines overview", keyword="fine")

        self.assertEqual(best_id, "sem-1")

    @patch.object(HybridBookSearch, "_semantic_search")
    @patch.object(HybridBookSearch, "_keyword_search")
    def test_no_results_returns_none(self, mock_kw, mock_sem):
        """When both searches return empty, None is returned."""
        mock_kw.return_value = []
        mock_sem.return_value = []

        search = self._make_search()
        best_id = search._find_best_match(query="nonexistent topic", keyword="zzzzz")

        self.assertIsNone(best_id)

    @patch.object(HybridBookSearch, "_semantic_search")
    @patch.object(HybridBookSearch, "_keyword_search")
    def test_single_word_query_skips_keyword(self, mock_kw, mock_sem):
        """A single unsplittable word skips the keyword search."""
        mock_sem.return_value = [
            ChunkVo(id="sem-1", chunk="semantic hit", origin="pgvector_semantic", similarity=0.5, doc_pos=0),
        ]

        search = self._make_search()
        search._find_best_match(query="cartel", keyword="cartel")

        mock_kw.assert_not_called()


class TestFetchChapterText(unittest.TestCase):
    """Tests for _fetch_chapter_text row mapping (delegated to BookChunkRepository)."""

    def test_maps_rows_to_book_chapter_vo(self):
        """DB rows are correctly mapped to BookChapterVo instances."""
        expected = [
            BookChapterVo(chapter_title="Chapter Title", sub_chapter_title="Sub Ch.", level=3,
                          document="First paragraph text.", path="Root -> Part I -> Ch. 1",
                          token_count=120, doc_pos=0),
            BookChapterVo(chapter_title="Chapter Title", sub_chapter_title="Sub Ch.", level=4,
                          document="Sub-section text.", path="Root -> Part I -> Ch. 1 -> 1.1",
                          token_count=80, doc_pos=1),
        ]

        search = HybridBookSearch(engine=MagicMock(), embeddings=MagicMock())
        search._book_repo = MagicMock()
        search._book_repo.fetch_chapter_text.return_value = expected

        results = search._fetch_chapter_text("some-id")

        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], BookChapterVo)
        self.assertEqual(results[0].chapter_title, "Chapter Title")
        self.assertEqual(results[0].level, 3)
        self.assertEqual(results[0].document, "First paragraph text.")
        self.assertEqual(results[0].path, "Root -> Part I -> Ch. 1")
        self.assertEqual(results[0].token_count, 120)
        self.assertEqual(results[1].level, 4)
        self.assertEqual(results[1].token_count, 80)

    def test_handles_null_token_count(self):
        """Null token_count in DB is mapped to None."""
        expected = [
            BookChapterVo(chapter_title="Title", sub_chapter_title="Sub Ch.", level=2,
                          document="Text.", path="Root -> Ch",
                          token_count=None, doc_pos=0),
        ]

        search = HybridBookSearch(engine=MagicMock(), embeddings=MagicMock())
        search._book_repo = MagicMock()
        search._book_repo.fetch_chapter_text.return_value = expected

        results = search._fetch_chapter_text("id-1")

        self.assertIsNone(results[0].token_count)


class TestSearchPublicMethod(unittest.TestCase):
    """Tests for the public search() method."""

    @patch.object(HybridBookSearch, "_fetch_chapter_text")
    @patch.object(HybridBookSearch, "_find_best_match")
    def test_search_returns_chapter_rows(self, mock_best, mock_chapter):
        """search() chains _find_best_match and _fetch_chapter_text."""
        mock_best.return_value = "matched-id"
        mock_chapter.return_value = [
            BookChapterVo(chapter_title="Ch1",sub_chapter_title="Sub Ch1", level=3, document="text", path="A -> B -> C", token_count=50),
        ]

        search = HybridBookSearch(engine=MagicMock(), embeddings=MagicMock())
        results = search.search(query="cartel fines", keyword="fine")

        self.assertEqual(len(results), 1)
        mock_chapter.assert_called_once_with("matched-id")

    @patch.object(HybridBookSearch, "_fetch_chapter_text")
    @patch.object(HybridBookSearch, "_find_best_match")
    def test_search_no_match_returns_empty(self, mock_best, mock_chapter):
        """search() returns empty list when no match is found."""
        mock_best.return_value = None

        search = HybridBookSearch(engine=MagicMock(), embeddings=MagicMock())
        results = search.search(query="nonexistent", keyword="zzz")

        self.assertEqual(results, [])
        mock_chapter.assert_not_called()


class TestHybridBookSearchToolkitSearch(unittest.TestCase):
    """Tests for HybridBookSearchToolkit.search()."""

    def _make_toolkit(self) -> HybridBookSearchToolkit:
        """Create a HybridBookSearchToolkit with mocked engine and embeddings."""
        mock_tools = MagicMock()
        mock_tools.engine = MagicMock()
        mock_tools.embeddings = MagicMock()
        return HybridBookSearchToolkit(tools=mock_tools)

    @patch.object(HybridBookSearch, "search")
    def test_forwards_the_keyword(self, mock_search: MagicMock) -> None:
        """search() passes the caller's keyword through (it used to hardcode '')."""
        mock_search.return_value = [
            BookChapterVo(chapter_title="Ch1", sub_chapter_title="Sub Ch1", level=2, document="text", path="A -> B -> C", token_count=42),
        ]
        toolkit = self._make_toolkit()

        toolkit.search("cartel fines overview", keyword="fine")

        mock_search.assert_called_once_with(query="cartel fines overview", keyword="fine")

    @patch.object(HybridBookSearch, "search")
    def test_default_keyword_is_empty(self, mock_search: MagicMock) -> None:
        """Without a keyword the search stays semantic-only."""
        mock_search.return_value = []
        toolkit = self._make_toolkit()

        toolkit.search("cartel fines overview")

        mock_search.assert_called_once_with(query="cartel fines overview", keyword="")

    @patch.object(HybridBookSearch, "search")
    def test_returns_chapter_vos_from_engine(self, mock_search: MagicMock) -> None:
        """search() propagates the BookChapterVo list returned by the engine."""
        expected = [
            BookChapterVo(chapter_title="Ch1", sub_chapter_title="Sub1", level=1, document="Para 1", path="Root -> Ch1", token_count=10),
            BookChapterVo(chapter_title="Ch1", sub_chapter_title="Sub1", level=2, document="Para 2", path="Root -> Ch1 -> 1.1", token_count=5),
        ]
        mock_search.return_value = expected
        toolkit = self._make_toolkit()

        results = toolkit.search("market sharing agreement")

        self.assertEqual(results, expected)

    @patch.object(HybridBookSearch, "search")
    def test_returns_empty_list_when_no_match(self, mock_search: MagicMock) -> None:
        """search() returns an empty list when the engine finds nothing."""
        mock_search.return_value = []
        toolkit = self._make_toolkit()

        results = toolkit.search("topic with no match")

        self.assertEqual(results, [])
        mock_search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
