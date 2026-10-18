"""Regression tests for the lexical-reserve injection in HybridSearch.

Bug
---
When ``top_k`` is smaller than the total ranked pool, lexical (Postgres FTS)
chunks that matched the query's surface terms can be completely buried by the
cross-encoder ranker's positional and semantic biases.  Chunks where the terms
appear only incidentally (e.g. "German labour court" in a paragraph about
quota-fixing) score very low and get dropped — defeating the purpose of the
FTS pass.

Fix
---
``HybridSearch.search()`` guarantees at least ``_LEXICAL_RESERVE`` FTS-origin
chunks in the final result by injecting the highest-ranked missing FTS chunks
that fell below the ``top_k`` cutoff.
"""

import unittest
from unittest.mock import MagicMock, patch

from config.settings import settings
from extract.rag.hybrid_search import HybridSearch, _LEXICAL_RESERVE
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo


def _make_chunk(chunk_id: str, celex_id: str, origin: str, doc_pos: int = 0) -> ChunkVo:
    return ChunkVo(
        id=chunk_id,
        celex_id=celex_id,
        chunk=f"chunk text {chunk_id}",
        origin=origin,
        similarity=0.5,
        doc_pos=doc_pos,
    )


class TestLexicalReserveInjection(unittest.TestCase):
    """Verify that FTS-origin chunks survive a tight top_k cutoff."""

    def _build_hybrid(self, fts_chunks, semantic_chunks, ranker_order):
        """Wire up a HybridSearch with mocked repo and deterministic ranker."""
        mock_repo = MagicMock()
        mock_repo.websearch_search_in_celex.return_value = fts_chunks
        mock_repo.semantic_search_in_celex.return_value = semantic_chunks

        mock_ranker = MagicMock()

        def _rank(query, chunks, top_k=None):
            cmap = {c.id: c for c in chunks}
            ranked = []
            for rank_id, score in ranker_order:
                if rank_id in cmap:
                    c = cmap[rank_id]
                    ranked.append(
                        RankedChunkVo(**c.model_dump(), score=score, base_score=score)
                    )
            if top_k:
                ranked = ranked[:top_k]
            return ranked

        mock_ranker.rank_chunkvo.side_effect = _rank

        return HybridSearch(
            repo=mock_repo, ranker=mock_ranker, embed_query=lambda q: [0.0]
        )

    def test_fts_chunks_injected_when_below_top_k(self):
        """FTS chunks ranked below top_k are injected up to _LEXICAL_RESERVE."""
        celex = "38344_5856_3"

        fts_chunks = [
            _make_chunk("fts-1", celex, "pgvector_fts", doc_pos=103),
            _make_chunk("fts-2", celex, "pgvector_fts", doc_pos=63),
            _make_chunk("fts-3", celex, "pgvector_fts", doc_pos=315),
        ]
        sem_chunks = [
            # High similarity so relative_scoring keeps them.
            ChunkVo(id="sem-1", celex_id=celex, chunk="s1", origin="pgvector_semantic", similarity=0.90, doc_pos=594),
            ChunkVo(id="sem-2", celex_id=celex, chunk="s2", origin="pgvector_semantic", similarity=0.89, doc_pos=585),
            ChunkVo(id="sem-3", celex_id=celex, chunk="s3", origin="pgvector_semantic", similarity=0.88, doc_pos=581),
            ChunkVo(id="sem-4", celex_id=celex, chunk="s4", origin="pgvector_semantic", similarity=0.87, doc_pos=572),
            ChunkVo(id="sem-5", celex_id=celex, chunk="s5", origin="pgvector_semantic", similarity=0.86, doc_pos=568),
        ]

        # Ranker order: all semantic chunks first, then FTS chunks.
        # With top_k=3, no FTS chunks make the cutoff.
        ranker_order = [
            ("sem-1", 1.85), ("sem-2", 1.84), ("sem-3", 1.83),
            ("sem-4", 1.72), ("sem-5", 1.67),
            ("fts-3", 0.16), ("fts-1", 0.002), ("fts-2", 0.0003),
        ]

        hybrid = self._build_hybrid(fts_chunks, sem_chunks, ranker_order)
        results = hybrid.search(celex_id=celex, query="courts", top_k=3)

        fts_in_result = sum(1 for c in results if c.origin == "pgvector_fts")
        self.assertGreater(
            fts_in_result, 0,
            "At least one FTS chunk must survive via reservation.",
        )
        self.assertLessEqual(
            fts_in_result, _LEXICAL_RESERVE,
            f"No more than {_LEXICAL_RESERVE} FTS chunks should be injected.",
        )
        self.assertGreater(len(results), 3)

    def test_no_injection_when_reserve_already_met(self):
        """No injection needed when enough FTS chunks are in top_k."""
        celex = "TEST"

        fts_chunks = [_make_chunk(f"fts-{i}", celex, "pgvector_fts") for i in range(6)]
        sem_chunks = [_make_chunk(f"sem-{i}", celex, "pgvector_semantic") for i in range(4)]

        ranker_order = [
            (f"fts-{i}", 2.0 - i * 0.1) for i in range(6)
        ] + [
            (f"sem-{i}", 0.5 - i * 0.1) for i in range(4)
        ]

        hybrid = self._build_hybrid(fts_chunks, sem_chunks, ranker_order)
        results = hybrid.search(celex_id=celex, query="test", top_k=6)

        self.assertEqual(len(results), 6)

    def test_no_injection_when_no_fts_chunks(self):
        """No injection when there are no FTS chunks at all."""
        celex = "TEST"

        sem_chunks = [_make_chunk(f"sem-{i}", celex, "pgvector_semantic") for i in range(5)]
        ranker_order = [(f"sem-{i}", 1.0 - i * 0.1) for i in range(5)]

        hybrid = self._build_hybrid([], sem_chunks, ranker_order)
        results = hybrid.search(celex_id=celex, query="test", top_k=3)

        self.assertEqual(len(results), 3)

    def test_ranker_invoked_once_in_reserve_case(self):
        """The cross-encoder runs exactly once even when the reserve triggers.

        Regression guard for the removed double rerank: the lexical reserve must
        reuse the single full ranking, not score the fused pool a second time.
        """
        celex = "38344_5856_3"

        fts_chunks = [
            _make_chunk("fts-1", celex, "pgvector_fts", doc_pos=103),
            _make_chunk("fts-2", celex, "pgvector_fts", doc_pos=63),
        ]
        sem_chunks = [
            ChunkVo(id="sem-1", celex_id=celex, chunk="s1", origin="pgvector_semantic", similarity=0.90, doc_pos=594),
            ChunkVo(id="sem-2", celex_id=celex, chunk="s2", origin="pgvector_semantic", similarity=0.89, doc_pos=585),
            ChunkVo(id="sem-3", celex_id=celex, chunk="s3", origin="pgvector_semantic", similarity=0.88, doc_pos=581),
        ]
        # Semantic chunks rank above the FTS hits, so with top_k=1 the reserve
        # injection path is exercised.
        ranker_order = [
            ("sem-1", 1.85), ("sem-2", 1.84), ("sem-3", 1.83),
            ("fts-1", 0.02), ("fts-2", 0.01),
        ]

        hybrid = self._build_hybrid(fts_chunks, sem_chunks, ranker_order)
        hybrid.search(celex_id=celex, query="courts", top_k=1)

        self.assertEqual(
            hybrid.ranker.rank_chunkvo.call_count,
            1,
            "Reranker must be scored once; the reserve reuses that ranking.",
        )

    def test_fts_preserved_through_input_cap(self):
        """FTS chunks reach the reranker even when the input cap drops them.

        With RERANKER_INPUT_TOP_K forced to 0 the plain cap yields an empty
        pool; the FTS reserve must still hand the FTS-origin chunks to the
        cross-encoder so the lexical leg is not silently discarded.
        """
        celex = "TEST"

        fts_chunks = [
            _make_chunk("fts-1", celex, "pgvector_fts", doc_pos=10),
            _make_chunk("fts-2", celex, "pgvector_fts", doc_pos=20),
        ]
        sem_chunks = [
            ChunkVo(id="sem-1", celex_id=celex, chunk="s1", origin="pgvector_semantic", similarity=0.90, doc_pos=30),
        ]
        ranker_order = [("fts-1", 0.9), ("fts-2", 0.8), ("sem-1", 0.7)]

        hybrid = self._build_hybrid(fts_chunks, sem_chunks, ranker_order)
        with patch.object(settings, "RERANKER_INPUT_TOP_K", 0):
            hybrid.search(celex_id=celex, query="courts", top_k=3)

        scored_chunks = hybrid.ranker.rank_chunkvo.call_args.args[1]
        scored_ids = {chunk.id for chunk in scored_chunks}
        self.assertIn("fts-1", scored_ids)
        self.assertIn("fts-2", scored_ids)
        self.assertNotIn(
            "sem-1", scored_ids,
            "With the cap at 0, only the FTS reserve should reach the reranker.",
        )

    def test_no_injection_when_no_top_k(self):
        """When top_k is None, all chunks are returned — no injection needed."""
        celex = "TEST"

        fts_chunks = [_make_chunk("fts-1", celex, "pgvector_fts")]
        sem_chunks = [_make_chunk("sem-1", celex, "pgvector_semantic")]

        ranker_order = [("sem-1", 1.0), ("fts-1", 0.1)]

        hybrid = self._build_hybrid(fts_chunks, sem_chunks, ranker_order)
        results = hybrid.search(celex_id=celex, query="test", top_k=None)

        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
