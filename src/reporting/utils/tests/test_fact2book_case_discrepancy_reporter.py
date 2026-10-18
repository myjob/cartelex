"""Unit tests for the fact2book case-discrepancy reporter.

The classification, rendering and splicing helpers are pure functions over book
paths and chunk text, so these run in isolation; the builder is tested with
mocked repositories (the DB boundary). Paths and headings are verbatim from the
Russo-Schinkel book corpus.
"""

import unittest
from unittest.mock import MagicMock

from reporting.utils.fact2book_case_discrepancy_reporter import (
    REPORT_END,
    REPORT_START,
    DiscrepancyClass,
    Fact2BookCaseDiscrepancyReporter,
    MentionKind,
    MismatchFact,
    assess_case,
    classify_mention,
    decision_number,
    excerpt,
    is_own_landmark,
    render_report,
    splice_report,
)

_S52 = "Infringements -> 5 Vertical restrictions -> 5.2 Territorial exclusivity and parallel import /export bans"
_E52 = f"{_S52} -> E Commission decisions in chronological order•"
_F52 = (
    f"{_S52} -> F Economic landmark cases -> F.1 BMW Belgium NV and Belgian BMW "
    "dealers Commission decision 78/155/EEC [1978] OJ L 46/33"
)
_E55 = (
    "Infringements -> 5 Vertical restrictions -> 5.5 Selective distribution systems"
    " -> E Commission decisions in chronological order•"
)
_E21 = (
    "Infringements -> 2 Horizontal restrictions -> 2.1 Price fixing collusion"
    " -> E Commission decisions in chronological order•"
)
_A21 = "Infringements -> 2 Horizontal restrictions -> 2.1 Price fixing collusion -> A Introduction"


def _fact(claimed: list[str]) -> MismatchFact:
    """A mismatched horizontal-category fact claiming *claimed*."""
    return MismatchFact(
        name="infringement_category_hor_vert_lic_joint",
        value="horizontal",
        claimed=claimed,
        reference_type="landmark",
        explanation="Book taxonomy files the case under 5 Vertical restrictions",
    )


class TestClassifyMention(unittest.TestCase):
    """Sub-chapter kinds from the book path."""

    def test_listing(self):
        self.assertIs(classify_mention(_E52), MentionKind.LISTING)

    def test_selection_listing_variant(self):
        path = f"{_S52} -> E Selection of commission decisions in chronological order•"
        self.assertIs(classify_mention(path), MentionKind.LISTING)

    def test_landmark_subsection(self):
        self.assertIs(classify_mention(_F52), MentionKind.LANDMARK)

    def test_singular_landmark_heading(self):
        path = "Infringements -> 5 Vertical restrictions -> 5.6 Franchising -> F Economic landmark case"
        self.assertIs(classify_mention(path), MentionKind.LANDMARK)

    def test_introduction(self):
        self.assertIs(classify_mention(_A21), MentionKind.INTRODUCTION)

    def test_reference_list(self):
        path = f"{_S52} -> D Related legal texts"
        self.assertIs(classify_mention(path), MentionKind.REFERENCE)

    def test_section_body(self):
        self.assertIs(classify_mention(_S52), MentionKind.BODY)

    def test_index(self):
        self.assertIs(classify_mention("Index -> Cases"), MentionKind.INDEX)


class TestDecisionNumber(unittest.TestCase):
    """CELEX id to Commission decision number."""

    def test_two_digit_year(self):
        self.assertEqual(decision_number("CELEX-31978D0155"), "78/155/")

    def test_four_digit_year_from_1999(self):
        self.assertEqual(decision_number("32001D0478"), "2001/478/")

    def test_non_decision_id(self):
        self.assertIsNone(decision_number("34579_1889_2"))


class TestIsOwnLandmark(unittest.TestCase):
    """Own F.n write-up versus a landmark that only cites the decision."""

    def test_heading_in_path(self):
        self.assertTrue(is_own_landmark(_F52, "Case IV/29.146 ...", "78/155/"))

    def test_heading_in_text_under_generic_path(self):
        path = (
            "Infringements -> 3 Abuse of dominance -> 3.2 Loyalty discounts and "
            "fidelity rebates -> F Economic landmark cases"
        )
        text = (
            "F Economic landmark cases\nF.1 European sugar industry Commission "
            "decision 73/109/EC [1973] OJ L 140/17\nRelevance: The first case ..."
        )
        self.assertTrue(is_own_landmark(path, text, "73/109/"))

    def test_citation_in_relevance_is_not_own(self):
        text = (
            "F.2 Magill TV Guide Commission decision 89/205/EEC\nRelevance: as in "
            "Commission decision 92/213/EEC the Commission ..."
        )
        self.assertFalse(is_own_landmark("x -> F Economic landmark cases", text, "92/213/"))

    def test_longer_number_does_not_match(self):
        text = "F.1 Other Commission decision 178/155/EEC"
        self.assertFalse(is_own_landmark("x", text, "78/155/"))

    def test_unknown_decision(self):
        self.assertFalse(is_own_landmark(_F52, "", None))


class TestExcerpt(unittest.TestCase):
    """Single-line windows around the case id."""

    def test_centres_on_surface(self):
        text = "a " * 300 + "Case IV/29.146 - Decision" + " b" * 300
        window = excerpt(text, "iv/29.146", budget=60)
        self.assertIn("IV/29.146", window)
        self.assertTrue(window.startswith("...") and window.endswith("..."))

    def test_short_text_uncut(self):
        self.assertEqual(excerpt("Case\n IV/71", "IV/71"), "Case IV/71")


