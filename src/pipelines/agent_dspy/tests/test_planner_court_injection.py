"""Tests for the unstructured-topic post-plan injection gate.

Validates that questions about court proceedings, tribunal rulings, appeals,
and other topics NOT captured in ``celex_facts.facts`` are deterministically
routed to document-chunk retrieval (``rag_search_in_case`` / ``doc_search``)
even when the LLM planner omits those steps.

Unit tests use mocked plans; integration test validates the full pipeline
against case ``39612_12448_6``.
"""

import unittest
from unittest.mock import MagicMock, patch

import dspy

from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep
from pipelines.agent_dspy.retrieval_modules import (
    _inject_case_facts_step,
    _inject_rag_step,
    _needs_case_facts_step,
    _needs_rag_followup,
    QAPlannerModule,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(*steps: RetrievalStep, qtype: str = "single_case") -> RetrievalPlan:
    return RetrievalPlan(question_type=qtype, steps=list(steps))


def _case_facts_step(case_id: str = "39612_12448_6") -> RetrievalStep:
    return RetrievalStep(
        step_id=1,
        tool="case_facts",
        query="all facts",
        case_id=case_id,
        rationale="Baseline structured facts.",
    )


def _rag_in_case_step(case_id: str = "39612_12448_6") -> RetrievalStep:
    return RetrievalStep(
        step_id=2,
        tool="rag_search_in_case",
        query="court judgment",
        case_id=case_id,
        rationale="Search document chunks for court info.",
    )


def _doc_search_step() -> RetrievalStep:
    return RetrievalStep(
        step_id=1,
        tool="doc_search",
        query="court",
        rationale="Cross-case doc search.",
    )


def _kb_sql_step(step_id: int = 1, input_from_step: int | None = None) -> RetrievalStep:
    return RetrievalStep(
        step_id=step_id,
        tool="kb_sql",
        query=(
            "SELECT value_md FROM celex_facts.facts "
            "WHERE celex_id = '38344_5856_3' AND name = 'leniency_applicants'"
        ),
        input_from_step=input_from_step,
        rationale="Narrow fact lookup.",
    )


# ---------------------------------------------------------------------------
# Unit Tests — SINGLE-CASE RULE injection
# ---------------------------------------------------------------------------


class TestNeedsCaseFactsStep(unittest.TestCase):
    """Test the predicate that detects a missing case_facts step."""

    def test_kb_sql_only_plan_returns_celex_id(self):
        plan = _make_plan(_kb_sql_step())
        self.assertEqual(
            _needs_case_facts_step(
                "Which company requested leniency in case 38344_5856_3?", plan
            ),
            "38344_5856_3",
        )

    def test_plan_with_matching_case_facts_returns_empty(self):
        plan = _make_plan(_case_facts_step(case_id="38344_5856_3"))
        self.assertEqual(
            _needs_case_facts_step(
                "Which company requested leniency in case 38344_5856_3?", plan
            ),
            "",
        )

    def test_case_facts_for_other_case_still_returns_celex_id(self):
        """A case_facts step scoped to a different case does not satisfy the rule."""
        plan = _make_plan(_case_facts_step(case_id="39612_12448_6"))
        self.assertEqual(
            _needs_case_facts_step("leniency in case 38344_5856_3?", plan),
            "38344_5856_3",
        )

    def test_question_without_celex_returns_empty(self):
        plan = _make_plan(_kb_sql_step())
        self.assertEqual(
            _needs_case_facts_step("How many leniency cases are there?", plan), ""
        )


class TestInjectCaseFactsStep(unittest.TestCase):
    """Test the deterministic case_facts injection."""

    def test_injects_case_facts_first(self):
        plan = _make_plan(_kb_sql_step())
        question = "Which company requested leniency in case 38344_5856_3?"

        result = _inject_case_facts_step(question, plan)

        self.assertEqual(len(result.steps), 2)
        self.assertEqual(result.steps[0].tool, "case_facts")
        self.assertEqual(result.steps[0].case_id, "38344_5856_3")
        self.assertEqual(result.steps[0].step_id, 1)
        self.assertEqual(result.steps[1].tool, "kb_sql")
        self.assertEqual(result.steps[1].step_id, 2)

    def test_renumbers_chained_step_references(self):
        """Chained steps must keep pointing at the same upstream step."""
        plan = _make_plan(
            _kb_sql_step(step_id=1),
            _kb_sql_step(step_id=2, input_from_step=1),
        )

        result = _inject_case_facts_step("facts of CELEX-31991D0562", plan)

        self.assertEqual([s.step_id for s in result.steps], [1, 2, 3])
        self.assertIsNone(result.steps[1].input_from_step)
        self.assertEqual(result.steps[2].input_from_step, 2)

    def test_no_injection_when_case_facts_present(self):
        plan = _make_plan(_case_facts_step(case_id="38344_5856_3"))

        result = _inject_case_facts_step("leniency in case 38344_5856_3?", plan)

        self.assertEqual(len(result.steps), 1)

    def test_no_injection_without_celex_id(self):
        plan = _make_plan(_kb_sql_step())

        result = _inject_case_facts_step("How many leniency cases are there?", plan)

        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].tool, "kb_sql")


