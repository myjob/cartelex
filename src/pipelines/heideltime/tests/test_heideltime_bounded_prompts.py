"""Bounded prompts for the HeidelTime aggregate modules (#186, #188).

The store and the DSPy predictors are faked at the boundary (no DB, no LLM);
the digest, batching and token guard run for real.
"""

import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from config.settings import settings
from pipelines.agent_dspy.dspy_signature import (
    HeidelTimeCriticalEvent,
    HeidelTimeCriticalEventResult,
    HeideltimeCartelPeriodResult,
)
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.heideltime.heideltime_module import (
    HeidelTimeEventAggregateModule,
    HeideltimeCartelPeriodModule,
)
from pipelines.heideltime.period_inheritance import (
    InheritanceBasis,
    InheritedPeriod,
    PeriodDonor,
)

_CELEX = "TEST-HT-BOUNDED"


class _FakeInheritor:
    """``PeriodInheritor`` stub recording each lookup and serving one answer."""

    def __init__(self, answer: InheritedPeriod | None):
        self.calls: list[str] = []
        self._answer = answer

    def find(self, celex_id: str, fact_name: str) -> InheritedPeriod | None:
        self.calls.append(celex_id)
        return self._answer


class _FakeStore:
    """``select_fact4celex`` stub serving one ``heideltime_event`` payload."""

    def __init__(self, rows: list[dict]):
        self._payload = json.dumps(rows)

    def select_fact4celex(self, celex_id: str, fact_name: str) -> list:
        if fact_name == "heideltime_event":
            return [SimpleNamespace(value_json=self._payload, value_md=None)]
        return []


class _FakePredictor:
    """Records every prompt and answers with a canned result per call."""

    def __init__(self, answer):
        self.prompts: list[str] = []
        self._answer = answer

    def __call__(self, heideltime_event_result: str):
        self.prompts.append(heideltime_event_result)
        return SimpleNamespace(output=self._answer(heideltime_event_result))


def _rows(count: int, start_year: int = 1990) -> list[dict]:
    """``count`` real events, one per month from ``start_year``, plus filler."""
    rows = [
        {
            "event_date": f"{start_year + i // 12}-{i % 12 + 1:02d}-15",
            "actor": "redacted",
            "predicate_object": f"attended cartel meeting number {i}",
        }
        for i in range(count)
    ]
    rows.append({"event_date": "1990-01-01", "actor": "redacted", "predicate_object": "no activity described in text"})
    rows.append({"event_date": "0010-01-02", "actor": "Akzo", "predicate_object": "met"})
    return rows


def _first_and_last_of(prompt: str) -> HeidelTimeCriticalEventResult:
    """Answer an aggregate prompt with its first and last line as events."""
    lines = prompt.splitlines()
    events = []
    for line in (lines[0], lines[-1]):
        date, actor, predicate = line.split(" | ")
        events.append(HeidelTimeCriticalEvent(event=date, actor=actor, predicate_object=predicate))
    return HeidelTimeCriticalEventResult(events=events)


def _params(name: str, pipeline: str) -> FactExtractorParamVO:
    return FactExtractorParamVO(
        target_fact_name=name,
        question="q",
        celex_id=_CELEX,
        output_schema="event, actor, predicate_object",
        pipeline=pipeline,
        force_override=True,
    )


