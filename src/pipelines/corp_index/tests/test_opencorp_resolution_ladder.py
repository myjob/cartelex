"""Unit tests for the OpenCorporates resolution ladder.

Pure functions throughout -- no mocks, no client, no database. Every case is
either a real roster value that failed resolution in production or a real
OpenCorporates hit recorded by the 2026-07-31 probe, so a regression here means
a name that used to resolve has stopped resolving.
"""

import unittest
from datetime import date

from pipelines.corp_index.opencorp_models import OpenCorpCompany
from pipelines.corp_index.opencorp_resolution_ladder import (
    RUNG_CORE_ANY,
    RUNG_CORE_COUNTRY,
    RUNG_NO_PAREN,
    RUNG_SPLIT,
    RUNG_VERBATIM,
    RUNG_VERBATIM_ANY,
    OpenCorpTarget,
    RelaxationPolicy,
    build_ladder,
    rank_and_filter,
    relaxation_variants,
    score_hit,
    split_multi_company,
    strip_legal_form,
    strip_parentheticals,
    strip_trailing_role_clause,
)

# The roster row that motivated the ladder: headquartered in Athens, stored as
# GR, actually registered in Cyprus.
_HALKIS = OpenCorpTarget(
    company_name="Halkis",
    iso_country_code="GR",
    jurisdication_code="GR",
    address="Athens Tower GR-11527 ATHENS",
    industries=("cement",),
    decision_year=1994,
)

# The hit the probe found for it, verbatim from the live response.
_HALKIS_HIT = OpenCorpCompany(
    name="HALKIS CEMENT TRANSPORTERS CO. LIMITED",
    company_number="HE12345",
    jurisdiction_code="cy",
    registered_address_in_full=(
        "Arch. Makariou III, 284,\nFORTUNA COURT BLOCK B, Floor 2,\n"
        "3105, Limassol, Cyprus"
    ),
)

# Also returned by the unfiltered 'Halkis' search, and must not be linked: it
# contains the token but does not begin with it.
_GESTION_HALKIS_HIT = OpenCorpCompany(
    name="GESTION HALKIS INC.",
    company_number="1163456789",
    jurisdiction_code="ca_qc",
    registered_address_in_full="4240 av. Harvard Montreal (Quebec) H4A2W7",
)


class TestNameRelaxation(unittest.TestCase):
    """The four name-relaxation primitives."""

    def test_strip_parentheticals_removes_the_contents_not_just_the_brackets(self):
        """Kept bracket contents become extra AND terms and zero the query."""
        polluted = 'Bischof + Klein GmbH & Co. KG (hereinafter "Bischof + Klein")'
        stripped = strip_parentheticals(polluted)
        self.assertNotIn("hereinafter", stripped)
        self.assertNotIn("(", stripped)
        self.assertEqual("Bischof + Klein GmbH & Co. KG", stripped)

    def test_strip_parentheticals_handles_the_united_arab_shipping_case(self):
        """The ISSUES.md example: the qualifier is what zeroed the search."""
        self.assertEqual(
            "United Arab Shipping Company",
            strip_parentheticals("United Arab Shipping Company (S.A.G)"),
        )

    def test_strip_parentheticals_leaves_a_clean_name_alone(self):
        self.assertEqual("Halkis", strip_parentheticals("Halkis"))

    def test_strip_trailing_role_clause_drops_a_role_description(self):
        self.assertEqual(
            "Gebruder Junghans GmbH",
            strip_trailing_role_clause(
                "Gebruder Junghans GmbH, clock and watch manufacturers"
            ),
        )

    def test_strip_trailing_role_clause_drops_an_appended_town(self):
        self.assertEqual(
            "Solvay et Cie", strip_trailing_role_clause("Solvay et Cie, Brussels")
        )

    def test_strip_trailing_role_clause_keeps_a_name_with_no_comma(self):
        self.assertEqual("Halkis", strip_trailing_role_clause("Halkis"))

    def test_strip_trailing_role_clause_refuses_to_strip_below_the_minimum(self):
        """Cutting 'PC, Nederland BV' to 'PC' would send a useless query."""
        self.assertEqual("PC, Nederland BV", strip_trailing_role_clause("PC, Nederland BV"))

    def test_strip_legal_form_collapses_a_compound_suffix(self):
        self.assertEqual(
            "Bischof + Klein", strip_legal_form("Bischof + Klein GmbH & Co. KG")
        )

    def test_strip_legal_form_removes_a_single_suffix(self):
        self.assertEqual(
            "Nordenia International", strip_legal_form("Nordenia International AG")
        )

    def test_strip_legal_form_degrades_gracefully_on_a_lost_space(self):
        """'KodakS.A.' has no token boundary to strip, so it must survive whole."""
        self.assertEqual("KodakS.A.", strip_legal_form("KodakS.A."))

    def test_strip_legal_form_never_empties_a_name(self):
        self.assertEqual("SA", strip_legal_form("SA"))


