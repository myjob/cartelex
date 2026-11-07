"""Unit tests for :mod:`pipelines.legal.undertaking_count` (pure, no DB)."""

import json
import unittest

from pipelines.legal.undertaking_count import count_undertakings, parse_roster


class TestParseRoster(unittest.TestCase):
    """Roster payloads of every stored shape yield their entity names."""

    def test_current_companies_key_with_location(self):
        payload = json.dumps(
            [
                {"companies": "Peine-Salzgitter AG", "location": "NA"},
                {"companies": "Rolling-Steel Association", "location": "Brussels"},
            ]
        )
        self.assertEqual(
            parse_roster(payload), ["Peine-Salzgitter AG", "Rolling-Steel Association"]
        )

    def test_legacy_company_key(self):
        payload = json.dumps([{"company": "Installe"}, {"company": "SACA"}])
        self.assertEqual(parse_roster(payload), ["Installe", "SACA"])

    def test_unknown_name_key_falls_back_but_skips_location(self):
        payload = [{"location": "Paris", "party": "Astral"}]
        self.assertEqual(parse_roster(payload), ["Astral"])

    def test_placeholders_and_non_dict_rows_are_dropped(self):
        payload = json.dumps([{"companies": "NA"}, {"companies": "  "}, "junk", {"companies": "X SA"}])
        self.assertEqual(parse_roster(payload), ["X SA"])

    def test_invalid_or_missing_payload_is_empty(self):
        for payload in (None, "", "not json", "{}", json.dumps({"companies": "A"})):
            with self.subTest(payload=payload):
                self.assertEqual(parse_roster(payload), [])


class TestCountUndertakings(unittest.TestCase):
    """Distinct legal units are counted once, distinct names stay distinct."""

    def test_legal_form_variants_collapse(self):
        result = count_undertakings(["Akzo Chemicals BV", "Akzo Chemicals", "AKZO CHEMICALS B.V."])
        self.assertEqual(result.number_undertaking, 1)
        self.assertEqual(result.undertakings, ["Akzo Chemicals BV"])
        self.assertEqual(result.roster_rows, 3)

    def test_short_name_is_not_swallowed_by_longer_one(self):
        result = count_undertakings(["AZ", "AstraZeneca"])
        self.assertEqual(result.number_undertaking, 2)

    def test_parent_and_subsidiaries_count_individually(self):
        result = count_undertakings(["Hoechst AG", "Hoechst France SA", "Hoechst Iberica SA"])
        self.assertEqual(result.number_undertaking, 3)

    def test_associations_are_counted(self):
        result = count_undertakings(
            ["Stichting Certificatie Kraanverhuurbedrijf", "Federatie van Nederlandse Kraanverhuurbedrijven"]
        )
        self.assertEqual(result.number_undertaking, 2)

    def test_empty_roster_is_zero(self):
        result = count_undertakings([])
        self.assertEqual((result.number_undertaking, result.roster_rows), (0, 0))


if __name__ == "__main__":
    unittest.main()
