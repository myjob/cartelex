"""Integration test for the Q&A Retrieval Pipeline — no mocks.
see design consideration in obsidian ~/a/celex-facts/docs/DSPy QA Complex Retrieval Module.md

Exercises the full Plan → Execute → Synthesize pipeline against the
live Ollama LLM and PostgreSQL database.

Requires:
  - Ollama running with the configured model (settings.LLM_MODEL_ID)
  - PostgreSQL with celex_facts schema populated

Usage:
    cd ~/a/celex-facts
    PYTHONPATH=src .venv/bin/python src/pipelines/agent_dspy/tests/test_qa_retrieval_integration.py
"""

import logging
import unittest

import dspy
from sqlalchemy import text

from config.llm_thinking import thinking_kwargs
from config.settings import settings
from extract.rag.hybrid_search import HybridSearch
from extract.rag.ranker.flash_ranker import FlashRanker
from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep
from pipelines.agent_dspy.retrieval_dispatch import KB_SCHEMA_DESCRIPTION
from pipelines.agent_dspy.retrieval_modules import (
    QAExecutorModule,
    QAPlannerModule,
    QARetrievalPipeline,
    QASynthesizerModule,
)
from pipelines.rag.pgvector.fact_repository import FactRepository
from tools.celex_tools import CelexDatabaseToolkit

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CrossCaseAggregator — concrete implementation for integration tests
# ---------------------------------------------------------------------------


class FactAggregator:
    """Read-only SQL execution over celex_facts.facts.

    Satisfies the ``CrossCaseAggregator`` protocol.
    """

    def __init__(self, engine) -> None:
        self.engine = engine

    def aggregate_facts(self, sql: str) -> list[dict]:
        with self.engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in result.fetchall()]


# ---------------------------------------------------------------------------
# Base class with shared setup
# ---------------------------------------------------------------------------


class _QAIntegrationBase(unittest.TestCase):
    """Shared setUp for all Q&A integration tests.

    Provisions:
      - DSPy LM (Ollama)
      - FactRepository (SQL store)
      - CelexDatabaseToolkit (KB searcher + doc searcher)
      - HybridSearch (in-case doc search)
      - FactAggregator (cross-case SQL)
    """

    @classmethod
    def setUpClass(cls):
        model_name = f"openai/{settings.LLM_MODEL_ID}"
        lm = dspy.LM(
            model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=True,
            **thinking_kwargs(model_name),
        )
        dspy.configure(lm=lm)

        cls.fact_repo = FactRepository()
        cls.toolkit = CelexDatabaseToolkit(fact_repo=cls.fact_repo)
        cls.aggregator = FactAggregator(engine=cls.fact_repo.engine)

        ranker = FlashRanker()
        cls.hybrid_search = HybridSearch(
            repo=cls.toolkit.chunk_repo, ranker=ranker, embed_query=cls.toolkit.embed_query
        )


# ---------------------------------------------------------------------------
# Test 1 — Synthesizer only (evidence pre-built, LLM generates answer)
# ---------------------------------------------------------------------------


