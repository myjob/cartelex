"""Unit tests for EnumerationSearch orchestration.

Mirrors the lightweight-mock style of test_hybrid_search.py: stub the
``ChunkRetriever`` and the ``FlashRanker``, then assert that
``EnumerationSearch`` wires them together correctly.

Key behaviours verified:
* Dense + BM25 candidate union is deduplicated by chunk id.
* The ranker is invoked with ``apply_position_boost=False`` and
  ``top_k=None`` — the two flags that distinguish enumeration from Q&A.
* The configured ``hits`` settings reach the repository unmodified.
"""

import unittest
from typing import List

from config.settings import settings
from extract.rag.enumeration_search import EnumerationSearch
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo


class _StubChunkRepo:
    """Minimal ChunkRetriever stub that records every call it received."""

    def __init__(
        self,
        dense_result: List[ChunkVo],
        bm25_result: List[ChunkVo],
    ) -> None:
        self._dense = dense_result
        self._bm25 = bm25_result
        self.dense_calls: list[dict] = []
        self.bm25_calls: list[dict] = []
        self.keyword_calls: list[dict] = []

    def semantic_search_in_celex(
        self, celex_id: str, query_embedding: list, hits: int, section_types=None
    ):
        self.dense_calls.append(
            {
                "celex_id": celex_id,
                "query_embedding": query_embedding,
                "hits": hits,
                "section_types": section_types,
            }
        )
        return list(self._dense)

    def fulltext_search_in_celex(
        self, celex_id: str, query: str, hits: int, section_types=None
    ):
        self.bm25_calls.append(
            {
                "celex_id": celex_id,
                "query": query,
                "hits": hits,
                "section_types": section_types,
            }
        )
        return list(self._bm25)

    def keyword_search(self, celex_id: str, keyword: str):
        # Present only to satisfy the broader ChunkRetriever protocol surface;
        # EnumerationSearch must never call this.
        self.keyword_calls.append({"celex_id": celex_id, "keyword": keyword})
        return []


class _SpyFlashRanker:
    """Records ranker invocations and returns a deterministic ordering."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def rank_chunkvo(
        self,
        query: str,
        chunks,
        top_k=None,
        apply_position_boost: bool = True,
    ):
        self.calls.append(
            {
                "query": query,
                "chunk_ids": [c.id for c in chunks],
                "top_k": top_k,
                "apply_position_boost": apply_position_boost,
            }
        )
        # Mirror real shape: emit RankedChunkVo with base_score==score.
        return [
            RankedChunkVo(
                id=c.id,
                celex_id=c.celex_id,
                chunk=c.chunk,
                origin=c.origin,
                similarity=c.similarity,
                doc_pos=c.doc_pos,
                score=float(len(c.chunk)),
                base_score=float(len(c.chunk)),
            )
            for c in chunks
        ]


def _chunk(chunk_id: str, origin: str, doc_pos: int = 0) -> ChunkVo:
    return ChunkVo(
        id=chunk_id,
        celex_id="TEST",
        chunk=f"text-of-{chunk_id}",
        origin=origin,
        similarity=0.5,
        doc_pos=doc_pos,
    )


class TestEnumerationSearch(unittest.TestCase):
    def setUp(self) -> None:
        self.dense = [
            _chunk("d1", "pgvector_semantic", doc_pos=10),
            _chunk("shared", "pgvector_semantic", doc_pos=5),
        ]
        self.bm25 = [
            _chunk("shared", "pgvector_fts", doc_pos=5),
            _chunk("b1", "pgvector_fts", doc_pos=1),
        ]
        self.repo = _StubChunkRepo(dense_result=self.dense, bm25_result=self.bm25)
        self.ranker = _SpyFlashRanker()
        self.search = EnumerationSearch(
            repo=self.repo, ranker=self.ranker, embed_query=lambda q: [0.0]
        )

    def test_search_unions_and_deduplicates_by_id(self):
        """The shared chunk id appears once after the dense ∪ BM25 union."""
        results = self.search.search(celex_id="TEST", query="list all courts")
        result_ids = [r.id for r in results]
        self.assertEqual(sorted(result_ids), ["b1", "d1", "shared"])

    def test_search_calls_ranker_with_boost_off_and_no_top_k(self):
        """Enumeration must rank with apply_position_boost=False and top_k=None."""
        self.search.search(celex_id="TEST", query="list every fine")
        self.assertEqual(len(self.ranker.calls), 1)
        call = self.ranker.calls[0]
        self.assertEqual(call["query"], "list every fine")
        self.assertIsNone(call["top_k"])
        self.assertFalse(call["apply_position_boost"])

    def test_search_passes_settings_hits_to_repo(self):
        """The configured ENUMERATION_DENSE_HITS / ENUMERATION_BM25_HITS reach the repo."""
        self.search.search(celex_id="TEST", query="enumerate parties")
        self.assertEqual(
            self.repo.dense_calls[0]["hits"], settings.ENUMERATION_DENSE_HITS
        )
        self.assertEqual(
            self.repo.bm25_calls[0]["hits"], settings.ENUMERATION_BM25_HITS
        )

    def test_search_never_calls_ilike_keyword_search(self):
        """The keyword leg of HybridSearch must NOT be invoked here."""
        self.search.search(celex_id="TEST", query="list all courts")
        self.assertEqual(self.repo.keyword_calls, [])

    def test_empty_legs_yield_empty_result(self):
        """Both legs empty -> ranker is still called with [] and returns []."""
        search = EnumerationSearch(
            repo=_StubChunkRepo(dense_result=[], bm25_result=[]),
            ranker=self.ranker,
            embed_query=lambda q: [0.0],
        )
        results = search.search(celex_id="TEST", query="anything")
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
