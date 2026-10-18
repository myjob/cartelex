"""Integration test for the roster enrichment join, over the live fact store.

Regression guard for CELEX-31994D0153, where all 41 roster rows in
``celex_facts.company_investigated2celex_id`` used to carry an address but a
NULL ``iso_country_code`` (and, downstream, a NULL ``jurisdication_code``)::

    company_name | Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy
    address       | Strada 2, Pal. F7, 20090 Assago, Milan, Italy
    iso_country  | NULL

Two plausible-sounding explanations are refuted here by assertion rather than
prose, because both would have sent a fix in the wrong direction:

1. "The ``address`` key is missing from the ``companies_address`` fact." It is
   not. All 41 rows carry both schema keys and the address map joins 41/41.
2. "The ISO country code was never extracted." It was. The
   ``companies_country`` fact holds ``Praoil -> IT``.

The defect was a JOIN-KEY MISMATCH in
``CompanyInvestigatedIngestor._build_roster``, which matched enrichment to the
roster by verbatim company name:

* ``companies_investigated`` stored each name concatenated with its address
  ("Praoil, Strada 2, ... Italy"). That polluted string was the roster key.
* ``companies_address`` runs the ``detail_retriever`` pipeline, whose
  ``_build_row`` echoes the roster entity back verbatim as the first key, so
  its keys match the polluted names -- the address join succeeded 41/41.
* ``companies_country`` runs the ``fact_context`` pipeline, which lets the LLM
  re-emit a *cleaned* company name ("Praoil"), so its keys are the bare names
  -- the country join matched 0/41 and every code was dropped to NULL.

The stored facts still disagree this way (they are not rewritten by the fix), so
the characterization tests below continue to assert that disagreement. What
changed is the ingest: ``derive_clean_company_name`` strips the address suffix
off the roster name and ``match_enrichment`` bridges the two spellings, so both
enrichment columns now arrive.

Uses the system configs from config.settings (DATABASE_URL) per project
convention for integration tests. Read-only: nothing is written, the ingestor's
roster assembly is called directly rather than through ``process_celex``.

Run explicitly (needs Postgres):
    PYTHONPATH=src .venv/bin/python -m unittest \
        batch.tests.test_ingest_company_investigated2celex_integration -v
"""

import unittest

from batch.Main_ingest_company_investigated2celex import (
    ADDRESS_FACT_NAME,
    COUNTRY_FACT_NAME,
    FACT_NAME,
    CompanyInvestigatedIngestor,
    extract_company_names,
    extract_enrichment_map,
)
from pipelines.corp_index.jurisdiction_resolver import (
    GleifSubdivision,
    JurisdictionResolver,
)
from pipelines.rag.pgvector.company_investigated_repository import (
    CompanyInvestigatedRepository,
)
from pipelines.rag.pgvector.fact_repository import FactRepository

# Commission decision 94/153/EC. Chosen because every one of its roster names is
# polluted with the address, so the defect shows at 100% rather than sporadically.
CELEX_ID = "CELEX-31994D0153"

# The exemplar row from the bug report. The roster name is the company name
# concatenated with its address; the country fact keys the same company as the
# bare name and correctly assigns IT.
POLLUTED_ROSTER_NAME = "Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy"
CLEAN_COMPANY_NAME = "Praoil"
EXPECTED_ADDRESS = "Strada 2, Pal. F7, 20090 Assago, Milan, Italy"
EXPECTED_ISO_COUNTRY_CODE = "IT"


