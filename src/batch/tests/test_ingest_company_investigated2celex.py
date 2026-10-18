import json
import os
import sys
import unittest
from unittest.mock import MagicMock

# Ensure src is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from batch.Main_ingest_company_investigated2celex import (  # noqa: E402
    ADDRESS_FACT_NAME,
    COUNTRY_FACT_NAME,
    FACT_NAME,
    CompanyInvestigatedIngestor,
    build_enrichment_index,
    derive_clean_company_name,
    extract_company_names,
    extract_enrichment_map,
    match_enrichment,
    strip_alias_suffix,
)
from pipelines.rag.pgvector.company_investigated_repository import (  # noqa: E402
    InvestigatedCompany,
)


class TestExtractCompanyNames(unittest.TestCase):
    """Boundary-free parsing of ``companies_investigated`` value_json."""

    def test_plural_companies_key(self):
        raw = json.dumps([{"companies": "Alpha"}, {"companies": "Beta"}])
        self.assertEqual(extract_company_names(raw), ["Alpha", "Beta"])

    def test_singular_company_key(self):
        raw = json.dumps([{"company": "Valeo S.A."}, {"company": "Hella KGaA"}])
        self.assertEqual(extract_company_names(raw), ["Valeo S.A.", "Hella KGaA"])

    def test_escaped_unicode_decoded_to_readable(self):
        # As stored in the DB: JSON \uXXXX escapes for accented characters.
        raw = '[{"companies": "Soci\\u00e9t\\u00e9 Grosfillex"}]'
        names = extract_company_names(raw)
        self.assertEqual(names, ["Société Grosfillex"])
        self.assertIn("é", names[0])

    def test_empty_array(self):
        self.assertEqual(extract_company_names("[]"), [])

    def test_none_and_blank(self):
        self.assertEqual(extract_company_names(None), [])
        self.assertEqual(extract_company_names("   "), [])

    def test_invalid_json(self):
        self.assertEqual(extract_company_names("{not json"), [])

    def test_multi_key_row_uses_first_key_only(self):
        # Secondary keys (location/reference) must be ignored.
        raw = json.dumps(
            [{"company": "Acme", "location": "Paris", "reference": "42"}]
        )
        self.assertEqual(extract_company_names(raw), ["Acme"])

    def test_within_celex_duplicates_collapsed(self):
        raw = json.dumps([{"companies": "Interbrew"}, {"companies": "Interbrew"}])
        self.assertEqual(extract_company_names(raw), ["Interbrew"])

    def test_blank_names_dropped(self):
        raw = json.dumps([{"company": "  "}, {"company": "Real"}, {"company": ""}])
        self.assertEqual(extract_company_names(raw), ["Real"])


class TestExtractEnrichmentMap(unittest.TestCase):
    """Boundary-free parsing of enrichment (address/country) value_json."""

    def test_address_shape(self):
        raw = json.dumps(
            [
                {"company": "Acme", "address": "Paris"},
                {"company": "Beta", "address": "Berlin"},
            ]
        )
        self.assertEqual(
            extract_enrichment_map(raw), {"Acme": "Paris", "Beta": "Berlin"}
        )

    def test_country_shape_positional_keys(self):
        # Country facts use a different first-key name; parsing is positional.
        raw = json.dumps(
            [{"companies_investigated": "Acme", "country": "FR"}]
        )
        self.assertEqual(extract_enrichment_map(raw), {"Acme": "FR"})

    def test_na_value_dropped(self):
        raw = json.dumps([{"company": "Acme", "address": "NA"}])
        self.assertEqual(extract_enrichment_map(raw), {})

    def test_blank_value_dropped(self):
        raw = json.dumps([{"company": "Acme", "address": "  "}])
        self.assertEqual(extract_enrichment_map(raw), {})

    def test_escaped_unicode_key_normalized(self):
        raw = '[{"company": "Soci\\u00e9t\\u00e9", "address": "Lyon"}]'
        self.assertEqual(extract_enrichment_map(raw), {"Société": "Lyon"})

    def test_single_key_row_skipped(self):
        # A row without an enrichment value contributes nothing.
        raw = json.dumps([{"company": "Acme"}])
        self.assertEqual(extract_enrichment_map(raw), {})

    def test_first_value_wins_on_duplicate(self):
        raw = json.dumps(
            [
                {"company": "Acme", "address": "Paris"},
                {"company": "Acme", "address": "Lyon"},
            ]
        )
        self.assertEqual(extract_enrichment_map(raw), {"Acme": "Paris"})

    def test_none_blank_and_invalid(self):
        self.assertEqual(extract_enrichment_map(None), {})
        self.assertEqual(extract_enrichment_map("   "), {})
        self.assertEqual(extract_enrichment_map("{not json"), {})
        self.assertEqual(extract_enrichment_map("[]"), {})