class TestQASynthesizerIntegration(_QAIntegrationBase):
    """Verify the synthesizer produces a grounded answer from real evidence."""

    def test_synthesize_leniency_answer(self):
        """Given pre-built evidence about leniency applicants, the LLM
        should produce a coherent answer citing the celex_id."""
        evidence = (
            "--- Step 1 (case_facts) ---\n"
            "[case_facts] celex_id=38344_5856_3\n"
            "  leniency_applicants: Mueller Industries Inc.\n"
            "  leniency_case: True\n"
            "  fines: | company | amount |\n| Mueller Industries | 0 |\n"
        )

        synth = QASynthesizerModule()
        pred = synth(
            question="Which company requested leniency in case 38344_5856_3?",
            evidence=evidence,
        )

        print(f"\n{'=' * 60}")
        print(f"SYNTHESIZER ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10, "Answer should be non-trivial")


# ---------------------------------------------------------------------------
# Test 2 — Executor with real DB (no LLM needed)
# ---------------------------------------------------------------------------


class TestQAExecutorIntegration(_QAIntegrationBase):
    """Verify the executor retrieves real data from the database."""

    def _make_executor(self):
        return QAExecutorModule(
            sql_store=self.fact_repo,
            kb_searcher=self.toolkit,
            aggregator=self.aggregator,
            hybrid_searcher=self.hybrid_search,
            doc_searcher=self.toolkit,
        )

    def test_case_facts_real_celex(self):
        """Retrieve facts for a known celex_id."""
        # Find a celex_id that has facts
        with self.fact_repo.engine.connect() as conn:
            row = conn.execute(
                text("SELECT DISTINCT celex_id FROM celex_facts.facts LIMIT 1")
            ).fetchone()
        if not row:
            self.skipTest("No facts in database")

        celex_id = row[0]
        plan = RetrievalPlan(
            question_type="single_case",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="case_facts",
                    query="all facts",
                    case_id=celex_id,
                    rationale="Retrieve all facts for a real case.",
                )
            ],
        )

        executor = self._make_executor()
        pred = executor(plan=plan)

        print(f"\n{'=' * 60}")
        print(f"CASE FACTS for {celex_id}:")
        print(pred.evidence[:500])
        print(f"{'=' * 60}")

        self.assertIn("[case_facts]", pred.evidence)
        self.assertIn(celex_id, pred.evidence)

    def test_kb_sql_count_leniency_cases(self):
        """Aggregate: count leniency cases via real SQL."""
        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="kb_sql",
                    query=(
                        "SELECT COUNT(DISTINCT celex_id) AS cnt "
                        "FROM celex_facts.facts "
                        "WHERE name = 'leniency_case' AND value_md = 'True'"
                    ),
                    rationale="Count all cases with leniency.",
                )
            ],
        )

        executor = self._make_executor()
        pred = executor(plan=plan)

        print(f"\n{'=' * 60}")
        print(f"KB_SQL EVIDENCE:\n{pred.evidence}")
        print(f"{'=' * 60}")

        self.assertIn("[kb_sql]", pred.evidence)
        self.assertIn("cnt", pred.evidence)

    def test_rag_search_cross_case(self):
        """Semantic search over fact embeddings across all cases."""
        plan = RetrievalPlan(
            question_type="exploratory",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="rag_search",
                    query="leniency applicants",
                    rationale="Search for leniency information across cases.",
                )
            ],
        )

        executor = self._make_executor()
        pred = executor(plan=plan)

        print(f"\n{'=' * 60}")
        print(f"RAG_SEARCH EVIDENCE:\n{pred.evidence[:600]}")
        print(f"{'=' * 60}")

        self.assertIn("[rag_search]", pred.evidence)

    def test_doc_search_commissioner(self):
        """doc_search: find document chunks mentioning a commissioner."""
        plan = RetrievalPlan(
            question_type="exploratory",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="doc_search",
                    query="commissioner competition cartel",
                    rationale="Find chunks mentioning EU commissioners.",
                )
            ],
        )

        executor = self._make_executor()
        pred = executor(plan=plan)

        print(f"\n{'=' * 60}")
        print(f"DOC_SEARCH EVIDENCE:\n{pred.evidence[:600]}")
        print(f"{'=' * 60}")

        self.assertIn("[doc_search]", pred.evidence)


# ---------------------------------------------------------------------------
# Test 3 — Planner with real LLM
# ---------------------------------------------------------------------------


class TestQAPlannerIntegration(_QAIntegrationBase):
    """Verify the planner produces valid plans using the real LLM."""

    def test_plan_single_case_question(self):
        """The planner should produce a case_facts or rag_search step."""
        planner = QAPlannerModule(kb_schema=KB_SCHEMA_DESCRIPTION)
        pred = planner(
            question="Which company requested leniency in case 38344_5856_3?"
        )
        plan: RetrievalPlan = pred.plan

        print(f"\n{'=' * 60}")
        print("PLAN (single_case):")
        for s in plan.steps:
            print(
                f"  Step {s.step_id}: tool={s.tool} case_id={s.case_id!r} query={s.query!r}"
            )
        print(f"{'=' * 60}")

        self.assertGreater(len(plan.steps), 0, "Plan should have at least one step")
        tools_used = {s.tool for s in plan.steps}
        # Should use case_facts or rag_search_in_case since celex_id is provided

        self.assertTrue(
            tools_used & {"case_facts"},
            f"Expected a retrieval tool, got: {tools_used}",
        )

    def test_plan_aggregation_question(self):
        """The planner should produce a kb_sql step for aggregation."""
        planner = QAPlannerModule(kb_schema=KB_SCHEMA_DESCRIPTION)
        pred = planner(
            question="Did the number of leniency cases accepted by the EU commission increase over time?"
        )
        plan: RetrievalPlan = pred.plan

        print(f"\n{'=' * 60}")
        print("PLAN (aggregation):")
        for s in plan.steps:
            print(f"  Step {s.step_id}: tool={s.tool} query={s.query[:80]!r}")
        print(f"{'=' * 60}")

        self.assertGreater(len(plan.steps), 0)
        tools_used = {s.tool for s in plan.steps}
        self.assertIn("kb_sql", tools_used, "Aggregation should use kb_sql")

    def test_plan_entity_search(self):
        """The planner should use doc_search for entity-specific queries."""
        planner = QAPlannerModule(kb_schema=KB_SCHEMA_DESCRIPTION)
        pred = planner(question="Which cases involved companies based in Belgium?")
        plan: RetrievalPlan = pred.plan

        print(f"\n{'=' * 60}")
        print("PLAN (entity_search):")
        for s in plan.steps:
            print(f"  Step {s.step_id}: tool={s.tool} query={s.query[:80]!r}")
        print(f"{'=' * 60}")

        self.assertGreater(len(plan.steps), 0)