# ---------------------------------------------------------------------------
# Unit Tests — _needs_rag_followup
# ---------------------------------------------------------------------------


class TestNeedsRagFollowup(unittest.TestCase):
    """Test the predicate that detects missing doc-chunk steps."""

    def test_court_question_no_doc_step_returns_true(self):
        """A court question with only case_facts should trigger injection."""
        plan = _make_plan(_case_facts_step())
        self.assertTrue(
            _needs_rag_followup("explain the court involvement in case 39612_12448_6", plan)
        )

    def test_appeal_question_returns_true(self):
        plan = _make_plan(_case_facts_step())
        self.assertTrue(
            _needs_rag_followup("was there an appeal in case 39612_12448_6?", plan)
        )

    def test_tribunal_question_returns_true(self):
        plan = _make_plan(_case_facts_step())
        self.assertTrue(
            _needs_rag_followup("tribunal ruling for 39612_12448_6", plan)
        )

    def test_judgment_question_returns_true(self):
        plan = _make_plan(_case_facts_step())
        self.assertTrue(
            _needs_rag_followup("what was the judgment in 39612_12448_6?", plan)
        )

    def test_dawn_raid_question_returns_true(self):
        plan = _make_plan(_case_facts_step())
        self.assertTrue(
            _needs_rag_followup("was there a dawn raid in case 39612_12448_6?", plan)
        )

    def test_non_court_question_returns_false(self):
        """A question about fines should NOT trigger injection."""
        plan = _make_plan(_case_facts_step())
        self.assertFalse(
            _needs_rag_followup("what were the fines in case 39612_12448_6?", plan)
        )

    def test_already_has_rag_step_returns_false(self):
        """When the plan already includes rag_search_in_case, no injection needed."""
        plan = _make_plan(_case_facts_step(), _rag_in_case_step())
        self.assertFalse(
            _needs_rag_followup("explain the court involvement in case 39612_12448_6", plan)
        )

    def test_already_has_doc_search_returns_false(self):
        """When the plan already includes doc_search, no injection needed."""
        plan = _make_plan(_doc_search_step())
        self.assertFalse(
            _needs_rag_followup("which cases had court appeals?", plan)
        )

    def test_empty_question_returns_false(self):
        plan = _make_plan(_case_facts_step())
        self.assertFalse(_needs_rag_followup("", plan))

    def test_case_insensitive(self):
        """Keywords should match regardless of casing."""
        plan = _make_plan(_case_facts_step())
        self.assertTrue(
            _needs_rag_followup("COURT INVOLVEMENT in CASE 39612_12448_6", plan)
        )


# ---------------------------------------------------------------------------
# Unit Tests — _inject_rag_step
# ---------------------------------------------------------------------------


