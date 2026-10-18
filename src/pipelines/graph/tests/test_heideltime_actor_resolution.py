"""Unit tests for HeidelTime actor -> company-node resolution.

Pure unit tests: the module under test performs no database access, so nothing
is mocked.  Every actor string used here was observed in the live fact store on
2026-09-05, so the fixtures are corpus shapes rather than invented ones.
"""

import unittest

from pipelines.graph.entity_graph_models import EdgeValue
from pipelines.graph.heideltime_actor_resolution import (
    ActorClass,
    CompanyCandidate,
    MatchMethod,
    classify_actor,
    company_stem,
    match_actor,
    normalize_actor,
    parse_heideltime_events,
    project_case_events,
)

_MIN_STEM = 4


def _events(*triples: tuple[str, str, str]) -> str:
    """Render (date, actor, predicate) triples as a stored ``value_json``."""
    import json

    return json.dumps(
        [
            {"event_date": date, "actor": actor, "predicate_object": predicate}
            for date, actor, predicate in triples
        ]
    )


class TestParseHeidelTimeEvents(unittest.TestCase):
    """Parsing tolerates every malformed payload the store actually holds."""

    def test_parses_the_stored_bare_list_shape(self) -> None:
        rows = parse_heideltime_events(
            _events(("1998-05-13", "Teva", "attended a meeting"))
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].actor, "Teva")
        self.assertEqual(rows[0].event_date, "1998-05-13")
        self.assertEqual(rows[0].predicate_object, "attended a meeting")

    def test_empty_markers_yield_no_rows(self) -> None:
        for payload in ("", "  ", "NA", "[]", "null", "none"):
            with self.subTest(payload=payload):
                self.assertEqual(parse_heideltime_events(payload), [])

    def test_none_and_malformed_json_yield_no_rows(self) -> None:
        self.assertEqual(parse_heideltime_events(None), [])
        self.assertEqual(parse_heideltime_events("{not json"), [])

    def test_non_list_payload_yields_no_rows(self) -> None:
        self.assertEqual(parse_heideltime_events('{"events": []}'), [])

    def test_elements_without_a_usable_actor_are_skipped(self) -> None:
        payload = '[{"event_date": "1998-05-13"}, {"actor": "   "}, "stray", 7]'
        self.assertEqual(parse_heideltime_events(payload), [])

    def test_missing_event_date_becomes_empty_not_an_error(self) -> None:
        rows = parse_heideltime_events('[{"actor": "Teva"}]')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].event_date, "")
        self.assertIsNone(rows[0].predicate_object)


class TestNormalizationAndStem(unittest.TestCase):
    """Normalisation collapses the variants that separate a name from itself."""

    def test_normalization_casefolds_and_collapses_whitespace(self) -> None:
        self.assertEqual(normalize_actor("  Teva   Pharmaceutical  "), "teva pharmaceutical")

    def test_normalization_strips_brackets_and_possessives(self) -> None:
        self.assertEqual(normalize_actor("[Redacted]"), "redacted")
        self.assertEqual(normalize_actor("Lundbeck's"), "lundbeck")

    def test_legal_forms_do_not_change_the_stem(self) -> None:
        self.assertEqual(company_stem("Akzo Chemicals BV"), company_stem("Akzo Chemicals"))

    def test_punctuated_legal_forms_leave_no_single_letter_tokens(self) -> None:
        # "A/S" splits to ('a', 's'); single characters must not survive or they
        # defeat subsequence matching for the name they trail.
        self.assertEqual(company_stem("Lundbeck A/S"), ("lundbeck",))
        self.assertEqual(company_stem("Fujisawa S.p.A."), ("fujisawa",))

    def test_stem_never_collapses_to_nothing(self) -> None:
        # A name made entirely of legal forms keeps its raw tokens rather than
        # becoming an empty stem that would match everything.
        self.assertEqual(company_stem("Holding AG"), ("holding", "ag"))


class TestClassifyActor(unittest.TestCase):
    """Placeholders and institutions are diverted before any matching."""

    def test_placeholders(self) -> None:
        for actor in ("redacted", "[REDACTED]", "parties", "the parties",
                      "unknown", "employee of non-addressee", "  "):
            with self.subTest(actor=actor):
                self.assertIs(classify_actor(actor), ActorClass.PLACEHOLDER)

    def test_institutions(self) -> None:
        for actor in ("Commission", "European Commission",
                      "Commission of the European Communities",
                      "Court of First Instance", "Bundeskartellamt",
                      "Office of Fair Trading"):
            with self.subTest(actor=actor):
                self.assertIs(classify_actor(actor), ActorClass.INSTITUTION)

    def test_company_candidates(self) -> None:
        for actor in ("Teva", "Lundbeck", "Servier", "Jungbunzlauer AG"):
            with self.subTest(actor=actor):
                self.assertIs(classify_actor(actor), ActorClass.COMPANY_CANDIDATE)

    def test_institution_marker_must_be_a_whole_token(self) -> None:
        # "Commissioning" contains "commission" as a substring but is not one.
        self.assertIs(
            classify_actor("Commissioning Services Ltd"), ActorClass.COMPANY_CANDIDATE
        )


