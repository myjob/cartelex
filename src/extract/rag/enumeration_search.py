"""Enumeration retrieval strategy — sibling to :class:`HybridSearch`.

The Q&A path (``HybridSearch``) is precision@k-tuned: it returns a small,
top-ranked slice for "answer this question." The enumeration path is the
opposite — it is recall-tuned for "list every X mentioned in the document."

Key design differences vs ``HybridSearch``:

* **BM25 leg, not ILIKE.** Lexical recall over chunk text uses the
  ``document_tsv`` GIN index (``ChunkRetriever.fulltext_search_in_celex``)
  rather than ``ILIKE %keyword%``. BM25 surfaces multiple lexemes from
  the question itself; no separate keyword argument is needed from the
  caller.
* **Position boost OFF.** Rare entities are often named once in the fact
  recitals (early in the document, ``depth ≈ 0``), so the Legal Climax
  boost would penalise them. ``FlashRanker.rank_chunkvo`` is called with
  ``apply_position_boost=False`` — the structural prior is wrong here.
* **No ``top_k`` truncation.** The downstream module maps an extraction
  step over **every** ranked candidate; truncating discards exactly the
  rare-mention chunks we are trying to recover. Recall is bounded by
  chunk coverage, not by ``k``.
* **No keyword-reserve logic.** Exact-string hits are already first-class
  in the BM25 leg; no rescue pass is needed.
"""

import logging
from collections.abc import Callable
from typing import List, Optional

from config.settings import settings
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from extract.rag.ranker.ranker_factory import RankerProtocol
from pipelines.agent_dspy.dspy_protocols import ChunkRetriever

logger = logging.getLogger(__name__)


class EnumerationSearch:
    """Hybrid dense + BM25 retrieval for enumeration / exhaustive-extraction queries.

    Parameters
    ----------
    repo:
        ``ChunkRetriever`` providing ``semantic_search_in_celex`` and
        ``fulltext_search_in_celex``.
    ranker:
        Shared ``FlashRanker`` instance. Used here with the position boost
        explicitly disabled (see module docstring).
    embed_query:
        Callable mapping a query string to its embedding vector. The embedding
        concern is kept external to the repository (Provisioner pattern).
    """

    def __init__(
        self,
        repo: ChunkRetriever,
        ranker: RankerProtocol,
        embed_query: Callable[[str], list],
    ):
        self.repo = repo
        self.ranker = ranker
        self.embed_query = embed_query

    def search(
        self,
        celex_id: str,
        query: str,
        section_types: Optional[List[str]] = None,
    ) -> List[RankedChunkVo]:
        """Retrieve a wide, deduplicated, unranked-truncation candidate set.

        Args:
            celex_id: Scope the retrieval to a single CELEX document.
            query: The natural-language question driving the enumeration.
            section_types: Optional decision-section filter applied to both
                legs (``section_scope`` fact-config key); None searches all
                sections.

        Returns:
            All candidate chunks from the dense ∪ BM25 union, ordered by
            ``FlashRanker`` score with the position boost disabled. The
            caller is expected to map an extraction step over the entire
            returned list.
        """
        logger.debug(
            "EnumerationSearch starting for celex=%s query=%r", celex_id, query
        )

        query_embedding = self.embed_query(query)
        dense_chunks = self.repo.semantic_search_in_celex(
            celex_id=celex_id,
            query_embedding=query_embedding,
            hits=settings.ENUMERATION_DENSE_HITS,
            section_types=section_types,
        )
        logger.debug("dense leg yielded %d chunks", len(dense_chunks))

        bm25_chunks = self.repo.fulltext_search_in_celex(
            celex_id=celex_id,
            query=query,
            hits=settings.ENUMERATION_BM25_HITS,
            section_types=section_types,
        )
        logger.debug("BM25 leg yielded %d chunks", len(bm25_chunks))

        unique_chunks = self._deduplicate_by_id(dense_chunks, bm25_chunks)
        logger.debug("after dedup: %d unique chunks", len(unique_chunks))

        ranked = self.ranker.rank_chunkvo(
            query=query,
            chunks=unique_chunks,
            top_k=None,
            apply_position_boost=False,
        )
        logger.debug(
            "EnumerationSearch finalized with %d ranked output chunks", len(ranked)
        )
        return ranked

    @staticmethod
    def _deduplicate_by_id(*chunk_lists: List[ChunkVo]) -> List[ChunkVo]:
        """Union the input lists, preserving first-seen order, keyed by ``id``."""
        seen: set[str] = set()
        deduped: list[ChunkVo] = []
        for chunk_list in chunk_lists:
            for chunk in chunk_list:
                if chunk.id in seen:
                    continue
                seen.add(chunk.id)
                deduped.append(chunk)
        return deduped
