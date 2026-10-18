"""Tests for the LLM-shape coercions on the heideltime aggregation result models.

Both validators exist because of real failures in the 2026-09-02 aggregation
run over the remediated corpus: one document was lost outright when the model
answered with a bare JSON array keyed ``event_date`` instead of the declared
``{"events": [{"event": ...}]}`` envelope.

Run with::

    .venv/bin/python -m unittest \
        src.pipelines.agent_dspy.tests.test_heideltime_aggregate_result -v
"""

import unittest

from pydantic import ValidationError

from pipelines.agent_dspy.dspy_signature import (
    HeidelTimeCriticalEvent,
    HeidelTimeCriticalEventResult,
    HeidelTimeEventAggregateSignature,
    HeideltimeCartelPeriodResult,
)


class TestHeidelTimeCriticalEventResult(unittest.TestCase):
    """Envelope coercion: a bare list is accepted as ``events``."""

    def test_bare_list_is_wrapped(self) -> None:
        """The exact shape that failed 39437_7332_3 on 2026-09-02."""
        payload = [
            {
                "event_date": "2002-03-01",
                "actor": "European Commission",
                "predicate_object": "decision on CPT cartel",
            }
        ]
        result = HeidelTimeCriticalEventResult.model_validate(payload)

        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0].event, "2002-03-01")
        self.assertEqual(result.events[0].actor, "European Commission")

    def test_declared_envelope_still_works(self) -> None:
        """The correct shape must be untouched by the coercion."""
        payload = {
            "events": [
                {
                    "event": "1998-01-14",
                    "actor": "European Commission",
                    "predicate_object": "imposed fines",
                }
            ]
        }
        result = HeidelTimeCriticalEventResult.model_validate(payload)

        self.assertEqual(result.events[0].event, "1998-01-14")

    def test_empty_list_yields_no_events(self) -> None:
        """An empty answer is a valid result, not a validation failure."""
        self.assertEqual(HeidelTimeCriticalEventResult.model_validate([]).events, [])

    def test_garbage_still_rejected(self) -> None:
        """The coercion must not turn the model into an anything-goes parser."""
        with self.assertRaises(ValidationError):
            HeidelTimeCriticalEventResult.model_validate("not an event list")


class TestHeidelTimeEventAggregateSchema(unittest.TestCase):
    """The limit the LLM sees must be the same in every place it is stated.

    Until 2026-09-13 the ``events`` field said "Maximum two" while the
    signature said 12; the JSON schema won, and CELEX-31965D0426 kept only its
    1965 decision and 1970 expiry, losing the 1962 agreement that starts it.
    """

    def test_events_field_does_not_cap_at_two(self) -> None:
        """The envelope description must not reintroduce the two-event cap."""
        description = HeidelTimeCriticalEventResult.model_fields["events"].description
        self.assertNotIn("two", description.lower())
        self.assertIn("12", description)

    def test_signature_requires_earliest_conduct_event(self) -> None:
        """Instruction and output field both demand the start of the conduct."""
        output_desc = HeidelTimeEventAggregateSignature.output_fields["output"]
        self.assertIn("EARLIEST conduct event", HeidelTimeEventAggregateSignature.__doc__)
        self.assertIn("12", output_desc.json_schema_extra["desc"])
        self.assertIn("earliest conduct event", output_desc.json_schema_extra["desc"])


