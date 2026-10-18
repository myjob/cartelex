"""Unit tests for the retrieval-pipeline intent router.

The router has two pure-function components and one orchestrator:

* ``classify_retrieval_intent`` — pure function over the question string,
  combining enumeration-intent and celex-presence axes.
* ``infer_enumeration_schema`` — pure function mapping a question to a
  ``(target_fact_name, output_schema)`` pair via a known-entity table
  plus a singularising fallback.
* ``RetrievalRouterModule.forward`` — dispatches to one of two injected
  strategies (QA pipeline / enumeration module) and tags the prediction
  with ``routing_decision``.

The orchestrator is exercised with lightweight stubs that record their
calls. The downstream pipelines themselves (``QARetrievalPipeline`` /
``EnumerationFactModule``) are tested in their own files; this suite
verifies only the routing decision and output contract.
"""

import unittest

import dspy

from pipelines.agent_dspy.dspy_protocols import FactOutput
from pipelines.agent_dspy.dspy_signature import CelexValidationResult
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.retrieval_modules import RetrievalEvent
from tools.celex_checker import NOT_FOUND
from tools.tests.celex_index_fixture import FakeCelexChecker
from pipelines.agent_dspy.retrieval_router import (
    RetrievalRouterModule,
    _is_celex_only_prompt,
    classify_retrieval_intent,
    format_enumeration_answer,
    infer_enumeration_schema,
)