class TestMultiCompanySplit(unittest.TestCase):
    """The highest-risk heuristic: splitting must not invent companies."""

    def test_splits_a_four_company_roster_row(self):
        members = split_multi_company(
            "OBB-Holding AG, Rail Cargo Austria AG, "
            "Rail Cargo Logistics - Austria GmbH and Express Interfracht Hellas A.E."
        )
        self.assertEqual(4, len(members))
        self.assertEqual("OBB-Holding AG", members[0])
        self.assertEqual("Express Interfracht Hellas A.E.", members[3])

    def test_does_not_split_a_single_company_containing_a_plus(self):
        self.assertEqual([], split_multi_company("Bischof + Klein GmbH & Co. KG"))

    def test_does_not_split_a_name_with_an_appended_town(self):
        """'Brussels' is not a legal form, so the gate holds."""
        self.assertEqual([], split_multi_company("Solvay et Cie, Brussels"))

    def test_does_not_split_a_role_description(self):
        self.assertEqual(
            [],
            split_multi_company("Gebruder Junghans GmbH, clock and watch manufacturers"),
        )

    def test_does_not_split_a_trailing_legal_form_into_its_own_company(self):
        """'S.A.B. de C.V.' is Orbia's legal form, not a second company.

        Caught by dry-running the rules over all 3,271 NA roster names: the
        segment ends in a recognized form ('C.V.') and so passed the original
        gate, but every one of its tokens is a form or a particle.
        """
        self.assertEqual(
            [], split_multi_company("Orbia Advance Corporation, S.A.B. de C.V.")
        )

    def test_does_not_split_a_plain_name(self):
        self.assertEqual([], split_multi_company("Halkis"))


class TestRelaxationVariants(unittest.TestCase):
    """Variant ordering and deduplication."""

    def test_a_clean_name_yields_exactly_one_variant(self):
        """Four identical searches would waste three calls per target."""
        self.assertEqual([(RUNG_VERBATIM, "Halkis")], relaxation_variants("Halkis"))

    def test_a_polluted_name_yields_progressively_shorter_variants(self):
        variants = relaxation_variants(
            "Gebruder Junghans GmbH, clock and watch manufacturers"
        )
        texts = [text for _, text in variants]
        self.assertEqual(
            [
                "Gebruder Junghans GmbH, clock and watch manufacturers",
                "Gebruder Junghans GmbH",
                "Gebruder Junghans",
            ],
            texts,
        )

    def test_variants_are_ordered_most_constrained_first(self):
        variants = relaxation_variants("United Arab Shipping Company (S.A.G)")
        self.assertEqual(RUNG_VERBATIM, variants[0][0])
        self.assertEqual(RUNG_NO_PAREN, variants[1][0])
        self.assertGreater(len(variants[0][1]), len(variants[-1][1]))


