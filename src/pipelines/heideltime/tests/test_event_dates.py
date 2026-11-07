"""Unit tests for :mod:`pipelines.heideltime.event_dates` (pure, no DB)."""

import datetime
import unittest

from pipelines.heideltime.event_dates import PartialDate, parse_partial_date


class TestParsePartialDate(unittest.TestCase):
    """Event dates are accepted at their stated precision, impossible ones rejected."""

    def test_full_year_month_and_year_only_precision(self):
        self.assertEqual(parse_partial_date("1994-03-15"), PartialDate(year=1994, month=3, day=15))
        self.assertEqual(parse_partial_date("1994-03"), PartialDate(year=1994, month=3))
        self.assertEqual(parse_partial_date(" 1994 "), PartialDate(year=1994))

    def test_years_outside_the_plausible_range_are_rejected(self):
        """HeidelTime normalises some expressions to years far out of range (#187)."""
        for value in ("0010-01-02", "0002-12-31", "0082-06-11", "2202-03-11", "1899"):
            with self.subTest(value=value):
                self.assertIsNone(parse_partial_date(value))

    def test_impossible_calendar_dates_are_rejected(self):
        """Month 00 broke case_timeline_vw (#190); Feb 30 is the same defect."""
        for value in ("2011-00-27", "1994-13", "1994-02-30", "1994-00"):
            with self.subTest(value=value):
                self.assertIsNone(parse_partial_date(value))

    def test_non_dates_are_rejected(self):
        for value in (None, "", "NA", "unknown", "1994-3-1", "94-03-01", "1994-03-01T00:00", 1994):
            with self.subTest(value=value):
                self.assertIsNone(parse_partial_date(value))

    def test_span_bounds_widen_partial_precision(self):
        """A year or month covers its whole span: first day as start, last day as end."""
        year = PartialDate(year=1994)
        self.assertEqual((year.first_day, year.last_day), (datetime.date(1994, 1, 1), datetime.date(1994, 12, 31)))
        february = PartialDate(year=1996, month=2)
        self.assertEqual(february.last_day, datetime.date(1996, 2, 29))
        day = PartialDate(year=1994, month=3, day=15)
        self.assertEqual(day.first_day, day.last_day)

    def test_text_round_trips_the_stated_precision(self):
        for value in ("1994", "1994-03", "1994-03-05"):
            with self.subTest(value=value):
                self.assertEqual(parse_partial_date(value).text, value)


if __name__ == "__main__":
    unittest.main()
