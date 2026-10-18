"""Regression test for the doc_search counting bug.

Bug: A query like "count the occurrence of keyword 'court' in case document
chunks" triggers ``doc_search`` but the synthesizer responds "No evidence of
the keyword 'court' was found" — despite hundreds of matching chunks across
many cases.

Root cause: ``_run_doc_search`` deduplicates results by ``celex_id`` (keeping
only 1 chunk per case), and ``keyword_search_chunks_all_cases`` uses
``DISTINCT ON (celex_id)`` in SQL.  Both layers discard per-case chunk counts,
so the synthesizer never sees the quantitative data it needs.

Fix: ``_run_doc_search`` now calls ``keyword_count_chunks_all_cases`` to embed
the total hit count and per-case counts into the evidence header, giving the
synthesizer the numbers it needs to answer counting / aggregation questions.
"""

import unittest
from unittest.mock import MagicMock

from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep
from pipelines.agent_dspy.retrieval_dispatch import _ToolDispatcher
from pipelines.agent_dspy.retrieval_modules import QAExecutorModule


def _make_chunk(celex_id: str, doc_pos: int, text: str, origin: str = "sql_keyword"):
    """Create a MagicMock chunk with the given attributes."""
    chunk = MagicMock()
    chunk.id = f"{celex_id}-chunk-{doc_pos}"
    chunk.celex_id = celex_id
    chunk.doc_pos = doc_pos
    chunk.origin = origin
    chunk.chunk = text
    return chunk


class TestDocSearchCountBug(unittest.TestCase):
    """Reproduce and verify the fix for the doc_search counting bug."""

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_kb_searcher = MagicMock()
        self.mock_aggregator = MagicMock()
        self.mock_hybrid_searcher = MagicMock()
        self.mock_doc_searcher = MagicMock()

        # Default: no case_title rows resolve.
        self.mock_sql_store.select_fact4celex.return_value = []

    def _make_dispatcher(self) -> _ToolDispatcher:
        return _ToolDispatcher(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )

    def _make_executor(self) -> QAExecutorModule:
        return QAExecutorModule(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )

    # ------------------------------------------------------------------
    # BUG REPRODUCTION: Before the fix, the count information was lost
    # ------------------------------------------------------------------

    def test_doc_search_includes_keyword_count_in_evidence(self):
        """doc_search evidence must include total keyword hit count and per-case counts.

        The bug: with the old dedup-only logic, a query "count occurrences of
        'court'" returned 1 sample chunk per case with no aggregate numbers.
        The synthesizer had no quantitative data to work with.

        After fix: the evidence header includes the total keyword hit count and
        per-case breakdown, so the synthesizer can answer counting questions.
        """
        # keyword_search returns 1 representative chunk per case (by DB design).
        kw_chunks = [
            _make_chunk("CASE_A", 10, "The court decided on damages."),
            _make_chunk("CASE_B", 5, "The court of justice ruled."),
            _make_chunk("CASE_C", 20, "Appeal to the court was filed."),
        ]
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = kw_chunks
        self.mock_doc_searcher.semantic_search_chunks.return_value = []

        # keyword_count returns the FULL per-case occurrence counts.
        self.mock_doc_searcher.keyword_count_chunks_all_cases.return_value = [
            {"celex_id": "CASE_A", "count": 45},
            {"celex_id": "CASE_B", "count": 30},
            {"celex_id": "CASE_C", "count": 12},
        ]

        step = RetrievalStep(
            step_id=1,
            tool="doc_search",
            query="court",
            rationale="Count keyword occurrences across cases.",
        )

        dispatcher = self._make_dispatcher()
        result = dispatcher.dispatch(step).text

        # Must include the total count.
        self.assertIn("87", result, "Total keyword hit count must be in evidence")
        # Must include per-case breakdown.
        self.assertIn("CASE_A", result)
        self.assertIn("45", result)
        self.assertIn("CASE_B", result)
        self.assertIn("30", result)
        self.assertIn("CASE_C", result)
        self.assertIn("12", result)

    def test_doc_search_count_zero_when_no_keyword_hits(self):
        """When keyword search returns nothing, count should be zero or absent."""
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = []
        self.mock_doc_searcher.semantic_search_chunks.return_value = []
        self.mock_doc_searcher.keyword_count_chunks_all_cases.return_value = []

        step = RetrievalStep(
            step_id=1,
            tool="doc_search",
            query="xyznonexistent",
            rationale="Search for a term that does not exist.",
        )

        dispatcher = self._make_dispatcher()
        result = dispatcher.dispatch(step).text

        self.assertIn("No chunks found", result)

    def test_doc_search_count_with_many_cases(self):
        """Simulate a high-volume keyword like 'court' across many cases."""
        # 50 cases, each with varying chunk counts.
        kw_chunks = [
            _make_chunk(f"CASE_{i:03d}", i, f"The court ruled in case {i}.")
            for i in range(50)
        ]
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = kw_chunks
        self.mock_doc_searcher.semantic_search_chunks.return_value = []

        count_data = [
            {"celex_id": f"CASE_{i:03d}", "count": (i + 1) * 3}
            for i in range(50)
        ]
        total_expected = sum(d["count"] for d in count_data)
        self.mock_doc_searcher.keyword_count_chunks_all_cases.return_value = count_data

        step = RetrievalStep(
            step_id=1,
            tool="doc_search",
            query="court",
            rationale="Count keyword occurrences.",
        )

        dispatcher = self._make_dispatcher()
        result = dispatcher.dispatch(step).text

        # The total count must be reported.
        self.assertIn(str(total_expected), result)
        # At least the first few per-case counts must be present.
        self.assertIn("CASE_000", result)

    def test_executor_passes_count_through_evidence(self):
        """End-to-end executor test: the count data flows into the evidence."""
        kw_chunks = [
            _make_chunk("CASE_A", 1, "Court decided."),
            _make_chunk("CASE_B", 2, "Court of Justice."),
        ]
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = kw_chunks
        self.mock_doc_searcher.semantic_search_chunks.return_value = []
        self.mock_doc_searcher.keyword_count_chunks_all_cases.return_value = [
            {"celex_id": "CASE_A", "count": 100},
            {"celex_id": "CASE_B", "count": 75},
        ]

        plan = RetrievalPlan(
            question_type="exploratory",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="doc_search",
                    query="court",
                    rationale="Count keyword occurrences across document chunks.",
                )
            ],
        )

        executor = self._make_executor()
        pred = executor(plan=plan)

        # Evidence must include the total count and per-case breakdown.
        self.assertIn("175", pred.evidence, "Total count must appear in evidence")
        self.assertIn("CASE_A", pred.evidence)
        self.assertIn("100", pred.evidence)

    def test_doc_search_count_graceful_fallback_on_missing_method(self):
        """If keyword_count_chunks_all_cases raises, doc_search still works."""
        kw_chunks = [
            _make_chunk("CASE_A", 1, "Court decided."),
        ]
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = kw_chunks
        self.mock_doc_searcher.semantic_search_chunks.return_value = []
        # Simulate a searcher that doesn't implement the count method.
        self.mock_doc_searcher.keyword_count_chunks_all_cases.side_effect = AttributeError(
            "keyword_count_chunks_all_cases not found"
        )

        step = RetrievalStep(
            step_id=1,
            tool="doc_search",
            query="court",
            rationale="Count occurrences.",
        )

        dispatcher = self._make_dispatcher()
        result = dispatcher.dispatch(step).text

        # Should still return evidence (graceful degradation).
        self.assertIn("[doc_search]", result)
        self.assertIn("CASE_A", result)


if __name__ == "__main__":
    unittest.main()
