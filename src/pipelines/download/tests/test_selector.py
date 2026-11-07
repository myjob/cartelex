"""Tests for the EN-first candidate selector."""

import unittest

from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    SelectionStatus,
)
from pipelines.download.selector import CandidateSelector


def _cand(
    doc_id: str,
    language: str | None,
    origin: str = "eur-lex",
    file_format: str = "pdf",
) -> CandidateDocument:
    """Build a candidate for tests."""
    return CandidateDocument(
        document_id=doc_id,
        source_url=f"https://example.org/{doc_id}",
        file_format=file_format,
        language=language,
        origin=origin,
    )


def _decision(candidates: list[CandidateDocument]) -> DecisionRequest:
    """Build a decision around the given candidates."""
    return DecisionRequest(
        decision_id="D1", candidates=candidates, loader_file="f.csv", loader_row=1
    )


class CandidateSelectorTest(unittest.TestCase):
    """The four selection rules of the design."""

    def setUp(self) -> None:
        """Create the selector."""
        self.selector = CandidateSelector()

    def test_en_wins_with_origin_priority(self) -> None:
        """EN candidates: eur-lex beats ec.europa.eu beats db-comp."""
        cands = [
            _cand("a", "EN", "db-comp"),
            _cand("b", "EN", "ec.europa.eu"),
            _cand("c", "EN", "eur-lex"),
            _cand("d", "DE", "eur-lex"),
        ]
        result = self.selector.select(_decision(cands))
        self.assertEqual(result.status, SelectionStatus.SELECTED)
        self.assertEqual([c.document_id for c in result.selected], ["c"])
        self.assertEqual(
            sorted(c.document_id for c in result.not_selected), ["a", "b", "d"]
        )
        self.assertEqual(result.language_init, "en")

    def test_no_en_with_unmarked_is_undecided_all_unmarked_selected(self) -> None:
        """Without EN, all unmarked candidates are kept, marked ones dropped."""
        cands = [_cand("u1", None), _cand("u2", None, "db-comp"), _cand("g", "DE")]
        result = self.selector.select(_decision(cands))
        self.assertEqual(result.status, SelectionStatus.UNDECIDED)
        self.assertEqual([c.document_id for c in result.selected], ["u1", "u2"])
        self.assertEqual([c.document_id for c in result.not_selected], ["g"])
        self.assertEqual(result.language_init, "NA")

    def test_only_non_en_marked_selects_original_and_sets_language_init(self) -> None:
        """Only marked non-EN: one original, DE only as tie-breaker."""
        cands = [_cand("fr", "FR"), _cand("de", "DE"), _cand("nl", "NL")]
        result = self.selector.select(_decision(cands))
        self.assertEqual(result.status, SelectionStatus.SELECTED)
        self.assertEqual([c.document_id for c in result.selected], ["de"])
        self.assertEqual(result.language_init, "de")

    def test_origin_beats_german_in_original_choice(self) -> None:
        """A better origin wins over DE; DE only breaks ties."""
        cands = [_cand("de", "DE", "db-comp"), _cand("fr", "FR", "eur-lex")]
        result = self.selector.select(_decision(cands))
        self.assertEqual([c.document_id for c in result.selected], ["fr"])
        self.assertEqual(result.language_init, "fr")

    def test_html_only_is_no_pdf_source(self) -> None:
        """No PDF candidate means no_pdf_source."""
        cands = [_cand("h", "EN", file_format="html")]
        result = self.selector.select(_decision(cands))
        self.assertEqual(result.status, SelectionStatus.NO_PDF_SOURCE)
        self.assertEqual(result.selected, [])
        self.assertEqual(result.language_init, "NA")
        self.assertEqual([c.document_id for c in result.not_selected], ["h"])

    def test_no_candidates_is_no_pdf_source(self) -> None:
        """An empty candidate list is no_pdf_source."""
        result = self.selector.select(_decision([]))
        self.assertEqual(result.status, SelectionStatus.NO_PDF_SOURCE)

    def test_non_pdf_candidates_never_selected(self) -> None:
        """Non-PDF candidates land in not_selected even next to PDFs."""
        cands = [_cand("h", "EN", file_format="html"), _cand("p", "EN")]
        result = self.selector.select(_decision(cands))
        self.assertEqual([c.document_id for c in result.selected], ["p"])
        self.assertEqual([c.document_id for c in result.not_selected], ["h"])


if __name__ == "__main__":
    unittest.main()
