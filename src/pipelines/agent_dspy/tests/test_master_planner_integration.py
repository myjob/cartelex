"""End-to-end integration tests for :class:`MasterPlannerModule`.

Unlike :mod:`test_master_planner_module` (which exercises individual stages in
isolation), these tests drive the whole ``forward`` pipeline -
analyze -> decompose -> execute -> join/aggregate -> verify -> synthesize - for
four representative user questions, mocking ONLY at the system boundaries (the
DSPy predictors, the retrieval dispatcher, the evidence verifier, and the SQL
store).  Everything between those seams - sub-task ordering, celex extraction,
the deterministic polars join / role-filter / group-count, the verify gate, and
the evidence rendered into the synthesizer - runs for real.

The four questions cover the agent's full decision surface:

  1. entity-only case list   -> ``master_planner`` (no role filter)
  2. entity + role case list -> ``master_planner`` (deterministic role filter)
  3. grouped country counts  -> ``master_planner`` (count_group aggregation)
  4. before/after trend      -> ``master_planner_na`` (out-of-scope short-circuit)
"""

import os
import unittest

from pipelines.agent_dspy.master_planner_module import MasterPlannerModule
from pipelines.agent_dspy.master_planner_signature import (
    MasterAnswer,
    MasterPlan,
    QueryAnalysis,
    ResolvedEntity,
    SubTask,
)
from pipelines.agent_dspy.retrieval_dispatch import EvidenceResult

# Reuse the boundary fakes from the unit-test module rather than redefining them.
from pipelines.agent_dspy.tests.test_master_planner_module import (
    _FakeDispatcher,
    _FakePredictor,
    _FakeVerifier,
    _sql_store,
)

# A catalog wide enough to ground all four scenarios (company, role, country).
_CATALOG = [
    "companies_investigated",
    "companies_country",
    "company_recidivism",
    "market_geographical",
    "infringement_category_member_states",
    "fines",
]


def _module(
    *,
    analysis=None,
    plan=None,
    answer=None,
    dispatcher=None,
    verifier=None,
    facts_by_celex=None,
) -> MasterPlannerModule:
    """Assemble a planner whose predictors return the supplied fixtures."""
    return MasterPlannerModule(
        sql_store=_sql_store(_CATALOG, facts_by_celex),
        dispatcher=dispatcher or _FakeDispatcher({}),
        verifier=verifier,
        analyze_predictor=_FakePredictor("analysis", analysis)
        if analysis is not None
        else None,
        decompose_predictor=_FakePredictor("plan", plan) if plan is not None else None,
        synthesize_predictor=_FakePredictor("result", answer)
        if answer is not None
        else None,
    )