class TestHeidelTimeCriticalEvent(unittest.TestCase):
    """Field coercion: ``event_date`` is accepted as an alias for ``event``."""

    def test_event_date_alias_is_mapped(self) -> None:
        """The upstream heideltime_event rows are keyed event_date."""
        event = HeidelTimeCriticalEvent.model_validate(
            {
                "event_date": "2005-09-14",
                "actor": "Commission",
                "predicate_object": "adopted decision",
            }
        )
        self.assertEqual(event.event, "2005-09-14")

    def test_explicit_event_wins_over_alias(self) -> None:
        """A correct response is never rewritten by the alias."""
        event = HeidelTimeCriticalEvent.model_validate(
            {
                "event": "correct",
                "event_date": "ignored",
                "actor": "Commission",
                "predicate_object": "adopted decision",
            }
        )
        self.assertEqual(event.event, "correct")

    def test_null_sentinels_still_become_unknown(self) -> None:
        """The pre-existing null-string handling must survive the new validator."""
        event = HeidelTimeCriticalEvent.model_validate(
            {"event": "2005-09-14", "actor": "null", "predicate_object": "N/A"}
        )
        self.assertEqual(event.actor, "unknown")
        self.assertEqual(event.predicate_object, "unknown")


class TestPartialEventDates(unittest.TestCase):
    """A date the document states only as a year must survive aggregation."""

    def test_year_only_event_is_accepted(self) -> None:
        """'1994' is a legitimate answer when the text names no month or day."""
        event = HeidelTimeCriticalEvent.model_validate(
            {"event": "1994", "actor": "Commission", "predicate_object": "opened file"}
        )
        self.assertEqual(event.event, "1994")

    def test_sparse_event_defaults_instead_of_failing(self) -> None:
        """The exact row that lost 38344_5856_3 and its other 170 events."""
        event = HeidelTimeCriticalEvent.model_validate(
            {"event_date": "2002-06", "event": "2002-06"}
        )
        self.assertEqual(event.event, "2002-06")
        self.assertEqual(event.actor, "unknown")
        self.assertEqual(event.predicate_object, "unknown")

    def test_sparse_event_does_not_fail_the_whole_list(self) -> None:
        """One unattributable event costs one event, not the document."""
        result = HeidelTimeCriticalEventResult.model_validate(
            {
                "events": [
                    {"event": "1998-01-14", "actor": "A", "predicate_object": "p"},
                    {"event_date": "2002-06"},
                ]
            }
        )
        self.assertEqual(len(result.events), 2)
        self.assertEqual(result.events[1].actor, "unknown")

    def test_malformed_element_is_dropped_not_fatal(self) -> None:
        """The bare ['event'] token that ended CELEX-31994D0815 at event 133."""
        result = HeidelTimeCriticalEventResult.model_validate(
            {
                "events": [
                    {"event": "1998-01-14", "actor": "A", "predicate_object": "p"},
                    ["event"],
                    {"event": "1999", "actor": "B", "predicate_object": "q"},
                ]
            }
        )
        self.assertEqual([e.event for e in result.events], ["1998-01-14", "1999"])

    def test_malformed_element_dropped_in_bare_list_too(self) -> None:
        """Both entry shapes go through the same sanitising path."""
        result = HeidelTimeCriticalEventResult.model_validate(
            [["event"], {"event": "1999", "actor": "B", "predicate_object": "q"}]
        )
        self.assertEqual([e.event for e in result.events], ["1999"])