class TestDeriveCleanCompanyName(unittest.TestCase):
    """Address-suffix stripping; no DB or LLM involved."""

    def test_address_suffix_stripped_with_separator(self):
        self.assertEqual(
            derive_clean_company_name(
                "Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy",
                "Strada 2, Pal. F7, 20090 Assago, Milan, Italy",
            ),
            "Praoil",
        )

    def test_semicolon_separator_stripped(self):
        self.assertEqual(
            derive_clean_company_name("Acme GmbH; Berlin, Germany", "Berlin, Germany"),
            "Acme GmbH",
        )

    def test_name_without_the_address_is_untouched(self):
        # The company is based in Paris but the name does not carry the address.
        self.assertEqual(derive_clean_company_name("Acme SA", "Paris"), "Acme SA")

    def test_address_in_the_middle_is_not_stripped(self):
        # Only a genuine suffix is removed -- never a substring elsewhere.
        self.assertEqual(
            derive_clean_company_name("Paris Bank SA", "Paris"), "Paris Bank SA"
        )

    def test_unknown_address_is_untouched(self):
        self.assertEqual(derive_clean_company_name("Acme SA", None), "Acme SA")
        self.assertEqual(derive_clean_company_name("Acme SA", ""), "Acme SA")

    def test_name_identical_to_address_is_kept(self):
        # Stripping would leave nothing; the original is less wrong than "".
        self.assertEqual(derive_clean_company_name("Paris", "Paris"), "Paris")

    def test_name_that_would_strip_to_empty_is_kept(self):
        self.assertEqual(derive_clean_company_name(", Paris", "Paris"), ", Paris")


class TestStripAliasSuffix(unittest.TestCase):
    """Removal of the trailing alias fact_context appends to a company name."""

    def test_trailing_alias_removed(self):
        self.assertEqual(
            strip_alias_suffix("Toyoda Gosei Co., Ltd. (TOYODA GOSEI)"),
            "Toyoda Gosei Co., Ltd.",
        )

    def test_name_without_a_parenthetical_is_untouched(self):
        self.assertEqual(strip_alias_suffix("Acme SA"), "Acme SA")

    def test_interior_parenthetical_is_kept(self):
        # Only a trailing parenthetical is an alias; one mid-name is part of it.
        self.assertEqual(
            strip_alias_suffix("Air America (Air Specialities) Corp."),
            "Air America (Air Specialities) Corp.",
        )

    def test_only_the_last_parenthetical_is_removed(self):
        self.assertEqual(
            strip_alias_suffix("Acme (Europe) Ltd (ACME)"), "Acme (Europe) Ltd"
        )

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(strip_alias_suffix("  Acme SA  (ACME)  "), "Acme SA")

    def test_unclosed_parenthesis_is_untouched(self):
        self.assertEqual(strip_alias_suffix("Acme SA (ACME"), "Acme SA (ACME")

    def test_name_that_is_only_an_alias_strips_to_empty(self):
        self.assertEqual(strip_alias_suffix("(ACME)"), "")