class TestAggregateModuleBoundedInput(unittest.TestCase):
    """``heideltime_event_agg`` never sends the raw fact or an unbounded prompt."""

    def _module(self, rows: list[dict]) -> tuple[HeidelTimeEventAggregateModule, _FakePredictor]:
        module = HeidelTimeEventAggregateModule(sql_store=_FakeStore(rows))
        module.aggregate = _FakePredictor(_first_and_last_of)
        return module, module.aggregate

    def test_small_input_is_one_call_on_the_digest(self):
        module, fake = self._module(_rows(5))
        module.forward(_params("heideltime_event_agg", "heideltime_event_agg"))
        self.assertEqual(len(fake.prompts), 1)
        prompt = fake.prompts[0]
        self.assertTrue(prompt.startswith("1990-01-15 | redacted | attended cartel meeting number 0"))
        self.assertNotIn("no activity", prompt)
        self.assertNotIn("0010", prompt)
        self.assertNotIn("{", prompt)

    def test_large_input_is_map_reduced_keeping_both_ends(self):
        module, fake = self._module(_rows(120))
        with patch.object(settings, "HEIDELTIME_AGG_BATCH_TOKENS", 400):
            out = module.forward(_params("heideltime_event_agg", "heideltime_event_agg")).output
        self.assertGreater(len(fake.prompts), 2)
        reduce_prompt = fake.prompts[-1]
        self.assertIn("meeting number 0", reduce_prompt)
        self.assertIn("meeting number 119", reduce_prompt)
        self.assertEqual(out.items[0]["event"], "1990-01-15")
        self.assertEqual(out.items[-1]["event"], "1999-12-15")

    def test_prompt_above_the_token_limit_fails_loudly(self):
        module, fake = self._module(_rows(120))
        with (
            patch.object(settings, "HEIDELTIME_AGG_BATCH_TOKENS", 100_000),
            patch.object(settings, "OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT", 200),
        ):
            with self.assertRaises(ValueError):
                module.forward(_params("heideltime_event_agg", "heideltime_event_agg"))
        self.assertEqual(fake.prompts, [])

    def test_no_real_events_makes_no_call(self):
        module, fake = self._module(_rows(0))
        out = module.forward(_params("heideltime_event_agg", "heideltime_event_agg")).output
        self.assertEqual((fake.prompts, out.items), ([], []))


class TestCartelPeriodModuleBoundedInput(unittest.TestCase):
    """``cartel_period`` (pipeline ``heideltime_cartel_period``) decides the period over conduct events only."""

    def _module(self, rows: list[dict], conduct_answer, inherited: InheritedPeriod | None = None) -> tuple:
        module = HeideltimeCartelPeriodModule(sql_store=_FakeStore(rows), inheritor=_FakeInheritor(inherited))
        module.select_conduct = _FakePredictor(conduct_answer)
        module.extract_period = _FakePredictor(lambda _: HeideltimeCartelPeriodResult(date_range="1990-01 : 1999-12"))
        return module, module.select_conduct, module.extract_period

    def test_small_input_is_one_period_call(self):
        module, conduct, period = self._module(_rows(5), _first_and_last_of)
        out = module.forward(_params("cartel_period", "heideltime_cartel_period")).output
        self.assertEqual((len(conduct.prompts), len(period.prompts)), (0, 1))
        self.assertEqual(out.items, [{"date_range": "1990-01 : 1999-12"}])

    def test_large_input_selects_conduct_per_batch_then_one_period_call(self):
        module, conduct, period = self._module(_rows(120), _first_and_last_of)
        with patch.object(settings, "HEIDELTIME_AGG_BATCH_TOKENS", 400):
            module.forward(_params("cartel_period", "heideltime_cartel_period"))
        self.assertGreater(len(conduct.prompts), 1)
        self.assertEqual(len(period.prompts), 1)
        self.assertEqual(len(period.prompts[0].splitlines()), 2 * len(conduct.prompts))

    def test_conduct_selection_is_capped_to_earliest_and_latest_per_batch(self):
        """A model that copies a whole batch back must not unbound the period prompt.

        Canary 20261104: asking for every conduct event made the model echo
        ~6.5k tokens per batch and time out at 150s on all 12 oversized docs.
        """
        def echo_batch(prompt):
            events = []
            for line in prompt.splitlines():
                date, actor, predicate = line.split(" | ")
                events.append(HeidelTimeCriticalEvent(event=date, actor=actor, predicate_object=predicate))
            return HeidelTimeCriticalEventResult(events=list(reversed(events)))

        module, conduct, period = self._module(_rows(120), echo_batch)
        with patch.object(settings, "HEIDELTIME_AGG_BATCH_TOKENS", 400):
            module.forward(_params("cartel_period", "heideltime_cartel_period"))
        period_lines = period.prompts[0].splitlines()
        self.assertEqual(len(period_lines), 2 * len(conduct.prompts))
        self.assertTrue(period_lines[0].startswith("1990-01-15"))
        self.assertTrue(period_lines[-1].startswith("1999-12-15"))

    def test_no_conduct_events_in_any_batch_means_no_period(self):
        module, conduct, period = self._module(_rows(120), lambda _: HeidelTimeCriticalEventResult(events=[]))
        with patch.object(settings, "HEIDELTIME_AGG_BATCH_TOKENS", 400):
            out = module.forward(_params("cartel_period", "heideltime_cartel_period")).output
        self.assertEqual((period.prompts, out.items), ([], []))