class TestBuildLadder(unittest.TestCase):
    """Rung ordering, which is where the design lives."""

    def test_unfiltered_verbatim_rung_comes_second(self):
        """The offshore path must not sit behind four name rungs."""
        ladder = build_ladder(_HALKIS, jurisdiction_code="gr")
        self.assertEqual(RUNG_VERBATIM, ladder[0].rung)
        self.assertEqual(RUNG_VERBATIM_ANY, ladder[1].rung)
        self.assertIsNone(ladder[1].jurisdiction_code)

    def test_every_unfiltered_rung_requires_scoring(self):
        ladder = build_ladder(_HALKIS, jurisdiction_code="gr")
        for rung in ladder:
            if rung.jurisdiction_code is None:
                self.assertTrue(rung.requires_scoring, rung.rung)

    def test_jurisdiction_filtered_rungs_are_not_scored(self):
        """Inside the target's own jurisdiction the filter is the precision."""
        ladder = build_ladder(_HALKIS, jurisdiction_code="gr")
        filtered = [rung for rung in ladder if rung.jurisdiction_code == "gr"]
        self.assertTrue(filtered)
        for rung in filtered:
            self.assertFalse(rung.requires_scoring, rung.rung)

    def test_the_last_rung_is_always_unfiltered(self):
        """The broadest attempt goes last, whichever name variant carries it.

        For a clean name the core text equals the verbatim text, so `core@any`
        deduplicates away and `verbatim@any` is the final rung; for a polluted
        name both survive. Either way the ladder ends unfiltered, which is what
        makes an offshore registration reachable.
        """
        clean = build_ladder(_HALKIS, jurisdiction_code="gr")
        self.assertIsNone(clean[-1].jurisdiction_code)
        self.assertEqual(RUNG_VERBATIM_ANY, clean[-1].rung)

        polluted = build_ladder(
            _HALKIS.model_copy(
                update={"company_name": "Halkis Cement Company (Greece) SA"}
            ),
            jurisdiction_code="gr",
        )
        self.assertIsNone(polluted[-1].jurisdiction_code)
        self.assertEqual(RUNG_CORE_ANY, polluted[-1].rung)

    def test_a_clean_name_produces_a_short_ladder(self):
        """'Halkis' has nothing to strip, so it costs two rungs, not eight."""
        ladder = build_ladder(_HALKIS, jurisdiction_code="gr")
        self.assertEqual(
            [RUNG_VERBATIM, RUNG_VERBATIM_ANY], [rung.rung for rung in ladder]
        )

    def test_country_fallback_rung_appears_only_for_a_subdivision(self):
        subdivision_target = _HALKIS.model_copy(update={"jurisdication_code": "AE-DU"})
        with_country = build_ladder(
            subdivision_target,
            jurisdiction_code="ae_du",
            country_jurisdiction_code="ae",
        )
        self.assertIn(RUNG_CORE_COUNTRY, [rung.rung for rung in with_country])

        without_country = build_ladder(_HALKIS, jurisdiction_code="gr")
        self.assertNotIn(RUNG_CORE_COUNTRY, [rung.rung for rung in without_country])

    def test_a_target_with_no_jurisdiction_still_gets_unfiltered_rungs(self):
        """An unresolvable jurisdiction must not mean an unsearchable company."""
        ladder = build_ladder(_HALKIS, jurisdiction_code=None)
        self.assertTrue(ladder)
        for rung in ladder:
            self.assertIsNone(rung.jurisdiction_code)

    def test_split_members_become_their_own_rungs(self):
        multi = OpenCorpTarget(
            company_name="OBB-Holding AG and Rail Cargo Austria AG",
            iso_country_code="AT",
            jurisdication_code="AT",
        )
        ladder = build_ladder(multi, jurisdiction_code="at")
        split_queries = [
            rung.query_text for rung in ladder if rung.rung == RUNG_SPLIT
        ]
        self.assertEqual(["OBB-Holding AG", "Rail Cargo Austria AG"], split_queries)

    def test_ladder_never_repeats_a_query_and_jurisdiction_pair(self):
        ladder = build_ladder(
            _HALKIS.model_copy(update={"company_name": "Nordenia International AG"}),
            jurisdiction_code="ch",
        )
        keys = [(rung.query_text, rung.jurisdiction_code) for rung in ladder]
        self.assertEqual(len(keys), len(set(keys)))


