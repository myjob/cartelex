"""Unit tests for the pure fact-family -> edge projection (no database)."""
import unittest

from pipelines.graph.entity_graph_mapping import (
    COMPANY_ROSTER_FAMILY,
    EXCLUDED_FAMILIES,
    FAMILY_EDGE_MAP,
    TEMPORAL_EDGE_FAMILY,
    parse_rows,
)
from pipelines.graph.entity_graph_models import EdgeRelation, EntityKind


class TestParseCompanyAmountRows(unittest.TestCase):
    """The fine/leniency shape keeps each amount paired with its company."""

    def test_multi_row_preserves_pairing(self):
        spec = FAMILY_EDGE_MAP["leniency_granted"]
        rows = parse_rows(
            '[{"company": "Roto", "amount": "100%"}, {"company": "Winkhaus", "amount": "5%"}]',
            spec,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].endpoint_token, "Roto")
        self.assertEqual(rows[0].payload.amount, "100%")
        self.assertEqual(rows[1].endpoint_token, "Winkhaus")
        self.assertEqual(rows[1].payload.amount, "5%")

    def test_fines_amount_payload_serializes(self):
        spec = FAMILY_EDGE_MAP["fines"]
        rows = parse_rows('[{"company": "Akzo Chemicals BV", "amount": "ECU 5 000"}]', spec)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].payload.to_json(), '{"amount": "ECU 5 000"}')


class TestParseCompanyOnlyRows(unittest.TestCase):
    """Company-only families yield no amount payload."""

    def test_company_list(self):
        # companies_investigated declares ``output_schema: companies, location``,
        # so its rows key the company name under "companies".
        spec = FAMILY_EDGE_MAP[COMPANY_ROSTER_FAMILY]
        rows = parse_rows('[{"companies": "RWE AG"}, {"companies": "RWE Energy AG"}]', spec)
        self.assertEqual([row.endpoint_token for row in rows], ["RWE AG", "RWE Energy AG"])
        self.assertEqual([row.source_field for row in rows], ["companies", "companies"])
        self.assertIsNone(rows[0].payload.amount)
        self.assertIsNone(rows[0].payload.to_json())

    def test_leniency_applicants_use_company_key(self):
        spec = FAMILY_EDGE_MAP["leniency_applicants"]
        rows = parse_rows('[{"company": "Murat SA"}]', spec)
        self.assertEqual([row.endpoint_token for row in rows], ["Murat SA"])

    def test_bare_string_element_defensive(self):
        spec = FAMILY_EDGE_MAP[COMPANY_ROSTER_FAMILY]
        rows = parse_rows('["Murat SA"]', spec)
        self.assertEqual([row.endpoint_token for row in rows], ["Murat SA"])


class TestRosterRowKeyVariance(unittest.TestCase):
    """The roster fact was written with two row keys; both must yield edges.

    229 of 743 documents key the company name as "company" instead of the
    declared "companies"; reading only the declared key dropped 1,627 rows.
    """

    def test_variant_company_key_is_accepted(self):
        spec = FAMILY_EDGE_MAP[COMPANY_ROSTER_FAMILY]
        rows = parse_rows('[{"company": "Solvay SA"}, {"company": "Akzo NV"}]', spec)
        self.assertEqual([row.endpoint_token for row in rows], ["Solvay SA", "Akzo NV"])
        self.assertEqual([row.source_field for row in rows], ["company", "company"])

    def test_declared_key_wins_when_both_present(self):
        spec = FAMILY_EDGE_MAP[COMPANY_ROSTER_FAMILY]
        rows = parse_rows('[{"companies": "Declared", "company": "Variant"}]', spec)
        self.assertEqual(rows[0].endpoint_token, "Declared")
        self.assertEqual(rows[0].source_field, "companies")

    def test_blank_declared_key_falls_through_to_variant(self):
        spec = FAMILY_EDGE_MAP[COMPANY_ROSTER_FAMILY]
        rows = parse_rows('[{"companies": "   ", "company": "Variant"}]', spec)
        self.assertEqual(rows[0].endpoint_token, "Variant")
        self.assertEqual(rows[0].source_field, "company")

    def test_location_key_is_not_an_endpoint(self):
        # ``location`` is the roster's second output column; it qualifies the
        # company, it is not a company of its own.
        spec = FAMILY_EDGE_MAP[COMPANY_ROSTER_FAMILY]
        rows = parse_rows('[{"companies": "Solvay SA", "location": "Brussels"}]', spec)
        self.assertEqual([row.endpoint_token for row in rows], ["Solvay SA"])
        self.assertIsNone(rows[0].payload.to_json())

    def test_fine_and_leniency_families_stay_single_keyed(self):
        # The "company"/"companies" ambiguity must not leak into the families
        # that legitimately own the "company" vocabulary.
        for family in ("fines", "leniency_applicants", "leniency_granted",
                       "leniency_rejected", "exemption_applicants"):
            spec = FAMILY_EDGE_MAP[family]
            self.assertEqual(spec.endpoint_fields, ("company",), family)
            self.assertEqual(parse_rows('[{"companies": "Wrong Key"}]', spec), [], family)


