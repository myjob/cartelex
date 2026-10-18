"""Unit tests for the roster jurisdiction resolver.

Pure logic: the GLEIF subdivision reference rows are constructed in-process, so
no DB, no network and no OpenCorporates involvement.
"""

import os
import sys
import unittest

# Ensure src is on the path (src/pipelines/corp_index/tests -> src).
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
)

from pipelines.corp_index.jurisdiction_resolver import (  # noqa: E402
    GleifSubdivision,
    JurisdictionResolver,
    country_part,
    normalize_jurisdiction_code,
    to_opencorp_code,
)

# A slice of the real GLEIF subdivision list, including the awkward shapes: an
# accented / typographic-apostrophe label, a 3-letter code, a numeric code, and
# a multi-word label.
_SUBDIVISIONS = [
    GleifSubdivision(
        jurisdiction_code="AE-AJ",
        jurisdiction_name="‘Ajmān (United Arab Emirates)",
        iso_country_code="AE",
    ),
    GleifSubdivision(
        jurisdiction_code="AE-AZ",
        jurisdiction_name="Abu Dhabi (United Arab Emirates)",
        iso_country_code="AE",
    ),
    GleifSubdivision(
        jurisdiction_code="AE-DU",
        jurisdiction_name="Dubai (United Arab Emirates)",
        iso_country_code="AE",
    ),
    GleifSubdivision(
        jurisdiction_code="US-CA",
        jurisdiction_name="California (United States of America)",
        iso_country_code="US",
    ),
    GleifSubdivision(
        jurisdiction_code="US-MI",
        jurisdiction_name="Michigan (United States of America)",
        iso_country_code="US",
    ),
    GleifSubdivision(
        jurisdiction_code="US-MO",
        jurisdiction_name="Missouri (United States of America)",
        iso_country_code="US",
    ),
    GleifSubdivision(
        jurisdiction_code="US-NY",
        jurisdiction_name="New York (United States of America)",
        iso_country_code="US",
    ),
    GleifSubdivision(
        jurisdiction_code="GB-SCT",
        jurisdiction_name=(
            "Scotland (United Kingdom of Great Britain and Northern Ireland)"
        ),
        iso_country_code="GB",
    ),
    GleifSubdivision(
        jurisdiction_code="PT-20",
        jurisdiction_name="Região Autónoma dos Açores (Portugal)",
        iso_country_code="PT",
    ),
]


class TestNormalizeJurisdictionCode(unittest.TestCase):
    """Case- and separator-insensitive parsing into the canonical form."""

    def test_every_casing_and_separator_variant_normalizes_alike(self) -> None:
        """OpenCorporates, ISO and mixed spellings all yield AE-DU."""
        for raw in ("AE-DU", "ae_du", "AE_DU", "ae-du", "Ae_Du", "  ae_du  "):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_jurisdiction_code(raw), "AE-DU")

    def test_country_only_code_normalizes(self) -> None:
        """A bare country code is canonical on its own."""
        self.assertEqual(normalize_jurisdiction_code("gb"), "GB")

    def test_numeric_and_three_letter_subdivisions_are_accepted(self) -> None:
        """PT-20 and GB-SCT are both well-formed ISO 3166-2."""
        self.assertEqual(normalize_jurisdiction_code("pt_20"), "PT-20")
        self.assertEqual(normalize_jurisdiction_code("gb_sct"), "GB-SCT")

    def test_malformed_values_return_none(self) -> None:
        """Junk degrades to None rather than raising."""
        for raw in (None, "", "   ", "DUBAI", "A", "AE-", "AE-TOOLONG", "1E-DU"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_jurisdiction_code(raw))


class TestCodeConversion(unittest.TestCase):
    """Canonical form to OpenCorporates spelling, and country extraction."""

    def test_to_opencorp_code(self) -> None:
        """Canonical codes lowercase and switch to the underscore separator."""
        self.assertEqual(to_opencorp_code("AE-DU"), "ae_du")
        self.assertEqual(to_opencorp_code("GB"), "gb")
        self.assertEqual(to_opencorp_code("PT-20"), "pt_20")

    def test_country_part(self) -> None:
        """The parent country is the segment before the separator."""
        self.assertEqual(country_part("AE-DU"), "AE")
        self.assertEqual(country_part("GB"), "GB")


