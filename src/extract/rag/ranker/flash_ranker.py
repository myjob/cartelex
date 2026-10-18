import logging
from typing import List, Optional

from flashrank import Ranker, RerankRequest
from config.settings import settings
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from extract.rag.ranker.position_boost import structural_boost
from pipelines.nace.nace_tree import NaceVo, RankedNaceVo

logger = logging.getLogger(__name__)

class FlashRanker:
    """
    A declarative Ranker module conforming to DSPy/Systems engineered principles.
    Injects configuration dynamically from global settings.
    """
    def __init__(self, model_name: str = "ms-marco-MiniLM-L-12-v2"):
        self.model_name = model_name
        self.cache_dir = settings.CACHE_DIR
        
        # Provisioner Pattern
        logger.debug(f"Initializing FlashRanker with model '{self.model_name}' at '{self.cache_dir}'")
        self.ranker = Ranker(model_name=self.model_name, cache_dir=self.cache_dir)

    def rank_chunkvo(
        self,
        query: str,
        chunks: List[ChunkVo],
        top_k: Optional[int] = None,
        apply_position_boost: bool = True,
    ) -> List[RankedChunkVo]:
        """Rerank ``chunks`` against ``query`` using FlashRank.

        Args:
            query: Free-text query to rerank against.
            chunks: Candidate chunks to score.
            top_k: If given, return only the top *k* results.
            apply_position_boost: When ``True`` (default) the structural
                boost selected by ``settings.RANKER_BOOST_MODE`` (section-type
                weighting by default; the Legal Climax depth boost for A/B) is
                added to the model score. Pass ``False`` for enumeration /
                recall tasks where structural priors must not penalise rare
                mentions.

        Returns:
            ``RankedChunkVo`` list sorted descending by score.
        """
        if not chunks:
            return []

        # 1. Format for FlashRank
        passages = [
            {"id": chunk.id, "text": chunk.chunk, "meta": {"origin": chunk.origin, "similarity":chunk.similarity }}
            for chunk in chunks
        ]

        # 2. Execute Ranking
        request = RerankRequest(query=query, passages=passages)
        results = self.ranker.rerank(request)

        # 3. Create mapping for fast retrieval
        chunk_map = {str(c.id): c for c in chunks}

        # 4. Find the local max for depth calculation
        local_max = max((c.doc_pos for c in chunks), default=1)

        ranked_chunks = []

        for res in results:
            base_score = res["score"]
            original_chunk = chunk_map.get(str(res["id"]))

            if original_chunk and apply_position_boost:
                # Structural prior (section-type weights or the Legal Climax
                # depth boost, per settings.RANKER_BOOST_MODE). See
                # position_boost.py for the rationale.
                adjusted_score = base_score + structural_boost(
                    original_chunk, local_max
                )
            else:
                # Either chunk metadata is missing, or the caller opted out of
                # the structural boost (enumeration / recall mode).
                adjusted_score = base_score

            ranked_chunks.append(RankedChunkVo(
                id=res["id"],
                celex_id=original_chunk.celex_id if original_chunk else "",
                chunk=res["text"],
                origin=res["meta"]["origin"],
                similarity=res["meta"]["similarity"],
                score=adjusted_score,
                base_score=base_score,
                doc_pos=original_chunk.doc_pos if original_chunk else 0,
                section_type=original_chunk.section_type if original_chunk else None,
            ))

        # 4. Re-sort based on adjusted scores
        ranked_chunks.sort(key=lambda x: x.score, reverse=True)

        return ranked_chunks[:top_k] if top_k else ranked_chunks

    def rank_nacevo(self, query: str, nodes: List[NaceVo], top_k: Optional[int] = None) -> List[RankedNaceVo]:
        """Rerank a list of ``NaceVo`` objects against *query* using FlashRank.

        The node ``title`` is used as the passage text.  Unlike
        :meth:`rank_chunkvo`, there is no structural position boost — NACE
        entries have no document-depth semantics, so the model score is used
        directly.

        Args:
            query: Free-text product or activity description to rank against.
            nodes: List of ``NaceVo`` objects of any type to rerank.
            top_k: If given, return only the top *k* results.

        Returns:
            A list of :class:`~pipelines.nace.nace_tree.RankedNaceVo` objects
            sorted descending by score so ``result[0]`` is the best match.
        """
        if not nodes:
            return []

        # 1. Format for FlashRank — use code as id, title as passage text
        passages = [
            {"id": vo.code, "text": vo.title, "meta": {"type": vo.type}}
            for vo in nodes
        ]

        # 2. Execute ranking
        request = RerankRequest(query=query, passages=passages)
        results = self.ranker.rerank(request)

        # 3. Build lookup by code for O(1) access
        node_map = {vo.code: vo for vo in nodes}

        ranked = []
        for res in results:
            base_score: float = float(res["score"])
            original_vo = node_map.get(str(res["id"]))
            if original_vo is None:
                logger.warning("rank_nacevo: no NaceVo found for code '%s'", res["id"])
                continue
            ranked.append(RankedNaceVo(vo=original_vo, score=base_score, base_score=base_score))

        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked[:top_k] if top_k else ranked


        """
        The "Wins" in this DesignThe Power Curve ($depth^8$): This is a very clever move. By using an exponent of 8, you are creating a "climax" effect. Chunks at 50% or even 80% depth receive almost no boost, while the final 5-10% (the "Order" or "Judgment") receives a massive spike. This effectively filters out the "Facts" section noise.The 0.98 Cap: This is highly sophisticated domain knowledge in action. In legal docs, the last few paragraphs are often boilerplate (service of process, signatures, notary stamps). By capping the boost, you ensure the Holding (usually just before the signatures) wins.Decoupled Score Tracking: Including base_score in your RankedChunkVo is excellent for observability. When a result looks weird, you can instantly see if it was because the model loved it or because your boost "forced" it to the top.Provisioner Pattern: Good use of dependency injection and global settings.Points for Refinement1. The "Global Max" vs. "Parent Max"Your code calculates max_index = max((c.chunk_index for c in chunks)).The Risk: If your retrieval results only contain chunks from the beginning of a document (e.g., indices 1 through 10), max_index will be 10. Chunk #10 will then get the full "end of document" boost ($10/10 = 1.0$), even though it is actually from the start of the filing.The Fix: Ideally, total_chunks should be an attribute of the ChunkVo itself (calculated at ingestion). If that's not possible, your current approach is a fine "heuristic," but be aware it only works if your retrieval is broad enough to catch chunks from various parts of the document.2. The Multiplier Magnitude (15.0)With a multiplier of 15.0 and MS-Marco scores being 0-1:Your structural boost is now the primary signal, and the semantic model is effectively just a "secondary filter."This is fine if you trust your document structure more than the embedding model, but it might "bury" a very relevant fact from the middle of the document if a conclusion exists.3. Handling Multiple OriginsIf chunks contains pieces from Document A (10 chunks) and Document B (500 chunks), your max_index logic treats them globally.Scenario: Document A, Chunk 10 will get a boost of 15.0. Document B, Chunk 10 will get a boost of almost 0.The Better Way: Group by origin and calculate the depth relative to the source document's own size.

        # Group by origin to find the local max for each document
origins = {c.origin for c in chunks}
max_indices = {org: max(c.chunk_index for c in chunks if c.origin == org) for org in origins}

for res in results:
    # ... inside loop ...
    local_max = max_indices.get(original_chunk.origin, 1)
    depth = original_chunk.chunk_index / max(1, local_max)
    # ... rest of your logic ...
        """