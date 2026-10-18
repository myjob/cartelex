"""Unit tests for the closing-reserve injection in HybridSearch.

Bug
---
A decision's closing chunk carries the structurally fixed tail: the addressees,
``Done at Brussels, <date>`` and ``For the Commission / <name> / Member of the
Commission``. It answers a whole class of questions (commissioner, decision
date, addressees) while sharing no vocabulary with them -- the text never uses
the word "commissioner" -- so the FTS leg cannot match it and the dense leg
ranks the recitals arguing about "the Commission" above it. For
``39824_6567_14`` the only chunk naming Margrethe Vestager (``doc_pos 170`` of
171) was absent from the 24 chunks retrieval returned, and the extraction
answered "Not specified" from a context that genuinely lacked the name.

Fix
---
``HybridSearch.search()`` fetches the last ``settings.HYBRID_CLOSING_RESERVE``
chunks positionally, feeds them to the cross-encoder alongside the fused pool,
and re-appends any that the ``top_k`` cut dropped. Skipped for section-scoped
searches, which have already declared where the answer lives.
"""

import unittest
from unittest.mock import MagicMock, patch

from config.settings import settings
from extract.rag.hybrid_search import HybridSearch
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo

CELEX = "39824_6567_14"


def _make_chunk(chunk_id: str, origin: str, doc_pos: int, similarity: float = 0.5) -> ChunkVo:
    """Build a ChunkVo with the fields HybridSearch actually reads."""
    return ChunkVo(
        id=chunk_id,
        celex_id=CELEX,
        chunk=f"chunk text {chunk_id}",
        origin=origin,
        similarity=similarity,
        doc_pos=doc_pos,
    )


