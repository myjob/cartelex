"""Cartel period inheritance for follow-up decisions (no DB, no LLM).

Cases mirror the live corpus probe of 20261106: Fasteners (39168), Raw Tobacco
Spain (38238), Euro Interest Rate Derivatives (39914) and the CSV pair
CELEX-31976D0593 / CELEX-31978D0732 that must *not* inherit.
"""

import unittest
import uuid

from pipelines.heideltime.period_inheritance import (
    INHERITANCE_METHOD,
    InheritanceBasis,
    PeriodDonor,
    PeriodInheritor,
    case_numbers,
    cited_years,
    id_year,
    parse_period,
    select_inheritance,
)

_FACT = "cartel_period"


def _donor(celex_id: str, date_range: str, refs: list[str] | None = None, gate: str | None = "True") -> PeriodDonor:
    """Build a donor whose case number comes from its id or ``refs``."""
    return PeriodDonor(
        fact_id=uuid.uuid4(),
        celex_id=celex_id,
        date_range=date_range,
        gate=gate,
        case_numbers=case_numbers(celex_id, refs or []),
    )


class TestCaseNumbers(unittest.TestCase):
    """Case keys are 5-digit AT numbers from the id, case ids and titles."""

    def test_id_patterns(self):
        self.assertEqual(case_numbers("39168_757_6", []), {"39168"})
        self.assertEqual(case_numbers("DEC-AT39168-20110331", []), {"39168"})
        self.assertEqual(case_numbers("AT_37990_9687627_5129_3", []), {"37990"})

    def test_celex_id_digits_are_not_a_case_number(self):
        self.assertEqual(case_numbers("CELEX-31976D0593", []), set())

    def test_refs_are_normalised_and_junk_prefixes_dropped(self):
        refs = ["COMP/E", "COMP/C.38.238/B.2", "IV/39168", "AT.39914",
                "('COMP/E-1/39.168 PO/ Articles of metal', 'Fasteners')"]
        self.assertEqual(case_numbers("CELEX-31976D0593", refs), {"38238", "39168", "39914"})

    def test_longer_digit_runs_do_not_match(self):
        self.assertEqual(case_numbers("CELEX-31976D0593", ["C(2007) 4257", "Decision 123456"]), set())


class TestIdYearAndCitations(unittest.TestCase):
    """The target's own year comes from its id; citations must be earlier."""

    def test_id_year(self):
        self.assertEqual(id_year("DEC-AT39914-20160406"), 2016)
        self.assertEqual(id_year("DEC-AT38589-20160629_2"), 2016)
        self.assertEqual(id_year("CELEX-31976D0593"), 1976)
        self.assertIsNone(id_year("39168_757_6"))

    def test_cited_years_keep_only_earlier_decisions(self):
        matches = ["2013", "2016", "2009"]
        self.assertEqual(cited_years(matches, before=2016), {2009, 2013})

    def test_cited_years_ignore_noise(self):
        self.assertEqual(cited_years(["abcd", "", "1850"], before=2016), set())


class TestParsePeriod(unittest.TestCase):
    """Donor periods are ``START : END`` at year, month or day precision."""

    def test_full_and_partial(self):
        self.assertIsNotNone(parse_period("1977-01-15 : 2003-01-13"))
        start, end = parse_period("1994 : 1999-03")
        self.assertEqual((start.year, end.year, end.month), (1994, 1999, 3))

    def test_rejects_na_and_shapeless(self):
        for value in ("NA", "", "1994", "1994 : NA", None):
            self.assertIsNone(parse_period(value), value)


class TestSelectInheritance(unittest.TestCase):
    """Donor eligibility and choice."""

    def test_fasteners_amending_decision_inherits_from_the_original(self):
        original = _donor("39168_757_6", "1977-01-15 : 2003-01-13")
        result = select_inheritance("DEC-AT39168-20110331", {"39168"}, {2004, 2007}, [original])
        self.assertEqual(result.date_range, "1977-01-15 : 2003-01-13")
        self.assertEqual(result.basis, InheritanceBasis.SINGLE_SIBLING)
        self.assertEqual([d.celex_id for d in result.donors], ["39168_757_6"])

    def test_target_never_inherits_from_itself(self):
        own = _donor("DEC-AT39168-20110331", "1977 : 2003")
        self.assertIsNone(select_inheritance("DEC-AT39168-20110331", {"39168"}, {2007}, [own]))

    def test_other_case_numbers_are_ignored(self):
        other = _donor("38238_249_1", "1996-03-13 : 2001-10-03")
        self.assertIsNone(select_inheritance("DEC-AT39168-20110331", {"39168"}, {2007}, [other]))

    def test_conduct_ending_after_the_cited_decision_is_not_the_original(self):
        later = _donor("39168_757_6", "1977 : 2009")
        self.assertIsNone(select_inheritance("DEC-AT39168-20110331", {"39168"}, {2007}, [later]))

    def test_gate_false_donor_is_ignored(self):
        exemption = _donor("39168_757_6", "1977 : 2003", gate="False")
        self.assertIsNone(select_inheritance("DEC-AT39168-20110331", {"39168"}, {2007}, [exemption]))

    def test_cited_year_picks_among_several_donors(self):
        """EIRD: the 2016 amendment cites C(2013) 8512, the 2013 settlement decision."""
        donors = [
            _donor("39914_8910_5", "2006-09-27 : 2007-03-19"),
            _donor("DEC-AT39914-20131204", "2005-09-29 : 2008-05-30"),
            _donor("DEC-AT39914-20210628", "2007-02-12 : 2007-03-27"),
        ]
        result = select_inheritance("DEC-AT39914-20160406", {"39914"}, {2013}, donors)
        self.assertEqual(result.basis, InheritanceBasis.CITED_DECISION)
        self.assertEqual(result.date_range, "2005-09-29 : 2008-05-30")
        self.assertEqual([d.celex_id for d in result.donors], ["DEC-AT39914-20131204"])

    def test_several_donors_without_cited_match_are_unioned(self):
        donors = [
            _donor("39914_8910_5", "2006-09-27 : 2007-03-19"),
            _donor("40000_1_1", "2005 : 2007-01", refs=["AT.39914"]),
        ]
        result = select_inheritance("DEC-AT39914-20160406", {"39914"}, {2013}, donors)
        self.assertEqual(result.basis, InheritanceBasis.SIBLING_UNION)
        self.assertEqual(result.date_range, "2005 : 2007-03-19")
        self.assertEqual(len(result.donors), 2)

    def test_no_citation_means_no_inheritance(self):
        """CSV 1976 shares its case number with a later decision but amends nothing."""
        later = _donor("CELEX-31978D0732", "1955-04-04 : 1974-01-02", refs=["IV/26.186"])
        self.assertIsNone(select_inheritance("CELEX-31976D0593", {"26186"}, set(), [later]))


