import logging
from typing import List, Optional

from config.settings import settings
from extract.rag.models.chunk_vo import RankedChunkVo
from extract.rag.ranker.ranker_factory import RankerProtocol, create_ranker
from pipelines.agent_dspy.dspy_protocols import ChunkRetriever

logger = logging.getLogger(__name__)


class KeywordSearch:
    """
    Keyword-only fallback strategy: Postgres full-text search over a single
    CELEX document, with an accent-insensitive trigram fallback for surface
    variants the FTS leg misses.
    """

    def __init__(self, repo: ChunkRetriever, ranker: Optional[RankerProtocol] = None):
        """
        Initialize KeywordSearch with a chunk repository and a reranker.

        Parameters
        ----------
        repo:
            Data-access object providing FTS and trigram chunk retrieval.
        ranker:
            Cross-encoder reranker to inject (Provisioner pattern). When omitted,
            one is built via ``create_ranker()``; callers that already hold a
            shared ranker should pass it so the heavy model is not loaded twice.
        """
        self.repo = repo
        self.ranker = ranker or create_ranker()

    def search(
        self,
        celex_id: str,
        query: str,
        top_k: Optional[int] = None,
        section_types: Optional[List[str]] = None,
    ) -> List[RankedChunkVo]:
        """
        Execute lexical search over a CELEX document and return ranked chunks.

        The FTS leg (``websearch_to_tsquery``) runs on the raw question — no
        client-side keyword extraction. AND semantics is used (``use_or=False``):
        as a boolean "does this document mention X" existence check, every
        salient lexeme must be present, so a chunk is not matched merely because
        it shares a ubiquitous word like "case". When FTS returns nothing (a
        vocabulary or diacritic mismatch), an accent-folded trigram fallback
        runs so that variant spellings still surface. Results are reranked by
        the cross-encoder.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier to scope the search.
        query:
            Natural-language query, used for both retrieval and reranking.
        top_k:
            Maximum number of results to return. None returns all ranked results.
        section_types:
            Optional decision-section filter applied to the FTS leg
            (``section_scope`` fact-config key); None searches all sections.

        Returns
        -------
        List[RankedChunkVo]:
            Ranked list of matching chunks, empty if no matches found.
        """
        logger.debug(
            f"Starting keyword search for CELEX: '{celex_id}' with query '{query}'"
        )

        chunks = self.repo.websearch_search_in_celex(
            celex_id=celex_id,
            query=query,
            hits=settings.HYBRID_FTS_HITS,
            use_or=False,
            section_types=section_types,
        )
        logger.debug(f"FTS keyword search yielded {len(chunks)} chunks.")

        if not chunks:
            chunks = self.repo.trigram_search(
                celex_id=celex_id, term=query, hits=settings.HYBRID_FTS_HITS
            )
            logger.debug(f"Trigram fallback yielded {len(chunks)} chunks.")

        if not chunks:
            return []

        return self.ranker.rank_chunkvo(query, chunks, top_k=top_k)