class TestBayerCartelMembershipCaseList(unittest.TestCase):
    """Q1: "List all cases where Bayer AG was part of a cartel".

    Entity-only case list: every retrieved case survives because the plan
    carries no role-filter sub-task.  Exercises the no-join / no-aggregate path
    through join_aggregate and the verify gate over multiple cases.
    """

    def test_returns_all_bayer_cases(self) -> None:
        analysis = QueryAnalysis(
            entities=[
                ResolvedEntity(
                    surface="Bayer AG",
                    entity_type="company",
                    available_in=["fact", "chunk"],
                    candidate_fact_names=["companies_investigated"],
                )
            ],
            target_type="case_list",
            aggregation="none",
            requires_statistics=False,
            rationale="single entity, enumerate cases",
        )
        plan = MasterPlan(
            supported=True,
            target_type="case_list",
            subtasks=[
                SubTask(
                    task_id=1,
                    kind="retrieve",
                    tool="doc_search",
                    query="Bayer AG cartel",
                    rationale="find cases mentioning Bayer AG",
                )
            ],
        )
        answer = MasterAnswer(
            answer_md=(
                "Bayer AG appears in CELEX-31994D0601 and CELEX-32004D0337."
            ),
            confident=True,
            cited_celex_ids=["CELEX-31994D0601", "CELEX-32004D0337"],
        )
        dispatcher = _FakeDispatcher(
            {
                "doc_search": EvidenceResult(
                    text=(
                        "[doc pos=1 CELEX-31994D0601] Bayer AG was a cartel member.\n"
                        "[doc pos=2 CELEX-32004D0337] Bayer AG participated in the cartel."
                    ),
                    chunk_ids=["c1", "c2"],
                )
            }
        )
        verifier = _FakeVerifier(
            {
                ("CELEX-31994D0601", "companies_investigated"): "verified_supported",
                ("CELEX-32004D0337", "companies_investigated"): "verified_supported",
            }
        )
        facts_by_celex = {
            "CELEX-31994D0601": [("f1", "companies_investigated", "Bayer AG, BASF")],
            "CELEX-32004D0337": [("f2", "companies_investigated", "Bayer AG, Dow")],
        }
        module = _module(
            analysis=analysis,
            plan=plan,
            answer=answer,
            dispatcher=dispatcher,
            verifier=verifier,
            facts_by_celex=facts_by_celex,
        )

        pred = module.forward("List all cases where Bayer AG was part of a cartel")

        self.assertEqual(pred.routing_decision, "master_planner")
        # Both cases survived the (no-op) filter and reached verification.
        verified_celex = {celex for celex, _ in verifier.calls}
        self.assertEqual(verified_celex, {"CELEX-31994D0601", "CELEX-32004D0337"})
        # The evidence handed to the synthesizer carries both verified cases.
        joined_evidence = module.synthesize.calls[0]["joined_evidence"]
        self.assertIn("CELEX-31994D0601", joined_evidence)
        self.assertIn("CELEX-32004D0337", joined_evidence)
        # Chunk provenance from the retrieval tool propagates to the prediction.
        self.assertEqual(pred.chunk_ids, ["c1", "c2"])


class TestBayerCartelLeaderCaseList(unittest.TestCase):
    """Q2: "List all cases where company Bayer AG was the cartel leader".

    Entity + role case list: the deterministic role filter must drop a Bayer
    case that has no leadership surface, so only the ringleader case reaches the
    verifier and the answer.
    """

    def test_role_filter_keeps_only_the_leader_case(self) -> None:
        analysis = QueryAnalysis(
            entities=[
                ResolvedEntity(
                    surface="Bayer AG",
                    entity_type="company",
                    available_in=["fact", "chunk"],
                    candidate_fact_names=["companies_investigated", "company_recidivism"],
                )
            ],
            target_type="case_list",
            aggregation="none",
            requires_statistics=False,
            rationale="entity plus ringleader role filter",
        )
        plan = MasterPlan(
            supported=True,
            target_type="case_list",
            subtasks=[
                SubTask(
                    task_id=1,
                    kind="retrieve",
                    tool="doc_search",
                    query="Bayer AG ringleader instigator cartel leader",
                    rationale="candidate cases",
                ),
                SubTask(
                    task_id=2,
                    kind="filter",
                    filter_role="ringleader / instigator / cartel leader",
                    depends_on=[1],
                    rationale="keep only cases where Bayer led",
                ),
            ],
        )
        answer = MasterAnswer(
            answer_md="CELEX-31994D0601 - Bayer AG acted as the ringleader.",
            confident=True,
            cited_celex_ids=["CELEX-31994D0601"],
        )
        dispatcher = _FakeDispatcher(
            {
                "doc_search": EvidenceResult(
                    text=(
                        "[doc pos=1 CELEX-31994D0601] Bayer AG acted as ringleader "
                        "and instigator of the cartel.\n"
                        "[doc pos=2 CELEX-32004D0337] Bayer AG was a participant only."
                    ),
                    chunk_ids=["c1", "c2"],
                )
            }
        )
        verifier = _FakeVerifier(
            {
                ("CELEX-31994D0601", "companies_investigated"): "verified_supported",
                ("CELEX-31994D0601", "company_recidivism"): "verified_supported",
            }
        )
        facts_by_celex = {
            "CELEX-31994D0601": [
                ("f1", "companies_investigated", "Bayer AG, BASF"),
                ("f2", "company_recidivism", "Bayer AG flagged as ringleader"),
            ],
            "CELEX-32004D0337": [("f3", "companies_investigated", "Bayer AG, Dow")],
        }
        module = _module(
            analysis=analysis,
            plan=plan,
            answer=answer,
            dispatcher=dispatcher,
            verifier=verifier,
            facts_by_celex=facts_by_celex,
        )

        pred = module.forward(
            "List all cases where company Bayer AG was the cartel leader"
        )

        self.assertEqual(pred.routing_decision, "master_planner")
        # The participant-only case is filtered out before verification.
        verified_celex = {celex for celex, _ in verifier.calls}
        self.assertEqual(verified_celex, {"CELEX-31994D0601"})
        joined_evidence = module.synthesize.calls[0]["joined_evidence"]
        self.assertIn("CELEX-31994D0601", joined_evidence)
        self.assertNotIn("CELEX-32004D0337", joined_evidence)


