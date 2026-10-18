"""Tests for the fact-name fallback in QAExecutorModule.

Validates that when all plan steps return empty evidence and the user's
question mentions a known fact name (e.g. ``court_list_raw``), the
executor falls back to a direct SQL query against the facts table.

This is the fix for the "no evidence" bug where cross-case general queries
about specific fact names returned no data.
"""

import unittest
from unittest.mock import MagicMock

from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep
from pipelines.agent_dspy.retrieval_modules import (
    QAExecutorModule,
)
from pipelines.agent_dspy.retrieval_text import (
    _resolve_topic_synonyms,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(*steps: RetrievalStep, qtype: str = "multi_case_aggregation") -> RetrievalPlan:
    return RetrievalPlan(question_type=qtype, steps=list(steps))


def _kb_sql_step(query: str) -> RetrievalStep:
    return RetrievalStep(
        step_id=1,
        tool="kb_sql",
        query=query,
        rationale="Cross-case SQL aggregation.",
    )


def _doc_search_step(query: str) -> RetrievalStep:
    return RetrievalStep(
        step_id=2,
        tool="doc_search",
        query=query,
        rationale="Document search.",
    )


class _FakeAggregator:
    """Aggregator that tracks calls and returns configurable results."""

    def __init__(self, results_by_sql: dict[str, list[dict]] | None = None):
        self._results = results_by_sql or {}
        self.calls: list[str] = []

    def aggregate_facts(self, sql: str) -> list[dict]:
        self.calls.append(sql)
        for pattern, rows in self._results.items():
            if pattern in sql:
                return rows
        return []


# ---------------------------------------------------------------------------
# Topic Synonym Tests — court_list_raw must be resolvable
# ---------------------------------------------------------------------------


class TestCourtTopicSynonyms(unittest.TestCase):
    """Verify that court-related terms resolve to the correct fact names.

    DESIGN NOTE: Bare keywords like 'court', 'courts', and 'jurisdiction'
    are deliberately NOT mapped to ``court_list_raw`` because they cause
    false passthrough / synonym-fallback matches when the user asks about
    court *rulings* or *assessments* (which need document-chunk search).
    Only EXPLICIT fact names and multi-word listing phrases resolve.
    """

    def test_court_list_raw_exact(self):
        """The literal string 'court_list_raw' should resolve."""
        resolved = _resolve_topic_synonyms("list from fact table court_list_raw")
        self.assertIn("court_list_raw", resolved)

    def test_court_list_space_separated(self):
        """'court list raw' (with spaces) should also resolve."""
        resolved = _resolve_topic_synonyms("show me the court list raw data")
        self.assertIn("court_list_raw", resolved)

    def test_list_courts_resolves(self):
        """'list courts' (a listing command) should map to court_list_raw."""
        resolved = _resolve_topic_synonyms("list courts for this case")
        self.assertIn("court_list_raw", resolved)

    def test_list_of_courts_resolves(self):
        """'list of courts' should map to court_list_raw."""
        resolved = _resolve_topic_synonyms("show the list of courts")
        self.assertIn("court_list_raw", resolved)

    def test_bare_court_does_not_resolve(self):
        """Bare 'court' should NOT resolve to court_list_raw — prevents
        false matches on 'what was the court ruling'."""
        resolved = _resolve_topic_synonyms("which court was involved?")
        self.assertNotIn("court_list_raw", resolved)

    def test_bare_courts_does_not_resolve(self):
        """Bare 'courts' should NOT resolve to court_list_raw."""
        resolved = _resolve_topic_synonyms("how many courts participated?")
        self.assertNotIn("court_list_raw", resolved)

    def test_ruling_does_not_resolve_to_court_fact(self):
        """'ruling' should NOT resolve to court_list_raw — it needs doc search."""
        resolved = _resolve_topic_synonyms("what was the ruling of courts?")
        self.assertNotIn("court_list_raw", resolved)

    def test_court_list_exact(self):
        """'court_list' should resolve to court_list."""
        resolved = _resolve_topic_synonyms("show court_list data")
        self.assertIn("court_list", resolved)

    def test_court_involvement_resolves(self):
        """'court involvement' should resolve to is_court_involved."""
        resolved = _resolve_topic_synonyms("was there court involvement?")
        self.assertIn("is_court_involved", resolved)


# ---------------------------------------------------------------------------
# Fact-Name Fallback Tests
# ---------------------------------------------------------------------------


class TestFactNameFallback(unittest.TestCase):
    """Test that QAExecutorModule._fact_name_fallback works correctly."""

    def _make_executor(self, aggregator: _FakeAggregator) -> QAExecutorModule:
        """Build an executor with a fake aggregator and stubs for other deps."""
        sql_store = MagicMock()
        sql_store.select_facts4celex.return_value = {}
        kb_searcher = MagicMock()
        hybrid_searcher = MagicMock()
        doc_searcher = MagicMock()
        doc_searcher.keyword_search_chunks_all_cases.return_value = []
        doc_searcher.semantic_search_chunks.return_value = []
        doc_searcher.keyword_count_chunks_all_cases.return_value = []

        return QAExecutorModule(
            sql_store=sql_store,
            kb_searcher=kb_searcher,
            aggregator=aggregator,
            hybrid_searcher=hybrid_searcher,
            doc_searcher=doc_searcher,
        )

    def test_fallback_fires_on_explicit_fact_name(self):
        """When the question contains 'court_list_raw', the fallback should
        issue a direct SQL query for that fact name."""
        aggregator = _FakeAggregator(results_by_sql={
            "court_list_raw": [
                {"celex_id": "39226_8310_11", "value_md": "| Court | Country |\n|---|---|\n| High Court | UK |"},
            ],
        })
        executor = self._make_executor(aggregator)

        question = "list from fact table court_list_raw with | court | country | jurisdiction |"
        result = executor._fact_name_fallback(question=question)

        self.assertIn("court_list_raw", result.text)
        self.assertIn("Fallback (fact_name", result.text)
        self.assertIn("High Court", result.text)
        # The queried fact name is carried as a typed edge.
        self.assertEqual(result.fact_names, ["court_list_raw"])

    def test_fallback_captures_fact_ids_when_projected(self):
        """When the matched rows carry an ``id`` column, it becomes a fact edge."""
        aggregator = _FakeAggregator(results_by_sql={
            "court_list_raw": [
                {
                    "id": "fact-uuid-court",
                    "celex_id": "39226_8310_11",
                    "value_md": "| Court | UK |",
                },
            ],
        })
        executor = self._make_executor(aggregator)

        result = executor._fact_name_fallback(
            question="list from fact table court_list_raw"
        )
        self.assertEqual(result.fact_ids, ["fact-uuid-court"])
        self.assertEqual(result.fact_names, ["court_list_raw"])

    def test_fallback_skips_when_no_fact_name(self):
        """Questions without a recognizable fact name produce empty result."""
        aggregator = _FakeAggregator()
        executor = self._make_executor(aggregator)

        result = executor._fact_name_fallback(question="hello world")
        self.assertEqual(result.text, "")

    def test_fallback_returns_empty_when_no_db_rows(self):
        """Even with a detected fact name, if DB returns nothing, result is empty."""
        aggregator = _FakeAggregator(results_by_sql={})
        executor = self._make_executor(aggregator)

        result = executor._fact_name_fallback(
            question="list from fact table court_list_raw"
        )
        self.assertEqual(result.text, "")

    def test_fallback_integrated_in_forward(self):
        """The full forward() method should trigger fact-name fallback
        when all plan steps return empty."""
        aggregator = _FakeAggregator(results_by_sql={
            "court_list_raw": [
                {"celex_id": "CASE1", "value_md": "| Court | Country |\n|---|---|\n| ECJ | EU |"},
            ],
        })
        executor = self._make_executor(aggregator)

        # A plan with a kb_sql step that returns no rows (LLM generated wrong SQL)
        plan = _make_plan(
            _kb_sql_step("SELECT value_md FROM celex_facts.facts WHERE name = 'wrong_name'"),
        )

        pred = executor.forward(plan=plan, question="list from fact table court_list_raw")
        evidence = pred.evidence

        # Should contain the fallback evidence with court data
        self.assertIn("court_list_raw", evidence)
        self.assertIn("ECJ", evidence)


# ---------------------------------------------------------------------------
# KB Schema Description Tests
# ---------------------------------------------------------------------------


class TestKBSchemaContainsFacts(unittest.TestCase):
    """Verify the KB schema description lists all critical fact names."""

    def test_court_list_raw_in_schema(self):
        from pipelines.agent_dspy.retrieval_dispatch import KB_SCHEMA_DESCRIPTION
        self.assertIn("court_list_raw", KB_SCHEMA_DESCRIPTION)

    def test_court_list_in_schema(self):
        from pipelines.agent_dspy.retrieval_dispatch import KB_SCHEMA_DESCRIPTION
        self.assertIn("court_list", KB_SCHEMA_DESCRIPTION)

    def test_is_court_involved_in_schema(self):
        from pipelines.agent_dspy.retrieval_dispatch import KB_SCHEMA_DESCRIPTION
        self.assertIn("is_court_involved", KB_SCHEMA_DESCRIPTION)

    def test_infringement_gravity_in_schema(self):
        from pipelines.agent_dspy.retrieval_dispatch import KB_SCHEMA_DESCRIPTION
        self.assertIn("infringement_gravity", KB_SCHEMA_DESCRIPTION)

    def test_law_article_in_schema(self):
        from pipelines.agent_dspy.retrieval_dispatch import KB_SCHEMA_DESCRIPTION
        self.assertIn("law_article_all_relevant", KB_SCHEMA_DESCRIPTION)

    def test_company_recidivism_in_schema(self):
        from pipelines.agent_dspy.retrieval_dispatch import KB_SCHEMA_DESCRIPTION
        self.assertIn("company_recidivism", KB_SCHEMA_DESCRIPTION)

    def test_court_list_raw_sql_example(self):
        from pipelines.agent_dspy.retrieval_dispatch import KB_SCHEMA_DESCRIPTION
        self.assertIn("name = 'court_list_raw'", KB_SCHEMA_DESCRIPTION)


if __name__ == "__main__":
    unittest.main()
