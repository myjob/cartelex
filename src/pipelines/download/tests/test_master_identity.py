"""Unit tests of the master identity index (case numbers, rules, ambiguity)."""

import datetime as dt
import unittest

from pipelines.download.identifiers import case_number, case_suffix, master_date
from pipelines.download.master_identity import MasterIdentityIndex

# Real-data fixtures (master entries of 2026-10-30, trimmed).
M_36571 = ("36571_222_1", {
    "case_ids": ["IV/36571"],
    "webDocDescriptor": {
        "case": "36571", "decision_date": "2002-06-11 00:00:00",
        "url_pdf": "https://ec.europa.eu/competition/antitrust/cases/dec_docs/"
                   "36571/36571_222_1.pdf"}})
M_33585 = ("33585_2_4", {
    "case_ids": [],
    "webDocDescriptor": {
        "case": "33585", "decision_date": "",
        "url_pdf": "https://ec.europa.eu/competition/antitrust/cases/dec_docs/"
                   "33585/33585_2_4.pdf"}})
NEW_36571_URL = "https://ec.europa.eu/competition/antitrust/cases1/202514/AT_36571_222.pdf"
EURLEX_31992D0568 = ("https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/"
                     "?uri=CELEX:31992D0568")


class CaseNumberTest(unittest.TestCase):
    """case_number over the real case id shapes."""

    def test_shapes(self) -> None:
        """Prefixes, dots, sub-file suffixes and office parts are ignored."""
        cases = {
            "IV/32800": "32800",
            "IV/32.800": "32800",
            "AT.36571": "36571",
            "IV/34179-1": "34179",
            "IV/93-3": "93",
            "COMP/38.069": "38069",
            "COMP/E-1/38.069": "38069",
            "IV/30.373/F3": "30373",
            "36571": "36571",
            "400-1": "400",
            "IV/A-00061": "61",
            " AT.40 881 ": "40881",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(case_number(raw), expected)

    def test_no_number(self) -> None:
        """Blank, digit-free and CELEX-shaped values give None."""
        for raw in ("", "IV/", "n/a", "31987D0359", "31996M0802"):
            with self.subTest(raw=raw):
                self.assertIsNone(case_number(raw))

    def test_case_suffix(self) -> None:
        """Only a -<digits> right after the case number is a sub-file suffix."""
        self.assertEqual(case_suffix("IV/34179-1"), ("34179", "1"))
        self.assertEqual(case_suffix("400-01"), ("400", "1"))
        self.assertIsNone(case_suffix("IV/34179"))
        self.assertIsNone(case_suffix("COMP/E-1/38.069"))

    def test_master_date(self) -> None:
        """The stored date spellings normalise to ISO; others give None."""
        self.assertEqual(master_date("2002-06-11 00:00:00"), "2002-06-11")
        self.assertEqual(master_date("2002/06/11"), "2002-06-11")
        self.assertEqual(master_date("1964-03-11"), "1964-03-11")
        for raw in ("", None, "11.06.2002", "2002-13-40"):
            with self.subTest(raw=raw):
                self.assertIsNone(master_date(raw))


class MatchTest(unittest.TestCase):
    """Rule precedence and ambiguity of MasterIdentityIndex.match."""

    def test_36571_matches_by_case_and_date(self) -> None:
        """AT.36571 + 2002-06-11 at a moved URL is master 36571_222_1."""
        index = MasterIdentityIndex([M_36571, M_33585])
        match = index.match("DEC-AT36571-20020611", [], [NEW_36571_URL], ["AT.36571"],
                            dt.date(2002, 6, 11))
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual((match.key, match.rule), ("36571_222_1", "case+date"))

    def test_33585_matches_by_non_selected_url(self) -> None:
        """A CELEX decision whose second PDF URL is master 33585_2_4's url_pdf."""
        index = MasterIdentityIndex([M_36571, M_33585])
        match = index.match("CELEX-31992D0568", [],
            [EURLEX_31992D0568, M_33585[1]["webDocDescriptor"]["url_pdf"]],
            ["AT.33585"], dt.date(1992, 11, 25))
        assert match is not None
        self.assertEqual((match.key, match.rule), ("33585_2_4", "url"))

    def test_url_matches_spelling_variants(self) -> None:
        """http vs https compare equal (pdf_url_key)."""
        index = MasterIdentityIndex([M_33585])
        url = M_33585[1]["webDocDescriptor"]["url_pdf"].replace("https", "http", 1)
        match = index.match(None, [], [url], [], None)
        assert match is not None
        self.assertEqual(match.key, "33585_2_4")

    def test_own_key_wins_over_url_and_case(self) -> None:
        """Rule 1 first: an own master key is kept even when a URL points elsewhere."""
        own = ("CELEX-31992D0568", {"webDocDescriptor": {}})
        index = MasterIdentityIndex([M_33585, own])
        match = index.match("CELEX-31992D0568", [],
                            [M_33585[1]["webDocDescriptor"]["url_pdf"]], [], None)
        assert match is not None
        self.assertEqual((match.key, match.rule), ("CELEX-31992D0568", "celex"))

    def test_url_wins_over_case_and_date(self) -> None:
        """Rule 2 before rule 3."""
        index = MasterIdentityIndex([M_36571, M_33585])
        match = index.match(None, [], [M_33585[1]["webDocDescriptor"]["url_pdf"]],
                            ["AT.36571"], dt.date(2002, 6, 11))
        assert match is not None
        self.assertEqual((match.key, match.rule), ("33585_2_4", "url"))

    def test_registry_alias_url_matches_like_url_pdf(self) -> None:
        """A registry alias URL of K maps a decision to K by url."""
        index = MasterIdentityIndex([M_36571, M_33585],
                                    [("36571_222_1", NEW_36571_URL),
                                     ("NOT_A_KEY", EURLEX_31992D0568)])
        match = index.match("DEC-AT36571-20020611", [NEW_36571_URL], [NEW_36571_URL],
                            ["AT.36571"], dt.date(2002, 6, 11))
        assert match is not None
        self.assertEqual((match.key, match.rule), ("36571_222_1", "url"))
        self.assertIsNone(index.match(None, [], [EURLEX_31992D0568], [], None))

    def test_registry_alias_url_keeps_date_guard(self) -> None:
        """An alias URL whose master date differs is dropped like url_pdf."""
        index = MasterIdentityIndex([M_36571], [("36571_222_1", NEW_36571_URL)])
        match, conflicts = index.match_detail(None, [NEW_36571_URL], [NEW_36571_URL],
                                              [], dt.date(2003, 1, 1))
        self.assertIsNone(match)
        self.assertEqual([c.key for c in conflicts], ["36571_222_1"])

    def test_blank_master_date_never_matches_by_case(self) -> None:
        """33585_2_4 has no date: case 33585 alone is not enough."""
        index = MasterIdentityIndex([M_33585])
        self.assertIsNone(index.match(None, [], [], ["IV/33585"], dt.date(1992, 11, 25)))

    def test_other_date_does_not_match(self) -> None:
        """Same case, other date: no match."""
        index = MasterIdentityIndex([M_36571])
        self.assertIsNone(index.match(None, [], [], ["AT.36571"], dt.date(2002, 6, 12)))
        self.assertIsNone(index.match(None, [], [], ["AT.36571"], None))

    def test_ambiguous_case_and_date(self) -> None:
        """Two master entries of one case and date: no key, both listed."""
        twin = ("36571_222_2", {"case_ids": ["IV/36.571"],
                                "webDocDescriptor": {"decision_date": "2002/06/11"}})
        index = MasterIdentityIndex([M_36571, twin])
        match = index.match(None, [], [], ["AT.36571"], dt.date(2002, 6, 11))
        assert match is not None
        self.assertIsNone(match.key)
        self.assertEqual(match.rule, "case+date")
        self.assertEqual(match.ambiguous, ["36571_222_1", "36571_222_2"])

    def test_ambiguous_url(self) -> None:
        """Two candidate URLs owned by two master keys are ambiguous."""
        index = MasterIdentityIndex([M_36571, M_33585])
        match = index.match(None, [], [M_36571[1]["webDocDescriptor"]["url_pdf"],
                                   M_33585[1]["webDocDescriptor"]["url_pdf"]], [], None)
        assert match is not None
        self.assertIsNone(match.key)
        self.assertEqual(match.ambiguous, ["33585_2_4", "36571_222_1"])

    def test_selected_url_wins_over_other_candidates(self) -> None:
        """2a: the selected URL names one key; other candidates' keys are ignored."""
        index = MasterIdentityIndex([M_36571, M_33585])
        selected = M_36571[1]["webDocDescriptor"]["url_pdf"]
        match = index.match(None, [selected],
                            [selected, M_33585[1]["webDocDescriptor"]["url_pdf"]],
                            [], None)
        assert match is not None
        self.assertEqual((match.key, match.rule, match.ambiguous),
                         ("36571_222_1", "url", []))

    def test_no_selected_match_several_candidates_is_ambiguous(self) -> None:
        """2b: selected URL unknown, two keys, no case tie-break: ambiguous."""
        index = MasterIdentityIndex([M_36571, M_33585])
        match = index.match(
            None, [NEW_36571_URL],
            [NEW_36571_URL, M_36571[1]["webDocDescriptor"]["url_pdf"],
             M_33585[1]["webDocDescriptor"]["url_pdf"]], ["AT.99999"],
            dt.date(2002, 6, 11))
        assert match is not None
        self.assertIsNone(match.key)
        self.assertEqual((match.rule, match.ambiguous),
                         ("url", ["33585_2_4", "36571_222_1"]))

    def test_url_tie_broken_by_case_number(self) -> None:
        """39678-like: two keys by selected URL, one shares the case: url+case."""
        k1 = ("39678_2514_15", {"case_ids": ["IV/39678"], "webDocDescriptor": {
            "case": "39678", "url_pdf": "https://ec.europa.eu/x/39678_2514_15.pdf"}})
        k2 = ("39731_801_8", {"case_ids": [], "webDocDescriptor": {
            "case": "39731", "url_pdf": "https://ec.europa.eu/x/39731_801_8.pdf"}})
        urls = ["https://ec.europa.eu/x/39731_801_8.pdf",
                "https://ec.europa.eu/x/39678_2514_15.pdf"]
        match = MasterIdentityIndex([k1, k2]).match(
            "DEC-AT39678-20131218", urls, urls, ["AT.39678"], dt.date(2013, 12, 18))
        assert match is not None
        self.assertEqual((match.key, match.rule), ("39678_2514_15", "url+case"))

    def test_url_tie_with_shared_case_stays_ambiguous(self) -> None:
        """40013-like: one URL owned by two keys of the same case: ambiguous."""
        url = "https://ec.europa.eu/x/AT_40013_5708398_1084_5.pdf"
        k1 = ("40013_1084_2", {"case_ids": [], "webDocDescriptor": {
            "case": "40013", "url_pdf": url}})
        k2 = ("AT_40013_5708398_1084_5", {"case_ids": ["AT.40013"],
                                          "webDocDescriptor": {"url_pdf": url}})
        match = MasterIdentityIndex([k1, k2]).match(
            None, [url], [url], ["AT.40013"], dt.date(2017, 6, 21))
        assert match is not None
        self.assertIsNone(match.key)
        self.assertEqual((match.rule, match.ambiguous),
                         ("url", ["40013_1084_2", "AT_40013_5708398_1084_5"]))

    def test_url_with_other_date_is_dropped_with_conflict(self) -> None:
        """Date guard: the URL owner's date differs by 4 days: no key, conflict."""
        index = MasterIdentityIndex([M_36571])
        url = M_36571[1]["webDocDescriptor"]["url_pdf"]
        match, conflicts = index.match_detail(None, [url], [url], ["AT.99999"],
                                              dt.date(2002, 6, 15))
        self.assertIsNone(match)
        self.assertIsNone(index.match(None, [url], [url], ["AT.99999"],
                                      dt.date(2002, 6, 15)))
        self.assertEqual([(c.key, c.master_date, c.decision_date) for c in conflicts],
                         [("36571_222_1", "2002-06-11", "2002-06-15")])

    def test_url_date_guard_falls_through_to_case_and_date(self) -> None:
        """URL owner dated otherwise; another key matches by case + date."""
        other = ("36571_9_9", {"case_ids": ["IV/36571"], "webDocDescriptor": {
            "decision_date": "2003-01-01"}})
        url = M_36571[1]["webDocDescriptor"]["url_pdf"]
        match, conflicts = MasterIdentityIndex([M_36571, other]).match_detail(
            None, [url], [url], ["AT.36571"], dt.date(2003, 1, 1))
        assert match is not None
        self.assertEqual((match.key, match.rule), ("36571_9_9", "case+date"))
        self.assertEqual([c.key for c in conflicts], ["36571_222_1"])

    def test_blank_dates_never_exclude_a_url_key(self) -> None:
        """Blank master date (33585_2_4) or no decision date: URL still matches."""
        index = MasterIdentityIndex([M_33585, M_36571])
        url = M_33585[1]["webDocDescriptor"]["url_pdf"]
        self.assertEqual(index.match(None, [url], [url], [], dt.date(1992, 11, 25)).key,
                         "33585_2_4")
        url = M_36571[1]["webDocDescriptor"]["url_pdf"]
        self.assertEqual(index.match(None, [url], [url], [], None).key, "36571_222_1")

    def test_case_and_date_skips_other_celex(self) -> None:
        """Both sides carry a CELEX number and they differ: no case+date match."""
        k = ("CELEX-31990D0001", {"case_ids": ["IV/100"], "webDocDescriptor": {
            "decision_date": "1990-01-01"}})
        index = MasterIdentityIndex([k])
        self.assertIsNone(index.match("CELEX-31990D0002", [], [], ["IV/100"],
                                      dt.date(1990, 1, 1)))
        match = index.match("DEC-IV100-19900101", [], [], ["IV/100"], dt.date(1990, 1, 1))
        assert match is not None
        self.assertEqual(match.key, "CELEX-31990D0001")

    def test_case_and_date_skips_other_sub_file(self) -> None:
        """IV/34179-1 never matches K of IV/34179-2; bare ids still match."""
        k = ("34179_2", {"case_ids": ["IV/34179-2"], "webDocDescriptor": {
            "decision_date": "1994-03-01"}})
        index = MasterIdentityIndex([k])
        day = dt.date(1994, 3, 1)
        self.assertIsNone(index.match(None, [], [], ["IV/34179-1"], day))
        self.assertEqual(index.match(None, [], [], ["IV/34179-2"], day).key, "34179_2")
        self.assertEqual(index.match(None, [], [], ["AT.34179"], day).key, "34179_2")

    def test_case_from_web_case_field_only(self) -> None:
        """K's case number may come from webDocDescriptor.case alone."""
        entry = ("38511_1", {"webDocDescriptor": {"case": "38.511",
                                                  "decision_date": "2009-01-01"}})
        match = MasterIdentityIndex([entry]).match(None, [], [], ["COMP/38.511"],
                                                   dt.date(2009, 1, 1))
        assert match is not None
        self.assertEqual(match.key, "38511_1")

    def test_no_match(self) -> None:
        """Nothing known: None."""
        self.assertIsNone(MasterIdentityIndex([]).match("X", [], ["https://a/b.pdf"],
                                                        ["IV/1"], dt.date(2000, 1, 1)))


if __name__ == "__main__":
    unittest.main()
