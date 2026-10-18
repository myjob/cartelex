"""Router-seam tests: when does the Master Planner fallback fire?"""

import types
import unittest

import dspy

from pipelines.agent_dspy.retrieval_modules import RetrievalEvent
from pipelines.agent_dspy.retrieval_router import RetrievalRouterModule
from tools.tests.celex_index_fixture import FakeCelexChecker


class _FakeCelexChecker(FakeCelexChecker):
    """Resolves nothing — every question is treated as cross-case (no_celex)."""

    def __init__(self) -> None:
        super().__init__({})


class _FakeQAPipeline:
    """Stand-in QARetrievalPipeline yielding a fixed terminal result."""

    def __init__(self, answer: str, evidence: str) -> None:
        self._answer = answer
        self._evidence = evidence
        self.attach_references = lambda **kw: (kw.get("answer", ""), [])

    def run_events(
        self,
        question,
        *,
        validation=None,
        question_for_planner=None,
        pinned_celex_ids=None,
    ):
        yield RetrievalEvent("status", "qa working...\n")
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                celex_validation=validation,
                plan=None,
                evidence=self._evidence,
                answer=self._answer,
                references=[],
                fact_ids=[],
                chunk_ids=[],
                fact_names=[],
            ),
        )


class _FakeMasterPlanner:
    """Records invocation and yields a distinctive terminal result."""

    def __init__(self) -> None:
        self.invoked_with: list[str] = []

    def run_events(self, question, validation=None):
        self.invoked_with.append(question)
        yield RetrievalEvent("status", "[master] working...\n")
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                celex_validation=validation,
                plan=None,
                evidence="master evidence",
                answer="MASTER ANSWER: CELEX-31994D0601",
                references=[],
                fact_ids=[],
                chunk_ids=[],
                fact_names=[],
                routing_decision="master_planner",
            ),
        )


def _build_router(qa: _FakeQAPipeline, master=None) -> RetrievalRouterModule:
    return RetrievalRouterModule(
        qa_pipeline=qa,
        enumeration=types.SimpleNamespace(),  # never reached on the QA path
        celex_checker=_FakeCelexChecker(),
        master_planner=master,
    )


def _terminal(router: RetrievalRouterModule, question: str):
    result = None
    for event in router.run_events(question):
        if event.kind == "result" and event.prediction is not None:
            result = event.prediction
    return result


class TestRouterFallbackGate(unittest.TestCase):
    def test_sufficient_qa_result_skips_master_planner(self) -> None:
        qa = _FakeQAPipeline(
            answer="CELEX-31994D0601 - Cartonboard. Fine was 132m EUR, court upheld it.",
            evidence="--- Step 1 (case_facts) ---\nfines: ...",
        )
        master = _FakeMasterPlanner()
        router = _build_router(qa, master)

        result = _terminal(
            router, "Which countries have a lot of banking cartels?"
        )
        self.assertEqual(master.invoked_with, [])
        self.assertIn("CELEX-31994D0601", result.answer)

    def test_insufficient_and_compositional_invokes_master_planner(self) -> None:
        qa = _FakeQAPipeline(answer="No relevant cases found.", evidence="No rows returned.")
        master = _FakeMasterPlanner()
        router = _build_router(qa, master)

        result = _terminal(
            router, "List all cases where company Bayer AG was the cartel leader"
        )
        self.assertEqual(len(master.invoked_with), 1)
        self.assertEqual(result.routing_decision, "master_planner")
        self.assertIn("MASTER ANSWER", result.answer)

    def test_insufficient_but_not_compositional_skips_master_planner(self) -> None:
        qa = _FakeQAPipeline(answer="No relevant cases found.", evidence="No rows returned.")
        master = _FakeMasterPlanner()
        router = _build_router(qa, master)

        result = _terminal(router, "What is the definition of leniency?")
        self.assertEqual(master.invoked_with, [])
        # The held QA result passes through unchanged.
        self.assertIn("No relevant cases", result.answer)

    def test_no_master_planner_yields_qa_result(self) -> None:
        qa = _FakeQAPipeline(answer="No relevant cases found.", evidence="No rows returned.")
        router = _build_router(qa, master=None)

        result = _terminal(
            router, "List all cases where company Bayer AG was the cartel leader"
        )
        self.assertIn("No relevant cases", result.answer)


if __name__ == "__main__":
    unittest.main()
