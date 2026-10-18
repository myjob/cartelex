"""
Unit tests for :mod:`pipelines.agent_dspy.book_taxonomy`.

Pure functions over section-path strings and fact values -- no DB or LLM
boundary to fake.
"""

import unittest

from pydantic import ValidationError

from config.settings import settings
from pipelines.agent_dspy.book_taxonomy import (
    BookTaxonomyRule,
    book_placement,
    judge_taxonomy,
    load_taxonomy_rules,
)

_CHIQUITA_LANDMARK = (
    "Infringements -> 3 Abuse of dominance -> 3.1 Excessive pricing, discriminatory "
    "sales conditions, and prohibition to resale -> F Economic landmark cases -> "
    "F.1 Chiquita Commission decision 76/353/EEC [1976] OJ L 95/1"
)
_CHIQUITA_LISTING = (
    "Infringements -> 3 Abuse of dominance -> 3.7 Refusal to deal and essential "
    "facilities doctrine -> E Commission decisions in chronological order"
)
_PRICE_FIXING = (
    "Infringements -> 2 Horizontal restrictions -> 2.1 Price fixing collusion -> "
    "A Introduction"
)
_TERRITORIAL = (
    "Infringements -> 5 Vertical restrictions -> 5.2 Territorial exclusivity and "
    "parallel import /export bans -> A Introduction"
)
_SINGULAR_LANDMARK = (
    "Infringements -> 6 Joint ventures and alliances -> 6.3 Strategic and "
    "technological alliances -> F Economic landmark case -> F.1 BT/MCI"
)
_MERGER = "Infringements -> 8 Mergers and acquisitions -> F Economic landmark case"
_ART_106 = (
    "Infringements -> 7 Decisions addressed to Member States pursuant to Article "
    "106 FEU Treaty -> E Commission decisions in chronological Order"
)
_INDEX = "Index -> Table of cases"

_RULES = load_taxonomy_rules()
_HOR_VERT = _RULES["infringement_category_hor_vert_lic_joint"]
_HORIZONTAL_SUB = _RULES["infringement_subcategory_horizontal_restraints"]
_VERTICAL_SUB = _RULES["infringement_subcategory_vertical_restraints"]
_ABUSE = _RULES["infringement_category_abuse_of_dominance"]


def _label(rule: BookTaxonomyRule, value: str, paths: list[str]) -> str | None:
    """Return the taxonomy label for *value* on *paths*, or ``None``."""
    verdict = judge_taxonomy(rule, value, book_placement(paths))
    return verdict.label if verdict is not None else None


class TestBookPlacement(unittest.TestCase):
    """Placement parses numbered headings and prefers landmark paths."""

    def test_parses_chapters_and_sections(self):
        placement = book_placement([_PRICE_FIXING, _TERRITORIAL])
        self.assertEqual(set(placement.chapters), {"2", "5"})
        self.assertEqual(set(placement.sections), {"2.1", "5.2"})
        self.assertEqual(placement.path_for("5.2"), _TERRITORIAL)
        self.assertEqual(placement.chapter_title("2"), "2 Horizontal restrictions")

    def test_landmark_paths_replace_example_mentions(self):
        placement = book_placement([_PRICE_FIXING, _CHIQUITA_LANDMARK])
        self.assertEqual(set(placement.chapters), {"3"})

    def test_singular_landmark_heading_counts_as_landmark(self):
        placement = book_placement([_PRICE_FIXING, _SINGULAR_LANDMARK])
        self.assertEqual(set(placement.chapters), {"6"})

    def test_unnumbered_section_and_index_paths(self):
        placement = book_placement([_ART_106, _INDEX])
        self.assertEqual(set(placement.chapters), {"7"})
        self.assertEqual(placement.sections, {})

    def test_citing_path_is_stable_regardless_of_input_order(self):
        paths = [_CHIQUITA_LISTING, _CHIQUITA_LANDMARK.replace("F Economic", "E Other")]
        self.assertEqual(
            book_placement(paths).path_for("3"), book_placement(paths[::-1]).path_for("3")
        )