class TestScoreHit(unittest.TestCase):
    """Scoring: one veto, three boosts, nothing geographic ever negative."""

    def test_exact_folded_name_scores_highest(self):
        hit = OpenCorpCompany(
            name="halkis", company_number="1", jurisdiction_code="cy"
        )
        score = score_hit(hit, _HALKIS, query_text="Halkis")
        self.assertEqual(1.0, score.name_score)

    def test_whole_word_prefix_is_accepted(self):
        """The Halkis case: the registry name is longer than the roster name."""
        score = score_hit(_HALKIS_HIT, _HALKIS, query_text="Halkis")
        self.assertEqual(0.8, score.name_score)
        self.assertGreaterEqual(score.score, 0.8)

    def test_mere_containment_is_rejected(self):
        """'GESTION HALKIS INC.' contains the token but does not begin with it."""
        score = score_hit(_GESTION_HALKIS_HIT, _HALKIS, query_text="Halkis")
        self.assertEqual(0.5, score.name_score)
        self.assertLess(score.score, 0.8)

    def test_an_unrelated_name_scores_zero(self):
        hit = OpenCorpCompany(
            name="Siemens AG", company_number="1", jurisdiction_code="de"
        )
        self.assertEqual(0.0, score_hit(hit, _HALKIS, query_text="Halkis").score)

    def test_a_town_mismatch_is_not_penalised(self):
        """A Cyprus registered office must not count against an Athens HQ.

        This is the whole reason town is a boost: registration and headquarters
        legitimately diverge for exactly the companies the ladder exists to find.
        """
        matching_town = _HALKIS_HIT.model_copy(
            update={"registered_address_in_full": "Athens Tower, ATHENS"}
        )
        mismatched = score_hit(_HALKIS_HIT, _HALKIS, query_text="Halkis")
        matched = score_hit(matching_town, _HALKIS, query_text="Halkis")
        absent = score_hit(
            _HALKIS_HIT.model_copy(update={"registered_address_in_full": None}),
            _HALKIS,
            query_text="Halkis",
        )
        self.assertFalse(mismatched.town_match)
        self.assertTrue(matched.town_match)
        self.assertGreater(matched.score, mismatched.score)
        self.assertEqual(absent.score, mismatched.score)

    def test_industry_matches_against_the_name_not_a_structured_field(self):
        """industry_codes is empty outside 'gb'; 'cement' is in the name."""
        score = score_hit(_HALKIS_HIT, _HALKIS, query_text="Halkis")
        self.assertTrue(score.industry_match)

    def test_industry_alone_cannot_lift_a_weak_name_over_the_threshold(self):
        score = score_hit(_GESTION_HALKIS_HIT, _HALKIS, query_text="Halkis")
        self.assertLess(score.score, 0.8)

    def test_incorporation_after_the_decision_vetoes_the_hit(self):
        too_young = _HALKIS_HIT.model_copy(
            update={"incorporation_date": date(2011, 6, 22)}
        )
        score = score_hit(too_young, _HALKIS, query_text="Halkis")
        self.assertFalse(score.temporally_plausible)
        self.assertEqual(0.0, score.score)

    def test_incorporation_before_the_decision_is_kept(self):
        old_enough = _HALKIS_HIT.model_copy(
            update={"incorporation_date": date(1975, 1, 1)}
        )
        score = score_hit(old_enough, _HALKIS, query_text="Halkis")
        self.assertTrue(score.temporally_plausible)
        self.assertGreaterEqual(score.score, 0.8)

    def test_an_unknown_decision_year_never_vetoes(self):
        undated = _HALKIS.model_copy(update={"decision_year": None})
        young = _HALKIS_HIT.model_copy(update={"incorporation_date": date(2020, 1, 1)})
        self.assertTrue(score_hit(young, undated, query_text="Halkis").temporally_plausible)


class TestRankAndFilter(unittest.TestCase):
    """Acceptance, ordering and the per-target link ceiling."""

    def test_accepts_the_prefix_match_and_rejects_the_containment_match(self):
        accepted = rank_and_filter(
            [_GESTION_HALKIS_HIT, _HALKIS_HIT], _HALKIS, query_text="Halkis"
        )
        self.assertEqual(1, len(accepted))
        self.assertEqual("HALKIS CEMENT TRANSPORTERS CO. LIMITED", accepted[0][0].name)

    def test_returns_empty_when_nothing_clears_the_threshold(self):
        """Empty means ambiguous, not absent -- hits did come back."""
        self.assertEqual(
            [], rank_and_filter([_GESTION_HALKIS_HIT], _HALKIS, query_text="Halkis")
        )

    def test_orders_best_first(self):
        exact = OpenCorpCompany(
            name="Halkis", company_number="2", jurisdiction_code="cy"
        )
        accepted = rank_and_filter(
            [_HALKIS_HIT, exact], _HALKIS, query_text="Halkis"
        )
        self.assertEqual("Halkis", accepted[0][0].name)

    def test_truncates_to_the_link_ceiling(self):
        hits = [
            OpenCorpCompany(
                name=f"Halkis Holding {index}",
                company_number=str(index),
                jurisdiction_code="cy",
            )
            for index in range(20)
        ]
        accepted = rank_and_filter(hits, _HALKIS, query_text="Halkis", max_links=5)
        self.assertEqual(5, len(accepted))


