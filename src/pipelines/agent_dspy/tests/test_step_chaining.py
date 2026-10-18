"""Unit tests for multi-hop step chaining in the Q&A retrieval pipeline.

Covers:
  1. ``_stem_search_terms`` — deterministic ILIKE stem relaxation.
  2. ``EvidenceResult.celex_ids`` — populated by every dispatcher tool.
  3. ``QAExecutorModule`` chaining — {celex_ids} substitution, WHERE wrapping,
     per-case fan-out with cap, upstream-empty skip, and ILIKE stem retry.

Uses unittest with MagicMock — no live DB or LLM required.
"""

import unittest
from unittest.mock import MagicMock

from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep
from pipelines.agent_dspy.retrieval_dispatch import _ToolDispatcher
from pipelines.agent_dspy.retrieval_modules import (
    _MAX_CHAINED_CASES,
    QAExecutorModule,
)
from pipelines.agent_dspy.retrieval_text import (
    _is_empty_evidence,
    _stem_search_terms,
)


class TestStemSearchTerms(unittest.TestCase):
    """Deterministic suffix stripping for ILIKE vocabulary mismatches."""

    def test_brewery_stems_to_brew(self):
        self.assertEqual(_stem_search_terms("brewery"), ["brew"])

    def test_breweries_yields_progressively_shorter_stems(self):
        stems = _stem_search_terms("breweries")
        self.assertIn("brew", stems)
        # Longest (least aggressive) stem comes first.
        self.assertEqual(stems, sorted(stems, key=len, reverse=True))

    def test_plural_stripped(self):
        self.assertEqual(_stem_search_terms("cartels"), ["cartel"])

    def test_short_word_produces_no_stem(self):
        # 'beer' -> 'be' falls below the minimum stem length.
        self.assertEqual(_stem_search_terms("beer"), [])

    def test_word_without_known_suffix(self):
        self.assertEqual(_stem_search_terms("brew"), [])

    def test_empty_term(self):
        self.assertEqual(_stem_search_terms(""), [])


class _DispatcherTestBase(unittest.TestCase):
    """Shared mocked dispatcher provisioning."""

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_kb_searcher = MagicMock()
        self.mock_aggregator = MagicMock()
        self.mock_hybrid_searcher = MagicMock()
        self.mock_doc_searcher = MagicMock()
        self.mock_knowledge_searcher = MagicMock()

        # Default: no case_title rows resolve unless a test wires them up.
        self.mock_sql_store.select_fact4celex.return_value = []

        self.dispatcher = _ToolDispatcher(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
            knowledge_searcher=self.mock_knowledge_searcher,
        )


