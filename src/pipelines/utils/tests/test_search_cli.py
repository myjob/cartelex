import unittest
from unittest.mock import patch
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from extract.rag.ranker.flash_ranker import FlashRanker


def _make_chunk(id_: str, origin: str, similarity: float, doc_pos: int = 0) -> ChunkVo:
    return ChunkVo(id=id_, celex_id="", chunk=f"chunk text for {id_}", origin=origin, similarity=similarity, doc_pos=doc_pos)


def _make_ranked(id_: str, origin: str, similarity: float,  score: float = 0.9) -> RankedChunkVo:
    return RankedChunkVo(id=id_, celex_id="", chunk=f"chunk text for {id_}", origin=origin, similarity=similarity, score=score, base_score=score)



class TestSearchCLI(unittest.TestCase):
    """
    Unit tests covering the documented usage samples.
    --type defaults to hybrid; --celex_id is optional.

      1. hybrid  --celex_id ... --query "fine amount"        (explicit type)
      2. hybrid  --query "fine amount"                       (default type, no celex_id)
      3. semantic --celex_id ... --query "penalty for cartel"
      4. keyword  --celex_id ... --query fine
      5. hybrid  ... --query "fine amount" --top_k 5
    """

    def _run(self, argv: list[str]) -> int:
        """Import-and-run main() with sys.argv patched; return SystemExit code or 0."""
        with patch("sys.argv", ["search_cli.py"] + argv):
            from pipelines.utils.search_cli import main
            try:
                main()
                return 0
            except SystemExit as exc:
                return int(exc.code) if exc.code is not None else 1

    # ------------------------------------------------------------------
    # Sample 1 — hybrid with explicit --type and --celex_id
    # ------------------------------------------------------------------
    @patch("pipelines.utils.search_cli.HybridSearch")
    @patch("pipelines.utils.search_cli.create_ranker")
    @patch("pipelines.utils.search_cli.ChunkRepository")
    @patch("pipelines.utils.search_cli.QueryEmbeddingRepository")
    @patch("pipelines.utils.search_cli.Qwen3OllamaEmbeddings")
    def test_hybrid_search_explicit(self, MockEmb, MockQueryRepo, MockChunkRepo, MockRanker, MockHybrid):
        expected = [_make_ranked("h-1", "sql_keyword", similarity=0.5), _make_ranked("h-2", "pgvector_semantic", similarity=0.5)]
        instance = MockHybrid.return_value
        instance.search.return_value = expected

        rc = self._run([
            "--type", "hybrid",
            "--celex_id", "62013CJ0067",
            "--query", "fine amount",
        ])

        self.assertEqual(rc, 0)
        instance.search.assert_called_once_with(
            celex_id="62013CJ0067",
            query="fine amount",
            top_k=None,
        )

    # ------------------------------------------------------------------
    # Sample 2 — no --celex_id: show matching CELEX IDs with chunk counts
    # ------------------------------------------------------------------
    @patch("pipelines.utils.search_cli.ChunkRepository")
    @patch("pipelines.utils.search_cli.QueryEmbeddingRepository")
    @patch("pipelines.utils.search_cli.Qwen3OllamaEmbeddings")
    def test_no_celex_id_lists_documents(self, MockEmb, MockQueryRepo, MockChunkRepo):
        chunk_instance = MockChunkRepo.return_value
        chunk_instance.select_distinct_celex_for_keyword.return_value = [
            {"celex_id": "62013CJ0067", "numof": 42},
            {"celex_id": "32014R0001", "numof": 17},
        ]

        rc = self._run(["--query", "fine amount"])

        self.assertEqual(rc, 0)
        chunk_instance.select_distinct_celex_for_keyword.assert_called_once_with("fine amount")

    @patch("pipelines.utils.search_cli.ChunkRepository")
    @patch("pipelines.utils.search_cli.QueryEmbeddingRepository")
    @patch("pipelines.utils.search_cli.Qwen3OllamaEmbeddings")
    def test_no_celex_id_no_matches(self, MockEmb, MockQueryRepo, MockChunkRepo):
        chunk_instance = MockChunkRepo.return_value
        chunk_instance.select_distinct_celex_for_keyword.return_value = []

        rc = self._run(["--query", "fine amount"])

        self.assertEqual(rc, 0)

    # ------------------------------------------------------------------
    # Sample 5 — hybrid with --top_k
    # ------------------------------------------------------------------
    @patch("pipelines.utils.search_cli.HybridSearch")
    @patch("pipelines.utils.search_cli.create_ranker")
    @patch("pipelines.utils.search_cli.ChunkRepository")
    @patch("pipelines.utils.search_cli.QueryEmbeddingRepository")
    @patch("pipelines.utils.search_cli.Qwen3OllamaEmbeddings")
    def test_hybrid_search_with_top_k(self, MockEmb, MockQueryRepo, MockChunkRepo, MockRanker, MockHybrid):
        expected = [_make_ranked(f"h-{i}", "sql_keyword", similarity=-1.0) for i in range(5)]
        instance = MockHybrid.return_value
        instance.search.return_value = expected

        rc = self._run([
            "--celex_id", "62013CJ0067",
            "--query", "fine amount",
            "--top_k", "5",
        ])

        self.assertEqual(rc, 0)
        instance.search.assert_called_once_with(
            celex_id="62013CJ0067",
            query="fine amount",
            top_k=5,
        )

    # ------------------------------------------------------------------
    # Sample 2 — semantic search
    # ------------------------------------------------------------------
    @patch("pipelines.utils.search_cli.create_ranker")
    @patch("pipelines.utils.search_cli.ChunkRepository")
    @patch("pipelines.utils.search_cli.QueryEmbeddingRepository")
    @patch("pipelines.utils.search_cli.Qwen3OllamaEmbeddings")
    def test_semantic_search(self, MockEmb, MockQueryRepo, MockChunkRepo, MockRanker):
        raw_chunks = [_make_chunk("v-1", "pgvector_semantic", similarity=0.5)]
        ranked_chunks = [_make_ranked("v-1", "pgvector_semantic", similarity=0.5)]

        # embed_fn returns the cached embedding; make it deterministic so we can
        # assert the vector that gets forwarded to the repo.
        query_repo_instance = MockQueryRepo.return_value
        query_repo_instance.get_or_cache.return_value = [0.1, 0.2, 0.3]

        chunk_instance = MockChunkRepo.return_value
        chunk_instance.semantic_search_in_celex.return_value = raw_chunks

        ranker_instance = MockRanker.return_value
        ranker_instance.rank_chunkvo.return_value = ranked_chunks

        rc = self._run([
            "--type", "semantic",
            "--celex_id", "62013CJ0067",
            "--query", "penalty for cartel",
        ])

        self.assertEqual(rc, 0)
        chunk_instance.semantic_search_in_celex.assert_called_once_with(
            celex_id="62013CJ0067",
            query_embedding=[0.1, 0.2, 0.3],
            hits=40,
        )
        ranker_instance.rank_chunkvo.assert_called_once_with("penalty for cartel", raw_chunks, top_k=None)

    # ------------------------------------------------------------------
    # Sample 3 — keyword search
    # ------------------------------------------------------------------
    @patch("pipelines.utils.search_cli.KeywordSearch")
    @patch("pipelines.utils.search_cli.ChunkRepository")
    @patch("pipelines.utils.search_cli.QueryEmbeddingRepository")
    @patch("pipelines.utils.search_cli.Qwen3OllamaEmbeddings")
    def test_keyword_search(self, MockEmb, MockQueryRepo, MockChunkRepo, MockKeyword):
        expected = [_make_chunk("k-1", "sql_keyword", similarity=0.5)]
        instance = MockKeyword.return_value
        instance.search.return_value = expected

        rc = self._run([
            "--type", "keyword",
            "--celex_id", "62013CJ0067",
            "--query", "fine",
        ])

        self.assertEqual(rc, 0)
        instance.search.assert_called_once_with(
            celex_id="62013CJ0067",
            query="fine",
            top_k=None,
        )

    # ------------------------------------------------------------------
    # Validation error — --query is always required
    # ------------------------------------------------------------------
    def test_missing_query_exits_with_error(self):
        rc = self._run(["--type", "semantic"])
        self.assertNotEqual(rc, 0)


    def test_global_semantic_search_and_rank(self):
        from tools.celex_tools import CelexDatabaseToolkit
        tools = CelexDatabaseToolkit()
        chunks = tools.semantic_search_chunks(
            query="Export cartels",
            hits=15,
        )
        ranker = FlashRanker()
        results = ranker.rank_chunkvo("Export cartels", chunks, top_k=10)
        self.assertIsInstance(results, list)
        self.assertTrue(all(isinstance(r, RankedChunkVo) for r in results))

if __name__ == "__main__":
    unittest.main()