class TestClosingReserveInjection(unittest.TestCase):
    """The document tail reaches the reranker and survives the top_k cut."""

    def _build_hybrid(self, fts_chunks, semantic_chunks, closing_chunks, ranker_order):
        """Wire up a HybridSearch with a mocked repo and a deterministic ranker.

        Args:
            fts_chunks: Chunks the lexical leg returns.
            semantic_chunks: Chunks the dense leg returns.
            closing_chunks: Chunks ``select_closing_chunks`` returns.
            ranker_order: ``(chunk_id, score)`` pairs, best first; ids absent
                from the scored pool are skipped.

        Returns:
            A ``HybridSearch`` whose ``repo`` and ``ranker`` are mocks.
        """
        mock_repo = MagicMock()
        mock_repo.websearch_search_in_celex.return_value = fts_chunks
        mock_repo.semantic_search_in_celex.return_value = semantic_chunks
        mock_repo.select_closing_chunks.return_value = closing_chunks

        mock_ranker = MagicMock()

        def _rank(query, chunks, top_k=None):
            by_id = {chunk.id: chunk for chunk in chunks}
            ranked = [
                RankedChunkVo(**by_id[rank_id].model_dump(), score=score, base_score=score)
                for rank_id, score in ranker_order
                if rank_id in by_id
            ]
            return ranked[:top_k] if top_k else ranked

        mock_ranker.rank_chunkvo.side_effect = _rank

        return HybridSearch(
            repo=mock_repo, ranker=mock_ranker, embed_query=lambda q: [0.0]
        )

    @staticmethod
    def _scania_pool():
        """Return (fts, semantic, closing, ranker_order) modelling 39824_6567_14.

        The closing chunk is reached by neither leg and is scored last by the
        cross-encoder, exactly as observed in the live run.
        """
        fts = [_make_chunk("fts-93", "pgvector_fts", doc_pos=93)]
        semantic = [
            ChunkVo(
                id=f"sem-{pos}",
                celex_id=CELEX,
                chunk=f"recital at {pos}",
                origin="pgvector_semantic",
                similarity=0.90 - index * 0.01,
                doc_pos=pos,
            )
            for index, pos in enumerate((3, 5, 9, 34, 77))
        ]
        closing = [_make_chunk("closing-170", "pgvector_closing", doc_pos=170, similarity=0.0)]
        ranker_order = [
            ("sem-3", 0.91), ("sem-5", 0.88), ("sem-9", 0.72),
            ("sem-34", 0.55), ("sem-77", 0.41),
            ("fts-93", 0.09), ("closing-170", 0.01),
        ]
        return fts, semantic, closing, ranker_order

    def test_closing_chunk_is_scored_by_the_reranker(self):
        """The trailing chunk joins the pool even though no leg retrieved it."""
        fts, semantic, closing, order = self._scania_pool()
        hybrid = self._build_hybrid(fts, semantic, closing, order)

        hybrid.search(celex_id=CELEX, query="Who was the commissioner?", top_k=3)

        scored_ids = {
            chunk.id for chunk in hybrid.ranker.rank_chunkvo.call_args.args[1]
        }
        self.assertIn("closing-170", scored_ids)
        hybrid.repo.select_closing_chunks.assert_called_once_with(
            celex_id=CELEX, count=settings.HYBRID_CLOSING_RESERVE
        )

    def test_closing_chunk_survives_a_tight_top_k(self):
        """The lowest-scored trailing chunk is re-appended after the cut."""
        fts, semantic, closing, order = self._scania_pool()
        hybrid = self._build_hybrid(fts, semantic, closing, order)

        results = hybrid.search(
            celex_id=CELEX, query="Who was the commissioner?", top_k=3
        )

        self.assertIn("closing-170", {chunk.id for chunk in results})
        self.assertGreater(len(results), 3)

    def test_no_duplicate_when_the_closing_chunk_already_ranked(self):
        """A trailing chunk both legs found is returned exactly once."""
        closing = [_make_chunk("closing-170", "pgvector_closing", doc_pos=170)]
        semantic = [
            ChunkVo(
                id="closing-170",
                celex_id=CELEX,
                chunk="chunk text closing-170",
                origin="pgvector_semantic",
                similarity=0.95,
                doc_pos=170,
            ),
            _make_chunk("sem-3", "pgvector_semantic", doc_pos=3, similarity=0.90),
        ]
        order = [("closing-170", 0.99), ("sem-3", 0.50)]

        hybrid = self._build_hybrid([], semantic, closing, order)
        results = hybrid.search(celex_id=CELEX, query="commissioner", top_k=2)

        ids = [chunk.id for chunk in results]
        self.assertEqual(ids.count("closing-170"), 1)
        self.assertEqual(len(ids), 2)

    def test_reserve_skipped_for_section_scoped_search(self):
        """A section_scope declares where the answer lives; do not widen it."""
        fts, semantic, closing, order = self._scania_pool()
        hybrid = self._build_hybrid(fts, semantic, closing, order)

        results = hybrid.search(
            celex_id=CELEX, query="List all fines", top_k=3, section_types=["fines"]
        )

        hybrid.repo.select_closing_chunks.assert_not_called()
        self.assertNotIn("closing-170", {chunk.id for chunk in results})

    def test_reserve_disabled_by_setting(self):
        """HYBRID_CLOSING_RESERVE=0 restores the previous behaviour."""
        fts, semantic, closing, order = self._scania_pool()
        hybrid = self._build_hybrid(fts, semantic, closing, order)

        with patch.object(settings, "HYBRID_CLOSING_RESERVE", 0):
            results = hybrid.search(
                celex_id=CELEX, query="Who was the commissioner?", top_k=3
            )

        hybrid.repo.select_closing_chunks.assert_not_called()
        # The ranker's top 3, plus fts-93 from the lexical reserve.
        self.assertEqual(
            [chunk.id for chunk in results], ["sem-3", "sem-5", "sem-9", "fts-93"]
        )

    def test_ranker_invoked_once_with_the_closing_reserve(self):
        """The cross-encoder still runs exactly once per query."""
        fts, semantic, closing, order = self._scania_pool()
        hybrid = self._build_hybrid(fts, semantic, closing, order)

        hybrid.search(celex_id=CELEX, query="Who was the commissioner?", top_k=3)

        self.assertEqual(hybrid.ranker.rank_chunkvo.call_count, 1)

    def test_no_injection_when_top_k_is_none(self):
        """Without a cut every scored chunk is returned, tail included."""
        fts, semantic, closing, order = self._scania_pool()
        hybrid = self._build_hybrid(fts, semantic, closing, order)

        results = hybrid.search(
            celex_id=CELEX, query="Who was the commissioner?", top_k=None
        )

        ids = [chunk.id for chunk in results]
        self.assertEqual(ids.count("closing-170"), 1)
        self.assertEqual(len(ids), len(order))


if __name__ == "__main__":
    unittest.main()