class TestMatchActor(unittest.TestCase):
    """Case-scoped matching, in tier order, refusing ambiguity."""

    def setUp(self) -> None:
        self.candidates = [
            CompanyCandidate("e-teva", "Teva Pharmaceutical Industries Ltd"),
            CompanyCandidate("e-az", "AstraZeneca AB"),
            CompanyCandidate("e-lund", "Lundbeck A/S"),
        ]

    def test_exact_match_wins(self) -> None:
        candidate, method, colliding = match_actor(
            "astrazeneca ab", self.candidates, _MIN_STEM
        )
        self.assertEqual(candidate.entity_id, "e-az")
        self.assertIs(method, MatchMethod.EXACT)
        self.assertEqual(colliding, [])

    def test_actor_stem_inside_company_name(self) -> None:
        candidate, method, _ = match_actor("Teva", self.candidates, _MIN_STEM)
        self.assertEqual(candidate.entity_id, "e-teva")
        self.assertIs(method, MatchMethod.STEM_ACTOR_IN_COMPANY)

    def test_company_stem_inside_actor_phrase(self) -> None:
        candidate, method, _ = match_actor(
            "Lundbeck's UK subsidiary", self.candidates, _MIN_STEM
        )
        self.assertEqual(candidate.entity_id, "e-lund")
        self.assertIs(method, MatchMethod.STEM_COMPANY_IN_ACTOR)

    def test_initialism_does_not_match_a_longer_name(self) -> None:
        # Deliberate conservative miss. A confident wrong link is worse than a
        # counted miss in an evidence system; changing this must argue here.
        candidate, method, colliding = match_actor("AZ", self.candidates, _MIN_STEM)
        self.assertIsNone(candidate)
        self.assertIsNone(method)
        self.assertEqual(colliding, [])

    def test_ambiguity_is_refused_and_reported(self) -> None:
        siblings = [
            CompanyCandidate("e-1", "Jungbunzlauer AG"),
            CompanyCandidate("e-2", "Jungbunzlauer Holding AG"),
        ]
        candidate, method, colliding = match_actor(
            "Jungbunzlauer", siblings, _MIN_STEM
        )
        self.assertIsNone(candidate)
        self.assertIsNone(method)
        self.assertEqual(colliding, ["Jungbunzlauer AG", "Jungbunzlauer Holding AG"])

    def test_no_candidates_yields_no_match(self) -> None:
        candidate, _, colliding = match_actor("Teva", [], _MIN_STEM)
        self.assertIsNone(candidate)
        self.assertEqual(colliding, [])

    def test_unrelated_actor_yields_no_match(self) -> None:
        candidate, _, colliding = match_actor(
            "Nowhere Chemicals Inc", self.candidates, _MIN_STEM
        )
        self.assertIsNone(candidate)
        self.assertEqual(colliding, [])

    def test_alias_tier_uses_the_injected_resolver(self) -> None:
        candidate, method, _ = match_actor(
            "The Danish one",
            self.candidates,
            _MIN_STEM,
            alias_resolver=lambda name: "Lundbeck A/S" if name == "The Danish one" else None,
        )
        self.assertEqual(candidate.entity_id, "e-lund")
        self.assertIs(method, MatchMethod.ALIAS)


