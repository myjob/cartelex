"""Tests for the Direct Fact Passthrough feature.

Tests cover:
  1. Helper functions: _normalise_fact_token, _match_fact_name,
     _looks_like_markdown, _is_direct_fact_request
  2. End-to-end passthrough in QARetrievalPipeline.forward()
  3. Passthrough bypass (falling through to full pipeline) when conditions
     are not met

Uses unittest with MagicMock — no live DB or LLM required.
"""

import unittest
from unittest.mock import MagicMock

import dspy

from pipelines.agent_dspy.retrieval_modules import (
    QARetrievalPipeline,
)
from pipelines.agent_dspy.retrieval_text import (
    _is_direct_fact_request,
    _looks_like_markdown,
    _match_fact_name,
    _normalise_fact_token,
)
from tools.tests.celex_index_fixture import FakeCelexChecker


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


_FakeCelexChecker = FakeCelexChecker


# ---------------------------------------------------------------------------
# _normalise_fact_token
# ---------------------------------------------------------------------------


class TestNormaliseFactToken(unittest.TestCase):
    def test_underscores_preserved(self):
        self.assertEqual(_normalise_fact_token("heideltime_event_agg"), "heideltime_event_agg")

    def test_spaces_collapsed_to_underscores(self):
        self.assertEqual(_normalise_fact_token("heideltime event agg"), "heideltime_event_agg")

    def test_hyphens_collapsed(self):
        self.assertEqual(_normalise_fact_token("heideltime-event-agg"), "heideltime_event_agg")

    def test_mixed_separators(self):
        self.assertEqual(_normalise_fact_token("case summary"), "case_summary")

    def test_case_insensitive(self):
        self.assertEqual(_normalise_fact_token("NACE_group_code"), "nace_group_code")


# ---------------------------------------------------------------------------
# _match_fact_name
# ---------------------------------------------------------------------------


class TestMatchFactName(unittest.TestCase):
    _NAMES = [
        "heideltime_event_agg",
        "heideltime_event",
        "heideltime_raw",
        "fines",
        "case_summary",
        "leniency_applicants",
        "NACE_group_code",
    ]

    def test_exact_underscored_match(self):
        self.assertEqual(
            _match_fact_name(
                "heideltime_event_agg of CELEX-31969D0216", self._NAMES
            ),
            "heideltime_event_agg",
        )

    def test_longest_match_wins(self):
        """'heideltime_event_agg' must win over 'heideltime_event'."""
        result = _match_fact_name(
            "show heideltime_event_agg for case", self._NAMES
        )
        self.assertEqual(result, "heideltime_event_agg")

    def test_spaced_variant_matches(self):
        result = _match_fact_name(
            "heideltime event agg of CELEX-31969D0216", self._NAMES
        )
        self.assertEqual(result, "heideltime_event_agg")

    def test_synonym_fallback(self):
        """'summary' should resolve to 'case_summary' via synonym table."""
        result = _match_fact_name(
            "summary of CELEX-31991D0562", self._NAMES
        )
        self.assertEqual(result, "case_summary")

    def test_summary_verb_phrasings_resolve(self):
        """Verb / alias phrasings must resolve to 'case_summary' too.

        The synonym table keys on the stem 'summar' (plus aliases) so that
        'summarize', 'summarise', and 'tldr' route like the noun 'summary'
        instead of falling through to the planner with no synonym hit.
        """
        for question in (
            "summarize CELEX-31991D0562",
            "summarise this case CELEX-31991D0562",
            "tldr CELEX-31991D0562",
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    _match_fact_name(question, self._NAMES), "case_summary"
                )

    def test_no_match_returns_none(self):
        result = _match_fact_name(
            "what is the weather today", self._NAMES
        )
        self.assertIsNone(result)

    def test_fines_matches(self):
        result = _match_fact_name(
            "fines of CELEX-31969D0216", self._NAMES
        )
        self.assertEqual(result, "fines")


# ---------------------------------------------------------------------------
# _looks_like_markdown
# ---------------------------------------------------------------------------


