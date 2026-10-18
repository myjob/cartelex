"""Unit tests for the address-in-company-name detectors.

Both detectors are pure functions over a string, so these run in isolation with
no database or LLM. Examples are taken verbatim from the corpus.
"""

import unittest

from reporting.report_company_name_address_pollution import (
    has_address_suffix,
    looks_like_address_suffix,
)


class TestHasAddressSuffix(unittest.TestCase):
    """The corroborated detector: the address fact confirms the suffix."""

    def test_reported_praoil_row(self):
        self.assertTrue(
            has_address_suffix(
                "Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy",
                "Strada 2, Pal. F7, 20090 Assago, Milan, Italy",
            )
        )

    def test_clean_name_is_not_flagged(self):
        self.assertFalse(has_address_suffix("Praoil", "Strada 2, Milan, Italy"))

    def test_address_elsewhere_in_the_name_is_not_a_suffix(self):
        self.assertFalse(has_address_suffix("Paris Bank SA", "Paris"))

    def test_unknown_address_cannot_corroborate(self):
        # The whole point of the second, standalone detector.
        self.assertFalse(
            has_address_suffix("Praoil, Strada 2, 20090 Assago, Italy", None)
        )

    def test_name_identical_to_address_is_not_flagged(self):
        # Stripping would leave nothing, so there is nothing to repair.
        self.assertFalse(has_address_suffix("Paris", "Paris"))


class TestLooksLikeAddressSuffix(unittest.TestCase):
    """The standalone heuristic, for documents with no usable address fact."""

    def test_name_with_street_postcode_and_country(self):
        self.assertTrue(
            looks_like_address_suffix(
                "Amerada Hess Corporation, 1185, Avenue of the Americas, "
                "New York, NY-10036, USA"
            )
        )

    def test_name_with_city_and_country_only(self):
        # No postcode and no street number; the trailing country carries it.
        self.assertTrue(
            looks_like_address_suffix(
                "Royal Lacquer-Varnish and Paint Factory Molijn & Co. N.V., "
                "Rotterdam, Netherlands"
            )
        )

    def test_historical_country_name(self):
        self.assertTrue(
            looks_like_address_suffix("Robert Krups, Solingen, West Germany")
        )
        self.assertTrue(
            looks_like_address_suffix("Storry Smithson Group, Hull, Great Britain")
        )

    def test_uk_style_postcode(self):
        self.assertTrue(
            looks_like_address_suffix(
                "BP Oil International Limited, Britannic House, London, EC2M 7BA"
            )
        )

    def test_legal_form_alone_is_not_an_address(self):
        # Two segments and no address signal -- must not be flagged.
        self.assertFalse(looks_like_address_suffix("Ashland Oil, Inc."))
        self.assertFalse(looks_like_address_suffix("Idemitsu Kosan Co., Ltd"))

    def test_company_named_after_a_country_is_not_flagged(self):
        # No commas, so it never reaches the country check.
        self.assertFalse(looks_like_address_suffix("Bank of Ireland"))
        self.assertFalse(looks_like_address_suffix("Air France"))

    def test_country_must_be_the_final_segment(self):
        # 'Italy' appears mid-name, not as the address tail.
        self.assertFalse(
            looks_like_address_suffix("Banca d'Italia, Servizio Studi, Direzione")
        )

    def test_digits_in_the_company_name_itself_are_ignored(self):
        # The postcode scan skips the first segment, so a numeric brand name in
        # the company name does not flag it on its own.
        self.assertFalse(looks_like_address_suffix("Euro 6000 S.A."))

    def test_three_bare_names_are_not_flagged_by_country_rule(self):
        # Known corpus false positive shape: several companies in one row. It
        # trips the postcode rule via '6000', which is a separate extraction bug.
        self.assertFalse(
            looks_like_address_suffix("Servired S.C., Sistema 4B S.A., Acme S.A.")
        )

    def test_empty_and_single_segment(self):
        self.assertFalse(looks_like_address_suffix(""))
        self.assertFalse(looks_like_address_suffix("Praoil"))


if __name__ == "__main__":
    unittest.main()