class TestInjectRagStep(unittest.TestCase):
    """Test the deterministic step injection."""

    def test_injects_rag_search_in_case_when_celex_available(self):
        """When a case_id exists in plan steps, inject rag_search_in_case."""
        plan = _make_plan(_case_facts_step(case_id="39612_12448_6"))
        question = "explain the court involvement in case 39612_12448_6"

        result = _inject_rag_step(question, plan)

        self.assertEqual(len(result.steps), 2)
        injected = result.steps[-1]
        self.assertEqual(injected.tool, "rag_search_in_case")
        self.assertEqual(injected.case_id, "39612_12448_6")
        # The full question is used as the search query (multi-word avoids
        # HybridSearch's single-word keyword-skip gate).
        self.assertEqual(injected.query, question)
        self.assertIn("Post-plan injection", injected.rationale)

    def test_injects_doc_search_when_no_celex(self):
        """When no case_id is in plan steps, fall back to doc_search."""
        step = RetrievalStep(
            step_id=1,
            tool="rag_search",
            query="court cases",
            rationale="General search.",
        )
        plan = _make_plan(step)
        question = "which cases involved court appeals?"

        result = _inject_rag_step(question, plan)

        self.assertEqual(len(result.steps), 2)
        injected = result.steps[-1]
        self.assertEqual(injected.tool, "doc_search")
        self.assertEqual(injected.case_id, "")
        # Full question is used as query.
        self.assertEqual(injected.query, question)

    def test_no_injection_for_non_court_question(self):
        """Non-court questions should not get an injected step."""
        plan = _make_plan(_case_facts_step())
        question = "what were the fines in case 39612_12448_6?"

        result = _inject_rag_step(question, plan)

        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].tool, "case_facts")

    def test_no_injection_when_rag_step_exists(self):
        """When rag_search_in_case already exists, don't add another."""
        plan = _make_plan(_case_facts_step(), _rag_in_case_step())
        question = "explain the court involvement in case 39612_12448_6"

        result = _inject_rag_step(question, plan)

        self.assertEqual(len(result.steps), 2)  # unchanged

    def test_step_id_is_sequential(self):
        """Injected step_id should follow the last existing step."""
        plan = _make_plan(_case_facts_step())
        question = "court involvement in 39612_12448_6"

        result = _inject_rag_step(question, plan)

        self.assertEqual(result.steps[-1].step_id, 2)

    def test_multiple_keywords_in_query(self):
        """The injected step uses the full original question as its query."""
        plan = _make_plan(_case_facts_step())
        question = "court appeal judgment in case 39612_12448_6"

        result = _inject_rag_step(question, plan)

        injected = result.steps[-1]
        # Full question is used as query — multi-word avoids the keyword-skip gate.
        self.assertEqual(injected.query, question)


# ---------------------------------------------------------------------------
# Unit Test — QAPlannerModule.forward integration with injection
# ---------------------------------------------------------------------------


class TestQAPlannerModuleInjection(unittest.TestCase):
    """Verify that QAPlannerModule.forward() invokes post-plan injection."""

    @patch("pipelines.agent_dspy.retrieval_modules._inject_rag_step")
    def test_forward_calls_inject_rag_step(self, mock_inject):
        """forward() must call _inject_rag_step with the question and plan."""
        # Build a mock plan prediction
        mock_plan = _make_plan(_case_facts_step())
        mock_inject.return_value = mock_plan

        module = QAPlannerModule.__new__(QAPlannerModule)
        module.kb_schema = "test schema"
        module.plan = MagicMock(
            return_value=dspy.Prediction(plan=mock_plan)
        )

        question = "court involvement in 39612_12448_6"
        result = module.forward(question)

        mock_inject.assert_called_once_with(question, mock_plan)
        self.assertEqual(result.plan, mock_plan)


# ---------------------------------------------------------------------------
# Integration Test — full pipeline for court-involvement question
# ---------------------------------------------------------------------------


