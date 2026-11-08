"""Unit tests of DecisionPlanner.resolve (merge pre-pass) and the recorder guard."""

import datetime as dt
import unittest
import uuid
from pathlib import Path
from unittest import mock

from pipelines.download.models import CandidateDocument, DecisionRequest, RowFailure
from pipelines.download.master_identity import MasterIdentityIndex
from pipelines.download.outcome_recorder import OutcomeRecorder
from pipelines.download.planner import DecisionPlanner
from pipelines.download.run_summary import RunSummary
from pipelines.download.selector import CandidateSelector

EN = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31988D0172"
DE = "https://eur-lex.europa.eu/legal-content/DE/TXT/PDF/?uri=CELEX:31988D0172"
EC = "https://ec.europa.eu/competition/antitrust/cases/dec_docs/1/1_1.pdf"


class _Store:
    """Read-only master store fake with no entries."""

    def has_key(self, key: str) -> bool:
        """Report no key.

        Args:
            key: Decision id.

        Returns:
            False.
        """
        return False

    def entries(self) -> list[tuple[str, dict[str, object]]]:
        """Return no entries.

        Returns:
            An empty list.
        """
        return []


def _decision(decision_id: str, urls: list[tuple[str, str | None]], *,
              celex_number: str | None = None, row: int = 1, case: str = "IV/1",
              title: str = "T") -> DecisionRequest:
    """Build a decision.

    Args:
        decision_id: Provisional id.
        urls: (url, language) pairs.
        celex_number: CELEX or None.
        row: Loader row.
        case: Case id.
        title: Title.

    Returns:
        A DecisionRequest.
    """
    return DecisionRequest(
        decision_id=decision_id, celex_number=celex_number, case_ids=[case],
        decision_date=dt.date(1988, 1, 1), title=title,
        candidates=[CandidateDocument(document_id="p", source_url=u, language=lang)
                    for u, lang in urls],
        loader_file="loader.xlsx", loader_row=row)


class ResolveTest(unittest.TestCase):
    """Merge pre-pass of DecisionPlanner.resolve."""

    def setUp(self) -> None:
        """Create the planner."""
        self.planner = DecisionPlanner(CandidateSelector(), _Store(), Path("/docs"),
                                       MasterIdentityIndex([]))

    def test_same_celex_rows_merge(self) -> None:
        """Same CELEX: one entry, URL-deduplicated candidates, first meta wins."""
        first = _decision("CELEX-31988D0172", [(EN, "EN")], celex_number="31988D0172",
                          row=272, case="IV/1", title="first")
        second = _decision("CELEX-31988D0172", [(EN, "EN"), (DE, "DE")],
                           celex_number="31988D0172", row=274, case="IV/2", title="second")
        out = self.planner.resolve([first, second])
        self.assertEqual(len(out), 1)
        merged = out[0].decision
        self.assertEqual(out[0].key, "CELEX-31988D0172")
        self.assertEqual([c.source_url for c in merged.candidates], [EN, DE])
        self.assertEqual(merged.case_ids, ["IV/1", "IV/2"])
        self.assertEqual(merged.title, "first")
        self.assertEqual(merged.loader_row, 272)
        self.assertIn("merged duplicate row 274 of loader.xlsx (CELEX-31988D0172)",
                      out[0].reasons)
        self.assertEqual(len(first.candidates), 1)

    def test_dec_ids_are_never_merged(self) -> None:
        """Two DEC- fallback rows with the same id stay two decisions."""
        a = _decision("DEC-IV1-19880101", [(EC, None)], row=1)
        b = _decision("DEC-IV1-19880101", [(EC, None)], row=2)
        self.assertEqual(len(self.planner.resolve([a, b])), 2)

    def test_same_master_like_key_merges(self) -> None:
        """Non-CELEX provisional keys (e.g. c1417) merge as well."""
        a = _decision("c1417", [(EC, None)], row=1)
        b = _decision("c1417", [(EN, "EN")], row=2)
        out = self.planner.resolve([a, b])
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].decision.candidates), 2)

    def test_unusable_key_is_an_error_not_merged(self) -> None:
        """An unsafe provisional id is kept as an error entry."""
        out = self.planner.resolve([_decision("bad/id", [(EC, None)])])
        self.assertIsNone(out[0].key)
        self.assertIn("not filesystem-safe", out[0].error or "")