class TestBankingCartelsByCountryCounts(unittest.TestCase):
    """Q3: "Which countries have a lot of banking cartels and which have a low
    number?".

    count_group aggregation: the deterministic group-by-country count runs over
    the retrieved cases and the grouped counts (sorted high -> low) are rendered
    into the synthesizer evidence.
    """

    def test_grouped_country_counts_are_rendered(self) -> None:
        analysis = QueryAnalysis(
            entities=[
                ResolvedEntity(
                    surface="banking",
                    entity_type="sector",
                    available_in=["chunk", "fact"],
                ),
                ResolvedEntity(
                    surface="country",
                    entity_type="country",
                    available_in=["fact"],
                    candidate_fact_names=["companies_country"],
                ),
            ],
            target_type="count_group",
            aggregation="group_count",
            requires_statistics=False,
            rationale="count banking cartel cases grouped by country",
        )
        plan = MasterPlan(
            supported=True,
            target_type="count_group",
            subtasks=[
                SubTask(
                    task_id=1,
                    kind="retrieve",
                    tool="doc_search",
                    query="banking cartel",
                    rationale="find banking cartel cases",
                ),
                SubTask(
                    task_id=2,
                    kind="aggregate",
                    group_by="country",
                    depends_on=[1],
                    rationale="count cases per country",
                ),
            ],
        )
        answer = MasterAnswer(
            answer_md=(
                "Germany has the most banking cartel cases (3); France has the "
                "fewest (1)."
            ),
            confident=True,
            cited_celex_ids=[
                "CELEX-32001D0001",
                "CELEX-32001D0002",
                "CELEX-32001D0003",
                "CELEX-32001D0004",
            ],
        )
        dispatcher = _FakeDispatcher(
            {
                "doc_search": EvidenceResult(
                    text=(
                        "[doc pos=1 CELEX-32001D0001] German banking cartel.\n"
                        "[doc pos=2 CELEX-32001D0002] German banking cartel.\n"
                        "[doc pos=3 CELEX-32001D0003] German banking cartel.\n"
                        "[doc pos=4 CELEX-32001D0004] French banking cartel."
                    ),
                    chunk_ids=["c1", "c2", "c3", "c4"],
                )
            }
        )
        verifier = _FakeVerifier(
            {
                ("CELEX-32001D0001", "companies_country"): "verified_supported",
                ("CELEX-32001D0002", "companies_country"): "verified_supported",
                ("CELEX-32001D0003", "companies_country"): "verified_supported",
                ("CELEX-32001D0004", "companies_country"): "verified_supported",
            }
        )
        facts_by_celex = {
            "CELEX-32001D0001": [("f1", "companies_country", "Germany")],
            "CELEX-32001D0002": [("f2", "companies_country", "Germany")],
            "CELEX-32001D0003": [("f3", "companies_country", "Germany")],
            "CELEX-32001D0004": [("f4", "companies_country", "France")],
        }
        module = _module(
            analysis=analysis,
            plan=plan,
            answer=answer,
            dispatcher=dispatcher,
            verifier=verifier,
            facts_by_celex=facts_by_celex,
        )

        pred = module.forward(
            "Which countries have a lot of banking cartels and which have a "
            "low number?"
        )

        self.assertEqual(pred.routing_decision, "master_planner")
        joined_evidence = module.synthesize.calls[0]["joined_evidence"]
        # Grouped counts are rendered, sorted high -> low.
        self.assertIn("Grouped counts", joined_evidence)
        self.assertIn("Germany: 3 case(s)", joined_evidence)
        self.assertIn("France: 1 case(s)", joined_evidence)
        self.assertLess(
            joined_evidence.index("Germany: 3"), joined_evidence.index("France: 1")
        )
        # The synthesizer is told this is a grouped-count answer.
        self.assertEqual(module.synthesize.calls[0]["target_type"], "count_group")