class TestJurisdictionResolver(unittest.TestCase):
    """Resolution precedence: address, then LEI, then country, then None."""

    def setUp(self) -> None:
        """Build a resolver over the sample subdivision reference rows."""
        self.resolver = JurisdictionResolver(_SUBDIVISIONS)

    def test_country_without_subdivisions_resolves_to_itself(self) -> None:
        """A single-jurisdiction country is its own jurisdiction."""
        self.assertEqual(self.resolver.resolve("DE", "Bonn, Germany"), "DE")

    def test_address_label_resolves_the_subdivision(self) -> None:
        """A subdivision name in the address identifies it (the Dubai case)."""
        self.assertEqual(
            self.resolver.resolve(
                "AE", "Al Garhoud Road, Deira, P.O. Box 55586, Dubai, UAE"
            ),
            "AE-DU",
        )

    def test_multi_word_address_label_resolves(self) -> None:
        """A multi-word label matches as a whole phrase."""
        self.assertEqual(
            self.resolver.resolve(
                "US", "1301 Avenue of the Americas, New York, NY 10019, USA"
            ),
            "US-NY",
        )

    def test_address_match_is_accent_and_case_insensitive(self) -> None:
        """Accented labels match their unaccented address spelling."""
        self.assertEqual(
            self.resolver.resolve("PT", "Ponta Delgada, regiao autonoma dos acores"),
            "PT-20",
        )
        self.assertEqual(
            self.resolver.resolve("AE", "Industrial Area 4, AJMAN, UAE"), "AE-AJ"
        )

    def test_postal_abbreviation_resolves_us_subdivision(self) -> None:
        """A US state code as a standalone token identifies the subdivision."""
        self.assertEqual(
            self.resolver.resolve("US", "One Busch Place, St Louis, MO 63188, USA"),
            "US-MO",
        )

    def test_label_wins_over_postal_abbreviation(self) -> None:
        """The full name is tried first, so a stray code cannot override it."""
        self.assertEqual(
            self.resolver.resolve("US", "Kalamazoo, Michigan, USA"), "US-MI"
        )

    def test_lowercase_two_letter_word_is_not_a_postal_abbreviation(self) -> None:
        """Only uppercase tokens count, so ordinary words are not state codes."""
        self.assertIsNone(self.resolver.resolve("US", "somewhere in mi, usa"))

    def test_ambiguous_address_labels_stay_unresolved(self) -> None:
        """Two subdivisions in one address is unresolved, not a coin flip."""
        self.assertIsNone(
            self.resolver.resolve("AE", "Office in Dubai, warehouse in Abu Dhabi")
        )

    def test_address_in_another_country_stays_unresolved(self) -> None:
        """An address contradicting the row's country resolves nothing."""
        self.assertIsNone(self.resolver.resolve("US", "Brussels, Belgium"))

    def test_missing_address_falls_through_to_none(self) -> None:
        """No address and no LEI leaves a subdivision country unresolved."""
        self.assertIsNone(self.resolver.resolve("AE", None))
        self.assertIsNone(self.resolver.resolve("AE", "   "))

    def test_lei_jurisdiction_used_when_country_agrees(self) -> None:
        """A same-country LEI subdivision resolves a row with no address."""
        self.assertEqual(
            self.resolver.resolve("US", None, lei_jurisdiction="US-CA"), "US-CA"
        )

    def test_lei_jurisdiction_rejected_on_country_mismatch(self) -> None:
        """The Emirates case: an AE row whose LEI record is Luxembourg."""
        self.assertIsNone(self.resolver.resolve("AE", None, lei_jurisdiction="LU"))
        self.assertIsNone(self.resolver.resolve("AE", None, lei_jurisdiction="LU-LU"))

    def test_country_level_lei_jurisdiction_adds_nothing(self) -> None:
        """An LEI carrying only a country cannot identify a subdivision."""
        self.assertIsNone(self.resolver.resolve("US", None, lei_jurisdiction="US"))

    def test_address_takes_precedence_over_lei(self) -> None:
        """The document's own address outranks the fuzzily matched LEI."""
        self.assertEqual(
            self.resolver.resolve(
                "US", "Kalamazoo, Michigan, USA", lei_jurisdiction="US-CA"
            ),
            "US-MI",
        )

    def test_lei_is_accepted_in_any_casing(self) -> None:
        """A register value spelled us_ca still resolves."""
        self.assertEqual(
            self.resolver.resolve("US", None, lei_jurisdiction="us_ca"), "US-CA"
        )

    def test_non_iso_country_value_resolves_to_none(self) -> None:
        """The roster holds some country names; those cannot resolve."""
        for country in ("Turkey", "unknown", "European Union", None, ""):
            with self.subTest(country=country):
                self.assertIsNone(self.resolver.resolve(country, "some address"))


if __name__ == "__main__":
    unittest.main()
