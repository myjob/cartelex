"""Unit tests for ``pipelines.legal.recital_count``.

The inputs mimic shapes measured in the MinerU corpus (2026-11-01): line-start
``(n)`` recitals, interleaved sub-lists restarting at (1), mid-sentence
cross-references, OCR outliers, dropped numbers, chunk-overlap repeats, and
older decisions that number paragraphs ``n.`` instead of ``(n)``.
"""

import unittest

from config.settings import settings
from pipelines.legal.recital_count import (
    REASON_CHAIN_TOO_SHORT,
    REASON_LOW_COVERAGE,
    REASON_NUMBERS_BEYOND_MAX,
    REASON_NUMBERING_RESTARTS,
    REASON_NO_NUMBERING,
    RecitalNumbering,
    RecitalStatus,
    RecitalThresholds,
    count_recitals,
    find_candidates,
    line_start_marker,
)

T = RecitalThresholds(
    max_gap=3, max_start=3, min_chain=5, min_coverage=0.6, min_coverage_confident=0.9
)


def _seq(nums) -> str:
    """Render recital numbers as line-start ``(n)`` paragraphs."""
    return "\n\n".join(f"({n}) Text of recital {n}." for n in nums)


class TestCountRecitals(unittest.TestCase):
    """The longest +1..+gap chain from a low start yields the recital max."""

    def test_clean_sequence(self):
        r = count_recitals(_seq(range(1, 21)), T)
        self.assertEqual((r.recitals_max, r.recitals_found, r.gaps), (20, 20, 0))
        self.assertEqual(r.status, RecitalStatus.CONFIDENT)
        self.assertEqual(r.numbering, RecitalNumbering.BRACKET)

    def test_mid_sentence_numbers_ignored(self):
        text = _seq(range(1, 11)) + "\n\nAs set out in recital (250) and Article 7(1), in (1998)."
        self.assertEqual(count_recitals(text, T).recitals_max, 10)

    def test_sublist_reset_android_shape(self):
        nums = list(range(1, 152)) + [1, 2, 3] + list(range(153, 160))
        r = count_recitals(_seq(nums), T)
        self.assertEqual((r.recitals_max, r.recitals_found, r.gaps), (159, 158, 1))

    def test_outlier_cannot_hijack_max(self):
        r = count_recitals(_seq(list(range(1, 31)) + [1998] + list(range(31, 41))), T)
        self.assertEqual(r.recitals_max, 40)

    def test_missing_numbers_within_gap(self):
        nums = [n for n in range(1, 101) if n % 10 != 0] + [100]
        r = count_recitals(_seq(nums), T)
        self.assertEqual((r.recitals_max, r.recitals_found), (100, 91))
        self.assertEqual(r.status, RecitalStatus.CONFIDENT)

    def test_chain_must_start_low(self):
        # 25..59 is longer but starts above max_start, so it is not admissible
        r = count_recitals(_seq(list(range(1, 21)) + list(range(25, 60))), T)
        self.assertEqual((r.recitals_max, r.coverage), (20, 1.0))
        # the 35 numbers above 20 reveal the cut, so it is not confident
        self.assertEqual((r.status, r.reason), (RecitalStatus.DOUBTFUL, REASON_NUMBERS_BEYOND_MAX))

    def test_repeats_from_overlap_ignored(self):
        r = count_recitals(_seq(list(range(1, 11)) + [10, 11, 12]), T)
        self.assertEqual((r.recitals_max, r.recitals_found), (12, 12))

    def test_bare_number_line_counts(self):
        text = _seq(range(1, 10)) + "\n\n(10)\nText of recital ten."
        self.assertEqual(count_recitals(text, T).recitals_max, 10)

    def test_marker_variants(self):
        lines = ["(1) a", "( 2) b", "( 3 ) c", "(4 ) d", "> (5) e", "- (6) f"]
        found = find_candidates("\n".join(lines), RecitalNumbering.BRACKET)
        self.assertEqual([c.number for c in found], [1, 2, 3, 4, 5, 6])

    def test_number_glued_to_text_counts(self):
        # MinerU drops the space after 4-digit numbers: "(1000)After the ..."
        text = _seq(range(1, 1000)) + "\n\n" + "\n\n".join(f"({n})After item {n}." for n in range(1000, 1011))
        self.assertEqual(count_recitals(text, T).recitals_max, 1010)

    def test_unicode_space_after_bracket_counts(self):
        lines = ["(1)\u202fa", "(2)\u00a0b", "(3) c"]
        found = find_candidates("\n".join(lines), RecitalNumbering.BRACKET)
        self.assertEqual([c.number for c in found], [1, 2, 3])

    def test_glued_digit_is_not_a_marker(self):
        found = find_candidates("(12)3 is not a recital", RecitalNumbering.BRACKET)
        self.assertEqual(found, [])

    def test_dot_fallback_skips_headers_and_tables(self):
        body = "\n\n".join(f"{n}. The Commission notes item {n}." for n in range(1, 31))
        text = "# 1. Facts\n\n| 2. | cell |\n\n" + body
        r = count_recitals(text, T)
        self.assertEqual((r.recitals_max, r.numbering), (30, RecitalNumbering.DOT))

    def test_dot_ignored_when_bracket_trustworthy(self):
        # modern decisions carry short dot lists that restart; they cannot outgrow the recitals
        lists = "\n\n".join("\n".join(f"{n}. list item" for n in range(1, 8)) for _ in range(6))
        text = _seq(range(1, 21)) + "\n\n" + lists
        r = count_recitals(text, T)
        self.assertEqual((r.recitals_max, r.numbering), (20, RecitalNumbering.BRACKET))

    def test_short_bracket_list_does_not_beat_dot_recitals(self):
        # 37396_45_3: a "(1)".."(10)" list of parties inside a dot-numbered decision
        parties = _seq(range(1, 11))
        body = "\n\n".join(f"{n}. The Commission notes item {n}." for n in range(1, 99))
        r = count_recitals(parties + "\n\n" + body, T)
        self.assertEqual((r.recitals_max, r.numbering), (98, RecitalNumbering.DOT))

    def test_numbering_restarting_per_section_not_confident(self):
        # CELEX-31983D0400: "1." restarts under every heading
        sections = [range(1, 9), range(1, 7), range(1, 8)]
        text = "\n\n".join(
            f"# Section {i}\n\n" + "\n\n".join(f"{n}. Paragraph {n}." for n in rng)
            for i, rng in enumerate(sections)
        )
        r = count_recitals(text, T)
        self.assertEqual((r.recitals_max, r.status, r.reason), (8, RecitalStatus.DOUBTFUL, REASON_NUMBERING_RESTARTS))

    def test_short_sublists_do_not_flag_restart(self):
        nums = list(range(1, 152)) + [1, 2, 3, 4, 5, 6] + list(range(152, 300))
        r = count_recitals(_seq(nums), T)
        self.assertEqual((r.recitals_max, r.status), (299, RecitalStatus.CONFIDENT))

    def test_first_recital_recorded(self):
        r = count_recitals(_seq(range(2, 30)), T)
        self.assertEqual((r.first_recital, r.recitals_max), (2, 29))

    def test_restart_longer_main_part_wins(self):
        r = count_recitals(_seq(list(range(1, 41)) + list(range(1, 31))), T)
        self.assertEqual(r.recitals_max, 40)

    def test_no_numbering_undetermined(self):
        r = count_recitals("Plain text without numbers.", T)
        self.assertEqual(
            (r.recitals_max, r.status, r.numbering, r.reason),
            (None, RecitalStatus.UNDETERMINED, RecitalNumbering.NONE, REASON_NO_NUMBERING),
        )

    def test_chain_too_short_undetermined(self):
        r = count_recitals(_seq([1, 2, 3]), T)
        self.assertEqual((r.recitals_max, r.reason), (None, REASON_CHAIN_TOO_SHORT))

    def test_doubtful_band(self):
        nums = [n for n in range(1, 101) if n % 4 != 0] + [100]  # 76/100
        r = count_recitals(_seq(nums), T)
        self.assertEqual((r.recitals_max, r.status), (100, RecitalStatus.DOUBTFUL))

    def test_low_coverage_undetermined(self):
        nums = list(range(1, 6)) + list(range(8, 200, 3))  # 3-steps: ~0.36 coverage
        r = count_recitals(_seq(nums), T)
        self.assertEqual((r.recitals_max, r.reason), (None, REASON_LOW_COVERAGE))

    def test_truncated_chain_not_confident(self):
        # main chain breaks at a gap wider than max_gap; the leading list must
        # not pass as a confident max while higher numbers follow
        nums = list(range(1, 8)) + list(range(9 + T.max_gap + 5, 60))
        r = count_recitals(_seq(nums), T)
        self.assertEqual(r.recitals_max, 7)
        self.assertEqual((r.status, r.reason), (RecitalStatus.DOUBTFUL, REASON_NUMBERS_BEYOND_MAX))
        self.assertEqual(r.beyond_max, len(range(9 + T.max_gap + 5, 60)))

    def test_single_stray_beyond_max_stays_confident(self):
        r = count_recitals(_seq(list(range(1, 31)) + [1998]), T)
        self.assertEqual((r.recitals_max, r.status, r.beyond_max), (30, RecitalStatus.CONFIDENT, 1))

    def test_footnotes_after_operative_part_ignored(self):
        # 33708_6_7: recitals end at (218), footnotes after the operative
        # part are rendered as line-start "(219)".."(432)"
        text = (
            _seq(range(1, 219))
            + "\n\n# HAS ADOPTED THIS DECISION:\n\nArticle 1\n\n"
            + "\n\n".join(f"({n}) See recital {n - 200}." for n in range(219, 433))
        )
        r = count_recitals(text, T)
        self.assertEqual((r.recitals_max, r.status), (218, RecitalStatus.CONFIDENT))

    def test_operative_list_does_not_extend_dot_chain(self):
        # CELEX-31978D0155: dot recitals end at 27, an operative list continues 28..44
        body = "\n\n".join(f"{n}. Whereas item {n}." for n in range(1, 28))
        tail = "\n\n".join(f"{n}. obligation {n}" for n in range(28, 45))
        r = count_recitals(body + "\n\nHAS ADOPTED THIS DECISION:\n\n" + tail, T)
        self.assertEqual(r.recitals_max, 27)

    def test_older_operative_phrasings_cut(self):
        for phrase in (
            "# HAS ISSUED THE FOLLOWING DECISION :",
            "HAS MADE THE FOLLOWING DECISION:",
            "# HAS GIVEN THE FOLLOWING ARRANGEMENT:",
        ):
            with self.subTest(phrase=phrase):
                text = _seq(range(1, 21)) + f"\n\n{phrase}\n\n" + _seq(range(21, 40))
                self.assertEqual(count_recitals(text, T).recitals_max, 20)

    def test_quoted_marker_inside_recitals_does_not_cut(self):
        text = (
            _seq(range(1, 11))
            + "\n\n(11) The 1994 decision stated that the Commission HAS ADOPTED THIS DECISION.\n\n"
            + _seq(range(12, 31))
            + "\n\nHAS ADOPTED THIS DECISION:\n\nArticle 1"
        )
        self.assertEqual(count_recitals(text, T).recitals_max, 30)

    def test_thresholds_from_settings(self):
        self.assertEqual(RecitalThresholds.from_settings().max_gap, settings.RECITAL_MAX_GAP)


class TestLineStartMarker(unittest.TestCase):
    """The marker locates one recital at a line start, not mid-sentence."""

    def test_bracket_marker(self):
        pattern = line_start_marker(21, RecitalNumbering.BRACKET)
        self.assertIsNotNone(pattern.search("intro\n( 21 ) A video game"))
        self.assertIsNone(pattern.search("see recital (21) above"))
        self.assertIsNone(pattern.search("(210) other"))

    def test_dot_marker(self):
        pattern = line_start_marker(7, RecitalNumbering.DOT)
        self.assertIsNotNone(pattern.search("x\n7. The Commission"))
        self.assertIsNone(pattern.search("# 7. Header"))


if __name__ == "__main__":
    unittest.main()