class TestCartelPeriodInheritanceFallback(unittest.TestCase):
    """A follow-up decision without its own period borrows the original's."""

    _DONOR = PeriodDonor(
        fact_id=uuid.UUID("00000000-0000-0000-0000-000000039168"),
        celex_id="39168_757_6",
        date_range="1977-01-15 : 2003-01-13",
        gate="True",
        case_numbers={"39168"},
    )

    def _module(self, period_answer: str | None, inherited: InheritedPeriod | None):
        inheritor = _FakeInheritor(inherited)
        module = HeideltimeCartelPeriodModule(sql_store=_FakeStore(_rows(5)), inheritor=inheritor)
        module.select_conduct = _FakePredictor(_first_and_last_of)
        module.extract_period = _FakePredictor(lambda _: HeideltimeCartelPeriodResult(date_range=period_answer))
        return module, inheritor

    def _inherited(self, basis: InheritanceBasis) -> InheritedPeriod:
        return InheritedPeriod(date_range=self._DONOR.date_range, basis=basis, donors=[self._DONOR])

    def test_null_period_falls_back_to_the_original_decision(self):
        module, inheritor = self._module(None, self._inherited(InheritanceBasis.SINGLE_SIBLING))
        out = module.forward(_params("cartel_period", "heideltime_cartel_period")).output
        self.assertEqual(out.items, [{"date_range": "1977-01-15 : 2003-01-13"}])
        self.assertEqual(out.source, "period_inheritance")
        self.assertEqual(inheritor.calls, [_CELEX])
        (link,) = out.lineage_sources
        self.assertEqual(
            (link.source_type, link.celex_id, link.source_fact_id, link.relation_type, link.extraction_method),
            ("fact", "39168_757_6", self._DONOR.fact_id, "derived_from", "period_inheritance"),
        )
        self.assertIsNone(link.trust_rank)

    def test_union_is_linked_with_the_lower_trust_rank(self):
        module, _ = self._module(None, self._inherited(InheritanceBasis.SIBLING_UNION))
        out = module.forward(_params("cartel_period", "heideltime_cartel_period")).output
        self.assertEqual(out.lineage_sources[0].trust_rank, settings.PERIOD_INHERITANCE_UNION_TRUST_RANK)

    def test_own_period_wins_and_skips_inheritance(self):
        module, inheritor = self._module("1990-01 : 1999-12", self._inherited(InheritanceBasis.SINGLE_SIBLING))
        out = module.forward(_params("cartel_period", "heideltime_cartel_period")).output
        self.assertEqual(out.items, [{"date_range": "1990-01 : 1999-12"}])
        self.assertEqual(inheritor.calls, [])

    def test_no_donor_keeps_the_empty_answer(self):
        module, _ = self._module(None, None)
        out = module.forward(_params("cartel_period", "heideltime_cartel_period")).output
        self.assertEqual((out.items, out.source, out.lineage_sources), ([], "none", None))


if __name__ == "__main__":
    unittest.main()
