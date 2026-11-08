"""Tests for the downloader pydantic models."""

import unittest

from pydantic import ValidationError

from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    FetchResult,
    MapperResult,
    Outcome,
    RowFailure,
)


def _candidate(**kw: object) -> CandidateDocument:
    base: dict[str, object] = {
        "document_id": "dbcomp-1",
        "source_url": "https://db-comp.eu/document_1.download",
    }
    base.update(kw)
    return CandidateDocument(**base)


class ModelsTest(unittest.TestCase):
    """Validation behaviour of the downloader models."""

    def test_decision_request_requires_decision_id(self) -> None:
        """An empty or missing decision_id is rejected."""
        with self.assertRaises(ValidationError):
            DecisionRequest(loader_file="a.csv", loader_row=1)
        with self.assertRaises(ValidationError):
            DecisionRequest(decision_id="  ", loader_file="a.csv", loader_row=1)

    def test_candidate_language_is_uppercase_or_none(self) -> None:
        """Languages are normalised to upper case; blank becomes None."""
        self.assertEqual(_candidate(language="en").language, "EN")
        self.assertIsNone(_candidate(language=None).language)
        self.assertIsNone(_candidate(language=" ").language)

    def test_fetch_result_failed_requires_reason(self) -> None:
        """A failed result without failure_reason is invalid."""
        common = {
            "document_id": "d",
            "source_url": "https://x/y.pdf",
            "duration_ms": 5,
        }
        with self.assertRaises(ValidationError):
            FetchResult(outcome=Outcome.FAILED, **common)
        ok = FetchResult(outcome=Outcome.FAILED, failure_reason="not_a_pdf", **common)
        self.assertEqual(ok.failure_reason, "not_a_pdf")

    def test_meta_keeps_every_extra_column_verbatim(self) -> None:
        """Extra loader columns survive unchanged in meta."""
        meta = {"gemma_x": "1", "manifest_row_id": "a | b", "odd": ""}
        req = DecisionRequest(
            decision_id="CELEX-31964D0233",
            meta=meta,
            candidates=[_candidate()],
            loader_file="a.csv",
            loader_row=2,
        )
        self.assertEqual(req.meta, meta)
        res = MapperResult(
            decisions=[req],
            failures=[RowFailure(loader_file="a.csv", loader_row=3, reason="bad")],
        )
        self.assertEqual(len(res.failures), 1)


if __name__ == "__main__":
    unittest.main()
