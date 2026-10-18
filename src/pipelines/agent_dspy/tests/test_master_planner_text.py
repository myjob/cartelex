"""Unit tests for the Master Planner pure helpers (no DSPy / DB)."""

import unittest

from pipelines.agent_dspy.master_planner_text import (
    build_fact_name_catalog,
    filter_known_fact_names,
    is_insufficient_answer,
    looks_compositional,
    map_concept_to_fact_names,
)


class TestIsInsufficientAnswer(unittest.TestCase):
    """The fallback gate must catch empty / error / non-answers."""

    def test_empty_evidence_is_insufficient(self) -> None:
        self.assertTrue(is_insufficient_answer(answer="anything here", evidence=""))

    def test_tool_error_marker_is_insufficient(self) -> None:
        self.assertTrue(
            is_insufficient_answer(
                answer="a reasonably long answer string", evidence="[error] boom"
            )
        )

    def test_empty_marker_is_insufficient(self) -> None:
        self.assertTrue(
            is_insufficient_answer(
                answer="a reasonably long answer string",
                evidence="--- Step 1 ---\nNo rows returned.",
            )
        )

    def test_short_answer_is_insufficient(self) -> None:
        self.assertTrue(is_insufficient_answer(answer="NA", evidence="some evidence text"))

    def test_non_answer_phrase_is_insufficient(self) -> None:
        self.assertTrue(
            is_insufficient_answer(
                answer="I could not find any cases matching that entity.",
                evidence="--- Step 1 (doc_search) ---\nsome chunk text",
            )
        )

    def test_grounded_answer_is_sufficient(self) -> None:
        self.assertFalse(
            is_insufficient_answer(
                answer="CELEX-31994D0601 - Cartonboard. The fine was 132m EUR.",
                evidence="--- Step 1 (case_facts) ---\nfines: ...",
            )
        )


class TestLooksCompositional(unittest.TestCase):
    """Only multi-step queries should reach the Master Planner."""

    def test_count_group_question(self) -> None:
        self.assertTrue(
            looks_compositional(
                "Which countries have a lot of banking cartels and which have a low number?"
            )
        )

    def test_how_many_per_question(self) -> None:
        self.assertTrue(looks_compositional("How many cartels per country?"))

    def test_role_filter_with_entity(self) -> None:
        self.assertTrue(
            looks_compositional("List all cases where company Bayer AG was the cartel leader")
        )

    def test_cases_where_clause(self) -> None:
        self.assertTrue(looks_compositional("cases where the fine exceeded 100 million"))

    def test_simple_fact_lookup_is_not_compositional(self) -> None:
        self.assertFalse(looks_compositional("What is the case summary of CELEX-31994D0601?"))

    def test_role_word_without_entity_is_not_compositional(self) -> None:
        # "leader" alone, no proper-noun entity to filter by.
        self.assertFalse(looks_compositional("who was the leader"))


class TestConceptMapping(unittest.TestCase):
    """Concept mapping must be grounded in the live catalog."""

    _CATALOG = frozenset(
        {
            "companies_investigated",
            "companies_investigated",
            "companies_country",
            "market_geographical",
            "OECD_sector",
            "NACE_group_code",
            "leniency_applicants",
            "fines",
        }
    )

    def test_country_concept_maps_to_known_names(self) -> None:
        names = map_concept_to_fact_names("banking cartels per country", self._CATALOG)
        self.assertIn("companies_country", names)
        self.assertIn("market_geographical", names)

    def test_sector_concept_maps_to_known_names(self) -> None:
        names = map_concept_to_fact_names("banking sector", self._CATALOG)
        self.assertIn("OECD_sector", names)
        self.assertIn("NACE_group_code", names)

    def test_unmapped_concept_returns_empty(self) -> None:
        self.assertEqual(map_concept_to_fact_names("ringleader instigator", self._CATALOG), [])

    def test_candidate_not_in_catalog_is_dropped(self) -> None:
        thin_catalog = frozenset({"companies_investigated"})
        names = map_concept_to_fact_names("country", thin_catalog)
        # companies_country / market_geographical are not in the thin catalog.
        self.assertEqual(names, [])


class TestCatalogGating(unittest.TestCase):
    """Unknown names must never reach execution."""

    def test_filter_splits_known_and_unknown(self) -> None:
        catalog = frozenset({"companies_investigated", "fines"})
        known, unknown = filter_known_fact_names(
            ["companies_investigated", "made_up_fact", "fines", "companies_investigated"], catalog
        )
        self.assertEqual(known, ["companies_investigated", "fines"])
        self.assertEqual(unknown, ["made_up_fact"])

    def test_build_catalog_drops_excluded_infra_names(self) -> None:
        distinct = [
            "companies_investigated",
            "fines",
            "decision_chapter_tokens",
        ]
        excluded = frozenset({"decision_chapter_tokens"})
        catalog = build_fact_name_catalog(distinct, excluded)
        self.assertEqual(catalog, frozenset({"companies_investigated", "fines"}))

    def test_build_catalog_applies_no_name_pattern_heuristic(self) -> None:
        """A name is dropped only when *excluded* lists it.

        Integration tests delete their own scaffolding rows, so the catalog no
        longer second-guesses names that merely look like test data.
        """
        catalog = build_fact_name_catalog(["fines", "test_fact_demo"], frozenset())
        self.assertEqual(catalog, frozenset({"fines", "test_fact_demo"}))

    def test_build_catalog_drops_empty_names(self) -> None:
        catalog = build_fact_name_catalog(["fines", ""], frozenset())
        self.assertEqual(catalog, frozenset({"fines"}))


class TestRelationalCues(unittest.TestCase):
    """Company-to-company questions must escalate to the Master Planner.

    These carry neither an aggregation cue nor a role cue, so before the
    relational cues were added they returned False and the graph traversal
    tool was unreachable for exactly the phrasings it exists to serve.
    """

    def test_co_participant_phrasing_escalates(self):
        self.assertTrue(looks_compositional("Who were Bayer AG co-participants?"))
        self.assertTrue(looks_compositional("List the coparticipants of Hoechst"))

    def test_same_companies_phrasing_escalates(self):
        self.assertTrue(
            looks_compositional(
                "Which other cartels involved the same companies as CELEX-31990D0645?"
            )
        )

    def test_connection_phrasings_escalate(self):
        for question in (
            "Which companies are connected to Bayer through shared cases?",
            "Which companies were in cartels together with Bayer?",
            "Which firms appear alongside Solvay?",
            "Which companies are linked to Akzo?",
        ):
            with self.subTest(question=question):
                self.assertTrue(looks_compositional(question))

    def test_single_fact_lookups_still_do_not_escalate(self):
        """The gate must stay closed for cheap single-case questions."""
        for question in (
            "What is the decision date of CELEX-31990D0645?",
            "Summarise case 38344_5856_3",
            "What is the NACE group code for CELEX-31991D0562?",
        ):
            with self.subTest(question=question):
                self.assertFalse(looks_compositional(question))



if __name__ == "__main__":
    unittest.main()
