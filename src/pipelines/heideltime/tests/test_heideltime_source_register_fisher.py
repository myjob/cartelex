"""Partial-precision lineage in ``heideltime_source_register_fisher`` (#183).

Pure helpers plus a fake connection at the DB boundary; no live database.
"""

import datetime
import unittest
import uuid
from types import SimpleNamespace

from pipelines.heideltime.event_dates import PartialDate
from pipelines.heideltime.heideltime_source_register_fisher import (
    _boundary_dates,
    _find_extractions,
)


class _FakeConn:
    """Records the statement parameters and returns one canned row."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, sql, params):
        self.calls.append((str(sql), params))
        row = SimpleNamespace(id=uuid.uuid4(), timex_text="1994")
        return SimpleNamespace(fetchall=lambda: [row])


class TestBoundaryDates(unittest.TestCase):
    """Period bounds keep their precision instead of being dropped."""

    def test_partial_bounds_are_kept(self):
        self.assertEqual(
            _boundary_dates(("1994", "1999-03")),
            [PartialDate(year=1994), PartialDate(year=1999, month=3)],
        )

    def test_invalid_bounds_are_dropped(self):
        self.assertEqual(_boundary_dates(("0000-00-00", "1999-03-01")), [PartialDate(year=1999, month=3, day=1)])

    def test_single_day_period_is_queried_once(self):
        self.assertEqual(len(_boundary_dates(("1994-03-01", "1994-03-01"))), 1)


class TestFindExtractions(unittest.TestCase):
    """A bound matches every extraction inside the span it states."""

    def test_year_bound_queries_its_whole_year(self):
        conn = _FakeConn()
        rows = _find_extractions(conn, "CELEX-1", PartialDate(year=1994))
        sql, params = conn.calls[0]
        self.assertIn("timex_value BETWEEN :first_day AND :last_day", sql)
        self.assertEqual(
            (params["first_day"], params["last_day"]),
            (datetime.date(1994, 1, 1), datetime.date(1994, 12, 31)),
        )
        self.assertEqual(rows[0]["timex_text"], "1994")

    def test_full_date_bound_is_an_exact_match(self):
        conn = _FakeConn()
        _find_extractions(conn, "CELEX-1", PartialDate(year=1994, month=3, day=5))
        _, params = conn.calls[0]
        self.assertEqual(params["first_day"], params["last_day"])


if __name__ == "__main__":
    unittest.main()