class TestAssessCase(unittest.TestCase):
    """Classification against the filing chapter."""

    def test_listing_only(self):
        case = assess_case(
            "CELEX-31980D0256", "IV/29595", [_fact(["2"])], "IV/29.595",
            [(_E52, "... IV/29.595 ...")],
        )
        self.assertIs(case.classification, DiscrepancyClass.LISTING_ONLY)
        self.assertEqual(case.filing_chapters, ["5"])
        self.assertEqual(case.filing_kinds(), [MentionKind.LISTING])

    def test_detailed_in_filing_chapter_uses_landmark_placement(self):
        case = assess_case(
            "CELEX-31978D0155", "IV/29146", [_fact(["2", "2.5"])], "IV/29.146",
            [(_E52, "IV/29.146"), (_F52, "Case IV/29.146"), (_E55, "IV/29.146")],
        )
        self.assertIs(case.classification, DiscrepancyClass.DETAILED_IN_FILING_CHAPTER)
        self.assertEqual(case.filing_chapters, ["5"])
        (landmark,) = case.detailed(in_filing=True)
        self.assertTrue(landmark.own_landmark)
        self.assertEqual(landmark.section, "5.2")

    def test_detailed_elsewhere(self):
        # Filed under 5 by an E listing; chapter 2's introduction discusses it,
        # yet a listing-only placement still counts every path.
        case = assess_case(
            "CELEX-31980D0256", "IV/29595", [_fact(["6"])], "IV/29.595",
            [(_E52, "IV/29.595"), (_A21, "IV/29.595")],
        )
        self.assertEqual(case.filing_chapters, ["2", "5"])
        self.assertIs(case.classification, DiscrepancyClass.DETAILED_IN_FILING_CHAPTER)

        case = assess_case(
            "CELEX-31980D0256", "IV/29595", [_fact(["2"])], "IV/29.595",
            [(_E52, "IV/29.595"), (_A21, "IV/29.595")],
        )
        self.assertEqual(case.filing_chapters, ["5"])
        self.assertIs(case.classification, DiscrepancyClass.DETAILED_ELSEWHERE)

    def test_non_decisive_chapter_is_not_filing(self):
        path = "Infringements -> 8 Mergers -> E Commission decisions in chronological order•"
        case = assess_case("x", "IV/1", [_fact(["2"])], "IV/1", [(_E52, "IV/1"), (path, "IV/1")])
        self.assertEqual(case.filing_chapters, ["5"])

    def test_unresolved_surface(self):
        case = assess_case("x", "IV/1", [_fact(["2"])], None, [])
        self.assertIsNone(case.classification)


class TestRenderAndSplice(unittest.TestCase):
    """Markdown block and its idempotent splice."""

    def setUp(self):
        self.cases = [
            assess_case("A", "IV/1", [_fact(["2"])], "IV/1", [(_E52, "IV/1")]),
            assess_case("B", "IV/29146", [_fact(["2"])], "IV/29.146", [(_F52, "IV/29.146")]),
            assess_case("C", "IV/3", [_fact(["2"])], None, []),
        ]

    def test_lists(self):
        block = render_report(self.cases, "%infringement%")
        self.assertTrue(block.startswith(REPORT_START))
        self.assertIn(REPORT_END, block)
        list1, list2 = block.split("### List 2")
        self.assertIn("| A | IV/1 |", list1)
        self.assertNotIn("| B |", list1)
        self.assertIn("| B | IV/29146 |", list2)
        self.assertIn("- C IV/3", list2)

    def test_splice_replaces_block_and_keeps_prose(self):
        doc = f"# Title\n\nDesign.\n\n{REPORT_START}\nold\n{REPORT_END}\n\n## Tail\n"
        spliced = splice_report(doc, f"{REPORT_START}\nnew\n{REPORT_END}\n")
        self.assertIn("Design.", spliced)
        self.assertIn("## Tail", spliced)
        self.assertIn("new", spliced)
        self.assertNotIn("old", spliced)
        self.assertEqual(splice_report(spliced, f"{REPORT_START}\nnew\n{REPORT_END}\n"), spliced)

    def test_splice_appends_without_markers(self):
        spliced = splice_report("# Title", "BLOCK\n")
        self.assertEqual(spliced, "# Title\nBLOCK\n")


class TestReporterBuild(unittest.TestCase):
    """Builder over mocked repositories."""

    def test_groups_rows_and_resolves_surface(self):
        check_repo = MagicMock()
        check_repo.list_mismatches.return_value = [
            {
                "celex_id": "CELEX-31980D0256", "case_id": "IV/29595",
                "name": "infringement_category_hor_vert_lic_joint",
                "value": "horizontal", "book_path": _E52,
                "reference_type": "simple", "explanation": "x",
            },
            {
                "celex_id": "CELEX-31980D0256", "case_id": "IV/29595",
                "name": "infringement_subcategory_horizontal_restraints",
                "value": "export cartels", "book_path": _E52,
                "reference_type": "simple", "explanation": "x",
            },
        ]
        book_repo = MagicMock()
        book_repo.keyword_exists.side_effect = lambda surface: surface == "IV/29.595"
        book_repo.select_reference_chunks.return_value = [(_E52, "IV/29.595")]

        (case,) = Fact2BookCaseDiscrepancyReporter(check_repo, book_repo).build()

        self.assertEqual(case.book_surface, "IV/29.595")
        self.assertEqual([f.claimed for f in case.facts], [["2"], ["2.5"]])
        self.assertIs(case.classification, DiscrepancyClass.LISTING_ONLY)
        book_repo.select_reference_chunks.assert_called_once()


if __name__ == "__main__":
    unittest.main()