class TestMatchEnrichment(unittest.TestCase):
    """The normalizing lookup cascade: verbatim, folded (alias-stripped
    included), unique folded prefix, unique folded reverse prefix."""

    def _match(self, enrichment, *candidates):
        return match_enrichment(
            enrichment, build_enrichment_index(enrichment), *candidates
        )

    def test_verbatim_key_wins(self):
        self.assertEqual(self._match({"Acme SA": "FR"}, "Acme SA"), "FR")

    def test_folded_match_on_case_and_accent(self):
        self.assertEqual(
            self._match({"Société Générale": "FR"}, "societe generale"), "FR"
        )

    def test_folded_match_on_punctuation(self):
        # fold_match_key collapses a punctuation run to a single space, so
        # "Acme SA." and "Acme, SA" agree but "S.A." and "SA" do not.
        self.assertEqual(self._match({"Acme SA.": "ES"}, "Acme, SA"), "ES")

    def test_folded_prefix_recovers_an_address_bearing_name(self):
        # The reported failure: companies_country names the company bare while
        # the roster name carries the address.
        enrichment = {"Praoil": "IT"}
        self.assertEqual(
            self._match(
                enrichment, "Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy"
            ),
            "IT",
        )

    def test_prefix_must_end_on_a_word_boundary(self):
        # "Praoil" must not match "Praoiland Ltd, ...".
        self.assertIsNone(self._match({"Praoil": "IT"}, "Praoiland Ltd, Rome, Italy"))

    def test_ambiguous_prefix_returns_none(self):
        # Two enrichment keys prefix-match with different answers -> no guess.
        enrichment = {"Acme": "FR", "Acme Holding": "DE"}
        self.assertIsNone(self._match(enrichment, "Acme Holding SA, Berlin"))

    def test_ambiguous_prefix_agreeing_on_one_value_resolves(self):
        # Several keys match but all say the same thing; that is not ambiguity.
        enrichment = {"Acme": "FR", "Acme Holding": "FR"}
        self.assertEqual(self._match(enrichment, "Acme Holding SA, Paris"), "FR")

    def test_candidates_are_tried_in_order(self):
        enrichment = {"Clean Name": "FR", "Polluted Name, Paris": "DE"}
        self.assertEqual(
            self._match(enrichment, "Clean Name", "Polluted Name, Paris"), "FR"
        )

    def test_falls_back_to_the_later_candidate(self):
        enrichment = {"Polluted Name, Paris": "DE"}
        self.assertEqual(
            self._match(enrichment, "Clean Name", "Polluted Name, Paris"), "DE"
        )

    def test_alias_bearing_key_recovers_the_bare_roster_name(self):
        # The reported failure: companies_country appends the decision's short
        # form to the name, so the key is longer than the roster name.
        enrichment = {"Toyoda Gosei Co., Ltd. (TOYODA GOSEI)": "JP"}
        self.assertEqual(self._match(enrichment, "Toyoda Gosei Co., Ltd."), "JP")

    def test_reverse_prefix_recovers_a_compound_key(self):
        # One country entry covering two companies; stripping the alias still
        # leaves a key longer than the roster name, so the reverse prefix rung
        # is what resolves it.
        enrichment = {"Autoliv, Inc. and Autoliv Japan Ltd. (AUTOLIV)": "JP"}
        self.assertEqual(self._match(enrichment, "Autoliv, Inc."), "JP")

    def test_reverse_prefix_does_not_match_a_trailing_member(self):
        # "Autoliv Japan Ltd." is not a LEADING run of the compound key, so it
        # stays unmatched rather than inheriting the group's value.
        enrichment = {"Autoliv, Inc. and Autoliv Japan Ltd. (AUTOLIV)": "JP"}
        self.assertIsNone(self._match(enrichment, "Autoliv Japan Ltd."))

    def test_reverse_prefix_must_end_on_a_word_boundary(self):
        # "Air Int" must not match "Air Inter Europe".
        self.assertIsNone(self._match({"Air Inter Europe": "FR"}, "Air Int"))

    def test_ambiguous_reverse_prefix_returns_none(self):
        # Two keys extend the candidate with different answers -> no guess.
        enrichment = {"Acme Holding SA": "DE", "Acme Holding Ltd": "GB"}
        self.assertIsNone(self._match(enrichment, "Acme Holding"))

    def test_ambiguous_reverse_prefix_agreeing_on_one_value_resolves(self):
        enrichment = {"Acme Holding SA": "DE", "Acme Holding GmbH": "DE"}
        self.assertEqual(self._match(enrichment, "Acme Holding"), "DE")

    def test_forward_prefix_wins_over_reverse_prefix(self):
        # Both rungs could fire with different answers; the tighter rule runs
        # first, so the shorter key that the candidate extends wins.
        enrichment = {"Acme": "FR", "Acme Holding SA Berlin": "DE"}
        self.assertEqual(self._match(enrichment, "Acme Holding"), "FR")

    def test_no_match_returns_none(self):
        self.assertIsNone(self._match({"Acme SA": "FR"}, "Totally Different Ltd"))

    def test_empty_enrichment_returns_none(self):
        self.assertIsNone(self._match({}, "Acme SA"))