# ---------------------------------------------------------------------------
# Test 4 — Full end-to-end pipeline
# ---------------------------------------------------------------------------


class TestQAFullPipelineIntegration(_QAIntegrationBase):
    """End-to-end: question → plan → execute → synthesize → answer."""

    def _make_pipeline(self):
        return QARetrievalPipeline(
            sql_store=self.fact_repo,
            kb_searcher=self.toolkit,
            aggregator=self.aggregator,
            hybrid_searcher=self.hybrid_search,
            doc_searcher=self.toolkit,
        )

    def test_e2e_single_case(self):
        """Full pipeline for a single-case question."""
        pipeline = self._make_pipeline()
        pred = pipeline(
            question="Which company requested leniency in case 38344_5856_3?"
        )

        print(f"\n{'=' * 60}")
        print("E2E SINGLE CASE")
        print(f"Plan type: {pred.plan.question_type}")
        print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)

    def test_e2e_aggregation(self):
        """Full pipeline for a macroscopic aggregation question."""
        pipeline = self._make_pipeline()
        pred = pipeline(
            question="How many cases in the database had leniency applications?"
        )

        print(f"\n{'=' * 60}")
        print("E2E AGGREGATION")
        print(f"Plan type: {pred.plan.question_type}")
        print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)

    def test_e2e_aggregation_leniency_trend(self):
        """Full pipeline for a macroscopic aggregation question."""
        pipeline = self._make_pipeline()
        pred = pipeline(
            question="Did the number of leniency cases accepted by the EU commission increase over time?"
        )

        print(f"\n{'=' * 60}")
        print("E2E AGGREGATION")
        print(f"Plan type: {pred.plan.question_type}")
        print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)
        """
        The original user question.\n2. `evidence` (str): Concatenated evidence collected during plan execution, with step labels.\n
        Your output fields are:\n1. `reasoning` (str): \n2. `answer` (str): Final answer grounded in the evidence, with inline citations.\n
        All interactions will be structured in the following way, with the appropriate values filled in.\n\n
        [[ ## question ## ]]\n{question}\n\n[[ ## evidence ## ]]\n{evidence}\n\n[[ ## reasoning ## ]]\n{reasoning}\n\n[[ ## answer ## ]]\n{answer}\n\n
        # [[ ## completed ## ]]\nIn adhering to this structure, your objective is: \n        
        # Synthesize a final, grounded answer from retrieved evidence.\n        \n
        #         Rules\n        -----\n        1. Base your answer ONLY on the provided evidence.  Do not hallucinate.\n        
        # 2. When evidence is insufficient, say so explicitly.\n        
        # 3. Cite sources by including the celex_id or step_id that supplied the evidence.\n        
        # 4. For numeric aggregations, show the numbers clearly.'}, {'role': 'user', 'content': '[[ ## question ## ]]\n
        # Did the number of leniency cases accepted by the EU commission increase over time?\n\n[[ ## evidence ## ]]\n--- 
        # Step 1 (kb_sql) ---\n[error] (psycopg.errors.InvalidDatetimeFormat) invalid input syntax for type date: "True"\n
        # [SQL: SELECT DATE(value_md) AS decision_date, COUNT(*) AS leniency_count 
        # FROM celex_facts.facts WHERE name = \'leniency_case\' AND value_md = \'True\' GROUP BY decision_date ORDER BY decision_date LIMIT 500]\n
        # (Background on this error at: https://sqlalche.me/e/20/9h9h)\n\nRespond with the corresponding output fields, starting with the field `[[ ## reasoning ## ]]`, then `[[ ## answer ## ]]`, and then ending with the marker for `[[ ## completed ## ]]`.'}], 
        """

    def test_e2e_aggregation_brewery_cartel_period(self):
        """Full pipeline for a macroscopic aggregation question."""
        pipeline = self._make_pipeline()
        pred = pipeline(question="list all cartel periods of brewery cases?")

        print(f"\n{'=' * 60}")
        print("E2E brewery_cartel_period")
        print(f"Plan type: {pred.plan.question_type}")
        print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)
        self.assertIn("CELEX-", pred.answer)

    def test_e2e_aggregation_brewery_focus(self):
        """Full pipeline for a macroscopic aggregation question."""
        pipeline = self._make_pipeline()
        pred = pipeline(
            question="given the cartel periods of brewery cases, can we assume that the EU commission focuses specifically on breweries during a ccertaion period?"
        )

        print(f"\n{'=' * 60}")
        print("E2E brewery_focus")
        print(f"Plan type: {pred.plan.question_type}")
        print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)
        self.assertIn("eu commission", pred.answer.lower())
        # "Based on the evidence provided, it cannot be concluded that the EU Commission specifically focuses on breweries during certain certification periods. The following cases are related to breweries, although they do not provide insight into the Commission's focus during cartel periods:\n\n- 37766_610_5 — Dutch beer market\n- CELEX-31971D0258 — Extra alloy\n- CELEX-31931971D0257 — brewing industry"

    def test_e2e_aggregation_cases_decision_1991(self):
        """Full pipeline for a macroscopic aggregation question."""
        pipeline = self._make_pipeline()
        pred = pipeline(question="which cases got decisions in 1991")

        print(f"\n{'=' * 60}")
        print("E2E cases_decision_1991")
        print(f"Plan type: {pred.plan.question_type}")
        print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)
        self.assertIn("31991D0050", pred.answer)
        self.assertIn("31991D0335", pred.answer)
        self.assertIn("31991D0562", pred.answer)

    def test_e2e_aggregation_cases_decision_1991_nace(self):
        """Full pipeline for a macroscopic aggregation question."""
        pipeline = self._make_pipeline()
        pred = pipeline(question="list the NACE of cases which got decisions in 1991")

        print(f"\n{'=' * 60}")
        print("E2E cases_decision_1991_nace")
        print(f"Plan type: {pred.plan.question_type}")
        print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)

    def test_e2e_aggregation_cases_nace(self):
        """Full pipeline: look up the NACE group code for a single known case."""
        pipeline = QARetrievalPipeline(
            sql_store=self.fact_repo,
            kb_searcher=self.toolkit,
            aggregator=self.aggregator,
            hybrid_searcher=self.hybrid_search,
            doc_searcher=self.toolkit,
        )
        pred = pipeline(question="NACE_group_code of CELEX-31991D0562?")
        print(f"\n{'=' * 60}")
        print("E2E cases_nace")
        # print(f"Plan type: {pred.plan.question_type}")
        # print(f"Steps: {len(pred.plan.steps)}")

        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)
        self.assertIn("26.3", pred.answer)

    def test_e2e_aggregation_cases_nace_desc(self):
        """Full pipeline: look up the NACE group  for a single known case."""
        pipeline = QARetrievalPipeline(
            sql_store=self.fact_repo,
            kb_searcher=self.toolkit,
            aggregator=self.aggregator,
            hybrid_searcher=self.hybrid_search,
            doc_searcher=self.toolkit,
        )
        pred = pipeline(question="NACE group of CELEX-31991D0562?")
        print(f"\n{'=' * 60}")
        print("E2E cases_nace")
        # print(f"Plan type: {pred.plan.question_type}")
        # print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)
        self.assertIn("communication equipment", pred.answer)

    def test_e2e_aggregation_cases_infringement_gravity(self):
        """Full pipeline: look up the infringement_gravity  for a single known case."""
        pipeline = QARetrievalPipeline(
            sql_store=self.fact_repo,
            kb_searcher=self.toolkit,
            aggregator=self.aggregator,
            hybrid_searcher=self.hybrid_search,
            doc_searcher=self.toolkit,
        )
        pred = pipeline(question="infringement_gravity of CELEX-31991D0562?")
        print(f"\n{'=' * 60}")
        print("E2E cases_nace")
        print(f"Plan type: {pred.plan.question_type}")
        print(f"Steps: {len(pred.plan.steps)}")

        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 5)
        self.assertIn("medium", pred.answer)

    def test_e2e_summary_synonym_fallback(self):
        """Vocabulary-mismatch case: 'summary' is not a fact name in the
        schema; the canonical name is ``case_summary``.  The planner rule 2
        change should route to ``case_facts`` first, and — if it didn't —
        the executor's synonym fallback must recover the answer.
        """
        pipeline = self._make_pipeline()
        pred = pipeline(question="summary of CELEX-31991D0562?")
        print(f"\n{'=' * 60}")
        print("E2E summary_synonym_fallback")
        # print(f"Plan type: {pred.plan.question_type}")
        # print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)
        # The synthesized answer should mention substantive content from the
        # case_summary fact rather than 'insufficient evidence'.
        self.assertNotIn("insufficient", pred.answer.lower())

    def test_e2e_aggregation_cases_market_geographical(self):
        """Full pipeline: look up the market_geographical  for a single known case."""
        pipeline = QARetrievalPipeline(
            sql_store=self.fact_repo,
            kb_searcher=self.toolkit,
            aggregator=self.aggregator,
            hybrid_searcher=self.hybrid_search,
            doc_searcher=self.toolkit,
        )
        pred = pipeline(question="geographical market of CELEX-31991D0562?")
        print(f"\n{'=' * 60}")
        print("E2E cases_nace")
        # print(f"Plan type: {pred.plan.question_type}")
        # print(f"Steps: {len(pred.plan.steps)}")

        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 5)
        self.assertIn("Ireland", pred.answer)

    def test_e2e_celex_gate_valid_short_form(self):
        """Short-form id '37766_610_5' must pass the gate and produce an answer."""
        pipeline = self._make_pipeline()
        pred = pipeline(question="overview of 37766_610_5")

        print(f"\n{'=' * 60}")
        print("E2E celex_gate_valid_short_form")
        # print(f"Celex status: {pred.celex_validation.status}")
        # print(f"Canonical id: {pred.celex_validation.canonical_celex_id}")
        print(f"ANSWER:\n{pred.answer}")
        print("=" * 60)

        self.assertEqual(pred.celex_validation.status, "valid")
        # self.assertIsNotNone(pred.plan) # No need for plan in overview cases
        self.assertGreater(len(pred.answer), 10)

    def test_e2e_celex_gate_invalid_prefix_aborts(self):
        """'CELEX-37766_610_5' is malformed — the gate must abort the pipeline."""
        pipeline = self._make_pipeline()
        pred = pipeline(question="overview of CELEX-37766_610_5")

        print(f"\n{'=' * 60}")
        print("E2E celex_gate_invalid_prefix_aborts")
        print(f"Celex status: {pred.celex_validation.status}")
        print(f"Candidate: {pred.celex_validation.candidate}")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertEqual(pred.celex_validation.status, "invalid")
        self.assertEqual(pred.celex_validation.candidate, "CELEX-37766_610_5")
        #   self.assertIsNone(pred.plan) # No need for plan in overview cases
        self.assertEqual(pred.evidence, "")
        self.assertIn("CELEX-37766_610_5", pred.answer)

    def test_e2e_celex_gate_no_celex_goes_general(self):
        """Question without any celex id must proceed through the general path."""
        pipeline = self._make_pipeline()
        pred = pipeline(
            question="How many cases in the database had leniency applications?"
        )

        print(f"\n{'=' * 60}")
        print("E2E celex_gate_no_celex_goes_general")
        print(f"Celex status: {pred.celex_validation.status}")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertEqual(pred.celex_validation.status, "no_celex")
        self.assertIsNotNone(pred.plan)
        self.assertGreater(len(pred.answer), 10)

    def test_e2e_summary_synonym_overview(self):
        """Vocabulary-mismatch case: 'summary' is not a fact name in the
        schema; the canonical name is ``case_summary``.  The planner rule 2
        change should route to ``case_facts`` first, and — if it didn't —
        the executor's synonym fallback must recover the answer.
        """
        pipeline = self._make_pipeline()
        pred = pipeline(question="overview of 37766_610_5")
        print(f"\n{'=' * 60}")
        print("E2E summary_synonym_fallback")
        # print(f"Plan type: {pred.plan.question_type}")
        # print(f"Steps: {len(pred.plan.steps)}")
        print(f"Evidence length: {len(pred.evidence)} chars")
        print(f"ANSWER:\n{pred.answer}")
        print(f"{'=' * 60}")

        self.assertIsInstance(pred.answer, str)
        self.assertGreater(len(pred.answer), 10)
        # The synthesized answer should mention substantive content from the
        # case_summary fact rather than 'insufficient evidence'.
        self.assertNotIn("No overview", pred.answer.lower())


if __name__ == "__main__":
    unittest.main()

# .venv/bin/python -m unittest -v src.pipelines.agent_dspy.tests.test_qa_retrieval_integration.TestQAFullPipelineIntegration.test_e2e_summary_synonym_overview