MASTER_36571_URL = ("https://ec.europa.eu/competition/antitrust/cases/dec_docs/"
                    "36571/36571_222_1.pdf")
NEW_36571_URL = "https://ec.europa.eu/competition/antitrust/cases1/202514/AT_36571_222.pdf"
MASTER_33585_URL = ("https://ec.europa.eu/competition/antitrust/cases/dec_docs/"
                    "33585/33585_2_4.pdf")
EURLEX_31992D0568 = ("https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/"
                     "?uri=CELEX:31992D0568")
MASTER = [
    ("36571_222_1", {"case_ids": ["IV/36571"], "webDocDescriptor": {
        "case": "36571", "decision_date": "2002-06-11 00:00:00",
        "url_pdf": MASTER_36571_URL}}),
    ("33585_2_4", {"case_ids": [], "webDocDescriptor": {
        "case": "33585", "decision_date": "", "url_pdf": MASTER_33585_URL}}),
]


class IdentityResolveTest(unittest.TestCase):
    """resolve() reuses master keys found by the identity index."""

    def _planner(self, entries: list) -> DecisionPlanner:
        """Build a planner over an identity index of the given entries.

        Args:
            entries: (key, entry) pairs.

        Returns:
            The planner.
        """
        return DecisionPlanner(CandidateSelector(), _Store(), Path("/docs"),
                               MasterIdentityIndex(entries))

    def _36571(self, row: int = 1) -> DecisionRequest:
        """Build the real manifest decision DEC-AT36571-20020611.

        Args:
            row: Loader row.

        Returns:
            The decision.
        """
        decision = _decision("DEC-AT36571-20020611", [(NEW_36571_URL, "EN")],
                             case="AT.36571", row=row)
        decision.decision_date = dt.date(2002, 6, 11)
        return decision

    def test_case_and_date_is_only_a_hint(self) -> None:
        """A moved PDF URL matching 36571_222_1 by case + date keeps its own key."""
        out = self._planner(MASTER).resolve([self._36571()])
        self.assertEqual(out[0].key, "DEC-AT36571-20020611")
        self.assertEqual(out[0].reasons,
                         ["possible duplicate of master entry 36571_222_1 (case+date)"])

    def test_celex_not_in_master_mapped_by_url_keeps_celex_reason(self) -> None:
        """CELEX 31992D0568: its non-selected EC URL is master 33585_2_4."""
        decision = _decision("CELEX-31992D0568",
                             [(EURLEX_31992D0568, "EN"), (MASTER_33585_URL, None)],
                             celex_number="31992D0568", case="AT.33585")
        out = self._planner(MASTER).resolve([decision])
        self.assertEqual(out[0].key, "33585_2_4")
        self.assertEqual(out[0].reasons, [
            "matched master entry 33585_2_4 by url",
            "CELEX 31992D0568 differs from master key 33585_2_4"])

    def test_own_master_key_has_no_reason(self) -> None:
        """A CELEX key that is a master key is kept silently."""
        own = ("CELEX-31992D0568", {"webDocDescriptor": {}})
        decision = _decision("CELEX-31992D0568", [(MASTER_33585_URL, None)],
                             celex_number="31992D0568")
        out = self._planner([*MASTER, own]).resolve([decision])
        self.assertEqual((out[0].key, out[0].reasons), ("CELEX-31992D0568", []))

    def test_ambiguous_case_and_date_keeps_own_key(self) -> None:
        """Two master entries of the case and date: no reuse, review reason."""
        twin = ("36571_222_2", {"case_ids": ["IV/36.571"],
                                "webDocDescriptor": {"decision_date": "2002-06-11"}})
        out = self._planner([*MASTER, twin]).resolve([self._36571()])
        self.assertEqual(out[0].key, "DEC-AT36571-20020611")
        self.assertEqual(out[0].reasons, [
            "possible duplicate of master entry 36571_222_1 (case+date)",
            "possible duplicate of master entry 36571_222_2 (case+date)"])

    def test_rows_reusing_one_key_are_merged(self) -> None:
        """Two rows matched to the same master key become one decision."""
        first = _decision("DEC-AT36571-20020611", [(MASTER_36571_URL, "EN")],
                          case="AT.36571", row=1)
        first.decision_date = dt.date(2002, 6, 11)
        second = _decision("DEC-AT36571-20020611_X",
                           [(MASTER_36571_URL, None), (NEW_36571_URL, None)],
                           case="IV/36571", row=2)
        second.decision_date = dt.date(2002, 6, 11)
        out = self._planner(MASTER).resolve([first, second])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].key, "36571_222_1")
        self.assertEqual(len(out[0].decision.candidates), 2)
        self.assertIn("matched master entry 36571_222_1 by url", out[0].reasons)

    def test_dated_rows_of_one_url_are_not_merged(self) -> None:
        """38589 shape: url row of another date stays apart from the case+date row."""
        url = "https://ec.europa.eu/competition/antitrust/cases/dec_docs/38589/38589_4669_7.pdf"
        master = [("38589_4669_7", {"case_ids": ["IV/38589"], "webDocDescriptor": {
            "case": "38589", "decision_date": "2016-06-29", "url_pdf": url}})]
        by_url = _decision("DEC-AT38589-20091111", [(url, "EN")], case="AT.38589", row=1)
        by_url.decision_date = dt.date(2009, 11, 11)
        by_case = _decision("DEC-AT38589-20160629", [(NEW_36571_URL, "EN")],
                            case="AT.38589", row=2)
        by_case.decision_date = dt.date(2016, 6, 29)
        out = self._planner(master).resolve([by_url, by_case])
        self.assertEqual([r.key for r in out],
                         ["DEC-AT38589-20091111", "DEC-AT38589-20160629"])
        self.assertEqual(out[0].reasons, [
            "url matches master entry 38589_4669_7 but date differs "
            "(2016-06-29 vs 2009-11-11)"])
        self.assertEqual(out[1].reasons,
                         ["possible duplicate of master entry 38589_4669_7 (case+date)"])

    def test_url_near_miss_date_keeps_own_key(self) -> None:
        """Dates 4 days apart: own key plus the date-differs reason."""
        decision = self._36571()
        decision.decision_date = dt.date(2002, 6, 15)
        decision.case_ids = ["AT.99999"]
        decision.candidates[0].source_url = MASTER_36571_URL
        out = self._planner(MASTER).resolve([decision])
        self.assertEqual(out[0].key, "DEC-AT36571-20020611")
        self.assertEqual(out[0].reasons, [
            "url matches master entry 36571_222_1 but date differs "
            "(2002-06-11 vs 2002-06-15)"])

    def test_html_candidate_is_not_a_url_match(self) -> None:
        """Only PDF candidates take part in the URL rule."""
        decision = _decision("DEC-IV1-19880101", [(MASTER_33585_URL, None)])
        decision.candidates[0].file_format = "html"
        out = self._planner(MASTER).resolve([decision])
        self.assertEqual(out[0].key, "DEC-IV1-19880101")


class RecorderDryRunTest(unittest.TestCase):
    """OutcomeRecorder never writes in a dry run."""

    def test_dry_run_counts_without_writing(self) -> None:
        """Registry and audit writers are not called; counters still move."""
        registry, audit = mock.Mock(), mock.Mock()
        run_id = uuid.uuid4()
        summary = RunSummary(run_id=run_id, dry_run=True)
        recorder = OutcomeRecorder(registry, audit, run_id, True, summary)
        decision = _decision("CELEX-31988D0172", [(EN, "EN")])
        selection = CandidateSelector().select(decision)
        recorder.register_decision(decision, selection)
        recorder.add_reasons(decision, selection, ["r"])
        recorder.row_failure(None, RowFailure(loader_file="l", loader_row=1,
                                              reason="x"))
        recorder.register_not_selected([(decision, decision.candidates[0])], {})
        self.assertEqual(registry.method_calls, [])
        self.assertEqual(audit.method_calls, [])
        self.assertEqual(summary.review_reasons, 1)
        self.assertEqual(summary.row_failures, 1)


if __name__ == "__main__":
    unittest.main()