class TestCategoryLabels(unittest.TestCase):
    """``hor_vert_lic_joint`` is judged against chapter numbers."""

    def test_chiquita_horizontal_is_contradicted(self):
        verdict = judge_taxonomy(
            _HOR_VERT, "horizontal", book_placement([_CHIQUITA_LANDMARK, _CHIQUITA_LISTING])
        )
        self.assertEqual(verdict.label, "contradicts")
        self.assertIsNone(verdict.evidence_quote)
        self.assertIn("3 Abuse of dominance", verdict.reason)

    def test_matching_chapter_supports_with_path_quote(self):
        verdict = judge_taxonomy(_HOR_VERT, "vertical", book_placement([_TERRITORIAL]))
        self.assertEqual(verdict.label, "supports")
        self.assertEqual(verdict.evidence_quote, _TERRITORIAL)
        self.assertEqual(verdict.support_score, 1.0)

    def test_multi_chapter_case_supports_any_match(self):
        self.assertEqual(_label(_HOR_VERT, "vertical", [_PRICE_FIXING, _TERRITORIAL]), "supports")

    def test_license_and_joint_venture_labels(self):
        licensing = "Infringements -> 4 Licensing -> 4.2 Patents -> A Introduction"
        self.assertEqual(_label(_HOR_VERT, "license", [licensing]), "supports")
        self.assertEqual(_label(_HOR_VERT, "joint venture", [_SINGULAR_LANDMARK]), "supports")

    def test_non_decisive_chapters_are_inconclusive(self):
        self.assertIsNone(_label(_HOR_VERT, "horizontal", [_MERGER]))
        self.assertIsNone(_label(_HOR_VERT, "horizontal", [_ART_106]))

    def test_unknown_value_is_inconclusive(self):
        self.assertIsNone(_label(_HOR_VERT, "something else", [_PRICE_FIXING]))

    def test_no_placement_is_inconclusive(self):
        self.assertIsNone(_label(_HOR_VERT, "horizontal", [_INDEX]))


class TestSubcategoryLabels(unittest.TestCase):
    """Subcategories are judged against section numbers within their chapter."""

    def test_matching_section_supports(self):
        self.assertEqual(_label(_HORIZONTAL_SUB, "price fixing collusion", [_PRICE_FIXING]), "supports")

    def test_list_valued_fact_matches_any_label(self):
        value = "['market sharing, quota cartels, and specialization agreements', 'price fixing collusion']"
        self.assertEqual(_label(_HORIZONTAL_SUB, value, [_PRICE_FIXING]), "supports")

    def test_semicolon_joined_vertical_value(self):
        value = "Exclusive Dealing / Market Foreclosure; Territorial Exclusivity / Parallel Trade Bans"
        self.assertEqual(_label(_VERTICAL_SUB, value, [_TERRITORIAL]), "supports")

    def test_other_section_in_same_chapter_is_inconclusive(self):
        value = "market sharing, quota cartels, and specialization agreements"
        self.assertIsNone(_label(_HORIZONTAL_SUB, value, [_PRICE_FIXING]))

    def test_other_decisive_chapter_contradicts(self):
        value = "market sharing, quota cartels, and specialization agreements"
        self.assertEqual(_label(_HORIZONTAL_SUB, value, [_CHIQUITA_LANDMARK]), "contradicts")


class TestBooleanCategories(unittest.TestCase):
    """True/False facts are decided only when the book files the case under their chapter."""

    def test_true_in_chapter_supports(self):
        verdict = judge_taxonomy(_ABUSE, "True", book_placement([_CHIQUITA_LANDMARK]))
        self.assertEqual(verdict.label, "supports")
        self.assertEqual(verdict.evidence_quote, _CHIQUITA_LANDMARK)

    def test_false_in_chapter_contradicts(self):
        self.assertEqual(_label(_ABUSE, "False", [_CHIQUITA_LANDMARK]), "contradicts")

    def test_json_quoted_value(self):
        self.assertEqual(_label(_ABUSE, '"true"', [_CHIQUITA_LANDMARK]), "supports")

    def test_book_silence_decides_nothing(self):
        self.assertIsNone(_label(_ABUSE, "True", [_PRICE_FIXING]))
        self.assertIsNone(_label(_ABUSE, "False", [_PRICE_FIXING]))


class TestRules(unittest.TestCase):
    """Rules load from settings and reject malformed definitions."""

    def test_rules_cover_configured_facts(self):
        expected = set(settings.BOOK_TAXONOMY_LABEL_NUMBERS) | set(
            settings.BOOK_TAXONOMY_BOOLEAN_CHAPTERS
        )
        self.assertEqual(set(_RULES), expected)

    def test_configured_facts_are_classification_facts(self):
        for name in _RULES:
            with self.subTest(fact=name):
                self.assertTrue(name.startswith(settings.BOOK_CLASSIFICATION_FACT_PREFIXES))

    def test_rule_needs_exactly_one_kind(self):
        with self.assertRaises(ValidationError):
            BookTaxonomyRule(fact_name="x")
        with self.assertRaises(ValidationError):
            BookTaxonomyRule(fact_name="x", label_numbers={"a": "2"}, boolean_chapter="3")


if __name__ == "__main__":
    unittest.main()