class _StubQAPipeline:
    """Records every QA delegation and yields a canned result via run_events()."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.validations: list[CelexValidationResult] = []
        self.attach_calls: list[tuple[str, list[str]]] = []

    def run_events(
        self,
        question: str,
        *,
        validation: CelexValidationResult | None = None,
        question_for_planner: str | None = None,
        pinned_celex_ids: list[str] | None = None,
    ):
        self.calls.append(question)
        self.validations.append(validation)
        yield RetrievalEvent("status", "📋 Building retrieval plan …\n")
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                answer="qa-stub",
                plan=None,
                evidence="qa-stub-evidence",
                celex_validation=validation
                or CelexValidationResult(status="no_celex"),
                references=[],
                fact_ids=[],
                chunk_ids=[],
                fact_names=[],
            ),
        )

    def attach_references(
        self,
        answer: str,
        validation: CelexValidationResult,
        fact_names: list[str],
        question: str = "",
        *,
        highlight: bool = True,
        fallback: bool = True,
    ) -> tuple[str, list]:
        self.attach_calls.append((answer, fact_names))
        return answer, []


class _StubEnumeration:
    """Records every call it receives and returns a canned enumeration result."""

    def __init__(self, items: list[dict] | None = None) -> None:
        self.calls: list[FactExtractorParamVO] = []
        self._items = items if items is not None else [{"court": "BGH"}]

    def __call__(self, params: FactExtractorParamVO) -> dspy.Prediction:
        self.calls.append(params)
        return dspy.Prediction(
            output=FactOutput(items=self._items, source="enumeration")
        )


class _StubCelexChecker(FakeCelexChecker):
    """``CelexValidator`` that resolves every detected token, or rejects all.

    Args:
        canonical: The celex_id every candidate resolves to; None rejects
            everything so the ``invalid`` branch can be exercised.
    """

    def __init__(self, canonical: str | None = "CELEX-31991D0562") -> None:
        super().__init__({})
        self._canonical = canonical

    def check_celex_or_case_id(self, candidate: str) -> str:
        """Return the fixed canonical id, or ``"NA"``.

        Args:
            candidate: The token to resolve.

        Returns:
            The configured canonical id, or ``"NA"``.
        """
        self.calls.append(candidate)
        return self._canonical if self._canonical else NOT_FOUND


class TestClassifyRetrievalIntent(unittest.TestCase):
    """Two-axis classifier — pure function, no I/O."""

    def test_enumeration_with_celex_returns_enumeration(self):
        """Both axes hold — enumeration intent AND celex_id present."""
        self.assertEqual(
            classify_retrieval_intent(
                "list all courts in case CELEX-31991D0562",
                has_case_reference=True,
            ),
            "enumeration",
        )

    def test_enumeration_without_celex_returns_qa(self):
        """Cross-case enumeration must NOT take the single-case path."""
        cross_case_questions = [
            "list all courts across all cases",
            "Enumerate every fine ever imposed",
            "Which courts have heard cartel appeals?",
        ]
        for q in cross_case_questions:
            with self.subTest(question=q):
                self.assertEqual(
                    classify_retrieval_intent(q, has_case_reference=False), "qa"
                )

    def test_qa_phrasings_return_qa(self):
        """Pure-QA phrasings stay on the QA pipeline regardless of celex."""
        qa_questions = [
            "How many cases had leniency?",
            "Who was the commissioner in CELEX-31991D0562?",
            "When did the commission decide CELEX-31991D0562?",
            "Summarize CELEX-31991D0562",
        ]
        for q in qa_questions:
            with self.subTest(question=q):
                self.assertEqual(
                    classify_retrieval_intent(q, has_case_reference=True), "qa"
                )

    def test_empty_question_defaults_to_qa(self):
        self.assertEqual(
            classify_retrieval_intent("", has_case_reference=True), "qa"
        )

    def test_case_insensitive(self):
        self.assertEqual(
            classify_retrieval_intent(
                "LIST ALL COURTS IN celex-31991D0562", has_case_reference=True
            ),
            "enumeration",
        )


class TestInferEnumerationSchema(unittest.TestCase):
    """Two-tier schema inference — known table, then generic fallback."""

    def test_known_entity_court(self):
        self.assertEqual(
            infer_enumeration_schema("list all courts in CELEX-31991D0562"),
            ("court_list_raw", "court, country, jurisdiction"),
        )

    def test_known_entity_fines(self):
        self.assertEqual(
            infer_enumeration_schema("list all fines in CELEX-31991D0562"),
            ("fines", "company, amount"),
        )

    def test_known_entity_penalties_maps_to_fines(self):
        self.assertEqual(
            infer_enumeration_schema("enumerate every penalty in this case"),
            ("fines", "company, amount"),
        )

    def test_known_entity_applicants(self):
        self.assertEqual(
            infer_enumeration_schema("list all applicants in CELEX-31991D0562"),
            ("leniency_applicants", "applicant"),
        )

    def test_generic_fallback_singularises_unknown_noun(self):
        """Unknown enumeration target falls through to singularised noun."""
        fact_name, schema = infer_enumeration_schema(
            "list all subsidiaries in CELEX-31991D0562"
        )
        self.assertEqual(fact_name, "subsidiary")
        self.assertEqual(schema, "subsidiary")

    def test_ultimate_fallback_when_no_pattern_matches(self):
        """Empty / unparseable input — returns the placeholder schema."""
        self.assertEqual(infer_enumeration_schema(""), ("entities", "entity"))


class TestFormatEnumerationAnswer(unittest.TestCase):
    """The markdown formatter that bridges FactOutput → answer string."""

    def test_empty_items_returns_friendly_message(self):
        answer = format_enumeration_answer([], "court_list_raw", "CELEX-31991D0562")
        self.assertIn("No court_list_raw", answer)
        self.assertIn("CELEX-31991D0562", answer)

    def test_single_key_items_render_as_bullet_list(self):
        answer = format_enumeration_answer(
            [{"applicant": "Acme"}, {"applicant": "Beta"}],
            "leniency_applicants",
            "CELEX-31991D0562",
        )
        self.assertIn("- Acme", answer)
        self.assertIn("- Beta", answer)
        self.assertNotIn("|", answer)  # not a table

    def test_multi_key_items_render_as_markdown_table(self):
        answer = format_enumeration_answer(
            [
                {"court": "BGH", "country": "DE"},
                {"court": "Cour de cassation", "country": "FR"},
            ],
            "court_list_raw",
            "CELEX-31991D0562",
        )
        self.assertIn("| court | country |", answer)
        self.assertIn("| --- | --- |", answer)
        self.assertIn("| BGH | DE |", answer)


class TestRetrievalRouterModuleDispatch(unittest.TestCase):
    """Verify the router delegates correctly and tags the prediction."""

    def setUp(self) -> None:
        self.qa = _StubQAPipeline()
        self.enum = _StubEnumeration(items=[{"court": "BGH", "country": "DE"}])
        self.checker = _StubCelexChecker(canonical="CELEX-31991D0562")
        self.router = RetrievalRouterModule(
            qa_pipeline=self.qa,
            enumeration=self.enum,
            celex_checker=self.checker,
        )

    def test_enumeration_with_valid_celex_dispatches_to_enumeration(self):
        prediction = self.router(question="list all courts in CELEX-31991D0562")

        self.assertEqual(len(self.enum.calls), 1)
        self.assertEqual(len(self.qa.calls), 0)
        self.assertEqual(prediction.routing_decision, "enumeration")

        params = self.enum.calls[0]
        self.assertIsInstance(params, FactExtractorParamVO)
        self.assertEqual(params.celex_id, "CELEX-31991D0562")
        self.assertEqual(params.target_fact_name, "court_list_raw")

    def test_enumeration_answer_contains_extracted_entities(self):
        prediction = self.router(question="list all courts in CELEX-31991D0562")
        self.assertIn("BGH", prediction.answer)

    def test_enumeration_with_invalid_celex_aborts(self):
        """A malformed celex/case ID now aborts at the single upfront gate.

        (Previously the router fell back to the QA pipeline, which then
        re-gated and aborted anyway — the single gate makes this explicit.)
        """
        invalid_checker = _StubCelexChecker(canonical=None)
        router = RetrievalRouterModule(
            qa_pipeline=self.qa,
            enumeration=self.enum,
            celex_checker=invalid_checker,
        )

        prediction = router(question="list all courts in CELEX-99999X9999")

        self.assertEqual(len(self.qa.calls), 0)
        self.assertEqual(len(self.enum.calls), 0)
        self.assertEqual(prediction.routing_decision, "invalid")
        self.assertEqual(prediction.celex_validation.status, "invalid")

    def test_qa_question_routes_to_qa_pipeline(self):
        prediction = self.router(question="Who was the commissioner?")

        self.assertEqual(len(self.qa.calls), 1)
        self.assertEqual(len(self.enum.calls), 0)
        self.assertEqual(prediction.routing_decision, "qa")
        self.assertEqual(prediction.answer, "qa-stub")

    def test_run_events_emits_status_then_terminal_result(self):
        """The streaming API yields ≥1 status event then exactly one result."""
        events = list(self.router.run_events("Who was the commissioner?"))
        kinds = [e.kind for e in events]
        self.assertIn("status", kinds)
        self.assertEqual(kinds[-1], "result")
        self.assertEqual(sum(k == "result" for k in kinds), 1)
        self.assertEqual(events[-1].prediction.answer, "qa-stub")

    def test_sticky_fallback_celex_promotes_to_qa_with_valid_validation(self):
        """A follow-up with no inline id uses the active case as a valid gate."""
        events = list(
            self.router.run_events("summary", fallback_celex_id="CELEX-31991D0562")
        )
        # QA delegated with a *valid* validation derived from the sticky id.
        self.assertEqual(len(self.qa.calls), 1)
        self.assertEqual(self.qa.validations[0].status, "valid")
        self.assertEqual(
            self.qa.validations[0].canonical_celex_id, "CELEX-31991D0562"
        )
        # The "active case" status is surfaced for the UI.
        self.assertTrue(any("active case" in e.text for e in events))

    def test_celex_only_prompt_emits_case_context_set(self):
        """A bare case id sets context and does not run QA / enumeration."""
        events = list(self.router.run_events("CELEX-31991D0562"))
        kinds = [e.kind for e in events]
        self.assertIn("case_context_set", kinds)
        self.assertEqual(len(self.qa.calls), 0)
        self.assertEqual(len(self.enum.calls), 0)
        terminal = events[-1]
        self.assertEqual(terminal.prediction.routing_decision, "case_context")

    def test_output_shape_contract_on_enumeration_path(self):
        """Both paths must expose the same attributes for downstream callers."""
        prediction = self.router(question="list all courts in CELEX-31991D0562")
        for attr in ("answer", "plan", "evidence", "celex_validation", "routing_decision"):
            with self.subTest(attribute=attr):
                self.assertTrue(
                    hasattr(prediction, attr),
                    f"Enumeration prediction missing attribute {attr!r}",
                )

    def test_output_shape_contract_on_qa_path(self):
        prediction = self.router(question="How many cases had leniency?")
        for attr in ("answer", "plan", "evidence", "celex_validation", "routing_decision"):
            with self.subTest(attribute=attr):
                self.assertTrue(
                    hasattr(prediction, attr),
                    f"QA prediction missing attribute {attr!r}",
                )


class TestIsCelexOnlyPrompt(unittest.TestCase):
    """Pure detector for "set a case, ask nothing" prompts."""

    def test_bare_id_is_celex_only(self):
        self.assertTrue(_is_celex_only_prompt("CELEX-31991D0562", "CELEX-31991D0562"))

    def test_framing_command_is_celex_only(self):
        self.assertTrue(
            _is_celex_only_prompt("set celex to 37766_610_5", "37766_610_5")
        )

    def test_question_with_id_is_not_celex_only(self):
        self.assertFalse(
            _is_celex_only_prompt("summary of CELEX-31991D0562", "CELEX-31991D0562")
        )

    def test_no_candidate_is_not_celex_only(self):
        self.assertFalse(_is_celex_only_prompt("anything", None))


if __name__ == "__main__":
    unittest.main()