class TestLooksLikeMarkdown(unittest.TestCase):
    def test_table_detected(self):
        value = "| event | actor | predicate |\n|---|---|---|\n| 2003-01-01 | Acme | colluded |"
        self.assertTrue(_looks_like_markdown(value))

    def test_bullet_list_detected(self):
        value = "Events:\n- First event\n- Second event"
        self.assertTrue(_looks_like_markdown(value))

    def test_header_detected(self):
        value = "Details\n## Section One\nSome text"
        self.assertTrue(_looks_like_markdown(value))

    def test_plain_text_not_markdown(self):
        value = "This is just a plain text value with no formatting."
        self.assertFalse(_looks_like_markdown(value))

    def test_short_scalar(self):
        value = "True"
        self.assertFalse(_looks_like_markdown(value))

    def test_date_value(self):
        value = "2003-12-09"
        self.assertFalse(_looks_like_markdown(value))


# ---------------------------------------------------------------------------
# _is_direct_fact_request
# ---------------------------------------------------------------------------


class TestIsDirectFactRequest(unittest.TestCase):
    def test_simple_fact_request(self):
        self.assertTrue(_is_direct_fact_request("heideltime_event_agg of CELEX-31969D0216"))

    def test_simple_fact_with_show(self):
        self.assertTrue(_is_direct_fact_request("show me fines for 38344_5856_3"))

    def test_aggregation_question_rejected(self):
        self.assertFalse(_is_direct_fact_request("how many leniency cases exist?"))

    def test_trend_question_rejected(self):
        self.assertFalse(_is_direct_fact_request("did leniency increase over time?"))

    def test_comparison_rejected(self):
        self.assertFalse(_is_direct_fact_request("compare fines across all cases"))

    def test_count_rejected(self):
        self.assertFalse(_is_direct_fact_request("count of cases with fines"))

    # Content-seeking keywords — these MUST block passthrough so the
    # pipeline searches document chunks for substantive content instead
    # of returning a structured fact table (e.g. court_list_raw).

    def test_ruling_rejected(self):
        """'ruling' needs doc search, not a court list table."""
        self.assertFalse(_is_direct_fact_request(
            "what was the ruling of courts in CELEX-31982D0123"
        ))

    def test_assessment_rejected(self):
        self.assertFalse(_is_direct_fact_request(
            "what was the assessment of courts in CELEX-31982D0123"
        ))

    def test_what_was_rejected(self):
        self.assertFalse(_is_direct_fact_request(
            "what was the court decision in CELEX-31982D0123"
        ))

    def test_explain_rejected(self):
        self.assertFalse(_is_direct_fact_request(
            "explain the court involvement in 39612_12448_6"
        ))

    def test_judgment_rejected(self):
        self.assertFalse(_is_direct_fact_request(
            "judgment of the general court in CELEX-31982D0123"
        ))

    def test_opinion_rejected(self):
        self.assertFalse(_is_direct_fact_request(
            "advocate general opinion in CELEX-31982D0123"
        ))

    def test_describe_rejected(self):
        self.assertFalse(_is_direct_fact_request(
            "describe the court proceedings in CELEX-31982D0123"
        ))

    def test_simple_list_court_still_passes(self):
        """'list courts' without content-seeking words should still
        allow passthrough for the court_list_raw fact."""
        self.assertTrue(_is_direct_fact_request(
            "court_list_raw of CELEX-31982D0123"
        ))


# ---------------------------------------------------------------------------
# End-to-end passthrough in QARetrievalPipeline
# ---------------------------------------------------------------------------