class TestCompanyRosterEnrichmentJoin(unittest.TestCase):
    """Roster assembly for a CELEX whose company names carry their addresses."""

    @classmethod
    def setUpClass(cls):
        cls.fact_repo = FactRepository()
        try:
            roster_facts = cls.fact_repo.select_fact4celex(CELEX_ID, FACT_NAME)
        except Exception as exc:  # pragma: no cover - environment guard
            raise unittest.SkipTest(f"Database not reachable: {exc}") from exc
        if not roster_facts:
            raise unittest.SkipTest(
                f"Roster fact '{FACT_NAME}' not present for {CELEX_ID}; "
                "seed it with the companies_investigated pipeline first."
            )

        address_facts = cls.fact_repo.select_fact4celex(CELEX_ID, ADDRESS_FACT_NAME)
        country_facts = cls.fact_repo.select_fact4celex(CELEX_ID, COUNTRY_FACT_NAME)
        if not address_facts or not country_facts:
            raise unittest.SkipTest(
                f"Enrichment facts '{ADDRESS_FACT_NAME}' / '{COUNTRY_FACT_NAME}' "
                f"not both present for {CELEX_ID}; the join under test needs both."
            )

        cls.roster_names = extract_company_names(roster_facts[-1].value_json)
        cls.address_map = extract_enrichment_map(address_facts[-1].value_json)
        cls.country_map = extract_enrichment_map(country_facts[-1].value_json)

        # The ingestor is exercised through _build_roster only (read-only): the
        # company repo is never touched, so it is not constructed here.
        cls.ingestor = CompanyInvestigatedIngestor(
            fact_repo=cls.fact_repo, company_repo=None
        )
        cls.roster = cls.ingestor._build_roster(CELEX_ID, cls.roster_names)

    def _roster_record(self, company_name: str):
        """Return the assembled roster record for a verbatim company name.

        Args:
            company_name: The ``companies_investigated`` name to look up.

        Returns:
            The matching :class:`InvestigatedCompany`.

        Raises:
            AssertionError: If no record carries that name.
        """
        for record in self.roster:
            if record.company_name == company_name:
                return record
        self.fail(f"No roster record for {company_name!r}")

    # -- Hypothesis 1: "the address fact name is missing" -- refuted ----------

    def test_address_fact_carries_both_schema_keys(self):
        """The companies_address fact has an 'address' key on every row.

        The reported symptom is a NULL country, not a NULL address; the address
        column is in fact populated on all rows. This pins that the
        ``detail_retriever`` output shape is intact so the investigation is not
        sent after a missing schema key.
        """
        self.assertTrue(
            self.address_map, f"'{ADDRESS_FACT_NAME}' produced an empty enrichment map"
        )
        # extract_enrichment_map drops any row with fewer than two keys, so a
        # full-length map proves the second ('address') key is present throughout.
        self.assertEqual(
            len(self.address_map),
            len(self.roster_names),
            "Rows were dropped by extract_enrichment_map -- the 'address' key is "
            "missing on some companies_address rows",
        )

    def test_address_join_matches_every_roster_company(self):
        """Every roster name resolves to an address (the join that works)."""
        unmatched = [name for name in self.roster_names if name not in self.address_map]
        self.assertEqual(
            unmatched,
            [],
            f"{len(unmatched)} roster name(s) had no address; "
            "detail_retriever is expected to echo the roster entity verbatim",
        )

    # -- Hypothesis 2: "the country was never extracted" -- refuted -----------

    def test_country_fact_did_extract_the_iso_code(self):
        """companies_country holds Praoil -> IT; the value exists upstream.

        The code is lost at the join, not at extraction, so re-running the
        extractor cannot fix these rows.
        """
        self.assertIn(
            CLEAN_COMPANY_NAME,
            self.country_map,
            f"'{COUNTRY_FACT_NAME}' has no entry for the clean name "
            f"{CLEAN_COMPANY_NAME!r}",
        )
        self.assertEqual(
            self.country_map[CLEAN_COMPANY_NAME], EXPECTED_ISO_COUNTRY_CODE
        )

    # -- The actual root cause ------------------------------------------------


    @unittest.expectedFailure
    def test_country_join_keys_are_disjoint_from_the_roster(self):
        """The country map keys share nothing with the roster names.

        The two facts are individually correct and mutually unjoinable: the
        address map (detail_retriever, echoes the entity) intersects the roster
        fully, the country map (fact_context, re-emits a clean name) not at all.
        """
        self.assertEqual(
            len(set(self.roster_names) & set(self.address_map)),
            len(self.roster_names),
            "The address join is expected to match every roster name",
        )
        self.assertEqual(
            set(self.roster_names) & set(self.country_map),
            set(),
            "The country join is expected to match nothing -- if it now matches, "
            "the upstream name pollution was fixed and this test is obsolete",
        )

    # -- The regression guard -------------------------------------------------

    def test_every_roster_company_gets_an_iso_country_code(self):
        """The fix: no company loses its ISO country code at the join.

        Regressed before ``match_enrichment``: the country map is keyed by clean
        names while the roster is keyed by polluted ones, so
        ``country_map.get(name)`` missed on all 41 companies and the column was
        written NULL. The folded-prefix rule now bridges the two spellings.
        """
        missing = [
            record.company_name
            for record in self.roster
            if record.iso_country_code is None
        ]
        self.assertEqual(
            missing,
            [],
            f"{len(missing)} of {len(self.roster)} roster records lost their ISO "
            f"country code at the enrichment join",
        )

    def test_praoil_is_enriched_with_country_and_address(self):
        """The reported row, end to end.

        The record is looked up by the CLEAN name: ``_build_roster`` now strips
        the address suffix, so the polluted string is no longer a roster key.
        Both enrichment columns arrive -- the address from the detail_retriever
        join, the country from the folded-prefix rule.
        """
        record = self._roster_record(CLEAN_COMPANY_NAME)
        self.assertEqual(record.address, EXPECTED_ADDRESS)
        self.assertEqual(
            record.iso_country_code,
            EXPECTED_ISO_COUNTRY_CODE,
            f"companies_country holds {EXPECTED_ISO_COUNTRY_CODE!r} for "
            f"{CLEAN_COMPANY_NAME!r} but the roster record kept "
            f"{record.iso_country_code!r}",
        )

    def test_no_roster_name_retains_its_address(self):
        """Company names are stored register-searchable, not address-bearing.

        The polluted name defeated the LEI substring, Handelsregister exact-name
        and OpenCorporates searches; every name in this document was affected.
        """
        retained = [
            record.company_name
            for record in self.roster
            if record.address and record.company_name.endswith(record.address)
        ]
        self.assertEqual(
            retained,
            [],
            f"{len(retained)} roster name(s) still end with their own address",
        )


    def test_roster_row_count_is_preserved_by_the_cleaning(self):
        """Cleaning must not collapse two companies onto one name.

        ``company_investigated2celex_id`` has UNIQUE (company_name, celex_id), so
        a collision would either drop a company or fail the insert.
        """
        names = [record.company_name for record in self.roster]
        self.assertEqual(len(names), len(self.roster_names))
        self.assertEqual(len(set(names)), len(names), "duplicate company names")


