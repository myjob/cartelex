"""Unit tests for the rule-first decision-section classifier.

Heading fixtures are real headings observed in the live corpus (e.g.
CELEX-32002D0759 doc_pos 42 starts ``# 3.4.4. FINAL AMOUNT OF THE FINES``).
Pure tests — no DB, no LLM; the DSPy module is exercised with a stub
predictor.
"""

import unittest

import dspy

from extract.rag.models.chunk_vo import SectionType
from pipelines.legal.section_classifier import (
    SectionClassification,
    SectionClassifierModule,
    classify_chunk_by_rules,
    classify_heading,
    forward_fill,
    iter_headings,
    rule_labels_plausible,
)


class TestIterHeadings(unittest.TestCase):
    """Heading extraction from chunk text (splitter keeps heading lines)."""

    def test_extracts_headings_in_order(self) -> None:
        text = "# I. THE FACTS\nSome body text.\n## A. The product\nMore text."
        self.assertEqual(iter_headings(text), ["I. THE FACTS", "A. The product"])

    def test_no_headings_returns_empty(self) -> None:
        self.assertEqual(iter_headings("Plain body text without headings."), [])


class TestClassifyHeading(unittest.TestCase):
    """Keyword rules over real corpus headings."""

    def test_real_corpus_headings(self) -> None:
        cases = {
            "3.4.4. FINAL AMOUNT OF THE FINES": SectionType.FINES,
            "8. REMEDIES": SectionType.FINES,
            "Article 4": SectionType.OPERATIVE,
            "II. LEGAL ASSESSMENT": SectionType.LEGAL_ASSESSMENT,
            "B. ARTICLE 85 (3)": SectionType.LEGAL_ASSESSMENT,
            "APPLICABILITY OF ARTICLE 81": SectionType.LEGAL_ASSESSMENT,
            "I. THE FACTS": SectionType.RECITALS_FACTS,
            "1. INTRODUCTION": SectionType.RECITALS_FACTS,
            "3. PROCEDURE": SectionType.RECITALS_FACTS,
            "WHEREAS :": SectionType.RECITALS_FACTS,
            "THE PARTIES": SectionType.PARTIES,
            "THE UNDERTAKINGS CONCERNED": SectionType.PARTIES,
            "ANNEX I": SectionType.ANNEX,
            "TABLE OF CONTENTS": SectionType.OTHER,
        }
        for heading, expected in cases.items():
            with self.subTest(heading=heading):
                self.assertIs(classify_heading(heading), expected)

    def test_treaty_article_reference_is_not_operative(self) -> None:
        """'ARTICLE 85 (3)' is a treaty reference, not a final Article."""
        self.assertIs(classify_heading("ARTICLE 85 (3)"), SectionType.LEGAL_ASSESSMENT)

    def test_party_comments_beat_parties(self) -> None:
        """'COMMENTS OF THE PARTIES' is assessment, not the parties section."""
        self.assertIs(
            classify_heading("V. COMMENTS OF THE PARTIES"),
            SectionType.LEGAL_ASSESSMENT,
        )

    def test_unknown_heading_abstains(self) -> None:
        """Bare numbering carries no signal — never a guess."""
        self.assertIsNone(classify_heading("1. Situation where the parties agree"))

    def test_ocr_garbled_heading_abstains(self) -> None:
        self.assertIsNone(classify_heading("lI7. TlHE F4CTS oF thE C&SE##"))


class TestClassifyChunkByRules(unittest.TestCase):
    """Chunk-level precedence: operative marker > heading > cover default."""

    def test_operative_marker_wins(self) -> None:
        text = "concluding recitals,\n\nHAS ADOPTED THIS DECISION:\n\nArticle 1\n..."
        self.assertIs(classify_chunk_by_rules(text, 40), SectionType.OPERATIVE)

    def test_operative_marker_following_decision_spelling(self) -> None:
        text = "HAS  ADOPTED THE\nFOLLOWING DECISION:"
        self.assertIs(classify_chunk_by_rules(text, 40), SectionType.OPERATIVE)

    def test_first_classifiable_heading(self) -> None:
        text = "# 3.4.4. FINAL AMOUNT OF THE FINES\nThe fines are set as follows."
        self.assertIs(classify_chunk_by_rules(text, 42), SectionType.FINES)

    def test_doc_pos_zero_defaults_to_parties(self) -> None:
        self.assertIs(
            classify_chunk_by_rules("Commission Decision of 5 June 2002", 0),
            SectionType.PARTIES,
        )

    def test_unheaded_mid_document_chunk_abstains(self) -> None:
        self.assertIsNone(classify_chunk_by_rules("Plain recital body text.", 17))