class TestCaseListVerifyScoping(unittest.TestCase):
    """The case_list path must scope verification to the entity / role facts.

    Regression for the "Verifying contributing facts" stall: an entity-only
    case list previously verified every fact of every candidate case (~60 each).
    Only the membership / role facts should reach the verifier now.
    """

    def test_only_membership_facts_are_verified(self) -> None:
        analysis = QueryAnalysis(
            entities=[
                ResolvedEntity(
                    surface="Bayer AG",
                    entity_type="company",
                    available_in=["fact"],
                    candidate_fact_names=["companies_investigated"],
                )
            ],
            target_type="case_list",
            aggregation="none",
            requires_statistics=False,
            rationale="entity-only case list",
        )
        plan = MasterPlan(
            supported=True,
            target_type="case_list",
            subtasks=[
                SubTask(
                    task_id=1,
                    kind="retrieve",
                    tool="doc_search",
                    query="Bayer AG cartel",
                    rationale="candidate cases",
                )
            ],
        )
        answer = MasterAnswer(
            answer_md="Bayer AG appears in CELEX-31994D0601.",
            confident=True,
            cited_celex_ids=["CELEX-31994D0601"],
        )
        dispatcher = _FakeDispatcher(
            {
                "doc_search": EvidenceResult(
                    text="[doc pos=1 CELEX-31994D0601] Bayer AG was in the cartel.",
                    chunk_ids=["c1"],
                )
            }
        )
        verifier = _FakeVerifier(
            {("CELEX-31994D0601", "companies_investigated"): "verified_supported"}
        )
        # The case carries the membership fact plus many irrelevant facts that
        # must NOT reach the verifier.
        facts_by_celex = {
            "CELEX-31994D0601": [
                ("f1", "companies_investigated", "Bayer AG, BASF"),
                ("f2", "fines", "EUR 1m"),
                ("f3", "infringement_category_member_states", "DE"),
                ("f4", "market_geographical", "EEA"),
            ]
        }
        module = _module(
            analysis=analysis,
            plan=plan,
            answer=answer,
            dispatcher=dispatcher,
            verifier=verifier,
            facts_by_celex=facts_by_celex,
        )

        pred = module.forward("List all cases where Bayer AG was part of a cartel")

        self.assertEqual(pred.routing_decision, "master_planner")
        # Only the membership fact was verified - not fines / geography / etc.
        verified_facts = {name for _, name in verifier.calls}
        self.assertEqual(verified_facts, {"companies_investigated"})