class _FakeFactStore:
    """Serves case refs per celex and a fixed donor row list."""

    def __init__(self, refs: dict[str, list[str]], donor_rows: list[dict]):
        self.refs = refs
        self.donor_rows = donor_rows
        self.donor_calls = 0
        self.excluded_methods: list[str] = []

    def select_case_refs(self, celex_id: str, title_fact_name: str) -> list[str]:
        return self.refs.get(celex_id, [])

    def select_period_donor_rows(
        self, fact_name: str, gate_fact_name: str, title_fact_name: str, exclude_method: str
    ) -> list[dict]:
        self.donor_calls += 1
        self.excluded_methods.append(exclude_method)
        return [row for row in self.donor_rows if row.get("method") != exclude_method]


class _FakeChunkStore:
    """Returns the cited decision years found in a document."""

    def __init__(self, matches: dict[str, list[str]]):
        self.matches = matches

    def select_regex_matches(self, celex_id: str, regex: str) -> list[str]:
        return self.matches.get(celex_id, [])


class TestPeriodInheritor(unittest.TestCase):
    """The injected resolver reads the stores only as far as needed."""

    def _row(self, celex_id: str, value_json: str, refs: list[str] | None = None) -> dict:
        return {"fact_id": uuid.uuid4(), "celex_id": celex_id, "value_json": value_json,
                "gate": "True", "case_refs": refs or []}

    def test_raw_tobacco_amendment_inherits(self):
        facts = _FakeFactStore(
            {"DEC-AT38238-20170616": ["('COMP/38.238', 'Raw Tobacco - Spain')"]},
            [self._row("38238_249_1", '[{"date_range": "1996-03-13 : 2001-10-03"}]', ["COMP/C.38.238/B.2"]),
             self._row("39168_757_6", '[{"date_range": "1977-01-15 : 2003-01-13"}]')],
        )
        chunks = _FakeChunkStore({"DEC-AT38238-20170616": ["2004", "2017"]})
        result = PeriodInheritor(fact_store=facts, chunk_store=chunks).find("DEC-AT38238-20170616", _FACT)
        self.assertEqual(result.date_range, "1996-03-13 : 2001-10-03")
        self.assertEqual(result.donors[0].celex_id, "38238_249_1")

    def test_inherited_twin_is_not_a_donor(self):
        """Live run 20261106: DEC-AT38589-20160629, inherited moments earlier, became a
        second donor of its twin DEC-AT38589-20160629_2 and forced a union."""
        twin = self._row("DEC-AT38589-20160629", '[{"date_range": "1987-02-24 : 2000-09-26"}]')
        twin["method"] = INHERITANCE_METHOD
        facts = _FakeFactStore(
            {}, [self._row("38589_4669_7", '[{"date_range": "1987-02-24 : 2000-09-26"}]'), twin]
        )
        chunks = _FakeChunkStore({"DEC-AT38589-20160629_2": ["2009", "2011"]})
        result = PeriodInheritor(fact_store=facts, chunk_store=chunks).find("DEC-AT38589-20160629_2", _FACT)
        self.assertEqual(facts.excluded_methods, [INHERITANCE_METHOD])
        self.assertEqual(result.basis, InheritanceBasis.SINGLE_SIBLING)
        self.assertEqual([d.celex_id for d in result.donors], ["38589_4669_7"])

    def test_unparseable_donor_value_is_skipped(self):
        facts = _FakeFactStore({}, [self._row("39168_757_6", "not json")])
        chunks = _FakeChunkStore({"DEC-AT39168-20110331": ["2007"]})
        self.assertIsNone(PeriodInheritor(fact_store=facts, chunk_store=chunks).find("DEC-AT39168-20110331", _FACT))

    def test_target_without_id_year_reads_no_donors(self):
        facts = _FakeFactStore({}, [])
        chunks = _FakeChunkStore({"39168_757_6": ["2004"]})
        self.assertIsNone(PeriodInheritor(fact_store=facts, chunk_store=chunks).find("39168_757_6", _FACT))
        self.assertEqual(facts.donor_calls, 0)

    def test_target_without_earlier_citation_reads_no_donors(self):
        facts = _FakeFactStore({}, [])
        chunks = _FakeChunkStore({"DEC-AT39168-20110331": ["2011"]})
        self.assertIsNone(PeriodInheritor(fact_store=facts, chunk_store=chunks).find("DEC-AT39168-20110331", _FACT))
        self.assertEqual(facts.donor_calls, 0)


if __name__ == "__main__":
    unittest.main()