class TestForwardFill(unittest.TestCase):
    """Sections are contiguous: gaps inherit the preceding label."""

    def test_fills_gaps_after_first_label(self) -> None:
        labels = [
            SectionType.PARTIES,
            None,
            SectionType.RECITALS_FACTS,
            None,
            None,
            SectionType.FINES,
        ]
        self.assertEqual(
            forward_fill(labels),
            [
                SectionType.PARTIES,
                SectionType.PARTIES,
                SectionType.RECITALS_FACTS,
                SectionType.RECITALS_FACTS,
                SectionType.RECITALS_FACTS,
                SectionType.FINES,
            ],
        )

    def test_leading_nones_stay_none(self) -> None:
        labels = [None, None, SectionType.RECITALS_FACTS, None]
        self.assertEqual(
            forward_fill(labels),
            [None, None, SectionType.RECITALS_FACTS, SectionType.RECITALS_FACTS],
        )

    def test_operative_persists_until_annex(self) -> None:
        labels = [SectionType.OPERATIVE, None, None, SectionType.ANNEX, None]
        self.assertEqual(
            forward_fill(labels),
            [
                SectionType.OPERATIVE,
                SectionType.OPERATIVE,
                SectionType.OPERATIVE,
                SectionType.ANNEX,
                SectionType.ANNEX,
            ],
        )


class TestRuleLabelsPlausible(unittest.TestCase):
    """The gate that keeps OCR-garbled documents away from rule labels."""

    def test_canonical_order_passes(self) -> None:
        labels = [
            SectionType.PARTIES,
            SectionType.RECITALS_FACTS,
            None,
            SectionType.LEGAL_ASSESSMENT,
            None,
            SectionType.FINES,
            SectionType.OPERATIVE,
            None,
        ]
        self.assertTrue(rule_labels_plausible(labels))

    def test_random_order_rejected(self) -> None:
        """OCR junk matching rules at random violates the canonical order."""
        labels = [
            SectionType.OPERATIVE,
            SectionType.PARTIES,
            SectionType.FINES,
            SectionType.RECITALS_FACTS,
            SectionType.OPERATIVE,
            SectionType.PARTIES,
        ]
        self.assertFalse(rule_labels_plausible(labels))

    def test_single_legitimate_regression_tolerated(self) -> None:
        """Real decisions regress occasionally (e.g. ADDRESSEES after fines)."""
        labels = [
            SectionType.PARTIES,
            SectionType.RECITALS_FACTS,
            SectionType.LEGAL_ASSESSMENT,
            SectionType.FINES,
            SectionType.LEGAL_ASSESSMENT,
            SectionType.OPERATIVE,
        ]
        self.assertTrue(rule_labels_plausible(labels))

    def test_too_few_hits_rejected(self) -> None:
        labels: list = [None] * 20
        labels[3] = SectionType.FINES
        self.assertFalse(rule_labels_plausible(labels))

    def test_low_coverage_rejected(self) -> None:
        labels: list = [None] * 100
        labels[10] = SectionType.RECITALS_FACTS
        labels[90] = SectionType.OPERATIVE
        self.assertFalse(rule_labels_plausible(labels))

    def test_empty_rejected(self) -> None:
        self.assertFalse(rule_labels_plausible([]))

    def test_other_labels_excluded_from_order_check(self) -> None:
        """OTHER floats (cover pages, TOC) and must not count as regression."""
        labels = [
            SectionType.PARTIES,
            SectionType.OTHER,
            SectionType.RECITALS_FACTS,
            SectionType.OTHER,
            SectionType.OPERATIVE,
        ]
        self.assertTrue(rule_labels_plausible(labels))


class _StubPredictor:
    """DSPy-predictor stand-in returning a canned SectionClassification."""

    def __init__(self, section_type: str, reason: str = "stub") -> None:
        self._payload = {"section_type": section_type, "reason": reason}

    def __call__(self, **kwargs) -> dspy.Prediction:
        return dspy.Prediction(output=self._payload)


class TestSectionClassifierModule(unittest.TestCase):
    """LLM fallback module with the predictor seam (no live model)."""

    def test_valid_output_is_validated_to_enum(self) -> None:
        module = SectionClassifierModule(predictor=_StubPredictor("fines"))
        prediction = module(chunk_text="The fine is set at EUR 30 million.")
        self.assertIsInstance(prediction.output, SectionClassification)
        self.assertIs(prediction.output.section_type, SectionType.FINES)

    def test_invalid_section_value_raises(self) -> None:
        module = SectionClassifierModule(predictor=_StubPredictor("not_a_section"))
        with self.assertRaises(Exception):
            module(chunk_text="whatever")


if __name__ == "__main__":
    unittest.main()