class TestBuildEnrichmentIndex(unittest.TestCase):
    """Per-CELEX folded index of an enrichment map."""

    def test_folds_keys(self):
        self.assertEqual(
            build_enrichment_index({"Acme, S.A.": "ES"}), {"acme s a": "ES"}
        )

    def test_first_value_wins_on_folding_collision(self):
        index = build_enrichment_index({"Acme SA": "FR", "ACME,  SA.": "DE"})
        self.assertEqual(index, {"acme sa": "FR"})

    def test_unfoldable_key_dropped(self):
        self.assertEqual(build_enrichment_index({"...": "FR"}), {})

    def test_alias_stripped_form_indexed_alongside_the_full_key(self):
        index = build_enrichment_index({"Toyoda Gosei Co., Ltd. (TOYODA GOSEI)": "JP"})
        self.assertEqual(
            index,
            {
                "toyoda gosei co ltd toyoda gosei": "JP",
                "toyoda gosei co ltd": "JP",
            },
        )

    def test_first_value_wins_when_two_keys_strip_to_the_same_name(self):
        index = build_enrichment_index({"Acme (A)": "FR", "Acme (B)": "DE"})
        self.assertEqual(index, {"acme a": "FR", "acme": "FR", "acme b": "DE"})

    def test_key_that_is_only_an_alias_adds_no_empty_key(self):
        self.assertEqual(build_enrichment_index({"(ACME)": "FR"}), {"acme": "FR"})


class TestBuildRosterNameCleaning(unittest.TestCase):
    """_build_roster: name repair, enrichment matching and the collision guard."""

    def setUp(self):
        self.fact_repo = MagicMock()
        self.ingestor = CompanyInvestigatedIngestor(
            fact_repo=self.fact_repo, company_repo=MagicMock()
        )

    def _configure_facts(self, by_name):
        def _side_effect(celex_id, name):
            facts = []
            for value in by_name.get(name, []):
                fact = MagicMock()
                fact.value_json = value
                facts.append(fact)
            return facts

        self.fact_repo.select_fact4celex.side_effect = _side_effect

    def test_address_stripped_and_country_recovered(self):
        """The reported failure, end to end through _build_roster."""
        polluted = "Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy"
        address = "Strada 2, Pal. F7, 20090 Assago, Milan, Italy"
        self._configure_facts(
            {
                ADDRESS_FACT_NAME: [
                    json.dumps([{"company": polluted, "address": address}])
                ],
                COUNTRY_FACT_NAME: [
                    json.dumps([{"companies_investigated": "Praoil", "country": "IT"}])
                ],
            }
        )
        roster = self.ingestor._build_roster("CELEX-1", [polluted])
        self.assertEqual(
            roster,
            [
                InvestigatedCompany(
                    company_name="Praoil", address=address, iso_country_code="IT"
                )
            ],
        )

    def test_colliding_names_keep_their_addresses_so_both_rows_survive(self):
        """UNIQUE (company_name, celex_id) must not be violated by cleaning.

        One company listed once per plant: cleaning both names would collapse
        them onto the same key and lose a site.
        """
        first = "Klöckner-Werke AG, Osnabrück, Germany"
        second = "Klöckner-Werke AG, Bremen, Germany"
        self._configure_facts(
            {
                ADDRESS_FACT_NAME: [
                    json.dumps(
                        [
                            {"company": first, "address": "Osnabrück, Germany"},
                            {"company": second, "address": "Bremen, Germany"},
                        ]
                    )
                ],
            }
        )
        roster = self.ingestor._build_roster("CELEX-1", [first, second])
        names = [record.company_name for record in roster]
        self.assertEqual(names, [first, second])
        self.assertEqual(len(set(names)), 2)

    def test_collision_with_an_already_clean_row_keeps_both(self):
        """A polluted duplicate of an already-clean row stays polluted."""
        clean = "Faber BV"
        polluted = "Faber BV, Amsterdam"
        self._configure_facts(
            {
                ADDRESS_FACT_NAME: [
                    json.dumps(
                        [
                            {"company": polluted, "address": "Amsterdam"},
                            {"company": clean, "address": "Amsterdam"},
                        ]
                    )
                ],
            }
        )
        roster = self.ingestor._build_roster("CELEX-1", [polluted, clean])
        self.assertEqual(
            [record.company_name for record in roster], [polluted, clean]
        )

    def test_non_colliding_names_are_still_cleaned(self):
        """A collision elsewhere must not stop unrelated names being cleaned."""
        self._configure_facts(
            {
                ADDRESS_FACT_NAME: [
                    json.dumps(
                        [
                            {"company": "Acme SA, Paris", "address": "Paris"},
                            {"company": "Beta AG, Berlin", "address": "Berlin"},
                            {"company": "Beta AG, Munich", "address": "Munich"},
                        ]
                    )
                ],
            }
        )
        roster = self.ingestor._build_roster(
            "CELEX-1", ["Acme SA, Paris", "Beta AG, Berlin", "Beta AG, Munich"]
        )
        self.assertEqual(
            [record.company_name for record in roster],
            ["Acme SA", "Beta AG, Berlin", "Beta AG, Munich"],
        )


