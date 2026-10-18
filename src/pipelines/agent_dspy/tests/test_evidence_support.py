"""
Unit tests for the shared evidence-support helpers.

Covers the surface-folding quote containment used by the book cross-check
(``quote_in_text``) and the judge-call wrapper's forwarding of the optional
case reference. No DB or LLM is touched.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.agent_dspy.evidence_support import (
    EvidenceSupportResult,
    judge_support,
    normalise_surface,
    quote_in_text,
)


class TestQuoteContainment(unittest.TestCase):
    """Conversion artefacts are folded; word choice is not."""

    def test_exact_substring_matches(self):
        self.assertTrue(quote_in_text("ran a cartel", "Acme ran a cartel."))

    def test_whitespace_runs_and_line_breaks_are_folded(self):
        self.assertTrue(
            quote_in_text("Acme ran a cartel", "Acme  ran\n  a\tcartel in 1990.")
        )

    def test_hyphenated_line_break_is_joined(self):
        self.assertTrue(
            quote_in_text("the decision of 28 July", "In the deci-\nsion of 28 July 1978")
        )

    def test_soft_hyphen_and_ligature_are_folded(self):
        self.assertTrue(quote_in_text("fine", "a ﬁne of"))
        self.assertTrue(quote_in_text("cartel", "car­tel"))

    def test_typographic_quotes_and_dashes_are_folded(self):
        self.assertTrue(
            quote_in_text('"fine" - EUR 10', "a “fine” – EUR 10 million")
        )

    def test_paraphrase_still_fails(self):
        self.assertFalse(quote_in_text("a large fine", "a big fine"))

    def test_empty_or_missing_quote_never_matches(self):
        self.assertFalse(quote_in_text(None, "anything"))
        self.assertFalse(quote_in_text("", "anything"))
        self.assertFalse(quote_in_text("   ", "anything"))

    def test_case_is_significant_unless_asked(self):
        self.assertFalse(quote_in_text("comp/123", "Case COMP/123"))
        self.assertTrue(quote_in_text("comp/123", "Case COMP/123", ignore_case=True))

    def test_normalise_surface_strips(self):
        self.assertEqual(normalise_surface("  a  b \n c "), "a b c")


class TestJudgeSupportForwarding(unittest.TestCase):
    """``judge_support`` forwards the optional rubric and case reference."""

    def _judge(self):
        return MagicMock(
            return_value=SimpleNamespace(
                output=EvidenceSupportResult(label="insufficient", support_score=0.1)
            )
        )

    def test_case_reference_forwarded(self):
        judge = self._judge()
        judge_support(judge, "s", "r", "chunk", question="q", case_reference="IV/26.699")
        self.assertEqual(judge.call_args.kwargs["case_reference"], "IV/26.699")
        self.assertEqual(judge.call_args.kwargs["extraction_question"], "q")

    def test_missing_case_reference_is_empty_string(self):
        judge = self._judge()
        judge_support(judge, "s", "r", "chunk")
        self.assertEqual(judge.call_args.kwargs["case_reference"], "")
        self.assertEqual(judge.call_args.kwargs["extraction_question"], "")


if __name__ == "__main__":
    unittest.main()