class TestPipelineDirectPassthrough(unittest.TestCase):
    """Verify QARetrievalPipeline.forward() short-circuits when all
    passthrough conditions are met."""

    def _build_pipeline(self, checker, fact_bag: dict[str, str] | None = None):
        mock_sql_store = MagicMock()
        if fact_bag is not None:
            mock_sql_store.select_facts4celex.return_value = fact_bag
        else:
            mock_sql_store.select_facts4celex.return_value = {}

        pipe = QARetrievalPipeline(
            sql_store=mock_sql_store,
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            celex_checker=checker,
        )
        # Stub downstream modules — they MUST NOT be called if passthrough fires.
        pipe.planner = MagicMock(
            side_effect=AssertionError("Planner must not run on passthrough.")
        )
        pipe.executor = MagicMock(
            side_effect=AssertionError("Executor must not run on passthrough.")
        )
        pipe.synthesizer = MagicMock(
            side_effect=AssertionError("Synthesizer must not run on passthrough.")
        )
        # But we need the dispatcher's sql_store accessible for the passthrough.
        pipe.executor = MagicMock()
        pipe.executor.dispatcher = MagicMock()
        pipe.executor.dispatcher.sql_store = mock_sql_store

        return pipe

    def test_markdown_table_passthrough(self):
        """A markdown-table fact is returned verbatim, skipping synthesis."""
        md_table = (
            "| event | actor | predicate |\n"
            "|---|---|---|\n"
            "| 2003-01-01 | Acme Corp | formed cartel |"
        )
        checker = _FakeCelexChecker({"CELEX-31969D0216": "CELEX-31969D0216"})
        pipe = self._build_pipeline(
            checker,
            fact_bag={"heideltime_event_agg": md_table, "fines": "10M"},
        )
        # Don't stub planner/executor/synthesizer with AssertionError because
        # _build_pipeline already stubs executor. We need to verify they're
        # NOT called via the passthrough path.

        pred = pipe(question="heideltime_event_agg of CELEX-31969D0216")

        self.assertIsNone(pred.plan)
        self.assertEqual(pred.evidence, "")
        self.assertIn("heideltime_event_agg", pred.answer)
        self.assertIn("CELEX-31969D0216", pred.answer)
        self.assertIn(md_table, pred.answer)

    def test_short_scalar_passthrough(self):
        """A short scalar fact (≤200 chars, non-markdown) is also returned directly."""
        checker = _FakeCelexChecker({"CELEX-31969D0216": "CELEX-31969D0216"})
        pipe = self._build_pipeline(
            checker,
            fact_bag={
                "eu_commisssion_decision_date": "2003-12-09",
                "fines": "| co | amt |\n|---|---|\n| X | 1M |",
            },
        )

        pred = pipe(question="eu_commisssion_decision_date of CELEX-31969D0216")

        self.assertIn("2003-12-09", pred.answer)
        self.assertIsNone(pred.plan)

    def test_no_celex_skips_passthrough(self):
        """When no celex_id is present, passthrough cannot fire."""
        checker = _FakeCelexChecker({})
        mock_sql = MagicMock()
        mock_sql.select_facts4celex.return_value = {}

        pipe = QARetrievalPipeline(
            sql_store=mock_sql,
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            celex_checker=checker,
        )
        # Stub planner to return a trivial plan so we can verify it runs.
        from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep

        trivial_plan = RetrievalPlan(
            question_type="exploratory",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="rag_search",
                    query="general question",
                    rationale="explore",
                )
            ],
        )
        pipe.planner = MagicMock(
            return_value=dspy.Prediction(plan=trivial_plan)
        )
        mock_sql.select_facts4celex.return_value = {}
        pipe.executor = MagicMock(
            return_value=dspy.Prediction(
                evidence="[rag_search] some results",
                fact_ids=[],
                chunk_ids=[],
                fact_names=[],
                celex_ids=[],
            )
        )
        pipe.synthesizer = MagicMock(
            return_value=dspy.Prediction(answer="The answer is 42.")
        )

        pred = pipe(question="how many leniency cases exist?")

        # The planner SHOULD have been called (passthrough did not fire).
        pipe.planner.assert_called_once()
        self.assertIsNotNone(pred.plan)

    def test_aggregation_question_skips_passthrough(self):
        """Complex analytical questions bypass passthrough even with a celex_id."""
        checker = _FakeCelexChecker({"CELEX-31969D0216": "CELEX-31969D0216"})
        mock_sql = MagicMock()
        mock_sql.select_facts4celex.return_value = {
            "fines": "| co | amt |\n|---|---|\n| X | 1M |"
        }

        pipe = QARetrievalPipeline(
            sql_store=mock_sql,
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            celex_checker=checker,
        )
        from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep

        trivial_plan = RetrievalPlan(
            question_type="single_case",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="case_facts",
                    query="fines",
                    case_id="CELEX-31969D0216",
                    rationale="lookup",
                )
            ],
        )
        pipe.planner = MagicMock(
            return_value=dspy.Prediction(plan=trivial_plan)
        )
        pipe.executor = MagicMock(
            return_value=dspy.Prediction(
                evidence="[case_facts] fines: 1M",
                fact_ids=["fines-uuid"],
                chunk_ids=[],
                fact_names=["fines"],
                celex_ids=[],
            )
        )
        pipe.synthesizer = MagicMock(
            return_value=dspy.Prediction(answer="The total fines are 1M.")
        )

        pipe(
            question="how many fines were imposed on CELEX-31969D0216 compared to average?"
        )

        # "how many" + "compared" → not a direct fact request → planner runs.
        pipe.planner.assert_called_once()

    def test_no_matching_fact_name_skips_passthrough(self):
        """When the question doesn't contain a recognisable fact name, passthrough
        does not fire."""
        checker = _FakeCelexChecker({"CELEX-31969D0216": "CELEX-31969D0216"})
        pipe = self._build_pipeline(
            checker,
            fact_bag={
                "fines": "| co | amt |\n|---|---|\n| X | 1M |",
                "case_summary": "Long summary text ...",
            },
        )
        # Override executor to NOT raise (passthrough won't fire, so we need
        # the pipeline to run normally).
        from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep

        trivial_plan = RetrievalPlan(
            question_type="single_case",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="case_facts",
                    query="all facts",
                    case_id="CELEX-31969D0216",
                    rationale="lookup",
                )
            ],
        )
        pipe.planner = MagicMock(
            return_value=dspy.Prediction(plan=trivial_plan)
        )
        pipe.executor = MagicMock(
            return_value=dspy.Prediction(
                evidence="[case_facts] fines: 1M",
                fact_ids=["fines-uuid"],
                chunk_ids=[],
                fact_names=["fines"],
                celex_ids=[],
            )
        )
        pipe.synthesizer = MagicMock(
            return_value=dspy.Prediction(answer="The answer.")
        )

        pipe(question="what happened in CELEX-31969D0216?")

        # "what happened" doesn't match any fact name → full pipeline runs.
        pipe.planner.assert_called_once()