class TestEvidenceResultCelexIds(_DispatcherTestBase):
    """Every tool surfaces the celex_ids it retrieved as typed edges."""

    def test_kb_sql_collects_deduplicated_celex_ids(self):
        self.mock_aggregator.aggregate_facts.return_value = [
            {"celex_id": "CELEX-31971D0257", "value_md": "brewing"},
            {"celex_id": "37750_87_1", "value_md": "brewing"},
            {"celex_id": "CELEX-31971D0257", "value_md": "duplicate"},
        ]
        step = RetrievalStep(
            step_id=1,
            tool="kb_sql",
            query="SELECT celex_id, value_md FROM celex_facts.facts",
            rationale="finder",
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertEqual(
            evidence.celex_ids, ["CELEX-31971D0257", "37750_87_1"]
        )

    def test_case_facts_carries_its_case_id(self):
        self.mock_sql_store.select_facts4celex_with_ids.return_value = [
            ("uuid-1", "cartel_period", "1990-01-01 : 1999-12-31"),
        ]
        step = RetrievalStep(
            step_id=1,
            tool="case_facts",
            query="",
            case_id="37766_610_5",
            rationale="lookup",
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertEqual(evidence.celex_ids, ["37766_610_5"])

    def test_rag_search_collects_celex_ids(self):
        result = MagicMock()
        result.id = "fact-uuid-1"
        result.celex_id = "CELEX-32002D0759"
        result.fact_name = "cartel_period"
        result.similarity = 0.9
        result.value_txt = "1985-10-01 : 2000-02-16"
        self.mock_kb_searcher.semantic_search_facts.return_value = [result]

        step = RetrievalStep(
            step_id=1, tool="rag_search", query="cartel periods", rationale="t"
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertEqual(evidence.celex_ids, ["CELEX-32002D0759"])

    def test_doc_search_merges_chunk_and_count_celex_ids(self):
        chunk = MagicMock()
        chunk.id = "chunk-1"
        chunk.celex_id = "CELEX-31971D0257"
        chunk.doc_pos = 3
        chunk.origin = "sql_keyword"
        chunk.chunk = "brewing industry agreement"
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = [chunk]
        self.mock_doc_searcher.semantic_search_chunks.return_value = []
        self.mock_doc_searcher.keyword_count_chunks_all_cases.return_value = [
            {"celex_id": "CELEX-31971D0257", "count": 4},
            {"celex_id": "CELEX-32000D0484", "count": 1},
        ]

        step = RetrievalStep(
            step_id=1, tool="doc_search", query="brew", rationale="t"
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertEqual(
            evidence.celex_ids, ["CELEX-31971D0257", "CELEX-32000D0484"]
        )

    def test_knowledge_search_collects_source_celex_ids(self):
        item = MagicMock()
        item.id = "k-1"
        item.knowledge_type = "procedure"
        item.scope = "topic"
        item.knowledge_text = "Brewery cartels were fined."
        item.confidence = 0.8
        self.mock_knowledge_searcher.search_knowledge_fulltext.return_value = [item]
        self.mock_knowledge_searcher.get_knowledge_sources.return_value = [
            {
                "source_kind": "fact",
                "fact_name": "fines",
                "celex_id": "CELEX-32002D0759",
                "chunk_id": None,
                "doc_pos": None,
            }
        ]

        step = RetrievalStep(
            step_id=1, tool="knowledge_search", query="brewery fines", rationale="t"
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertEqual(evidence.celex_ids, ["CELEX-32002D0759"])


class _ExecutorTestBase(unittest.TestCase):
    """Shared mocked executor provisioning."""

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_kb_searcher = MagicMock()
        self.mock_aggregator = MagicMock()
        self.mock_hybrid_searcher = MagicMock()
        self.mock_doc_searcher = MagicMock()

        self.mock_sql_store.select_fact4celex.return_value = []

        self.executor = QAExecutorModule(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )

    @staticmethod
    def _finder_step(step_id: int = 1) -> RetrievalStep:
        return RetrievalStep(
            step_id=step_id,
            tool="kb_sql",
            query=(
                "SELECT DISTINCT celex_id FROM celex_facts.facts "
                "WHERE name = 'companies_industry' AND value_md ILIKE '%brew%'"
            ),
            rationale="find brewery cases",
        )


class TestExecutorChaining(_ExecutorTestBase):
    """Chained steps consume the upstream step's celex_ids."""

    def test_placeholder_substitution_with_sql_escaping(self):
        finder_rows = [
            {"celex_id": "CELEX-31971D0257"},
            {"celex_id": "O'Brien_1_1"},
        ]
        dependent_rows = [
            {"celex_id": "CELEX-31971D0257", "value_md": "1969-01-01 : 1971-07-18"}
        ]
        self.mock_aggregator.aggregate_facts.side_effect = [
            finder_rows,
            dependent_rows,
        ]

        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                self._finder_step(),
                RetrievalStep(
                    step_id=2,
                    tool="kb_sql",
                    query=(
                        "SELECT celex_id, value_md FROM celex_facts.facts "
                        "WHERE name = 'cartel_period' "
                        "AND celex_id IN ({celex_ids})"
                    ),
                    input_from_step=1,
                    rationale="fetch periods for found cases",
                ),
            ],
        )
        pred = self.executor(plan=plan)

        second_sql = self.mock_aggregator.aggregate_facts.call_args_list[1].args[0]
        self.assertNotIn("{celex_ids}", second_sql)
        self.assertIn("'CELEX-31971D0257'", second_sql)
        self.assertIn("'O''Brien_1_1'", second_sql)
        self.assertIn("1969-01-01 : 1971-07-18", pred.evidence)

    def test_missing_placeholder_wraps_query(self):
        finder_rows = [{"celex_id": "A_1_1"}, {"celex_id": "B_2_2"}]
        self.mock_aggregator.aggregate_facts.side_effect = [
            finder_rows,
            [{"celex_id": "A_1_1", "value_md": "x"}],
        ]

        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                self._finder_step(),
                RetrievalStep(
                    step_id=2,
                    tool="kb_sql",
                    query=(
                        "SELECT celex_id, value_md FROM celex_facts.facts "
                        "WHERE name = 'cartel_period'"
                    ),
                    input_from_step=1,
                    rationale="planner forgot the placeholder",
                ),
            ],
        )
        self.executor(plan=plan)

        second_sql = self.mock_aggregator.aggregate_facts.call_args_list[1].args[0]
        self.assertIn("SELECT * FROM (", second_sql)
        self.assertIn("chained.celex_id IN ('A_1_1', 'B_2_2')", second_sql)

    def test_case_facts_fan_out_capped(self):
        many_ids = [{"celex_id": f"CASE_{i}_1"} for i in range(_MAX_CHAINED_CASES + 5)]
        self.mock_aggregator.aggregate_facts.return_value = many_ids
        self.mock_sql_store.select_facts4celex_with_ids.return_value = [
            ("uuid-1", "cartel_period", "1990-01-01 : 1999-12-31"),
        ]

        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                self._finder_step(),
                RetrievalStep(
                    step_id=2,
                    tool="case_facts",
                    query="all facts",
                    input_from_step=1,
                    rationale="full bags for found cases",
                ),
            ],
        )
        pred = self.executor(plan=plan)

        self.assertEqual(
            self.mock_sql_store.select_facts4celex_with_ids.call_count,
            _MAX_CHAINED_CASES,
        )
        self.assertIn("additional case(s) omitted", pred.evidence)
        self.assertIn("CASE_0_1", pred.evidence)

    def test_upstream_empty_skips_dependent_step(self):
        # Finder finds nothing; stem retries also come back empty.
        self.mock_aggregator.aggregate_facts.return_value = []

        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                self._finder_step(),
                RetrievalStep(
                    step_id=2,
                    tool="kb_sql",
                    query="SELECT celex_id FROM celex_facts.facts WHERE celex_id IN ({celex_ids})",
                    input_from_step=1,
                    rationale="dependent",
                ),
            ],
        )
        pred = self.executor(plan=plan)

        self.assertIn("Step 2 skipped — upstream step 1", pred.evidence)
        # Only the finder (plus its bounded stem retries) reached the DB.
        for call in self.mock_aggregator.aggregate_facts.call_args_list:
            self.assertNotIn("{celex_ids}", call.args[0])
        # The skip block reads as empty evidence so global fallbacks still fire.
        self.assertTrue(
            _is_empty_evidence("[chained] Step 2 skipped — upstream step 1 returned no case ids.")
        )

    def test_fallback_to_nearest_earlier_step_with_ids(self):
        # Referenced step 1 yields nothing, but step 2 surfaces case ids; the
        # chained step 3 references step 1 and must fall back to step 2's ids.
        self.mock_aggregator.aggregate_facts.side_effect = [
            [],  # step 1 finder ('%brew%' has no shorter stem, so no retry)
            [{"celex_id": "A_1_1", "value_md": "x"}],  # step 3 dependent
        ]
        chunk = MagicMock()
        chunk.id = "chunk-1"
        chunk.celex_id = "A_1_1"
        chunk.doc_pos = 1
        chunk.origin = "sql_keyword"
        chunk.chunk = "brewing"
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = [chunk]
        self.mock_doc_searcher.semantic_search_chunks.return_value = []
        self.mock_doc_searcher.keyword_count_chunks_all_cases.return_value = []

        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                self._finder_step(),
                RetrievalStep(
                    step_id=2, tool="doc_search", query="brew", rationale="finder 2"
                ),
                RetrievalStep(
                    step_id=3,
                    tool="kb_sql",
                    query=(
                        "SELECT celex_id, value_md FROM celex_facts.facts "
                        "WHERE name = 'cartel_period' AND celex_id IN ({celex_ids})"
                    ),
                    input_from_step=1,
                    rationale="dependent",
                ),
            ],
        )
        pred = self.executor(plan=plan)

        final_sql = self.mock_aggregator.aggregate_facts.call_args_list[-1].args[0]
        self.assertIn("'A_1_1'", final_sql)
        self.assertIn("value_md", pred.evidence)