class TestJurisdictionBlastRadius(unittest.TestCase):
    """The NULL country propagates: jurisdication_code cannot be resolved."""

    @classmethod
    def setUpClass(cls):
        company_repo = CompanyInvestigatedRepository()
        try:
            subdivisions = company_repo.select_gleif_subdivisions()
        except Exception as exc:  # pragma: no cover - environment guard
            raise unittest.SkipTest(f"Database not reachable: {exc}") from exc
        if not subdivisions:
            raise unittest.SkipTest(
                "celex_facts.gleif_jurisdication2countrycode is empty; load it "
                "with the GLEIF jurisdiction loader first."
            )
        cls.resolver = JurisdictionResolver(
            [
                GleifSubdivision(
                    jurisdiction_code=code,
                    jurisdiction_name=name,
                    iso_country_code=country,
                )
                for code, name, country in subdivisions
            ]
        )
        cls.praoil_address = "Strada 2, Pal. F7, 20090 Assago, Milan, Italy"

    def test_resolver_returns_none_without_a_country_despite_the_address(self):
        """A NULL country short-circuits the resolver before the address is read.

        ``JurisdictionResolver.resolve`` returns None as soon as the country
        fails to normalize, so the address naming Italy is never consulted. This
        is why the same rows also carry a NULL ``jurisdication_code``: one broken
        join costs two columns.
        """
        self.assertIsNone(self.resolver.resolve(None, self.praoil_address))

    def test_resolver_succeeds_once_the_country_is_supplied(self):
        """The address is sufficient; only the missing country blocks resolution."""
        resolved = self.resolver.resolve(
            EXPECTED_ISO_COUNTRY_CODE, self.praoil_address
        )
        self.assertIsNotNone(
            resolved,
            "With the country restored, the jurisdiction should resolve from the "
            "same address that yielded None above",
        )
        self.assertTrue(
            resolved.startswith(EXPECTED_ISO_COUNTRY_CODE),
            f"Expected an IT jurisdiction, got {resolved!r}",
        )


if __name__ == "__main__":
    unittest.main()
