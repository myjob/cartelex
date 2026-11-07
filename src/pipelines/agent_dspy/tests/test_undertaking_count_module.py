"""Unit tests for ``UndertakingCountModule`` (fake store, no DB, no LLM)."""

import json
import unittest
from types import SimpleNamespace

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import UndertakingCountModule

_TARGET = "number_undertaking"
_CELEX = "CELEX-31995D0551"


class _FakeStore:
    """Minimal ``select_fact4celex`` stub returning per-name fact rows."""

    def __init__(self, facts: dict[str, list]):
        self._facts = facts

    def select_fact4celex(self, celex_id: str, fact_name: str) -> list:
        return self._facts.get(fact_name, [])


def _row(value_json: str | None) -> SimpleNamespace:
    """A stand-in fact row carrying ``value_json``."""
    return SimpleNamespace(value_md=None, value_json=value_json)


def _params(force_override: bool = True) -> FactExtractorParamVO:
    """Build extraction params for the number_undertaking fact."""
    return FactExtractorParamVO(
        target_fact_name=_TARGET,
        question="Number of distinct legal units",
        celex_id=_CELEX,
        output_schema="",
        output_type="dict",
        pipeline="undertaking_count",
        force_override=force_override,
    )


class TestUndertakingCountModule(unittest.TestCase):
    """The module derives the count from the latest stored roster."""

    def test_counts_latest_roster_deduplicated(self):
        stale = _row(json.dumps([{"companies": "Old Co"}]))
        latest = _row(
            json.dumps(
                [
                    {"companies": "Akzo Chemicals BV", "location": "NA"},
                    {"companies": "Akzo Chemicals", "location": "NA"},
                    {"companies": "Solvay SA", "location": "Brussels"},
                ]
            )
        )
        module = UndertakingCountModule(sql_store=_FakeStore({"companies_investigated": [stale, latest]}))

        out = module.forward(_params()).output

        self.assertEqual(
            out.items,
            [
                {
                    "number_undertaking": 2,
                    "undertakings": ["Akzo Chemicals BV", "Solvay SA"],
                    "roster_rows": 3,
                }
            ],
        )
        self.assertEqual(out.source, "undertaking_count")

    def test_missing_roster_emits_no_items(self):
        module = UndertakingCountModule(sql_store=_FakeStore({}))
        self.assertEqual(module.forward(_params()).output.items, [])

    def test_placeholder_only_roster_emits_no_items(self):
        store = _FakeStore({"companies_investigated": [_row(json.dumps([{"companies": "NA"}]))]})
        module = UndertakingCountModule(sql_store=store)
        self.assertEqual(module.forward(_params()).output.items, [])

    def test_existing_fact_short_circuits_without_force(self):
        stored = [{"number_undertaking": 5, "undertakings": [], "roster_rows": 5}]
        store = _FakeStore({_TARGET: [_row(stored)]})
        module = UndertakingCountModule(sql_store=store)

        out = module.forward(_params(force_override=False)).output

        self.assertEqual(out.items, stored)


if __name__ == "__main__":
    unittest.main()
