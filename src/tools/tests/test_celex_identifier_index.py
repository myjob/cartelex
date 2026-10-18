"""Unit tests for the registry-backed case-identifier gate.

The index replaces the former ``CELEX_CANDIDATE_RE``: detection, resolution and
near-miss suggestions all derive from ``celex_facts.celex_case_id`` rows instead
of a hand-maintained pattern list.  These tests pin

  * which prompt tokens count as case references (and, just as importantly,
    which ones do not),
  * that a registry row is reachable under every spelling users type,
  * that the first retrieval is cached for the process lifetime, and
  * that an index miss still falls back to a single-row query.

Only the repository boundary is mocked; the index logic itself runs for real.
"""

import unittest
from unittest.mock import MagicMock

from tools.celex_checker import (
    CelexCheckerToolkit,
    CelexIdentifierIndex,
    load_identifier_index,
    reset_identifier_index_cache,
    shape_signature,
)
from tools.tests.celex_index_fixture import REGISTRY_SAMPLE, build_index


class ShapeSignatureTests(unittest.TestCase):
    """Digit/letter runs collapse so id families survive length differences."""

    def test_bare_and_truncated_celex_share_a_family(self) -> None:
        """The reported bug: 02D0180 must look like the id it was cut from."""
        self.assertEqual(
            shape_signature("02D0180"), shape_signature("32002D0180")
        )

    def test_prefixed_celex_is_its_own_family(self) -> None:
        self.assertEqual(shape_signature("CELEX-32002D0180"), "A-9A9")

    def test_repeated_digit_groups_collapse(self) -> None:
        """AT_ ids differ only in how many groups they carry."""
        self.assertEqual(
            shape_signature("AT_40588_6339"),
            shape_signature("AT_40760_9770404_1218_4"),
        )

    def test_plain_year_has_no_structure(self) -> None:
        self.assertEqual(shape_signature("1994"), "9")


class IdentifierDetectionTests(unittest.TestCase):
    """What the gate treats as a case reference versus ordinary prose."""

    def setUp(self) -> None:
        self.index = build_index()

    def test_truncated_celex_number_is_an_attempt(self) -> None:
        """The 'set 02D0180' defect: detected despite resolving to nothing."""
        self.assertTrue(self.index.is_identifier_attempt("02D0180"))
        self.assertIsNone(self.index.resolve("02D0180"))

    def test_registry_spellings_are_attempts(self) -> None:
        for token in (
            "CELEX-32002D0180",
            "32002D0180",
            "IV/37859",
            "37766_610_5",
            "AT_40588_6339",
        ):
            with self.subTest(token=token):
                self.assertTrue(self.index.is_identifier_attempt(token))

    def test_malformed_token_under_a_registry_prefix_is_an_attempt(self) -> None:
        """CELEX-37766_610_5 is a wrong spelling, not prose."""
        self.assertTrue(self.index.is_identifier_attempt("CELEX-37766_610_5"))

    def test_unseen_group_count_still_matches_its_family(self) -> None:
        """An AT_ id with a group count absent from the sample is recognised."""
        self.assertTrue(self.index.is_identifier_attempt("AT_11111_2222_3333"))

    def test_prose_numbers_are_not_attempts(self) -> None:
        """Years, money, article and section numbers must reach the planner."""
        for token in ("1994", "2005", "81", "2.5", "9.1.5", "17/62", "20260825"):
            with self.subTest(token=token):
                self.assertFalse(self.index.is_identifier_attempt(token))

    def test_foreign_prefix_is_not_an_attempt(self) -> None:
        """'No.17' shares a shape with AT.40588 but not its prefix."""
        self.assertFalse(self.index.is_identifier_attempt("No.17"))

    def test_alphabetic_token_is_not_an_attempt(self) -> None:
        self.assertFalse(self.index.is_identifier_attempt("CELEX"))


class CandidateExtractionTests(unittest.TestCase):
    """Ordering, deduplication and punctuation handling over a whole prompt."""

    def setUp(self) -> None:
        self.index = build_index()

    def test_set_command_yields_the_mistyped_id(self) -> None:
        self.assertEqual(self.index.find_candidates("set 02D0180"), ["02D0180"])

    def test_trailing_punctuation_is_stripped(self) -> None:
        self.assertEqual(
            self.index.find_candidates("summary of 37766_610_5?"), ["37766_610_5"]
        )

    def test_inline_context_tag_does_not_shadow_the_typed_id(self) -> None:
        """The typed id must come first so it wins the resolution loop."""
        candidates = self.index.find_candidates(
            "list courts of AT_40588_6339  [context: celex_id=39226_8310_11]"
        )
        self.assertEqual(candidates, ["AT_40588_6339", "39226_8310_11"])

    def test_repeated_id_appears_once(self) -> None:
        self.assertEqual(
            self.index.find_candidates("39226_8310_11 vs 39226_8310_11"),
            ["39226_8310_11"],
        )

    def test_question_without_a_case_yields_nothing(self) -> None:
        self.assertEqual(
            self.index.find_candidates(
                "what was the fine of 2.5 million in 1994, see section 9.1.5?"
            ),
            [],
        )