class TestProcessCelex(unittest.TestCase):
    """Dispatch logic with both repositories mocked at the boundary."""

    def setUp(self):
        self.fact_repo = MagicMock()
        self.company_repo = MagicMock()
        self.ingestor = CompanyInvestigatedIngestor(
            fact_repo=self.fact_repo, company_repo=self.company_repo
        )

    def _fact(self, value_json):
        fact = MagicMock()
        fact.value_json = value_json
        return fact

    def _configure_facts(self, by_name):
        """Route ``select_fact4celex`` by fact name.

        Args:
            by_name: Map of fact name to a list of ``value_json`` strings (one
                per stored fact row); an absent name yields no facts.
        """

        def _side_effect(celex_id, name):
            return [self._fact(value) for value in by_name.get(name, [])]

        self.fact_repo.select_fact4celex.side_effect = _side_effect

    def test_no_fact(self):
        self._configure_facts({})
        res = self.ingestor.process_celex("CELEX-1")
        self.assertEqual(res["status"], "no_fact")
        self.company_repo.insert_companies.assert_not_called()

    def test_empty_fact(self):
        self._configure_facts({FACT_NAME: ["[]"]})
        res = self.ingestor.process_celex("CELEX-1")
        self.assertEqual(res["status"], "empty")
        self.company_repo.insert_companies.assert_not_called()

    def _configure_roster(self, stored_names, links=0):
        """Set the roster rows the repository reports as already stored."""
        self.company_repo.select_company_names.return_value = set(stored_names)
        self.company_repo.count_opencorp_links.return_value = links
        self.company_repo.delete_companies.side_effect = (
            lambda celex_id, names: len(names)
        )
        self.company_repo.insert_companies.side_effect = (
            lambda celex_id, companies: len(companies)
        )
        self.company_repo.update_missing_enrichment.side_effect = (
            lambda celex_id, companies: len(companies)
        )

    def test_existing_rows_backfill_enrichment(self):
        # Name unchanged: keep the row (and its links) and fill NULL enrichment.
        self._configure_facts(
            {
                FACT_NAME: [json.dumps([{"company": "Acme"}])],
                ADDRESS_FACT_NAME: [
                    json.dumps([{"company": "Acme", "address": "Paris"}])
                ],
                COUNTRY_FACT_NAME: [
                    json.dumps(
                        [{"companies_investigated": "Acme", "country": "FR"}]
                    )
                ],
            }
        )
        self._configure_roster({"Acme"})
        res = self.ingestor.process_celex("CELEX-1")
        self.assertEqual(res["status"], "reconciled")
        self.assertEqual(res["kept"], 1)
        self.assertEqual(res["inserted"], 0)
        self.assertEqual(res["deleted"], 0)
        self.company_repo.insert_companies.assert_called_once_with("CELEX-1", [])
        self.company_repo.delete_companies.assert_called_once_with("CELEX-1", [])
        self.company_repo.update_missing_enrichment.assert_called_once_with(
            "CELEX-1",
            [
                InvestigatedCompany(
                    company_name="Acme", address="Paris", iso_country_code="FR"
                )
            ],
        )

    def test_edited_fact_replaces_stale_rows(self):
        # The packed name was split into its members: the old row is retired
        # (losing its links) and the new members are inserted.
        self._configure_facts(
            {
                FACT_NAME: [
                    json.dumps([{"company": "Acme"}, {"company": "Acme Sub"}])
                ]
            }
        )
        self._configure_roster({"Acme and its subsidiaries: Acme Sub"}, links=3)
        res = self.ingestor.process_celex("CELEX-1")
        self.assertEqual(res["status"], "reconciled")
        self.assertEqual(res["inserted"], 2)
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(res["kept"], 0)
        self.assertEqual(res["links_discarded"], 3)
        self.company_repo.delete_companies.assert_called_once_with(
            "CELEX-1", ["Acme and its subsidiaries: Acme Sub"]
        )
        self.company_repo.insert_companies.assert_called_once_with(
            "CELEX-1",
            [
                InvestigatedCompany(company_name="Acme"),
                InvestigatedCompany(company_name="Acme Sub"),
            ],
        )

    def test_surviving_name_is_not_reinserted(self):
        # A partially edited fact keeps the row (and links) of the name it still
        # carries, deleting only the name it dropped.
        self._configure_facts(
            {FACT_NAME: [json.dumps([{"company": "Acme"}, {"company": "Gamma"}])]}
        )
        self._configure_roster({"Acme", "Beta"}, links=2)
        res = self.ingestor.process_celex("CELEX-1")
        self.assertEqual(res["inserted"], 1)
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(res["kept"], 1)
        self.company_repo.delete_companies.assert_called_once_with("CELEX-1", ["Beta"])
        self.company_repo.insert_companies.assert_called_once_with(
            "CELEX-1", [InvestigatedCompany(company_name="Gamma")]
        )
        self.company_repo.update_missing_enrichment.assert_called_once_with(
            "CELEX-1", [InvestigatedCompany(company_name="Acme")]
        )

    def test_unchanged_fact_is_a_no_op(self):
        # Re-runs stay idempotent: nothing to insert, nothing to delete.
        self._configure_facts(
            {FACT_NAME: [json.dumps([{"company": "Acme"}, {"company": "Beta"}])]}
        )
        self._configure_roster({"Acme", "Beta"})
        res = self.ingestor.process_celex("CELEX-1")
        self.assertEqual(res["inserted"], 0)
        self.assertEqual(res["deleted"], 0)
        self.assertEqual(res["kept"], 2)
        self.company_repo.insert_companies.assert_called_once_with("CELEX-1", [])
        self.company_repo.delete_companies.assert_called_once_with("CELEX-1", [])

    def test_empty_fact_does_not_delete_stored_rows(self):
        # A failed extraction must not be able to empty a loaded roster.
        self._configure_facts({FACT_NAME: ["[]"]})
        self._configure_roster({"Acme"})
        res = self.ingestor.process_celex("CELEX-1")
        self.assertEqual(res["status"], "empty")
        self.company_repo.delete_companies.assert_not_called()

    def test_dry_run_does_not_write(self):
        self._configure_facts({FACT_NAME: [json.dumps([{"company": "Acme"}])]})
        self._configure_roster({"Beta"}, links=1)
        res = self.ingestor.process_celex("CELEX-1", dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(res["inserted"], 1)
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(res["links_discarded"], 1)
        self.company_repo.insert_companies.assert_not_called()
        self.company_repo.delete_companies.assert_not_called()
        self.company_repo.update_missing_enrichment.assert_not_called()

    def test_inserted_with_enrichment(self):
        self._configure_facts(
            {
                FACT_NAME: [json.dumps([{"company": "Acme"}, {"company": "Beta"}])],
                ADDRESS_FACT_NAME: [
                    json.dumps(
                        [
                            {"company": "Acme", "address": "Paris"},
                            {"company": "Beta", "address": "NA"},
                        ]
                    )
                ],
                COUNTRY_FACT_NAME: [
                    json.dumps(
                        [{"companies_investigated": "Acme", "country": "FR"}]
                    )
                ],
            }
        )
        self._configure_roster(set())
        res = self.ingestor.process_celex("CELEX-1")
        self.assertEqual(res["status"], "reconciled")
        self.assertEqual(res["inserted"], 2)
        self.company_repo.insert_companies.assert_called_once_with(
            "CELEX-1",
            [
                InvestigatedCompany(
                    company_name="Acme", address="Paris", iso_country_code="FR"
                ),
                # Beta: address "NA" dropped, no country match -> both NULL.
                InvestigatedCompany(
                    company_name="Beta", address=None, iso_country_code=None
                ),
            ],
        )

    def test_inserted_without_enrichment_facts(self):
        # Missing enrichment facts still produce one record per company.
        self._configure_facts({FACT_NAME: [json.dumps([{"company": "Acme"}])]})
        self._configure_roster(set())
        self.ingestor.process_celex("CELEX-1")
        self.company_repo.insert_companies.assert_called_once_with(
            "CELEX-1",
            [InvestigatedCompany(company_name="Acme")],
        )

    def test_uses_last_fact_row(self):
        # Repo convention: last row wins when multiple facts exist.
        self._configure_facts(
            {
                FACT_NAME: [
                    json.dumps([{"company": "Stale"}]),
                    json.dumps([{"company": "Fresh"}]),
                ]
            }
        )
        self._configure_roster(set())
        self.ingestor.process_celex("CELEX-1")
        self.company_repo.insert_companies.assert_called_once_with(
            "CELEX-1",
            [InvestigatedCompany(company_name="Fresh")],
        )


class TestEnrichHandelsregister(unittest.TestCase):
    """Bulk fk_handelsregister enrichment with the repository mocked."""

    def setUp(self):
        self.fact_repo = MagicMock()
        self.company_repo = MagicMock()
        self.ingestor = CompanyInvestigatedIngestor(
            fact_repo=self.fact_repo, company_repo=self.company_repo
        )

    def test_no_rows(self):
        self.company_repo.select_roster_by_country.return_value = []
        res = self.ingestor.enrich_handelsregister("DE")
        self.assertEqual(res["status"], "no_rows")
        self.company_repo.set_handelsregister.assert_not_called()

    def test_matched_and_na_placeholder(self):
        self.company_repo.select_roster_by_country.return_value = [
            ("id1", "Automotive Lighting Reutlingen GmbH"),
            ("id2", "Unknown GmbH"),
        ]
        # Unmatched names are simply absent from the resolver's map.
        self.company_repo.resolve_handelsregister_numbers.return_value = {
            "Automotive Lighting Reutlingen GmbH": "de-oc-stuttgart-hrb-353544",
        }
        self.company_repo.set_handelsregister.return_value = 2
        res = self.ingestor.enrich_handelsregister("DE")
        self.assertEqual(res["status"], "enriched")
        self.assertEqual(res["matched"], 1)
        self.assertEqual(res["unmatched"], 1)
        self.company_repo.set_handelsregister.assert_called_once_with(
            [
                ("id1", "de-oc-stuttgart-hrb-353544"),
                ("id2", "NA"),
            ]
        )

    def test_resolves_distinct_names_once(self):
        # A name repeated across CELEX rows is resolved once (distinct names).
        self.company_repo.select_roster_by_country.return_value = [
            ("id1", "Acme GmbH"),
            ("id2", "Acme GmbH"),
        ]
        self.company_repo.resolve_handelsregister_numbers.return_value = {
            "Acme GmbH": "de-123"
        }
        self.company_repo.set_handelsregister.return_value = 2
        self.ingestor.enrich_handelsregister("DE")
        self.company_repo.resolve_handelsregister_numbers.assert_called_once_with(
            ["Acme GmbH"]
        )
        self.company_repo.set_handelsregister.assert_called_once_with(
            [("id1", "de-123"), ("id2", "de-123")]
        )

    def test_dry_run_does_not_write(self):
        self.company_repo.select_roster_by_country.return_value = [
            ("id1", "Acme GmbH")
        ]
        self.company_repo.resolve_handelsregister_numbers.return_value = {
            "Acme GmbH": "de-123"
        }
        res = self.ingestor.enrich_handelsregister("DE", dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(res["matched"], 1)
        self.company_repo.set_handelsregister.assert_not_called()


class TestEnrichLei(unittest.TestCase):
    """Bulk fk_lei enrichment with the repository mocked."""

    def setUp(self):
        self.fact_repo = MagicMock()
        self.company_repo = MagicMock()
        self.ingestor = CompanyInvestigatedIngestor(
            fact_repo=self.fact_repo, company_repo=self.company_repo
        )

    def test_no_rows(self):
        self.company_repo.select_all_roster.return_value = []
        res = self.ingestor.enrich_lei()
        self.assertEqual(res["status"], "no_rows")
        self.company_repo.set_lei.assert_not_called()

    def test_matched_and_na_placeholder(self):
        # Whole roster, any country (LEI is global).
        self.company_repo.select_all_roster.return_value = [
            ("id1", "Anheuser-Busch"),
            ("id2", "Unknown Ltd"),
        ]
        self.company_repo.resolve_lei_codes.return_value = {
            "Anheuser-Busch": "5493008H3828EMEXB082",
        }
        self.company_repo.set_lei.return_value = 2
        res = self.ingestor.enrich_lei()
        self.assertEqual(res["status"], "enriched")
        self.assertEqual(res["matched"], 1)
        self.assertEqual(res["unmatched"], 1)
        self.company_repo.set_lei.assert_called_once_with(
            [
                ("id1", "5493008H3828EMEXB082"),
                ("id2", "NA"),
            ]
        )

    def test_resolves_distinct_names_once(self):
        self.company_repo.select_all_roster.return_value = [
            ("id1", "Acme SA"),
            ("id2", "Acme SA"),
        ]
        self.company_repo.resolve_lei_codes.return_value = {"Acme SA": "lei-123"}
        self.company_repo.set_lei.return_value = 2
        self.ingestor.enrich_lei()
        self.company_repo.resolve_lei_codes.assert_called_once_with(["Acme SA"])
        self.company_repo.set_lei.assert_called_once_with(
            [("id1", "lei-123"), ("id2", "lei-123")]
        )

    def test_dry_run_does_not_write(self):
        self.company_repo.select_all_roster.return_value = [("id1", "Acme SA")]
        self.company_repo.resolve_lei_codes.return_value = {"Acme SA": "lei-123"}
        res = self.ingestor.enrich_lei(dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(res["matched"], 1)
        self.company_repo.set_lei.assert_not_called()


class TestEnrichJurisdiction(unittest.TestCase):
    """Bulk jurisdication_code resolution with the repository mocked."""

    def setUp(self):
        self.fact_repo = MagicMock()
        self.company_repo = MagicMock()
        self.ingestor = CompanyInvestigatedIngestor(
            fact_repo=self.fact_repo, company_repo=self.company_repo
        )
        self.company_repo.select_gleif_subdivisions.return_value = [
            ("AE-DU", "Dubai (United Arab Emirates)", "AE"),
            ("AE-AZ", "Abu Dhabi (United Arab Emirates)", "AE"),
            ("US-MI", "Michigan (United States of America)", "US"),
            ("GB-SCT", "Scotland (United Kingdom)", "GB"),
        ]

    def test_no_rows(self):
        self.company_repo.select_jurisdiction_inputs.return_value = []
        res = self.ingestor.enrich_jurisdiction()
        self.assertEqual(res["status"], "no_rows")
        self.company_repo.set_jurisdiction_code.assert_not_called()

    def test_resolves_from_address_lei_and_country(self):
        """Each precedence path writes the code it resolved."""
        self.company_repo.select_jurisdiction_inputs.return_value = [
            ("id-dubai", "AE", "P.O. Box 55586, Dubai, UAE", None),
            ("id-lei", "US", None, "US-MI"),
            ("id-country", "DE", "Bonn, Germany", None),
            ("id-unresolved", "AE", None, "LU"),
        ]
        self.company_repo.set_jurisdiction_code.return_value = 4

        res = self.ingestor.enrich_jurisdiction()

        self.company_repo.set_jurisdiction_code.assert_called_once_with(
            [
                ("id-dubai", "AE-DU"),
                ("id-lei", "US-MI"),
                ("id-country", "DE"),
                ("id-unresolved", None),
            ]
        )
        self.assertEqual(res["status"], "enriched")
        self.assertEqual(res["matched"], 3)
        self.assertEqual(res["unmatched"], 1)
        self.assertEqual(res["subdivisions"], 2)

    def test_unsupported_subdivision_is_still_stored(self):
        """The roster stores any ISO subdivision; degrading is the consumer's job."""
        self.company_repo.select_jurisdiction_inputs.return_value = [
            ("id1", "GB", "Edinburgh, Scotland", None),
        ]
        self.ingestor.enrich_jurisdiction()
        self.company_repo.set_jurisdiction_code.assert_called_once_with(
            [("id1", "GB-SCT")]
        )

    def test_dry_run_does_not_write(self):
        self.company_repo.select_jurisdiction_inputs.return_value = [
            ("id1", "AE", "Dubai, UAE", None),
        ]
        res = self.ingestor.enrich_jurisdiction(dry_run=True)
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(res["matched"], 1)
        self.company_repo.set_jurisdiction_code.assert_not_called()


if __name__ == "__main__":
    unittest.main()