class TestProjectCaseEvents(unittest.TestCase):
    """Aggregation, payload shape, and the terminal-bucket counter contract."""

    def setUp(self) -> None:
        self.candidates = [CompanyCandidate("e-teva", "Teva Pharmaceutical Industries Ltd")]

    def test_aggregates_dates_and_actors_per_company(self) -> None:
        events = parse_heideltime_events(
            _events(
                ("2002-11-07", "Teva", "signed an agreement"),
                ("1998-05-13", "Teva Pharmaceutical Industries", "attended a meeting"),
                ("1999-01-04", "Teva", "sent an email"),
            )
        )
        projection = project_case_events(events, self.candidates, _MIN_STEM)
        payload = projection.payloads["e-teva"]
        self.assertEqual(payload.first_date, "1998-05-13")
        self.assertEqual(payload.last_date, "2002-11-07")
        self.assertEqual(payload.event_count, 3)
        # Both spellings resolved to the one node, and both are kept verbatim
        # because case_timeline_vw joins back on these strings.
        self.assertEqual(payload.actors, ("Teva", "Teva Pharmaceutical Industries"))

    def test_a_sibling_qualifier_is_not_assumed_to_be_the_parent(self) -> None:
        # "Teva UK" shares only a leading token with "Teva Pharmaceutical
        # Industries Ltd"; neither stem contains the other, so it is left
        # unresolved rather than folded into the parent. Documented because it
        # is the main source of the measured ~51% non-resolution.
        events = parse_heideltime_events(_events(("1998-05-13", "Teva UK", "met")))
        projection = project_case_events(events, self.candidates, _MIN_STEM)
        self.assertEqual(projection.payloads, {})
        self.assertEqual(projection.counters["heideltime_actor_unresolved"], 1)

    def test_partial_dates_count_but_never_set_the_bounds(self) -> None:
        events = parse_heideltime_events(
            _events(
                ("1994", "Teva", "began"),
                ("1994-03", "Teva", "continued"),
                ("1995-06-01", "Teva", "ended"),
            )
        )
        projection = project_case_events(events, self.candidates, _MIN_STEM)
        payload = projection.payloads["e-teva"]
        self.assertEqual(payload.first_date, "1995-06-01")
        self.assertEqual(payload.last_date, "1995-06-01")
        self.assertEqual(payload.event_count, 3)
        self.assertEqual(projection.counters["heideltime_event_partial_date"], 2)

    def test_all_partial_dates_leave_both_bounds_unset(self) -> None:
        events = parse_heideltime_events(_events(("1994", "Teva", "began")))
        payload = project_case_events(events, self.candidates, _MIN_STEM).payloads["e-teva"]
        self.assertIsNone(payload.first_date)
        self.assertIsNone(payload.last_date)
        self.assertEqual(payload.event_count, 1)

    def test_every_actor_lands_in_exactly_one_terminal_bucket(self) -> None:
        events = parse_heideltime_events(
            _events(
                ("1998-05-13", "Teva", "met"),
                ("1998-05-14", "redacted", "met"),
                ("1998-05-15", "Commission", "inspected"),
                ("1998-05-16", "Nowhere Ltd", "met"),
            )
        )
        counters = project_case_events(events, self.candidates, _MIN_STEM).counters
        terminal = (
            counters["heideltime_actor_placeholder"]
            + counters["heideltime_actor_institution"]
            + counters["heideltime_actor_ambiguous"]
            + counters["heideltime_actor_unresolved"]
            + sum(counters[f"heideltime_actor_{m.value}"] for m in MatchMethod)
        )
        self.assertEqual(counters["heideltime_actors_total"], 4)
        self.assertEqual(terminal, 4)

    def test_every_event_is_either_attached_or_dropped(self) -> None:
        events = parse_heideltime_events(
            _events(
                ("1998-05-13", "Teva", "met"),
                ("1998-05-14", "redacted", "met"),
                ("1998-05-15", "redacted", "met"),
            )
        )
        counters = project_case_events(events, self.candidates, _MIN_STEM).counters
        self.assertEqual(counters["heideltime_events_total"], 3)
        self.assertEqual(counters["heideltime_events_attached"], 1)
        self.assertEqual(counters["heideltime_events_dropped"], 2)

    def test_ambiguous_actors_are_reported_for_logging(self) -> None:
        siblings = [
            CompanyCandidate("e-1", "Jungbunzlauer AG"),
            CompanyCandidate("e-2", "Jungbunzlauer Holding AG"),
        ]
        events = parse_heideltime_events(_events(("1998-05-13", "Jungbunzlauer", "met")))
        projection = project_case_events(events, siblings, _MIN_STEM)
        self.assertEqual(projection.payloads, {})
        self.assertEqual(len(projection.ambiguous_actors), 1)
        self.assertEqual(projection.counters["heideltime_actor_ambiguous"], 1)

    def test_no_candidates_invents_no_company(self) -> None:
        events = parse_heideltime_events(_events(("1998-05-13", "Teva", "met")))
        projection = project_case_events(events, [], _MIN_STEM)
        self.assertEqual(projection.payloads, {})
        self.assertEqual(projection.counters["heideltime_actor_unresolved"], 1)


class TestEdgeValuePayload(unittest.TestCase):
    """The extension must not disturb the five roster-grounded relations."""

    def test_amount_only_payload_is_unchanged(self) -> None:
        self.assertEqual(EdgeValue(amount="ECU 5 000").to_json(), '{"amount": "ECU 5 000"}')

    def test_empty_payload_stays_null(self) -> None:
        self.assertIsNone(EdgeValue().to_json())

    def test_temporal_payload_serializes_actors_as_an_array(self) -> None:
        payload = EdgeValue(
            actors=("Teva", "Teva UK"),
            first_date="1998-05-13",
            last_date="2002-11-07",
            event_count=12,
        )
        self.assertEqual(
            payload.to_json(),
            '{"actors": ["Teva", "Teva UK"], "first_date": "1998-05-13", '
            '"last_date": "2002-11-07", "event_count": 12}',
        )


if __name__ == "__main__":
    unittest.main()