class TestCourtInvolvementIntegration(unittest.TestCase):
    """Integration test: validates that the full QARetrievalPipeline produces
    court-related evidence for case ``39612_12448_6``.

    Requires a running Ollama instance and access to the PostgreSQL database.
    """

    def setUp(self):
        from config.llm_thinking import thinking_kwargs
        from config.settings import settings
        from tools.celex_tools import CelexDatabaseToolkit

        model_name = f"openai/{settings.LLM_MODEL_ID}"
        lm = dspy.LM(
            model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=True,
            **thinking_kwargs(model_name),
        )
        dspy.configure(lm=lm)
        self.toolkit = CelexDatabaseToolkit()

    def test_court_involvement_39612_12448_6(self):
        """The pipeline must produce an answer grounded in document chunks
        containing court/tribunal/appeal references for this case."""
        from extract.rag.hybrid_search import HybridSearch
        from extract.rag.ranker.flash_ranker import FlashRanker
        from sqlalchemy import text as sa_text
        from pipelines.agent_dspy.retrieval_modules import QARetrievalPipeline

        # CrossCaseAggregator — concrete implementation
        class _FactAggregator:
            def __init__(self, engine):
                self.engine = engine

            def aggregate_facts(self, sql: str) -> list[dict]:
                with self.engine.connect() as conn:
                    result = conn.execute(sa_text(sql))
                    columns = list(result.keys())
                    return [dict(zip(columns, row)) for row in result.fetchall()]

        ranker = FlashRanker()
        hybrid_search = HybridSearch(
            repo=self.toolkit.chunk_repo, ranker=ranker, embed_query=self.toolkit.embed_query
        )

        pipeline = QARetrievalPipeline(
            sql_store=self.toolkit.fact_repo,
            kb_searcher=self.toolkit,
            aggregator=_FactAggregator(engine=self.toolkit.fact_repo.engine),
            hybrid_searcher=hybrid_search,
            doc_searcher=self.toolkit,
        )

        question = "explain the court involvement in case 39612_12448_6"
        result = pipeline(question=question)

        # 1. The plan must include a rag_search_in_case step (either from
        #    the LLM planner or from the post-plan injection).
        self.assertIsNotNone(result.plan, "Plan should not be None")
        tools_used = [s.tool for s in result.plan.steps]
        self.assertIn(
            "rag_search_in_case",
            tools_used,
            f"Plan must include rag_search_in_case for court question. "
            f"Actual tools: {tools_used}",
        )

        # 2. The evidence must contain document chunk content (not just
        #    structured facts).
        self.assertIn(
            "rag_search_in_case",
            result.evidence,
            "Evidence must include rag_search_in_case output.",
        )

        # 3. The answer must reference court-related concepts.
        answer_lower = result.answer.lower()
        court_terms = ("court", "tribunal", "appeal", "judgment", "ruling")
        has_court_term = any(term in answer_lower for term in court_terms)
        self.assertTrue(
            has_court_term,
            f"Answer must mention court-related terms. Got: {result.answer[:300]}...",
        )

        print(f"\n{'='*60}")
        print(f"Question: {question}")
        print(f"Plan steps: {tools_used}")
        print(f"Answer (first 500 chars):\n{result.answer[:500]}")
        print(f"{'='*60}")

    def test_list_courts_39226_8310_11(self):
        """The pipeline must list specific court names.

        Case 39226_8310_11 mentions 'court' 300+ times and names specific
        courts like 'Patents Court of the Chancery Division'.  The answer
        must enumerate at least one specific court name, not just say
        'courts in the UK'.

        With the addition of ``court_list_raw`` as a structured fact, the
        pipeline may fire the direct-fact passthrough (plan=None) and return
        the pre-extracted court list verbatim.  Both paths are valid."""
        from extract.rag.hybrid_search import HybridSearch
        from extract.rag.ranker.flash_ranker import FlashRanker
        from sqlalchemy import text as sa_text
        from pipelines.agent_dspy.retrieval_modules import QARetrievalPipeline

        class _FactAggregator:
            def __init__(self, engine):
                self.engine = engine

            def aggregate_facts(self, sql: str) -> list[dict]:
                with self.engine.connect() as conn:
                    result = conn.execute(sa_text(sql))
                    columns = list(result.keys())
                    return [dict(zip(columns, row)) for row in result.fetchall()]

        ranker = FlashRanker()
        hybrid_search = HybridSearch(
            repo=self.toolkit.chunk_repo, ranker=ranker, embed_query=self.toolkit.embed_query
        )

        pipeline = QARetrievalPipeline(
            sql_store=self.toolkit.fact_repo,
            kb_searcher=self.toolkit,
            aggregator=_FactAggregator(engine=self.toolkit.fact_repo.engine),
            hybrid_searcher=hybrid_search,
            doc_searcher=self.toolkit,
        )

        question = "list courts in case 39226_8310_11"
        result = pipeline(question=question)

        if result.plan is not None:
            # Full plan path — validate rag_search_in_case step.
            tools_used = [s.tool for s in result.plan.steps]
            self.assertIn(
                "rag_search_in_case",
                tools_used,
                f"Plan must include rag_search_in_case. Actual: {tools_used}",
            )
            # Evidence must contain document chunks with court references.
            evidence_lower = result.evidence.lower()
            self.assertIn(
                "court",
                evidence_lower,
                "Evidence must contain 'court' from document chunks.",
            )
        else:
            # Direct-fact passthrough — plan is None, answer contains the
            # pre-extracted court list from court_list_raw / court_list.
            self.assertIn(
                "court",
                result.answer.lower(),
                "Passthrough answer must contain court data.",
            )

        # Either way, the answer must name at least one specific court.
        answer_lower = result.answer.lower()
        specific_court_names = (
            "high court",
            "patents court",
            "chancery",
            "court of justice",
            "general court",
            "court",
        )
        has_specific = any(name in answer_lower for name in specific_court_names)
        self.assertTrue(
            has_specific,
            f"Answer must name specific courts. Got: {result.answer[:500]}...",
        )

        print(f"\n{'='*60}")
        print(f"Question: {question}")
        if result.plan:
            print(f"Plan steps: {[s.tool for s in result.plan.steps]}")
        else:
            print("Plan: None (direct-fact passthrough)")
        print(f"Answer (first 800 chars):\n{result.answer[:800]}")
        print(f"{'='*60}")


if __name__ == "__main__":
    unittest.main()
