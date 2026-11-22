"""Unit tests for ``tools.decision_date_report`` (pure parts, no database)."""

import datetime as dt
import unittest

from tools.decision_date_report import Agreement, agreement, build_frame, summarise

_FACTS = {
    "DEC-AT39168-20110331": {
        "eu_commission_decision_date": "2002-09-19",
        "human_index_decision_date": "2011-03-31",
        "human_index_year": "2011",
    },
    "DEC-AT38700-20080305": {"eu_commission_decision_date": "2008-03-04"},
    "40436_949_5": {"eu_commission_decision_date": "NA", "human_index_year": "2019"},
}


class AgreementTest(unittest.TestCase):
    """The classes follow the issue's breakdown: exact, day only, year, NA."""

    def test_classes(self) -> None:
        ref = dt.date(2011, 3, 31)
        self.assertIs(agreement(dt.date(2011, 3, 31), ref), Agreement.EXACT)
        self.assertIs(agreement(dt.date(2011, 3, 30), ref), Agreement.SAME_MONTH)
        self.assertIs(agreement(dt.date(2011, 1, 31), ref), Agreement.SAME_YEAR)
        self.assertIs(agreement(dt.date(2002, 9, 19), ref), Agreement.OTHER_YEAR)
        self.assertIs(agreement(None, ref), Agreement.NA)
        self.assertIsNone(agreement(dt.date(2011, 3, 31), None))

    def test_year_reference(self) -> None:
        self.assertIs(agreement(dt.date(2011, 3, 31), 2011), Agreement.EXACT)
        self.assertIs(agreement(dt.date(2010, 3, 31), 2011), Agreement.OTHER_YEAR)


class FrameTest(unittest.TestCase):
    """Rows and summary for stored and dry-run values."""

    def test_stored_and_new_columns(self) -> None:
        new = {"DEC-AT39168-20110331": dt.date(2011, 3, 31), "DEC-AT38700-20080305": dt.date(2008, 3, 5)}
        frame = build_frame(_FACTS, sorted(_FACTS), new)
        row = frame.filter(frame["celex_id"] == "DEC-AT39168-20110331").row(0, named=True)
        self.assertEqual(row["stored_vs_dec_id_date"], "other_year")
        self.assertEqual(row["new_vs_dec_id_date"], "exact")
        self.assertEqual(row["new_vs_human_year"], "exact")
        na = frame.filter(frame["celex_id"] == "40436_949_5").row(0, named=True)
        self.assertEqual((na["stored_vs_human_year"], na["stored_vs_dec_id_date"]), ("na", None))

    def test_summary_counts(self) -> None:
        summary = summarise(build_frame(_FACTS, sorted(_FACTS)))
        by = {r["comparison"]: r for r in summary.iter_rows(named=True)}
        self.assertEqual((by["stored_vs_dec_id_date"]["other_year"], by["stored_vs_dec_id_date"]["same_month"]), (1, 1))
        self.assertEqual(by["stored_vs_human_year"]["na"], 1)
        self.assertNotIn("new_vs_dec_id_date", by)


if __name__ == "__main__":
    unittest.main()