class TestHeideltimeCartelPeriodResult(unittest.TestCase):
    """The period keeps the precision the decision states -- no more, no less."""

    def test_full_precision_range_is_preserved(self) -> None:
        """The common case must be untouched."""
        result = HeideltimeCartelPeriodResult(date_range="1983-03-22 : 1986-08-07")
        self.assertEqual(result.date_range, "1983-03-22 : 1986-08-07")

    def test_year_only_range_is_accepted(self) -> None:
        """'from 1994 to 1999' must not be padded out to invented days."""
        result = HeideltimeCartelPeriodResult(date_range="1994 : 1999")
        self.assertEqual(result.date_range, "1994 : 1999")

    def test_mixed_precision_range_is_accepted(self) -> None:
        """Each side carries its own precision independently."""
        result = HeideltimeCartelPeriodResult(date_range="1994 : 1999-03")
        self.assertEqual(result.date_range, "1994 : 1999-03")

    def test_separator_is_normalized(self) -> None:
        """A range written without spaces still stores in the ' : ' contract."""
        result = HeideltimeCartelPeriodResult(date_range="1994-01-05:1999-03-02")
        self.assertEqual(result.date_range, "1994-01-05 : 1999-03-02")

    def test_sentinel_bounds_become_none(self) -> None:
        """The prompt's 1900/2030 search bounds are absence, not an answer."""
        for value in (
            "1900-01-01 : 1900-01-01",
            "1900-01-01 : 2030-12-31",
            "1989-10-01 : 2030-12-31",
        ):
            with self.subTest(value=value):
                self.assertIsNone(HeideltimeCartelPeriodResult(date_range=value).date_range)

    def test_reversed_range_becomes_none(self) -> None:
        """An end before its start is an extraction failure, not a period."""
        self.assertIsNone(
            HeideltimeCartelPeriodResult(date_range="1999 : 1994").date_range
        )

    def test_partial_bound_inside_its_own_year_is_not_reversed(self) -> None:
        """'1994 : 1994-06' is a real period, not a reversed one."""
        result = HeideltimeCartelPeriodResult(date_range="1994 : 1994-06")
        self.assertEqual(result.date_range, "1994 : 1994-06")

    def test_prose_answer_becomes_none(self) -> None:
        """Non-date prose reached the corpus as a stored fact on 2026-09-02."""
        self.assertIsNone(
            HeideltimeCartelPeriodResult(
                date_range="no valid cartel period found"
            ).date_range
        )

    def test_null_sentinel_strings_still_become_none(self) -> None:
        """The pre-existing null handling must survive the new validator."""
        self.assertIsNone(HeideltimeCartelPeriodResult(date_range="None").date_range)

    def test_nested_period_pair_is_assembled(self) -> None:
        """The exact shape 37956_1607_5 returned on 2026-09-05, extra keys and all."""
        result = HeideltimeCartelPeriodResult.model_validate(
            {
                "period": {"start": "1997-09-05", "end": "2019-07-04"},
                "entities": ["Alfa Acciai S.p.A.", "Feralpi Siderurgica S.R.L."],
            }
        )
        self.assertEqual(result.date_range, "1997-09-05 : 2019-07-04")

    def test_flat_start_end_pair_is_assembled(self) -> None:
        """The un-nested variant of the same mistake."""
        result = HeideltimeCartelPeriodResult.model_validate(
            {"start": "1994", "end": "1999-03"}
        )
        self.assertEqual(result.date_range, "1994 : 1999-03")

    def test_start_date_end_date_keys_are_assembled(self) -> None:
        """The keys the YAML output_schema names, which the model sometimes echoes."""
        result = HeideltimeCartelPeriodResult.model_validate(
            {"start_date": "1994-01-05", "end_date": "1999-03-02"}
        )
        self.assertEqual(result.date_range, "1994-01-05 : 1999-03-02")

    def test_declared_date_range_wins_over_pair(self) -> None:
        """A schema-compliant answer is never rewritten by the coercion."""
        result = HeideltimeCartelPeriodResult.model_validate(
            {
                "date_range": "1983-03-22 : 1986-08-07",
                "period": {"start": "1900-01-01", "end": "2030-12-31"},
            }
        )
        self.assertEqual(result.date_range, "1983-03-22 : 1986-08-07")

    def test_assembled_pair_still_passes_sentinel_rejection(self) -> None:
        """Coercion feeds the same validation, it does not bypass it."""
        result = HeideltimeCartelPeriodResult.model_validate(
            {"period": {"start": "1900-01-01", "end": "2030-12-31"}}
        )
        self.assertIsNone(result.date_range)

    def test_partial_pair_is_not_assembled(self) -> None:
        """A half-answer is absence, not a range against a missing bound."""
        result = HeideltimeCartelPeriodResult.model_validate({"start": "1994"})
        self.assertIsNone(result.date_range)


if __name__ == "__main__":
    unittest.main()