class TestAnheuserBuschMembershipHedgedSynthesizer(unittest.TestCase):
    """Q5: "List all cases where Anheuser-Busch was part of a cartel".

    Mirrors a real failure: the planner emits two kb_sql legs
    (companies_investigated / fines) joined on celex_id; the join + verify produce
    two supported cases, but the LLM synthesizer hedges (``confident=False``).
    The agent must NOT return NA - it falls back to a deterministic case list
    built from the verified rows.
    """

    def test_hedged_synthesizer_falls_back_to_case_list(self) -> None:
        case_a = "40134_2872_5"  # AT.40134 - AB InBev
        case_b = "CELEX-32000D0146"  # Anheuser-Busch / Scottish & Newcastle
        other = "AT_40401_2770"  # in the fines leg only -> filtered by the join

        analysis = QueryAnalysis(
            entities=[
                ResolvedEntity(
                    surface="Anheuser-Busch",
                    entity_type="company",
                    available_in=["fact"],
                    candidate_fact_names=["companies_investigated", "fines"],
                )
            ],
            target_type="case_list",
            aggregation="none",
            requires_statistics=False,
            rationale="entity-only case list",
        )
        plan = MasterPlan(
            supported=True,
            target_type="case_list",
            subtasks=[
                SubTask(
                    task_id=1,
                    kind="retrieve",
                    tool="kb_sql",
                    query="SELECT DISTINCT celex_id FROM celex_facts.facts "
                    "WHERE name = 'companies_investigated' AND value_md ILIKE '%Anheuser%'",
                    rationale="companies_investigated leg",
                ),
                SubTask(
                    task_id=2,
                    kind="retrieve",
                    tool="kb_sql",
                    query="SELECT DISTINCT celex_id FROM celex_facts.facts "
                    "WHERE name = 'fines' AND value_md ILIKE '%Anheuser%'",
                    rationale="fines leg",
                ),
                SubTask(
                    task_id=3, kind="join", depends_on=[1, 2], rationale="intersect"
                ),
            ],
        )
        # The synthesizer hedges despite good evidence - the exact failure mode.
        hedged = MasterAnswer(answer_md="", confident=False, cited_celex_ids=[])

        def kb_sql(step):
            if "companies_investigated" in step.query:
                return EvidenceResult(text=f"[kb_sql] {case_a}\n{case_b}")
            return EvidenceResult(text=f"[kb_sql] {other}\n{case_a}\n{case_b}")

        dispatcher = _FakeDispatcher({"kb_sql": kb_sql})
        verifier = _FakeVerifier(
            {
                (case_a, "companies_investigated"): "verified_supported",
                (case_a, "fines"): "verified_supported",
                (case_b, "companies_investigated"): "verified_supported",
                (case_b, "fines"): "verified_supported",
            }
        )
        facts_by_celex = {
            case_a: [
                ("f1", "companies_investigated", "Anheuser-Busch InBev NV/SA"),
                ("f2", "fines", "Anheuser-Busch InBev NV/SA, InBev Belgium"),
            ],
            case_b: [
                ("f3", "companies_investigated", "Anheuser-Busch Incorporated"),
                ("f4", "fines", "Anheuser-Busch Incorporated, Scottish & Newcastle"),
            ],
            other: [("f5", "fines", "Some Other Brewer")],
        }
        module = _module(
            analysis=analysis,
            plan=plan,
            answer=hedged,
            dispatcher=dispatcher,
            verifier=verifier,
            facts_by_celex=facts_by_celex,
        )

        pred = module.forward(
            "List all cases where Anheuser-Busch was part of a cartel"
        )

        # The hedging synthesizer must NOT collapse a verified result into NA.
        self.assertEqual(pred.routing_decision, "master_planner")
        self.assertNotIn("NA", pred.answer)
        self.assertIn(case_a, pred.answer)
        self.assertIn(case_b, pred.answer)
        # The fines-only case is excluded by the join intersection.
        self.assertNotIn(other, pred.answer)


