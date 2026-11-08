"""Unit tests for :mod:`pipelines.heideltime.event_digest` (pure, no DB, no LLM)."""

import json
import unittest

from pipelines.heideltime.event_digest import (
    EventLine,
    digest_events,
    render_digest,
    split_batches,
)


def _row(date: str, actor: str = "Akzo", predicate: str = "met competitors") -> dict:
    return {"event_date": date, "actor": actor, "predicate_object": predicate}


class TestDigestEvents(unittest.TestCase):
    """The digest keeps real, dated events in chronological order."""

    def test_rows_are_sorted_chronologically(self):
        rows = [_row("1999-03-01"), _row("1994"), _row("1994-02-10")]
        self.assertEqual([e.date for e in digest_events(json.dumps(rows))], ["1994", "1994-02-10", "1999-03-01"])

    def test_filler_rows_are_dropped_but_redacted_actors_kept(self):
        """A redacted name hides a real act; only 'no activity' rows are filler."""
        rows = [
            _row("1994-01-01", actor="redacted", predicate="no activity described in text"),
            _row("1994-01-02", actor="redacted", predicate="discussed prices"),
            _row("1994-01-03", predicate="unknown"),
            _row("1994-01-04", predicate=""),
        ]
        self.assertEqual(
            digest_events(rows), [EventLine(date="1994-01-02", actor="redacted", predicate="discussed prices")]
        )

    def test_impossible_dates_are_dropped(self):
        rows = [_row("0010-01-02"), _row("2011-00-27"), _row("unknown"), _row("2001-05-05")]
        self.assertEqual([e.date for e in digest_events(rows)], ["2001-05-05"])

    def test_malformed_payloads_yield_nothing(self):
        for payload in (None, "", "not json", json.dumps({"a": 1}), json.dumps(["event", 3])):
            with self.subTest(payload=payload):
                self.assertEqual(digest_events(payload), [])


class TestRenderAndSplit(unittest.TestCase):
    """One compact line per event; batches are contiguous and within budget."""

    def test_render_is_one_pipe_separated_line_per_event(self):
        lines = [EventLine(date="1994", actor="Akzo", predicate="met"), EventLine(date="1995", actor="X", predicate="y")]
        self.assertEqual(render_digest(lines), "1994 | Akzo | met\n1995 | X | y")

    def test_input_within_budget_is_one_batch(self):
        lines = [EventLine(date="1994", actor="A", predicate="p")] * 3
        self.assertEqual(split_batches(lines, budget_tokens=1000, count_tokens=len), [lines])

    def test_batches_respect_the_budget_and_keep_order(self):
        lines = [EventLine(date=f"19{90 + i}", actor="A", predicate="p") for i in range(6)]
        line_cost = len(render_digest(lines[:1]))
        batches = split_batches(lines, budget_tokens=line_cost * 2 + 1, count_tokens=len)
        self.assertEqual([b for batch in batches for b in batch], lines)
        self.assertEqual([len(b) for b in batches], [2, 2, 2])
        for batch in batches:
            self.assertLessEqual(len(render_digest(batch)), line_cost * 2 + 1)

    def test_empty_input_has_no_batches(self):
        self.assertEqual(split_batches([], budget_tokens=10, count_tokens=len), [])


if __name__ == "__main__":
    unittest.main()