class AliasResolutionTests(unittest.TestCase):
    """Every spelling of a registry row resolves to the stored celex_id."""

    def setUp(self) -> None:
        self.index = build_index()

    def test_stored_form_resolves(self) -> None:
        self.assertEqual(
            self.index.resolve("CELEX-32002D0180"), "CELEX-32002D0180"
        )

    def test_bare_number_resolves_to_the_prefixed_form(self) -> None:
        """Storage keeps the CELEX- prefix; users routinely omit it."""
        self.assertEqual(self.index.resolve("32002D0180"), "CELEX-32002D0180")

    def test_case_number_resolves_to_its_celex_id(self) -> None:
        self.assertEqual(self.index.resolve("IV/37859"), "CELEX-32002D0180")

    def test_lookup_is_case_insensitive(self) -> None:
        self.assertEqual(
            self.index.resolve("celex-32002d0180"), "CELEX-32002D0180"
        )

    def test_unknown_identifier_resolves_to_none(self) -> None:
        self.assertIsNone(self.index.resolve("CELEX-99999X9999"))

    def test_first_row_wins_on_a_conflicting_alias(self) -> None:
        """Authoritative rows are supplied first and must not be overwritten."""
        index = CelexIdentifierIndex.from_pairs(
            [("CELEX-31991D0562", "IV/999"), ("38344_5856_3", "IV/999")]
        )
        self.assertEqual(index.resolve("IV/999"), "CELEX-31991D0562")


class IndexCacheTests(unittest.TestCase):
    """The first retrieval is kept until the process (container) restarts."""

    def setUp(self) -> None:
        reset_identifier_index_cache()
        self.addCleanup(reset_identifier_index_cache)
        self.repo = MagicMock()
        self.repo.select_identifier_index.return_value = list(REGISTRY_SAMPLE)
        self.repo.select_scanned_identifier_index.return_value = []

    def test_registry_is_read_once_across_calls(self) -> None:
        load_identifier_index(self.repo)
        load_identifier_index(self.repo)
        load_identifier_index(self.repo)
        self.repo.select_identifier_index.assert_called_once()

    def test_second_call_returns_the_same_instance(self) -> None:
        self.assertIs(
            load_identifier_index(self.repo), load_identifier_index(self.repo)
        )

    def test_reset_forces_a_reload(self) -> None:
        load_identifier_index(self.repo)
        reset_identifier_index_cache()
        load_identifier_index(self.repo)
        self.assertEqual(self.repo.select_identifier_index.call_count, 2)

    def test_unreadable_supplement_does_not_break_the_index(self) -> None:
        """A failing supplementary read degrades recall, not the whole gate."""
        self.repo.select_scanned_identifier_index.side_effect = RuntimeError("down")
        index = load_identifier_index(self.repo)
        self.assertEqual(index.resolve("IV/37859"), "CELEX-32002D0180")


class CheckerFallbackTests(unittest.TestCase):
    """Resolution falls back to a row query when the cached index misses."""

    def setUp(self) -> None:
        reset_identifier_index_cache()
        self.addCleanup(reset_identifier_index_cache)
        self.toolkit = CelexCheckerToolkit.__new__(CelexCheckerToolkit)
        self.repo = MagicMock()
        self.repo.select_identifier_index.return_value = list(REGISTRY_SAMPLE)
        self.repo.select_scanned_identifier_index.return_value = []
        self.toolkit._repo = self.repo

    def test_index_hit_skips_the_database(self) -> None:
        self.assertEqual(
            self.toolkit.check_celex_or_case_id("32002D0180"), "CELEX-32002D0180"
        )
        self.repo.check_celex_or_case_id.assert_not_called()

    def test_index_miss_queries_the_database(self) -> None:
        """An id ingested after start-up still resolves via the row query."""
        self.repo.check_celex_or_case_id.return_value = "CELEX-31975D0358"
        self.assertEqual(
            self.toolkit.check_celex_or_case_id("CELEX-31975D0358"),
            "CELEX-31975D0358",
        )
        self.repo.check_celex_or_case_id.assert_called_once_with("CELEX-31975D0358")

    def test_unknown_identifier_returns_na(self) -> None:
        self.repo.check_celex_or_case_id.return_value = None
        self.assertEqual(self.toolkit.check_celex_or_case_id("02D0180"), "NA")

    def test_suggestion_names_the_truncated_id(self) -> None:
        """'02D0180' is a tail of CELEX-32002D0180 — offer it back."""
        self.assertIn("CELEX-32002D0180", self.toolkit.suggest("02D0180"))

    def test_short_token_gets_no_suggestions(self) -> None:
        self.assertEqual(list(self.toolkit.suggest("180")), [])


if __name__ == "__main__":
    unittest.main()
