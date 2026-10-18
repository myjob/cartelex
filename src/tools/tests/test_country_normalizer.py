import unittest

from tools.country_normalizer import (
    ends_with_country,
    normalize_country_fields,
    to_country_name,
    to_iso_alpha2,
)


class TestToIsoAlpha2(unittest.TestCase):
    """Unit tests for the pure country -> ISO alpha-2 resolver."""

    def test_canonical_name(self):
        self.assertEqual(to_iso_alpha2("Germany"), "DE")

    def test_alpha2_passthrough_and_casing(self):
        self.assertEqual(to_iso_alpha2("DE"), "DE")
        self.assertEqual(to_iso_alpha2("de"), "DE")

    def test_alpha3(self):
        self.assertEqual(to_iso_alpha2("DEU"), "DE")

    def test_surrounding_whitespace(self):
        self.assertEqual(to_iso_alpha2("  Netherlands  "), "NL")

    def test_alias_uk_maps_to_gb(self):
        self.assertEqual(to_iso_alpha2("UK"), "GB")
        self.assertEqual(to_iso_alpha2("United Kingdom"), "GB")
        self.assertEqual(to_iso_alpha2("England"), "GB")

    def test_alias_native_name(self):
        self.assertEqual(to_iso_alpha2("Deutschland"), "DE")

    def test_unresolvable_returns_none(self):
        self.assertIsNone(to_iso_alpha2("European Union"))
        self.assertIsNone(to_iso_alpha2("xx"))

    def test_empty_and_non_string(self):
        self.assertIsNone(to_iso_alpha2(""))
        self.assertIsNone(to_iso_alpha2("   "))
        self.assertIsNone(to_iso_alpha2(None))  # type: ignore[arg-type]


class TestNormalizeCountryFields(unittest.TestCase):
    """Unit tests for row-level normalization of the country field."""

    def test_normalizes_full_name_to_iso(self):
        items = [{"companies_investigated": "Deutsche Post AG", "country": "Germany"}]
        result = normalize_country_fields(items)
        self.assertEqual(
            result,
            [{"companies_investigated": "Deutsche Post AG", "country": "DE"}],
        )

    def test_already_iso_is_unchanged_and_same_reference(self):
        row = {"companies_investigated": "Deutsche Post AG", "country": "DE"}
        items = [row]
        result = normalize_country_fields(items)
        self.assertEqual(result, items)
        # No value changed -> row returned by reference (no needless copy).
        self.assertIs(result[0], row)

    def test_unresolvable_country_is_preserved(self):
        items = [{"court": "General Court", "country": "European Union"}]
        result = normalize_country_fields(items)
        self.assertEqual(result[0]["country"], "European Union")

    def test_row_without_country_key_untouched(self):
        items = [{"companies_investigated": "ACME Ltd"}]
        result = normalize_country_fields(items)
        self.assertEqual(result, items)

    def test_mixed_rows(self):
        items = [
            {"companies_investigated": "Deutsche Post AG", "country": "de"},
            {"companies_investigated": "TPG Post", "country": "Netherlands"},
            {"companies_investigated": "Unknown Co", "country": "Atlantis"},
        ]
        result = normalize_country_fields(items)
        self.assertEqual([r["country"] for r in result], ["DE", "NL", "Atlantis"])

    def test_custom_key(self):
        items = [{"court": "High Court", "jurisdiction_country": "UK"}]
        result = normalize_country_fields(items, key="jurisdiction_country")
        self.assertEqual(result[0]["jurisdiction_country"], "GB")

    def test_non_dict_elements_tolerated(self):
        items = ["not a dict", {"country": "France"}]
        result = normalize_country_fields(items)
        self.assertEqual(result[0], "not a dict")
        self.assertEqual(result[1]["country"], "FR")


class TestToCountryName(unittest.TestCase):
    """The reverse direction, used to complete a postal address."""

    def test_alpha2_becomes_a_name(self):
        self.assertEqual(to_country_name("BE"), "Belgium")

    def test_casing_and_alpha3(self):
        self.assertEqual(to_country_name("de"), "Germany")
        self.assertEqual(to_country_name("DEU"), "Germany")

    def test_aliases_resolve_the_same_way_as_the_forward_direction(self):
        """Routed through to_iso_alpha2, so every alias it knows works here."""
        self.assertEqual(to_country_name("UK"), "United Kingdom")

    def test_a_name_is_returned_canonicalized(self):
        self.assertEqual(to_country_name("Deutschland"), "Germany")

    def test_common_name_is_preferred_over_the_official_one(self):
        """An address carries the everyday name, not the ISO long form."""
        self.assertEqual(to_country_name("TW"), "Taiwan")

    def test_unresolvable_and_empty_return_none(self):
        self.assertIsNone(to_country_name("Atlantis"))
        self.assertIsNone(to_country_name(""))


class TestEndsWithCountry(unittest.TestCase):
    """The guard that stops an address acquiring a second country."""

    def test_trailing_country_is_detected(self):
        self.assertTrue(ends_with_country("100, rue Chalant, 59790 Ronchin, FRANCE"))

    def test_address_without_a_country_is_not(self):
        self.assertFalse(ends_with_country("Av. Emile Digneffe 26, 4000 Liege"))

    def test_a_country_that_is_not_last_does_not_count(self):
        """Positional on purpose: only the tail is the address's country."""
        self.assertFalse(ends_with_country("France Telecom, 6 place d'Alleray, Paris"))

    def test_a_disagreeing_tail_country_still_counts(self):
        """The roster holds iso_country_code='DE' against a French address.

        The address's own country is the more specific claim, so this must
        report True and leave the address alone rather than appending Germany.
        """
        self.assertTrue(
            ends_with_country("111/113, Cours Berriat, F-38019 Grenoble, Cedex 1, France")
        )

    def test_empty_and_non_string_are_false(self):
        self.assertFalse(ends_with_country(""))
        self.assertFalse(ends_with_country("   ,  , "))
        self.assertFalse(ends_with_country(None))


if __name__ == "__main__":
    unittest.main()
