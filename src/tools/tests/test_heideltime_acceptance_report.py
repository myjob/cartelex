"""Unit tests for tools.heideltime_acceptance_report (no DB)."""

import json
import unittest

import polars as pl

from tools.heideltime_acceptance_report import (
    AGG_FACT,
    DECISION_FACT,
    PERIOD_FACT,
    assess,
    decision_id_truth,
    first_value,
    lost_periods,
    period_value,
)


def _frame(rows: list[tuple[str, str, object]]) -> pl.DataFrame:
    """Build a latest-fact frame from (celex_id, fact, value) triples."""
    return pl.DataFrame(
        [(c, n, json.dumps(v)) for c, n, v in rows],
        schema=["celex_id", "name", "value_json"],
        orient="row",
    )


class TestValueReaders(unittest.TestCase):
    """Fact values are read by shape, not by a fixed key."""

    def test_first_value_ignores_the_key_name(self) -> None:
        """The decision-date fact stores its value under varying keys."""
        for key in ("eu_commission_decision_date", "extracted_fact", "extracted_value"):
            self.assertEqual(first_value(json.dumps([{key: "1990-12-19"}])), "1990-12-19")

    def test_first_value_treats_na_and_empty_as_missing(self) -> None:
        self.assertIsNone(first_value(json.dumps([{"extracted_fact": "NA"}])))
        self.assertIsNone(first_value(json.dumps([])))
        self.assertIsNone(first_value(None))

    def test_period_value_reads_date_range(self) -> None:
        self.assertEqual(period_value(json.dumps([{"date_range": "1994 : 1999-03"}])), "1994 : 1999-03")
        self.assertIsNone(period_value(json.dumps([])))


class TestAssess(unittest.TestCase):
    """Corpus-level checks over the latest facts."""

    def test_counts_sentinel_invalid_inverted_and_decision_order(self) -> None:
        frame = _frame(
            [
                ("A", PERIOD_FACT, [{"date_range": "1994 : 1999-03"}]),
                ("A", DECISION_FACT, [{"x": "2001-05-02"}]),
                ("B", PERIOD_FACT, [{"date_range": "1900-01-01 : 1999-12-31"}]),
                ("C", PERIOD_FACT, [{"date_range": "from 1994 onwards"}]),
                ("D", PERIOD_FACT, [{"date_range": "2001 : 1999"}]),
                ("E", PERIOD_FACT, [{"date_range": "1995 : 2003-06-27"}]),
                ("E", DECISION_FACT, [{"x": "2001-06-22"}]),
                ("E", AGG_FACT, [{"event": "1995"}, {"event": "2003"}]),
            ]
        )
        stats, flagged = assess(frame, ["A", "B", "C", "D", "E"])
        self.assertEqual(stats.period_filled, 5)
        self.assertEqual(stats.period_sentinel, 1)
        self.assertEqual(stats.period_invalid, 1)
        self.assertEqual(stats.period_inverted, 1)
        self.assertEqual(stats.end_before_decision, 1)
        self.assertEqual(stats.end_on_or_after_decision, 1)
        self.assertEqual(stats.agg_events_max, 2)
        self.assertEqual({f.celex_id for f in flagged}, {"B", "C", "D", "E"})


class TestComparisons(unittest.TestCase):
    """Before/after and id-embedded ground truth."""

    def test_lost_periods_classifies_why_a_period_disappeared(self) -> None:
        before = _frame(
            [
                ("A", PERIOD_FACT, [{"date_range": "1900-01-01 : 1990"}]),
                ("B", PERIOD_FACT, [{"date_range": "1990 : 2005"}]),
                ("C", PERIOD_FACT, [{"date_range": "1990 : 1995"}]),
            ]
        )
        after = _frame([("B", DECISION_FACT, [{"x": "2000-01-01"}]), ("C", DECISION_FACT, [{"x": "2000-01-01"}])])
        lost = lost_periods(before, after, ["A", "B", "C"])
        self.assertEqual(dict(zip(lost["celex_id"], lost["reason"])), {"A": "sentinel", "B": "end>=decision", "C": "plausible"})

    def test_decision_id_truth_compares_dec_ids_only(self) -> None:
        after = _frame(
            [
                ("DEC-AT1-20101109", DECISION_FACT, [{"x": "2010-11-09"}]),
                ("DEC-AT2-20130306", DECISION_FACT, [{"x": "2009-12-16"}]),
                ("CELEX-31987D0014", DECISION_FACT, [{"x": "1986-12-01"}]),
            ]
        )
        truth = decision_id_truth(after, ["DEC-AT1-20101109", "DEC-AT2-20130306", "CELEX-31987D0014"])
        self.assertEqual(truth.height, 2)
        self.assertEqual(truth.filter(pl.col("match")).get_column("celex_id").to_list(), ["DEC-AT1-20101109"])


if __name__ == "__main__":
    unittest.main()
