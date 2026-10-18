"""Unit tests for HybridSearch: RRF fusion, orchestration, relative scoring.

The lexical leg is Postgres full-text search fed the raw question (no keyword
extraction), and the two legs are merged with Reciprocal Rank Fusion before the
cross-encoder rerank. These tests mock the repository and ranker at the DB /
model boundary so they run without a live database or the FlashRank model.
"""

import unittest
from unittest.mock import MagicMock

from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from extract.rag.hybrid_search import HybridSearch


def _chunk(cid: str, origin: str, sim: float = 0.5, doc_pos: int = 0, text: str | None = None) -> ChunkVo:
    return ChunkVo(
        id=cid,
        celex_id="TEST",
        chunk=text or f"chunk {cid}",
        origin=origin,
        similarity=sim,
        doc_pos=doc_pos,
    )


class _LenRanker:
    """Deterministic stand-in for FlashRanker: score = length of chunk text."""

    def rank_chunkvo(self, query, chunks, top_k=None, apply_position_boost=True):
        ranked = [
            RankedChunkVo(
                **c.model_dump(), score=float(len(c.chunk)), base_score=float(len(c.chunk))
            )
            for c in chunks
        ]
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked[:top_k] if top_k else ranked


def _stub_embed(query: str) -> list:
    """Deterministic embed stub; HybridSearch only forwards the vector."""
    return [0.1, 0.2, 0.3]


class TestReciprocalRankFusion(unittest.TestCase):
    """Unit tests for HybridSearch.reciprocal_rank_fusion."""

    def setUp(self) -> None:
        self.search = HybridSearch(
            repo=MagicMock(), ranker=_LenRanker(), embed_query=_stub_embed
        )

    def test_fuses_and_dedups_by_id(self) -> None:
        fts = [_chunk("a", "pgvector_fts"), _chunk("b", "pgvector_fts")]
        vec = [_chunk("b", "pgvector_semantic"), _chunk("c", "pgvector_semantic")]
        fused = self.search.reciprocal_rank_fusion(fts, vec)
        ids = [c.id for c in fused]
        self.assertEqual(set(ids), {"a", "b", "c"})
        self.assertEqual(len(ids), 3)  # b deduplicated across the two legs
        # b is rank-1 in both legs → highest fused score → ranks first.
        self.assertEqual(ids[0], "b")

    def test_first_list_supplies_origin_on_tie(self) -> None:
        """A chunk in both legs keeps the instance from the first list (FTS)."""
        fts = [_chunk("x", "pgvector_fts")]
        vec = [_chunk("x", "pgvector_semantic")]
        fused = self.search.reciprocal_rank_fusion(fts, vec)
        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0].origin, "pgvector_fts")

    def test_rank_position_determines_order(self) -> None:
        lst = [_chunk("first", "pgvector_fts"), _chunk("second", "pgvector_fts")]
        fused = self.search.reciprocal_rank_fusion(lst)
        self.assertEqual([c.id for c in fused], ["first", "second"])

    def test_empty_lists_return_empty(self) -> None:
        self.assertEqual(self.search.reciprocal_rank_fusion([], []), [])


class MockChunkRepository:
    """Mock ChunkRetriever for the search-orchestration tests."""

    def __init__(self) -> None:
        self.last_fts_query = None
        self.last_fts_section_types = "unset"
        self.last_semantic_section_types = "unset"
        self.last_closing_count = None

    def websearch_search_in_celex(self, celex_id, query, hits=40, section_types=None):
        self.last_fts_query = query
        self.last_fts_section_types = section_types
        return [
            _chunk("fts-1", "pgvector_fts", text="This chunk explicitly mentions the fine amount due to the cartel."),
            _chunk("fts-2", "pgvector_fts", text="Lexical match but short."),
        ]

    def semantic_search_in_celex(self, celex_id, query_embedding, hits=40, section_types=None):
        self.last_semantic_section_types = section_types
        return [
            _chunk("vec-1", "pgvector_semantic", sim=0.90, text="The entity was penalized heavily for anticompetitive behavior."),
            _chunk("fts-1", "pgvector_semantic", sim=0.88, text="This chunk explicitly mentions the fine amount due to the cartel."),  # dup id across legs
        ]

    def keyword_search(self, celex_id, keyword):
        return [_chunk("sql-1", "sql_keyword"), _chunk("sql-2", "sql_keyword")]

    def select_closing_chunks(self, celex_id, count):
        self.last_closing_count = count
        return [
            _chunk(
                "closing-1",
                "pgvector_closing",
                sim=0.0,
                text="Done at Brussels. For the Commission, Member of the Commission.",
            )
        ]


