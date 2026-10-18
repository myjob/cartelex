"""Unit tests for the heuristic intent classifier and QueryRouterModule.

The router has two responsibilities and one external surface:
* ``classify_intent`` — pure function over the question string.
* ``QueryRouterModule.forward`` — dispatches to one of two injected
  strategies and tags the prediction with ``routing_decision``.

Both are exercised here with lightweight stubs that record which
sub-module was invoked. The downstream strategies themselves
(``UniversalFactModule`` / ``EnumerationFactModule``) are tested in
their own files; this suite verifies only the routing decision and
contract.
"""

import unittest

import dspy

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.query_router import (
    QueryRouterModule,
    classify_intent,
)


class _StubAgent:
    """Records every call it receives and returns a tagged ``dspy.Prediction``."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[FactExtractorParamVO] = []

    def __call__(self, params: FactExtractorParamVO) -> dspy.Prediction:
        self.calls.append(params)
        return dspy.Prediction(invoked_by=self.label)


def _params(question: str, target_fact_name: str = "test_fact") -> FactExtractorParamVO:
    return FactExtractorParamVO(
        target_fact_name=target_fact_name,
        question=question,
        celex_id="CELEX-TEST",
        output_schema="",
    )


class TestClassifyIntent(unittest.TestCase):
    """Heuristic classifier — pure function, no I/O."""

    def test_enumeration_phrasings(self):
        """A representative set of "list / enumerate / every" phrasings."""
        enumeration_questions = [
            "List all courts with assessment, order, ruling, decision or judgement?",
            "list every party involved",
            "Enumerate the fines imposed",
            "All courts with rulings",
            "Which courts heard the appeal?",
            "Identify every applicant for leniency",
        ]
        for q in enumeration_questions:
            with self.subTest(question=q):
                self.assertEqual(
                    classify_intent(q),
                    "enumeration",
                    f"Expected enumeration intent for: {q!r}",
                )

    def test_qa_phrasings(self):
        """Direct-answer questions must NOT be misrouted to enumeration."""
        qa_questions = [
            "Who was the commissioner?",
            "When did the EU commission decide this case?",
            "What was the cartel period?",
            "Is this an abuse of dominance case?",
            "Summarize the case into topics",
        ]
        for q in qa_questions:
            with self.subTest(question=q):
                self.assertEqual(
                    classify_intent(q),
                    "qa",
                    f"Expected qa intent for: {q!r}",
                )

    def test_empty_question_defaults_to_qa(self):
        """Empty / falsy questions must not crash and must default to qa."""
        self.assertEqual(classify_intent(""), "qa")

    def test_case_insensitive(self):
        """Patterns must match regardless of casing."""
        self.assertEqual(classify_intent("LIST ALL COURTS"), "enumeration")
        self.assertEqual(classify_intent("EvErY ApPlIcAnT"), "enumeration")


class TestQueryRouterModuleDispatch(unittest.TestCase):
    """Verify the router delegates correctly and tags the prediction."""

    def setUp(self) -> None:
        self.qa = _StubAgent(label="qa")
        self.enum = _StubAgent(label="enum")
        self.router = QueryRouterModule(qa=self.qa, enumeration=self.enum)

    def test_enumeration_question_routes_to_enumeration_module(self):
        params = _params("List all courts mentioned")
        prediction = self.router(params=params)

        self.assertEqual(len(self.enum.calls), 1)
        self.assertEqual(len(self.qa.calls), 0)
        self.assertEqual(prediction.invoked_by, "enum")
        self.assertEqual(prediction.routing_decision, "enumeration")

    def test_qa_question_routes_to_qa_module(self):
        params = _params("Who was the commissioner?")
        prediction = self.router(params=params)

        self.assertEqual(len(self.qa.calls), 1)
        self.assertEqual(len(self.enum.calls), 0)
        self.assertEqual(prediction.invoked_by, "qa")
        self.assertEqual(prediction.routing_decision, "qa")

    def test_router_passes_params_through_unchanged(self):
        """The downstream strategy must receive the exact params object."""
        params = _params("Enumerate the applicants", target_fact_name="leniency_all")
        self.router(params=params)
        delivered = self.enum.calls[0]
        self.assertIs(delivered, params)
        self.assertEqual(delivered.target_fact_name, "leniency_all")


if __name__ == "__main__":
    unittest.main()
