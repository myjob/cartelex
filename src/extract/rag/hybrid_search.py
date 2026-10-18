import logging
from collections.abc import Callable
from typing import List
from typing import Optional

from pipelines.agent_dspy.dspy_protocols import ChunkRetriever
from extract.rag.ranker.ranker_factory import RankerProtocol
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from config.settings import settings

logger = logging.getLogger(__name__)

# Minimum number of lexical (Postgres FTS) chunks guaranteed in the final
# result, even when the cross-encoder ranker scores them below the top_k
# cutoff.  Ensures literal surface-form matches are not completely buried by
# semantic-only results.
_LEXICAL_RESERVE = 5

# Cosine below which the dense leg is considered weak (e.g. a loaded question
# against a short, off-topic decision). Rather than discarding the whole leg,
# the top few chunks by similarity are retained so they still feed RRF (which
# fuses on rank, not absolute score); dropping them collapses retrieval to the
# sparse FTS leg and can omit the chunks that actually name the parties.
_SIMILARITY_HARD_FLOOR = 0.35
_LOW_SCORE_FALLBACK_N = 10


class HybridSearch:
    """
    A unified orchestration class bridging keyword-based RDBMS
    search with semantic vector search, resolving everything
    declaratively via the FlashRanker.
    """

    def __init__(
        self,
        repo: ChunkRetriever,
        ranker: RankerProtocol,
        embed_query: Callable[[str], list],
    ):
        """Initialize the hybrid searcher.

        Parameters
        ----------
        repo:
            Data-access object providing keyword and (vector-in) semantic
            chunk retrieval.
        ranker:
            Cross-encoder reranker applied to the merged candidate set.
        embed_query:
            Callable mapping a query string to its embedding vector. The
            embedding concern is kept external to the repository (Provisioner
            pattern); the caller typically passes a cached embed function.
        """
        self.repo = repo
        self.ranker = ranker
        self.embed_query = embed_query

    def deduplicate_chunks(self, *chunk_lists: List[ChunkVo]) -> List[ChunkVo]:
        """
        Deduplicate chunks strictly by their ID.
        """
        seen_ids = set()
        deduped = []
        for chunk_list in chunk_lists:
            for chunk in chunk_list:
                if chunk.id not in seen_ids:
                    seen_ids.add(chunk.id)
                    deduped.append(chunk)
        return deduped

    def reciprocal_rank_fusion(
        self, *ranked_lists: List[ChunkVo], k: Optional[int] = None
    ) -> List[ChunkVo]:
        """Fuse several best-first ranked chunk lists via Reciprocal Rank Fusion.

        RRF replaces the old union-dedup merge (which fabricated a fixed
        ``similarity=0.5`` for keyword hits so the two legs could be compared).
        ts_rank and cosine similarity live on different scales and cannot be
        compared directly; RRF sidesteps that by fusing on *rank position*
        alone: a chunk's fused score is ``sum 1 / (k + rank)`` over every list
        it appears in (rank is 1-based, best = 1).

        Each input list must already be ordered best-first. Chunks are
        deduplicated by ``id``; the first list a chunk appears in supplies the
        retained ``ChunkVo`` instance, so passing the lexical leg first gives
        it origin precedence (mirroring the previous keyword-first dedup).

        Parameters
        ----------
        ranked_lists:
            One or more best-first ``ChunkVo`` lists (e.g. FTS, then vector).
        k:
            RRF damping constant. Defaults to ``settings.RRF_K`` (60).

        Returns
        -------
        List[ChunkVo]
            Deduplicated chunks ordered by descending fused score.
        """
        k = settings.RRF_K if k is None else k
        fused_scores: dict[str, float] = {}
        chunk_by_id: dict[str, ChunkVo] = {}
        for ranked in ranked_lists:
            for rank, chunk in enumerate(ranked, start=1):
                fused_scores[chunk.id] = fused_scores.get(chunk.id, 0.0) + 1.0 / (
                    k + rank
                )
                chunk_by_id.setdefault(chunk.id, chunk)
        return sorted(
            chunk_by_id.values(), key=lambda c: fused_scores[c.id], reverse=True
        )

    def search(
        self,
        celex_id: str,
        query: str,
        top_k: Optional[int] = None,
        section_types: Optional[List[str]] = None,
    ) -> List[RankedChunkVo]:
        """
        Execute parallel hybrid search across the FTS and vector legs, fuse the
        two rankings with RRF, rerank via the cross-encoder, and return strictly
        typed RankedChunkVos. ``section_types`` optionally restricts both legs
        to specific decision sections (e.g. ``["fines", "operative"]`` for a
        fines question), per the ``section_scope`` fact-config key.

        Two reserves survive the ``top_k`` cut on top of the ranker's verdict:
        the lexical reserve (FTS-origin hits, steps 4 and 6) and the closing
        reserve (the document's trailing chunks, steps 4b and 7). The result
        can therefore exceed ``top_k`` by up to
        ``_LEXICAL_RESERVE + settings.HYBRID_CLOSING_RESERVE`` chunks.
        """
        logger.debug(
            f"Starting hybrid search for CELEX: '{celex_id}' with query '{query}'"
        )

        # 1. Lexical leg — Postgres full-text search on the raw question.
        #    websearch_to_tsquery applies English stemming and multi-word
        #    matching, so no client-side keyword extraction or plural stemming
        #    is needed (both were fragile heuristics; see the review).
        fts_chunks = self.repo.websearch_search_in_celex(
            celex_id=celex_id,
            query=query,
            hits=settings.HYBRID_FTS_HITS,
            section_types=section_types,
        )
        logger.debug(f"FTS lexical search yielded {len(fts_chunks)} chunks.")

        # 2. Dense leg — vector similarity, then relative-score filtering.
        query_embedding = self.embed_query(query)
        vector_chunks = self.repo.semantic_search_in_celex(
            celex_id=celex_id,
            query_embedding=query_embedding,
            hits=settings.SEMANTIC_SEARCH_HITS,
            section_types=section_types,
        )
        logger.debug(f"PGVector Semantic search yielded {len(vector_chunks)} chunks.")

        vector_chunks_filtered = self.relative_scoring(
            vector_chunks=vector_chunks, delta=0.15
        )

        # 3. Reciprocal Rank Fusion — principled, scale-free merge of the two
        #    legs (replaces union-dedup + the fabricated 0.5 keyword sentinel).
        fused = self.reciprocal_rank_fusion(fts_chunks, vector_chunks_filtered)

        # 4. Bound the reranker input. Cross-encoder scoring is linear in the
        #    number of (query, chunk) pairs, so cap the RRF-ordered fused pool to
        #    RERANKER_INPUT_TOP_K. FTS-origin chunks that fall outside the cap are
        #    preserved (up to _LEXICAL_RESERVE) so the lexical reserve in step 6
        #    still has material to draw from.
        rerank_input = fused[: settings.RERANKER_INPUT_TOP_K]
        if fts_chunks:
            capped_ids = {chunk.id for chunk in rerank_input}
            reserve_fts = [
                chunk
                for chunk in fused
                if chunk.origin == "pgvector_fts" and chunk.id not in capped_ids
            ][:_LEXICAL_RESERVE]
            rerank_input = rerank_input + reserve_fts

        # 4b. Closing reserve — force the document's trailing chunks into the
        #     scored pool. Unlike the lexical reserve, these chunks may be
        #     absent from `fused` altogether: neither leg can find the closing
        #     block for a question like "Who was the commissioner?", because
        #     the text says "For the Commission ... Member of the Commission"
        #     and never uses the word the question is built on. So they are
        #     fetched positionally rather than rescued from the fused pool.
        #     Skipped when section_types is set: a section-scoped search has
        #     already declared where the answer lives, and appending the tail
        #     would silently widen that scope.
        closing_ids: set[str] = set()
        if section_types is None and settings.HYBRID_CLOSING_RESERVE > 0:
            pooled_ids = {chunk.id for chunk in rerank_input}
            closing_chunks = self.repo.select_closing_chunks(
                celex_id=celex_id, count=settings.HYBRID_CLOSING_RESERVE
            )
            closing_ids = {chunk.id for chunk in closing_chunks}
            rerank_input = rerank_input + [
                chunk for chunk in closing_chunks if chunk.id not in pooled_ids
            ]

        # 5. Cross-encoder rerank with the structural boost — a single forward
        #    pass over the capped pool. The full ranking is reused for both the
        #    top_k result and the lexical reserve below, so the model runs
        #    exactly once per query (no duplicate scoring of the same pool).
        ranked_full = self.ranker.rank_chunkvo(query, rerank_input, top_k=None)
        ranked_results = ranked_full[:top_k] if top_k else ranked_full

        # 6. Lexical reservation: guarantee a minimum number of FTS hits
        #    survive the ranker's top_k cutoff.
        #
        #    The cross-encoder compares each chunk to the full question text.
        #    Chunks where the query terms appear only incidentally (e.g.
        #    "German labour court" in a paragraph about quota-fixing) score
        #    very low and get dropped.  But FTS hits are literal surface-form
        #    matches — the whole purpose of the lexical leg is to catch
        #    evidence semantic search misses.  Dropping them defeats that.
        #
        #    Strategy: if fewer than ``_LEXICAL_RESERVE`` FTS-origin chunks
        #    made it into ranked_results, inject the highest-ranked missing
        #    FTS chunks (preserving score order) until the reserve is met. The
        #    missing hits come from ``ranked_full`` — the same single rerank —
        #    rather than a second scoring pass.
        if fts_chunks and top_k:
            ranked_ids = {chunk.id for chunk in ranked_results}
            fts_in_result = sum(
                1 for chunk in ranked_results if chunk.origin == "pgvector_fts"
            )
            if fts_in_result < _LEXICAL_RESERVE:
                missing_fts = [
                    chunk
                    for chunk in ranked_full
                    if chunk.origin == "pgvector_fts" and chunk.id not in ranked_ids
                ]
                slots = _LEXICAL_RESERVE - fts_in_result
                for extra in missing_fts[:slots]:
                    ranked_results.append(extra)
                    logger.debug(
                        "Lexical-reserve injection: doc_pos=%d score=%.4f",
                        extra.doc_pos,
                        extra.score,
                    )

        # 7. Closing reservation: the trailing chunks were scored in step 5,
        #    but the cross-encoder rates them near zero for a question their
        #    vocabulary does not share, so a tight top_k drops them again.
        #    Re-append the ones that did not survive, from the same ranking.
        if closing_ids and top_k:
            ranked_ids = {chunk.id for chunk in ranked_results}
            for extra in ranked_full:
                if extra.id in closing_ids and extra.id not in ranked_ids:
                    ranked_results.append(extra)
                    logger.debug(
                        "Closing-reserve injection: doc_pos=%d score=%.4f",
                        extra.doc_pos,
                        extra.score,
                    )

        logger.debug(
            f"Hybrid Search finalized with {len(ranked_results)} ranked output chunks."
        )
        return ranked_results

    def relative_scoring(
        self,
        vector_chunks: List[ChunkVo],
        delta: float = 0.10,
        ratio: float | None = None,
        top_k: int | None = None,
    ) -> List[ChunkVo]:
        """
        Filter vector chunks using rank-aware relative scoring (Top-K + score gap).

        Instead of a fixed absolute threshold, keeps only chunks whose similarity
        is within a gap of the best score in the result set.

        Pattern A — absolute delta:
            threshold = best_score - delta      (default, delta ≈ 0.05–0.15)

        Pattern B — percentage-based (when ratio is provided):
            threshold = best_score * ratio      (ratio ≈ 0.85–0.90)

        When both delta and ratio are provided, ratio takes precedence.

        Parameters
        ----------
        vector_chunks:
            Chunks retrieved by semantic search, each carrying a similarity score.
        delta:
            Maximum allowed drop from the best score (absolute, cosine units).
            Ignored when ratio is provided. Default: 0.10.
        ratio:
            Minimum fraction of the best score a chunk must reach to be kept,
            e.g. 0.85 means >= 85 % of the top score. When supplied, overrides delta.
        top_k:
            Optional hard cap on the number of returned chunks (after score filtering).
            Chunks are returned in descending similarity order.

        Returns
        -------
        List[ChunkVo]
            Chunks that pass the relative score threshold, sorted by similarity desc,
            capped to top_k when specified.
        """
        if not vector_chunks:
            return []

        best_score = max(c.similarity for c in vector_chunks)

        # Weak dense leg: keep the top-N by similarity instead of discarding the
        # whole leg. RRF fuses on rank, so a low absolute score does not distort
        # fusion, and these chunks may be the only ones naming the parties.
        if best_score < _SIMILARITY_HARD_FLOOR:
            logger.warning(
                "Best cosine %.3f below floor %.2f for %d chunks; retaining "
                "top-%d by similarity for RRF.",
                best_score,
                _SIMILARITY_HARD_FLOOR,
                len(vector_chunks),
                _LOW_SCORE_FALLBACK_N,
            )
            return sorted(
                vector_chunks, key=lambda c: c.similarity, reverse=True
            )[:_LOW_SCORE_FALLBACK_N]

        if ratio is not None:
            threshold = best_score * ratio
        else:
            threshold = best_score - delta

        filtered = [c for c in vector_chunks if c.similarity >= threshold]
        filtered.sort(key=lambda c: c.similarity, reverse=True)

        if top_k is not None:
            filtered = filtered[:top_k]

        return filtered
