"""Integration test for ``migrate_cartel_period_partial_dates.sql`` (#184).

Runs the migration inside one transaction against ``settings.DATABASE_URL`` and
always rolls it back, so the live schema is never changed; Postgres DDL is
transactional. Skipped when the database is unreachable.
"""

import datetime
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from config.settings import settings

_MIGRATION = Path(__file__).resolve().parents[1] / "migrate_cartel_period_partial_dates.sql"


class TestCartelPeriodPartialDatesMigration(unittest.TestCase):
    """Partial-precision bounds widen to their span instead of becoming NA."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(settings.DATABASE_URL)
        try:
            with cls.engine.connect():
                pass
        except OperationalError as exc:  # pragma: no cover - environment
            raise unittest.SkipTest(f"database unreachable: {exc}") from exc

    def _in_rolled_back_migration(self, sql: str, params: dict | None = None):
        """Apply the migration, run ``sql``, return its rows, roll back."""
        with self.engine.connect() as conn:
            trans = conn.begin()
            try:
                conn.exec_driver_sql(_MIGRATION.read_text(encoding="utf-8"))
                return conn.execute(text(sql), params or {}).all()
            finally:
                trans.rollback()

    def test_bounds_widen_to_their_span(self):
        cases = {
            "1994": (datetime.date(1994, 1, 1), datetime.date(1994, 12, 31)),
            "1996-02": (datetime.date(1996, 2, 1), datetime.date(1996, 2, 29)),
            "1994-03-05": (datetime.date(1994, 3, 5), datetime.date(1994, 3, 5)),
            # Legacy rows zero-pad an unknown component (CELEX-31978D0172).
            "1973-00-00": (datetime.date(1973, 1, 1), datetime.date(1973, 12, 31)),
            "1977-03-00": (datetime.date(1977, 3, 1), datetime.date(1977, 3, 31)),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                rows = self._in_rolled_back_migration(
                    "SELECT celex_facts.partial_date_first_day(:r), celex_facts.partial_date_last_day(:r)",
                    {"r": raw},
                )
                self.assertEqual(tuple(rows[0]), expected)

    def test_impossible_or_non_dates_are_null(self):
        for raw in ("1994-02-30", "2011-00-27", "1994-13", "0000-00-00", "NA", "", " 1994"):
            with self.subTest(raw=raw):
                rows = self._in_rolled_back_migration(
                    "SELECT celex_facts.partial_date_first_day(:r), celex_facts.partial_date_last_day(:r)",
                    {"r": raw},
                )
                self.assertEqual(tuple(rows[0]), (None, None))

    def test_view_keeps_its_columns_and_raw_precision(self):
        rows = self._in_rolled_back_migration(
            "SELECT celex_id, cartel_period, legal_period, periods_category"
            " FROM celex_facts.facts_cartel_legal_period_category_vw LIMIT 5"
        )
        for row in rows:
            self.assertRegex(row.cartel_period.strip(), r"^(\d{4}(-\d{2}){0,2}|NA) : (\d{4}(-\d{2}){0,2}|NA)$")


if __name__ == "__main__":
    unittest.main()