class TestPipelineEnqueue(unittest.TestCase):
    """The injected knowledge-extraction sink fires on the QA (synthesize) path."""

    def _build(self, enqueue_fn):
        checker = _FakeCelexChecker({})
        mock_sql = MagicMock()
        mock_sql.select_facts4celex.return_value = {}
        pipe = QARetrievalPipeline(
            sql_store=mock_sql,
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            celex_checker=checker,
            enqueue_fn=enqueue_fn,
        )
        from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep

        pipe.planner = MagicMock(
            return_value=dspy.Prediction(
                plan=RetrievalPlan(
                    question_type="exploratory",
                    steps=[
                        RetrievalStep(
                            step_id=1,
                            tool="rag_search",
                            query="q",
                            rationale="r",
                        )
                    ],
                )
            )
        )
        pipe.executor = MagicMock(
            return_value=dspy.Prediction(
                evidence="[rag_search] results",
                fact_ids=["f1"],
                chunk_ids=["c1"],
                fact_names=["fines"],
                celex_ids=[],
            )
        )
        pipe.synthesizer = MagicMock(
            return_value=dspy.Prediction(answer="The answer.")
        )
        return pipe

    def test_enqueue_fn_called_with_evidence_and_ids(self):
        calls: list[dict] = []
        pipe = self._build(lambda **kw: calls.append(kw))

        pipe(question="how many leniency cases exist?")

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["agent_answer"], "The answer.")
        self.assertEqual(calls[0]["supporting_fact_ids"], ["f1"])
        self.assertEqual(calls[0]["supporting_chunk_ids"], ["c1"])

    def test_enqueue_failure_is_swallowed(self):
        def _boom(**kwargs):
            raise RuntimeError("queue down")

        pipe = self._build(_boom)
        # Must not raise — the answer still comes back.
        pred = pipe(question="how many leniency cases exist?")
        self.assertEqual(pred.answer, "The answer.")

    def test_no_enqueue_fn_is_noop(self):
        pipe = self._build(None)
        pred = pipe(question="how many leniency cases exist?")
        self.assertEqual(pred.answer, "The answer.")


if __name__ == "__main__":
    unittest.main()