class TestHybridSearchOrchestration(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = MockChunkRepository()
        self.search = HybridSearch(
            repo=self.repo, ranker=_LenRanker(), embed_query=_stub_embed
        )

    def test_fts_leg_receives_raw_question_not_a_keyword(self) -> None:
        """The lexical leg gets the full question — no keyword extraction."""
        self.search.search(celex_id="T", query="What penalties were applied?", top_k=5)
        self.assertEqual(self.repo.last_fts_query, "What penalties were applied?")

    def test_section_types_threaded_to_both_legs(self) -> None:
        """The section_scope filter reaches the FTS and the vector leg."""
        self.search.search(
            celex_id="T",
            query="fines?",
            top_k=5,
            section_types=["fines", "operative"],
        )
        self.assertEqual(self.repo.last_fts_section_types, ["fines", "operative"])
        self.assertEqual(self.repo.last_semantic_section_types, ["fines", "operative"])

    def test_section_types_default_is_none(self) -> None:
        """Without section_scope, both legs receive None (no filtering)."""
        self.search.search(celex_id="T", query="fines?", top_k=5)
        self.assertIsNone(self.repo.last_fts_section_types)
        self.assertIsNone(self.repo.last_semantic_section_types)

    def test_results_deduped_and_sorted(self) -> None:
        results = self.search.search(celex_id="T", query="penalties?", top_k=5)
        ids = [r.id for r in results]
        self.assertEqual(len(ids), len(set(ids)))  # fts-1 duplicate collapsed
        self.assertTrue(all(isinstance(r, RankedChunkVo) for r in results))
        scores = [r.score for r in results[:5]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_deduplicate_chunks_by_id(self) -> None:
        a = self.repo.keyword_search("c", "k")            # sql-1, sql-2
        b = self.repo.semantic_search_in_celex("c", [0.0])  # vec-1, fts-1
        deduped = self.search.deduplicate_chunks(a, b)
        self.assertEqual(len(deduped), 4)


def _make_chunk(chunk_id: str, similarity: float) -> ChunkVo:
    return ChunkVo(
        id=chunk_id,
        celex_id="TEST",
        chunk=f"chunk text {chunk_id}",
        origin="pgvector_semantic",
        similarity=similarity,
    )


class TestRelativeScoring(unittest.TestCase):
    """Unit tests for HybridSearch.relative_scoring."""

    def setUp(self) -> None:
        self.search = HybridSearch(
            repo=MockChunkRepository(), ranker=_LenRanker(), embed_query=_stub_embed
        )

    def test_empty_input_returns_empty(self) -> None:
        """An empty chunk list returns an empty list without error."""
        result = self.search.relative_scoring([])
        self.assertEqual(result, [])

    def test_absolute_delta_filters_low_similarity(self) -> None:
        """Chunks outside the absolute delta window are removed."""
        chunks = [
            _make_chunk("a", 0.90),
            _make_chunk("b", 0.85),
            _make_chunk("c", 0.75),  # 0.90 - 0.10 = 0.80 — below threshold
            _make_chunk("d", 0.60),  # well below
        ]
        result = self.search.relative_scoring(chunks, delta=0.10)
        ids = [c.id for c in result]
        self.assertIn("a", ids)
        self.assertIn("b", ids)
        self.assertNotIn("c", ids)
        self.assertNotIn("d", ids)

    def test_ratio_based_filters_low_similarity(self) -> None:
        """Chunks below best_score * ratio are removed."""
        chunks = [
            _make_chunk("a", 0.90),
            _make_chunk("b", 0.81),  # 0.90 * 0.90 = 0.81 — exactly at threshold
            _make_chunk("c", 0.80),  # just below 0.81
        ]
        result = self.search.relative_scoring(chunks, ratio=0.90)
        ids = [c.id for c in result]
        self.assertIn("a", ids)
        self.assertIn("b", ids)
        self.assertNotIn("c", ids)

    def test_ratio_takes_precedence_over_delta(self) -> None:
        """When ratio is supplied, delta is ignored."""
        chunks = [
            _make_chunk("a", 1.00),
            _make_chunk("b", 0.50),  # 1.00 - 0.10 = 0.90 would drop; 1.00 * 0.40 = 0.40 would keep
        ]
        result = self.search.relative_scoring(chunks, delta=0.10, ratio=0.40)
        ids = [c.id for c in result]
        self.assertIn("b", ids)

    def test_top_k_caps_output(self) -> None:
        """top_k limits the number of returned chunks."""
        chunks = [_make_chunk(str(i), 0.90 - i * 0.01) for i in range(10)]
        result = self.search.relative_scoring(chunks, delta=0.15, top_k=3)
        self.assertEqual(len(result), 3)

    def test_output_sorted_by_similarity_descending(self) -> None:
        """Results are always sorted highest similarity first."""
        chunks = [
            _make_chunk("low", 0.82),
            _make_chunk("high", 0.92),
            _make_chunk("mid", 0.88),
        ]
        result = self.search.relative_scoring(chunks, delta=0.15)
        scores = [c.similarity for c in result]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_single_chunk_always_returned(self) -> None:
        """A list with one chunk always passes (it is the best score)."""
        chunks = [_make_chunk("only", 0.72)]
        result = self.search.relative_scoring(chunks)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "only")

    def test_hard_floor_below_threshold_keeps_top_by_similarity(self) -> None:
        """Below the hard floor, the dense leg is retained (top-N by similarity),
        not discarded, so it can still feed Reciprocal Rank Fusion."""
        chunks = [
            _make_chunk("a", 0.34),
            _make_chunk("c", 0.10),
            _make_chunk("b", 0.30),
        ]
        result = self.search.relative_scoring(chunks)
        self.assertEqual([c.id for c in result], ["a", "b", "c"])

    def test_hard_floor_fallback_capped_to_n(self) -> None:
        """The weak-dense-leg fallback returns at most _LOW_SCORE_FALLBACK_N chunks."""
        from extract.rag.hybrid_search import _LOW_SCORE_FALLBACK_N

        chunks = [_make_chunk(str(i), 0.30 - i * 0.001) for i in range(20)]
        result = self.search.relative_scoring(chunks)
        self.assertEqual(len(result), _LOW_SCORE_FALLBACK_N)
        scores = [c.similarity for c in result]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_hard_floor_exactly_at_threshold_passes(self) -> None:
        """Best score at the 0.35 hard floor applies normal relative filtering."""
        chunks = [
            _make_chunk("a", 0.45),
            _make_chunk("b", 0.44),  # within best - delta(0.10) = 0.35, so kept
        ]
        result = self.search.relative_scoring(chunks, delta=0.10)
        ids = [c.id for c in result]
        self.assertIn("a", ids)
        self.assertIn("b", ids)


if __name__ == "__main__":
    unittest.main()