class TestParseEmptyAndMalformed(unittest.TestCase):
    """Empty / NA / malformed value_json is skipped with no value_md fallback."""

    def test_placeholders_and_junk_yield_no_rows(self):
        spec = FAMILY_EDGE_MAP["fines"]
        for raw in ("NA", "na", "[]", "", "   ", "null", None, "{not json", "[{}]"):
            self.assertEqual(parse_rows(raw, spec), [], f"expected [] for {raw!r}")

    def test_missing_endpoint_field_skipped(self):
        spec = FAMILY_EDGE_MAP["fines"]
        rows = parse_rows('[{"amount": "ECU 5 000"}, {"company": "Kept"}]', spec)
        self.assertEqual([row.endpoint_token for row in rows], ["Kept"])


class TestProvisionEndpointExtraction(unittest.TestCase):
    """Provision families expose their raw endpoint token for downstream resolution."""

    def test_infringement_concept_token(self):
        spec = FAMILY_EDGE_MAP["infringement_concept"]
        rows = parse_rows('[{"infringement_concept": "anticompetitive_agreement"}]', spec)
        self.assertEqual(rows[0].endpoint_token, "anticompetitive_agreement")
        self.assertFalse(spec.endpoint_is_src)  # case -> provision

    def test_law_article_tokens(self):
        spec = FAMILY_EDGE_MAP["law_article_all_relevant"]
        rows = parse_rows('[{"law_article": "Article 82"}, {"law_article": "Article 86"}]', spec)
        self.assertEqual([row.endpoint_token for row in rows], ["Article 82", "Article 86"])


class TestMappingCoverage(unittest.TestCase):
    """The family map covers the intended families and only valid enum values."""

    def test_excluded_families_absent(self):
        self.assertEqual(
            EXCLUDED_FAMILIES,
            frozenset(
                {
                    "companies_country",
                    "companies_address",
                    "companies_industry",
                    "company_recidivism",
                    "heideltime_raw",
                    "heideltime_event_agg",
                    "heideltime_cartel_period",
                }
            ),
        )
        for family in EXCLUDED_FAMILIES:
            self.assertNotIn(family, FAMILY_EDGE_MAP)

    def test_expected_families_present(self):
        expected = {
            "companies_investigated", "fines", "leniency_applicants", "leniency_granted",
            "leniency_rejected", "exemption_applicants", "infringement_concept",
            "law_article_all_relevant",
        }
        self.assertEqual(set(FAMILY_EDGE_MAP), expected)

    def test_temporal_family_is_projected_but_not_in_the_edge_map(self):
        # heideltime_event IS materialized (as dated_in), just not through
        # FAMILY_EDGE_MAP: parse_rows carries one endpoint token per element and
        # cannot express three fields plus cross-element aggregation. Asserted
        # so the absence stays a documented decision rather than an oversight -
        # which is exactly what it was before the temporal layer landed.
        self.assertEqual(TEMPORAL_EDGE_FAMILY, "heideltime_event")
        self.assertNotIn(TEMPORAL_EDGE_FAMILY, FAMILY_EDGE_MAP)
        self.assertNotIn(TEMPORAL_EDGE_FAMILY, EXCLUDED_FAMILIES)

    def test_every_heideltime_family_is_accounted_for(self):
        # No heideltime family may be invisible: each is either the projected
        # temporal family or explicitly excluded.
        heideltime_families = {
            "heideltime_raw", "heideltime_event", "heideltime_event_agg",
            "heideltime_cartel_period",
        }
        for family in heideltime_families:
            with self.subTest(family=family):
                self.assertTrue(
                    family == TEMPORAL_EDGE_FAMILY or family in EXCLUDED_FAMILIES,
                    f"{family} is neither projected nor excluded",
                )

    def test_specs_use_valid_enums(self):
        valid_kinds = set(EntityKind)
        valid_relations = set(EdgeRelation)
        for family, spec in FAMILY_EDGE_MAP.items():
            self.assertIn(spec.relation, valid_relations, family)
            self.assertIn(spec.src_kind, valid_kinds, family)
            self.assertIn(spec.dst_kind, valid_kinds, family)

    def test_retired_company_families_absent(self):
        # The company layer was originally sourced from ``companies_canoncial``;
        # these names are retired from FACT_EXTRACTOR.yaml and must never come
        # back as an edge source, or a re-run resurrects edges from dead facts.
        for retired in ("companies", "companies_all", "companies_canoncial",
                        "company_country"):
            self.assertNotIn(retired, FAMILY_EDGE_MAP)
        self.assertEqual(COMPANY_ROSTER_FAMILY, "companies_investigated")
        self.assertEqual(
            FAMILY_EDGE_MAP[COMPANY_ROSTER_FAMILY].relation, EdgeRelation.PARTICIPATED_IN
        )

    def test_participated_in_has_exactly_one_source_family(self):
        sources = [
            family
            for family, spec in FAMILY_EDGE_MAP.items()
            if spec.relation == EdgeRelation.PARTICIPATED_IN
        ]
        self.assertEqual(sources, [COMPANY_ROSTER_FAMILY])

    def test_company_specs_have_company_source(self):
        for family in (COMPANY_ROSTER_FAMILY, "fines", "leniency_granted"):
            spec = FAMILY_EDGE_MAP[family]
            self.assertTrue(spec.endpoint_is_src)
            self.assertEqual(spec.src_kind, EntityKind.COMPANY)
            self.assertEqual(spec.dst_kind, EntityKind.CASE)


if __name__ == "__main__":
    unittest.main()