class TestStemRetry(_ExecutorTestBase):
    """Empty ILIKE filters are retried with relaxed stems."""

    def test_brewery_retries_with_brew_stem(self):
        def _aggregate(sql: str) -> list[dict]:
            if "'%brewery%'" in sql:
                return []
            if "'%brew%'" in sql:
                return [{"celex_id": "CELEX-31971D0257", "value_md": "brewing"}]
            return []

        self.mock_aggregator.aggregate_facts.side_effect = _aggregate

        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="kb_sql",
                    query=(
                        "SELECT DISTINCT celex_id FROM celex_facts.facts "
                        "WHERE name = 'companies_industry' "
                        "AND value_md ILIKE '%brewery%'"
                    ),
                    rationale="finder with vocabulary mismatch",
                ),
            ],
        )
        pred = self.executor(plan=plan)

        self.assertIn("[stem-retry '%brewery%' -> '%brew%']", pred.evidence)
        self.assertIn("CELEX-31971D0257", pred.evidence)

    def test_no_ilike_means_no_retry(self):
        self.mock_aggregator.aggregate_facts.return_value = []
        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="kb_sql",
                    query="SELECT celex_id FROM celex_facts.facts WHERE name = 'fines'",
                    rationale="no ilike filter",
                ),
            ],
        )
        self.executor(plan=plan, question="")

        self.assertEqual(self.mock_aggregator.aggregate_facts.call_count, 1)

    def test_stem_retry_feeds_chained_step(self):
        """The recovered finder result must supply celex_ids to a chained step."""

        def _aggregate(sql: str) -> list[dict]:
            if "'%brewery%'" in sql:
                return []
            if "'%brew%'" in sql:
                return [{"celex_id": "CELEX-31971D0257"}]
            if "cartel_period" in sql:
                return [
                    {
                        "celex_id": "CELEX-31971D0257",
                        "value_md": "1969-01-01 : 1971-07-18",
                    }
                ]
            return []

        self.mock_aggregator.aggregate_facts.side_effect = _aggregate

        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="kb_sql",
                    query=(
                        "SELECT DISTINCT celex_id FROM celex_facts.facts "
                        "WHERE name = 'companies_industry' "
                        "AND value_md ILIKE '%brewery%'"
                    ),
                    rationale="finder",
                ),
                RetrievalStep(
                    step_id=2,
                    tool="kb_sql",
                    query=(
                        "SELECT celex_id, value_md FROM celex_facts.facts "
                        "WHERE name = 'cartel_period' "
                        "AND celex_id IN ({celex_ids})"
                    ),
                    input_from_step=1,
                    rationale="dependent",
                ),
            ],
        )
        pred = self.executor(plan=plan)

        self.assertIn("1969-01-01 : 1971-07-18", pred.evidence)


if __name__ == "__main__":
    unittest.main()