class TestBankingTrendIsOutOfScope(unittest.TestCase):
    """Q4: "Did the number of banking cartel cases increase after the leniency
    law?".

    Trend / before-after inference is out of scope: the analyze stage flags
    ``requires_statistics`` and the agent short-circuits to a clean NA with an
    offline-extraction suggestion - never a fabricated answer.
    """

    def test_trend_question_short_circuits_to_na(self) -> None:
        analysis = QueryAnalysis(
            entities=[
                ResolvedEntity(surface="banking", entity_type="sector"),
                ResolvedEntity(surface="leniency law", entity_type="law"),
            ],
            target_type="unknown",
            aggregation="count",
            requires_statistics=True,
            rationale="before/after trend over time is out of scope",
        )
        # No plan / answer fixtures: the pipeline must never reach decompose.
        module = _module(analysis=analysis)

        pred = module.forward(
            "Did the number of banking cartel cases increase after the leniency law?"
        )

        self.assertEqual(pred.routing_decision, "master_planner_na")
        self.assertTrue(pred.answer.startswith("NA"))
        self.assertIn("statistical or causal inference over time", pred.answer)
        self.assertIn("offline", pred.answer)
        # An NA carries no fabricated provenance.
        self.assertEqual(pred.fact_ids, [])
        self.assertEqual(pred.chunk_ids, [])
        self.assertEqual(pred.evidence, "")


@unittest.skipUnless(
    os.getenv("LIVE_SYNTH_SMOKE") == "1",
    "live LLM smoke; set LIVE_SYNTH_SMOKE=1 to run (needs the LLM host)",
)
class TestSynthesizerConfidentOnCleanEvidenceSmoke(unittest.TestCase):
    """Live check that the real synthesizer no longer hedges on clean evidence.

    Drives ONLY the real ``MasterSynthesizerSignature`` predictor against the
    configured LLM with pre-verified Anheuser-Busch rows. No DB / dispatcher /
    verifier - the evidence is the exact, already-normalized string the executor
    would hand the synthesizer. Asserts the model is confident and cites both
    cases, proving the prompt change cured the hedging end-to-end. Skipped
    unless ``LIVE_SYNTH_SMOKE=1`` and the LLM host is reachable.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Configure the real DSPy LM; skip the class if it is unavailable."""
        try:
            import dspy

            from config.llm_thinking import thinking_kwargs
            from config.settings import settings
            from pipelines.agent_dspy.master_planner_signature import (
                MasterSynthesizerSignature,
            )

            model_name = f"openai/{settings.LLM_MODEL_ID}"
            dspy.configure(
                lm=dspy.LM(
                    model_name,
                    api_base=f"{settings.OLLAMA_HOST}/v1",
                    api_key="ollama",
                    cache=False,
                    **thinking_kwargs(model_name),
                )
            )
            cls.synthesize = dspy.ChainOfThought(MasterSynthesizerSignature)
        except Exception as exc:  # LLM host down / config missing
            raise unittest.SkipTest(f"LLM host unavailable: {exc}")

    def test_clean_evidence_yields_confident_answer(self) -> None:
        case_a = "40134_2872_5"
        case_b = "CELEX-32000D0146"
        joined_evidence = (
            "=== Verified case rows ===\n"
            f"[{case_a}] companies_investigated = Anheuser-Busch InBev NV/SA\n"
            f"[{case_a}] fines = Anheuser-Busch InBev NV/SA, InBev Belgium\n"
            f"[{case_b}] companies_investigated = Anheuser-Busch Incorporated\n"
            f"[{case_b}] fines = "
            "Anheuser-Busch Incorporated, Scottish & Newcastle"
        )

        result = self.synthesize(
            question="List all cases where Anheuser-Busch was part of a cartel",
            joined_evidence=joined_evidence,
            target_type="case_list",
        ).result

        self.assertTrue(
            result.confident,
            msg="synthesizer hedged despite clean pre-verified evidence",
        )
        self.assertIn(case_a, result.answer_md)
        self.assertIn(case_b, result.answer_md)


if __name__ == "__main__":
    unittest.main()
