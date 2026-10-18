"""Tests for the malformed-date repair rules in ``pipelines.heideltime.qa.scan_dates``.

Pure text transformations — no database, no JAR.  Every fixture below is a real
shape measured in ``celex_facts.pg_embedding_qwen`` on 2026-08-31, so a
regression here means the corpus repair has actually stopped working.

Run with::

    .venv/bin/python -m unittest \
        src.pipelines.heideltime.tests.test_scan_dates -v
"""

import unittest

from pipelines.heideltime.qa.scan_dates import repair_document


class TestRepairDocument(unittest.TestCase):
    """Rule-by-rule behaviour of :func:`repair_document`."""

    def _repair(self, document: str) -> str:
        """Return only the repaired text, discarding the rewrite log."""
        return repair_document(document)[0]

    # ------------------------------------------------------------------
    # Shapes the 2026-04 day-first regex could not see
    # ------------------------------------------------------------------

    def test_month_first_glued_is_reordered(self):
        """``January152010`` carries day and year glued behind the month."""
        self.assertEqual(
            self._repair("valid until January152010 under"),
            "valid until 15 January 2010 under",
        )

    def test_month_first_glued_single_digit_day(self):
        """A five-digit run splits as one day digit plus the four-digit year."""
        self.assertEqual(
            self._repair("On January31995, ICG"), "On 3 January 1995, ICG"
        )

    def test_month_year_glued_is_separated(self):
        """``August1995`` yields a partial-precision DATE once separated."""
        self.assertEqual(
            self._repair("from August1995 through"), "from August 1995 through"
        )

    def test_word_glued_before_day_is_split(self):
        """``on9 November 1994`` hides a well-formed date behind a word tail."""
        self.assertEqual(
            self._repair("at the meeting on9 November 1994 (recital 66)"),
            "at the meeting on 9 November 1994 (recital 66)",
        )

    def test_word_glued_before_month_is_split(self):
        """``midOctober 2001`` and month-month runs both need a boundary."""
        self.assertEqual(
            self._repair("for the period from midOctober 2001 to"),
            "for the period from mid October 2001 to",
        )

    def test_word_glued_after_year_is_split(self):
        """``1989until`` glues the next word onto the year."""
        self.assertEqual(
            self._repair("From 19 December 1989until 15 May 2003"),
            "From 19 December 1989 until 15 May 2003",
        )

    def test_footnote_glued_to_year_is_split(self):
        """A footnote marker fused to the year hides the whole date."""
        self.assertEqual(
            self._repair("is dated 16 November 2005716."),
            "is dated 16 November 2005 716.",
        )

    def test_day_first_glued_still_repaired(self):
        """The original rule: the corpus is clean of this shape, keep it guarded."""
        self.assertEqual(
            self._repair("decision of 7April1965 applies"),
            "decision of 7 April 1965 applies",
        )

    # ------------------------------------------------------------------
    # Non-damage
    # ------------------------------------------------------------------

    def test_well_formed_date_is_left_alone(self):
        """A correctly spaced date must produce no rewrite at all."""
        document = "The decision of 7 April 1965 entered into force."
        repaired, rewrites = repair_document(document)
        self.assertEqual(repaired, document)
        self.assertEqual(rewrites, [])

    def test_long_digit_run_is_not_split(self):
        """``(?!\\d)`` keeps the rules off runs they cannot split unambiguously."""
        document = "reference January1234567 in the annex"
        self.assertEqual(self._repair(document), document)

    def test_year_followed_by_eight_digits_is_untouched(self):
        """Eight digits after a date are not a year plus a footnote marker."""
        document = "dated 1 January 20001998 in the file"
        self.assertEqual(self._repair(document), document)

    def test_uppercase_prefix_is_not_split(self):
        """Only a lowercase word tail is glue; capitals belong to identifiers."""
        document = "case IV13 March 2006"
        self.assertEqual(self._repair(document), document)

    def test_repair_is_idempotent(self):
        """Repairing twice must equal repairing once, or a rerun keeps churning."""
        document = (
            "untilJanuary152010 and from August1995, see on9 November 1994"
            " and 16 November 2005716."
        )
        once = self._repair(document)
        self.assertEqual(self._repair(once), once)

    # ------------------------------------------------------------------
    # Rewrite log
    # ------------------------------------------------------------------

    def test_rewrites_are_reported_with_their_rule_name(self):
        """The xlsx report needs to say which rule produced each rewrite."""
        _, rewrites = repair_document("until January152010")
        self.assertEqual(
            rewrites, [("month_first_glued", "January152010", "15 January 2010")]
        )

    def test_multiple_rules_report_each_rewrite(self):
        """A chunk hit by several rules logs one entry per substitution."""
        _, rewrites = repair_document("on9 November 1994 and August1995")
        self.assertEqual(
            [name for name, _, _ in rewrites],
            ["word_glued_before_day", "month_year_glued"],
        )


if __name__ == "__main__":
    unittest.main()