class TestRelaxationPolicy(unittest.TestCase):
    """The knobs a --relax run turns, and the ones it deliberately does not."""

    def test_relaxed_lowers_the_minimum_query_length(self):
        """'STET' is four characters and produces no rung under the default."""
        self.assertLess(
            RelaxationPolicy.relaxed().min_query_length,
            RelaxationPolicy().min_query_length,
        )

    def test_relaxed_does_not_lower_the_match_threshold(self):
        """Relaxation widens what is asked, never what is accepted."""
        relaxed = RelaxationPolicy.relaxed()
        default = RelaxationPolicy()

        self.assertEqual(default.min_match_score, relaxed.min_match_score)
        self.assertEqual(default.max_links, relaxed.max_links)

    def test_relaxed_does_not_raise_the_hit_caps(self):
        """score_over_cap buys the over-cap page for free; paging would cost."""
        relaxed = RelaxationPolicy.relaxed()
        default = RelaxationPolicy()

        self.assertEqual(default.unfiltered_hit_cap, relaxed.unfiltered_hit_cap)
        self.assertEqual(
            default.jurisdiction_hit_cap, relaxed.jurisdiction_hit_cap
        )
        self.assertTrue(relaxed.score_over_cap)


class TestPolicyDrivenRelaxation(unittest.TestCase):
    """Roster names the default policy silences and a relaxed one reaches."""

    def setUp(self) -> None:
        self.relaxed = RelaxationPolicy.relaxed()

    def test_a_four_character_name_is_unsearchable_by_default(self):
        """'STET' (CELEX-31997D0781, no country) gets no rung at all."""
        self.assertEqual([], relaxation_variants("STET"))

    def test_a_four_character_name_is_searchable_when_relaxed(self):
        variants = relaxation_variants("STET", policy=self.relaxed)

        self.assertEqual([(RUNG_VERBATIM, "STET")], variants)

    def test_a_leading_legal_form_survives_by_default(self):
        """The front of a name is where identity usually lives -- opt-in only."""
        self.assertEqual(
            "S.A. Cimenteries CBR", strip_legal_form("S.A. Cimenteries CBR")
        )

    def test_a_leading_legal_form_is_cut_when_requested(self):
        self.assertEqual(
            "Cimenteries CBR",
            strip_legal_form("S.A. Cimenteries CBR", strip_leading=True),
        )

    def test_leading_and_trailing_forms_are_cut_together(self):
        self.assertEqual(
            "Cimenteries", strip_legal_form("SA Cimenteries NV", strip_leading=True)
        )

    def test_leading_strip_stops_before_emptying_the_name(self):
        """A name that is nothing but forms keeps its original text."""
        self.assertEqual("SA NV", strip_legal_form("SA NV", strip_leading=True))

    def test_a_non_latin_name_is_searchable(self):
        """A Greek name folds to '' in ASCII; it must not vanish for it.

        OpenCorporates does index non-Latin registered names, so dropping every
        variant of one silences a searchable company.
        """
        name = "Ελληνική Εταιρεία Προστασίας της Πνευματικής Ιδιοκτησίας"

        self.assertEqual([(RUNG_VERBATIM, name)], relaxation_variants(name))

    def test_a_country_less_target_still_gets_the_unfiltered_rungs(self):
        """829 unmarked roster rows name no country; all of them search globally."""
        target = OpenCorpTarget(company_name="CEJI-Interlude")

        ladder = build_ladder(target, jurisdiction_code=None, policy=self.relaxed)

        self.assertEqual([RUNG_VERBATIM_ANY], [rung.rung for rung in ladder])
        self.assertIsNone(ladder[0].jurisdiction_code)
        self.assertTrue(ladder[0].requires_scoring)

    def test_the_policy_caps_reach_the_rungs(self):
        """A CLI-set cap must be what the client is actually budgeted with."""
        policy = RelaxationPolicy(unfiltered_hit_cap=17, jurisdiction_hit_cap=23)

        ladder = build_ladder(_HALKIS, jurisdiction_code="gr", policy=policy)

        caps = {rung.jurisdiction_code is None: rung.hit_cap for rung in ladder}
        self.assertEqual(17, caps[True])
        self.assertEqual(23, caps[False])

    def test_the_policy_threshold_reaches_the_scorer(self):
        """'GESTION HALKIS INC.' is containment-only: 0.5, rejected at 0.8."""
        accepted = rank_and_filter(
            [_GESTION_HALKIS_HIT],
            _HALKIS,
            query_text="Halkis",
            policy=RelaxationPolicy(min_match_score=0.4),
        )

        self.assertEqual(1, len(accepted))


if __name__ == "__main__":
    unittest.main()
