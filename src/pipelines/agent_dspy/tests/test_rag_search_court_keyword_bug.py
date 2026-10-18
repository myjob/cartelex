"""Regression: court-evidence retrieval survives the FTS lexical-leg migration.

History
-------
celex_id ``38344_5856_3`` contains many occurrences of "court" in its document
chunks, yet ``is_court_involved`` extraction returned zero hits.  The original
cause was ``UniversalFactModule`` passing ``keyword=target_fact_name``
(``"is_court_involved"``) to an ILIKE leg that matched nothing, plus a
single-word gate that suppressed the keyword pass entirely.

Current design
--------------
The lexical leg is now Postgres full-text search fed the **raw question** (no
keyword extraction, no plural stemming — ``websearch_to_tsquery`` stems on both
sides). These tests assert the module forwards the raw question and that the
FTS leg runs regardless of query word count.
"""

import unittest
from unittest.mock import MagicMock

from extract.rag.hybrid_search import HybridSearch
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO


def _make_is_court_params(celex_id: str = "38344_5856_3") -> FactExtractorParamVO:
    """Build the exact params for the ``is_court_involved`` fact."""
    return FactExtractorParamVO(
        target_fact_name="is_court_involved",
        question=(
            "Is this case involves any court with assessment, order, ruling, "
            "decision or judgement?\n"
            "Please note, the European Commission is not a court.\n"
            "# Constraint: reply with 'True' or 'False' or 'NA' in no info is provided ."
        ),
        celex_id=celex_id,
        output_schema="",
        pipeline="universal",
        force_override=True,
    )


class TestUniversalFactModuleForwardsRawQuestion(unittest.TestCase):
    """UniversalFactModule must pass the raw question, no ``keyword`` argument."""

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_sql_store.select_fact4celex.return_value = []

        self.mock_searcher = MagicMock()
        self.captured: dict = {}

        def _capture_search(celex_id, query, top_k=None, section_types=None):
            self.captured = {"celex_id": celex_id, "query": query, "top_k": top_k}
            return [
                RankedChunkVo(
                    id="c-42",
                    celex_id=celex_id,
                    chunk="The court ruled the cartel agreement was in violation.",
                    origin="pgvector_fts",
                    similarity=0.4,
                    doc_pos=42,
                    score=0.9,
                    base_score=0.9,
                )
            ]

        self.mock_searcher.search.side_effect = _capture_search

    def test_raw_question_forwarded_without_keyword(self):
        from pipelines.agent_dspy.universal_modules import UniversalFactModule

        module = UniversalFactModule(
            sql_store=self.mock_sql_store, searcher=self.mock_searcher
        )

        fake_output = MagicMock()
        fake_output.extracted_facts = [{"is_court_involved": "True"}]
        module.extract = MagicMock(return_value=MagicMock(output=fake_output))
        fake_refs = MagicMock()
        fake_refs.output.doc_positions = [42]
        module.find_doc_references = MagicMock(return_value=fake_refs)

        params = _make_is_court_params()
        module(params=params)

        self.assertEqual(self.captured.get("query"), params.question)
        # The searcher must not be invoked with a legacy ``keyword`` argument.
        _, kwargs = self.mock_searcher.search.call_args
        self.assertNotIn("keyword", kwargs)


class TestHybridSearchFtsLegOnRawQuery(unittest.TestCase):
    """The FTS lexical leg runs on the raw query regardless of word count."""

    def _build(self, fts_return):
        repo = MagicMock()
        self.calls: dict = {}

        def _ws(celex_id, query, hits=40, section_types=None):
            self.calls["fts_query"] = query
            return fts_return

        repo.websearch_search_in_celex.side_effect = _ws
        repo.semantic_search_in_celex.return_value = []

        ranker = MagicMock()
        ranker.rank_chunkvo.side_effect = lambda q, chunks, top_k=None: [
            RankedChunkVo(**c.model_dump(), score=0.9, base_score=0.9)
            for c in (chunks[:top_k] if top_k else chunks)
        ]
        return HybridSearch(repo=repo, ranker=ranker, embed_query=lambda q: [0.0])

    def test_single_word_query_runs_fts(self):
        court = [
            ChunkVo(
                id="kw-1",
                celex_id="X",
                chunk="The local German labour court confirmed the arrangement.",
                origin="pgvector_fts",
                similarity=0.3,
                doc_pos=42,
            )
        ]
        search = self._build(court)
        results = search.search(celex_id="X", query="court", top_k=5)
        self.assertEqual(self.calls["fts_query"], "court")
        self.assertGreater(len(results), 0)

    def test_multi_word_query_runs_fts(self):
        court = [
            ChunkVo(
                id="kw-1",
                celex_id="X",
                chunk="The court ruled the cartel agreement violated competition law.",
                origin="pgvector_fts",
                similarity=0.3,
                doc_pos=60,
            )
        ]
        search = self._build(court)
        results = search.search(
            celex_id="X", query="Is this case involves any court?", top_k=5
        )
        self.assertEqual(self.calls["fts_query"], "Is this case involves any court?")
        self.assertGreater(len(results), 0)


if __name__ == "__main__":
    unittest.main()
