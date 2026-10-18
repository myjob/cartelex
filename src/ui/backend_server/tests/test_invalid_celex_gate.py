"""Regression tests for the case-reference gate as the web UI sees it.

Observed defect (prompt ``"set 02D0180"``): a mistyped CELEX number is not
recognised as a case-reference *attempt* at all, so ``CelexSelectionModule``
reports ``no_celex`` instead of ``invalid`` and the router happily continues
into the planner.  The user gets a retrieval plan and a synthesised answer for
a case that does not exist, instead of being told the identifier is wrong.

These tests drive ``oi_dspy.stream_generator`` — the exact SSE producer behind
``/v1/chat/completions`` — with a stubbed orchestrator, so the assertions are
made against the text the browser actually renders.

Isolation: only the system boundaries are stubbed.  The celex checker stands in
for the DB lookup and the QA pipeline stands in for planner/LLM execution; the
candidate-detection and gating logic under test is the real implementation.
"""

import json
import unittest
from unittest import mock

import dspy

import ui.backend_server.oi_dspy as oi_dspy
from pipelines.agent_dspy.dspy_signature import CelexValidationResult
from pipelines.agent_dspy.retrieval_modules import RetrievalEvent
from pipelines.agent_dspy.retrieval_router import RetrievalRouterModule
from tools.tests.celex_index_fixture import FakeCelexChecker

# The identifier from the reported session: a truncated CELEX number that
# resolves to nothing (the real document is CELEX-32002D0180).
_WRONG_BARE_ID = "02D0180"
_WRONG_PREFIXED_ID = "CELEX-02D0180"

# A case that does exist, used as the positive control.
_VALID_ID = "CELEX-32002D0180"

# UI-visible markers.  Emoji are stripped from the expectations on purpose —
# substring matching keeps the test independent of the decorative prefixes the
# router emits.
_VALIDATION_STATUS = "Validating case reference"
_PLANNER_STATUS = "Building retrieval plan"
_NOT_FOUND_MESSAGE = "was not found in the database"


class _StubCelexChecker(FakeCelexChecker):
    """DB boundary — resolves only the ids that exist in the corpus.

    Detection comes from the shared registry sample, so the gate sees the same
    identifier vocabulary it would in production.

    Args:
        known: Mapping of accepted candidate token to canonical celex_id.
    """

    def __init__(self, known: dict[str, str] | None = None) -> None:
        super().__init__(known if known is not None else {_VALID_ID: _VALID_ID})


class _StubQAPipeline:
    """Planner/execute/synthesize boundary — records every delegation."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_events(
        self,
        question: str,
        *,
        validation: CelexValidationResult | None = None,
        question_for_planner: str | None = None,
        pinned_celex_ids: list[str] | None = None,
    ):
        """Yield the planner status the UI shows, then a canned answer.

        Args:
            question: The delegated question.
            validation: Gate result handed down by the router.
            question_for_planner: Alias-rewritten question, unused here.
            pinned_celex_ids: Cases carried from the previous turn, unused here.

        Yields:
            One ``status`` event followed by a terminal ``result`` event.
        """
        self.calls.append(question)
        yield RetrievalEvent("status", f"{_PLANNER_STATUS} ...\n")
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                answer="stub-answer",
                plan=None,
                evidence="",
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
        """Return *answer* unchanged — provenance is out of scope here."""
        return answer, []


class _StubEnumeration:
    """Enumeration boundary — must never be reached by these prompts."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, params: object) -> dspy.Prediction:
        """Fail the test — no prompt in this suite may reach enumeration.

        Args:
            params: The ``FactExtractorParamVO`` the router would dispatch.

        Raises:
            AssertionError: Always.
        """
        self.calls.append(params)
        raise AssertionError("Enumeration must not run for a case-context prompt.")


class InvalidCelexGateTests(unittest.TestCase):
    """A mistyped case ID must terminate at the validation step."""

    def setUp(self) -> None:
        self.qa = _StubQAPipeline()
        self.enumeration = _StubEnumeration()
        self.checker = _StubCelexChecker()
        router = RetrievalRouterModule(
            qa_pipeline=self.qa,
            enumeration=self.enumeration,
            celex_checker=self.checker,
            master_planner=None,
        )
        self._patch = mock.patch.object(oi_dspy, "orchestrator", router)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _rendered_text(question: str) -> str:
        """Run the SSE generator and concatenate the streamed content deltas.

        Args:
            question: The prompt typed into the chat input.

        Returns:
            The full text the web UI renders for the assistant turn.
        """
        parts: list[str] = []
        for raw in oi_dspy.stream_generator(question, messages=None):
            payload = raw.removeprefix("data: ").strip()
            if not payload or payload == "[DONE]":
                continue
            delta = json.loads(payload)["choices"][0]["delta"]
            parts.append(delta.get("content", ""))
        return "".join(parts)

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_wrong_bare_celex_id_stops_at_validation(self) -> None:
        """'set 02D0180' must be rejected at the gate, not planned.

        This is the reported defect: the bare (prefix-less) CELEX number is
        never detected as a case-reference attempt, so the gate reports
        ``no_celex`` and the router falls through to the planner.
        """
        rendered = self._rendered_text(f"set {_WRONG_BARE_ID}")

        self.assertIn(_VALIDATION_STATUS, rendered)
        self.assertEqual(
            self.qa.calls,
            [],
            "The QA planner ran for a non-existent case id: "
            f"{self.qa.calls!r}",
        )
        self.assertNotIn(_PLANNER_STATUS, rendered)
        self.assertIn(_NOT_FOUND_MESSAGE, rendered)

    def test_wrong_prefixed_celex_id_stops_at_validation(self) -> None:
        """The CELEX-prefixed form of the same wrong id already aborts.

        Control for the test above: detection, not DB validation, is what
        fails for the bare form.
        """
        rendered = self._rendered_text(f"set {_WRONG_PREFIXED_ID}")

        self.assertIn(_VALIDATION_STATUS, rendered)
        self.assertEqual(self.qa.calls, [])
        self.assertNotIn(_PLANNER_STATUS, rendered)
        self.assertIn(_NOT_FOUND_MESSAGE, rendered)

    def test_valid_case_id_still_sets_context(self) -> None:
        """A resolvable id must keep acknowledging the context switch.

        Guards against a fix that simply rejects every unrecognised token.
        """
        rendered = self._rendered_text(f"set {_VALID_ID}")

        self.assertIn(_VALIDATION_STATUS, rendered)
        self.assertIn(f"Case context set to `{_VALID_ID}`", rendered)
        self.assertNotIn(_NOT_FOUND_MESSAGE, rendered)
        self.assertEqual(self.qa.calls, [])


if __name__ == "__main__":
    unittest.main()
