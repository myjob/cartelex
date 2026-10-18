"""Unit tests for ``InfringementConceptModule``.

The normalizer math is covered by
``pipelines/legal/tests/test_infringement_concept.py``; these tests focus on the
module: reading inputs from the store, shaping the ``FactOutput``, the empty
case, and the ``force_override`` short-circuit. The store is a fake injected at
the boundary (no DB, no LLM).
"""

import unittest
from types import SimpleNamespace

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import InfringementConceptModule

_TARGET = "infringement_concept"


class _FakeStore:
    """Minimal ``select_fact4celex`` stub returning per-name fact rows."""

    def __init__(self, facts: dict[str, list]):
        self._facts = facts

    def select_fact4celex(self, celex_id: str, fact_name: str) -> list:
        return self._facts.get(fact_name, [])


def _row(value_md: str | None = None, value_json=None) -> SimpleNamespace:
    """A stand-in fact row carrying ``value_md`` / ``value_json``."""
    return SimpleNamespace(value_md=value_md, value_json=value_json)


def _params(celex_id: str, force_override: bool = True) -> FactExtractorParamVO:
    """Build extraction params for the infringement_concept fact."""
    return FactExtractorParamVO(
        target_fact_name=_TARGET,
        question="Canonical EU infringement concept(s)",
        celex_id=celex_id,
        output_schema="",
        output_type="str",
        pipeline="infringement_concept",
        force_override=force_override,
    )


class TestInfringementConceptModule(unittest.TestCase):
    """The module derives the persisted concept fact from stored inputs."""

    def test_modern_numbering_old_date_resolves_period_correct(self):
        """Decca: '102' text + 1988 date -> period-correct concepts.

        The literal token is the modern '102', but the decision date yields the
        treaty-era-correct concept set (sorted, comma-joined into value_md).
        """
        store = _FakeStore(
            {
                "law_article_all_relevant": [_row("Article 101, Article 102")],
                "eu_commisssion_decision_date": [_row("1988-12-21")],
            }
        )
        module = InfringementConceptModule(sql_store=store)

        out = module.forward(_params("CELEX-31988D0000")).output

        self.assertEqual(
            out.items,
            [{_TARGET: "abuse_of_dominance, anticompetitive_agreement"}],
        )
        self.assertEqual(out.source, "infringement_concept")
        self.assertIsNone(out.references_doc)

    def test_na_date_uses_celex_year_fallback(self):
        """An NA date resolves the ambiguous '86' via the CELEX year."""
        eec_store = _FakeStore(
            {
                "law_article_all_relevant": [_row("Article 86")],
                "eu_commisssion_decision_date": [_row("NA")],
            }
        )
        out = InfringementConceptModule(sql_store=eec_store).forward(
            _params("CELEX-31988D0000")
        ).output
        self.assertEqual(out.items, [{_TARGET: "abuse_of_dominance"}])

        ec_store = _FakeStore(
            {
                "law_article_all_relevant": [_row("Article 86")],
                "eu_commisssion_decision_date": [_row("NA")],
            }
        )
        out = InfringementConceptModule(sql_store=ec_store).forward(
            _params("CELEX-32003D0000")
        ).output
        self.assertEqual(out.items, [{_TARGET: "public_undertaking"}])

    def test_no_competition_article_yields_empty(self):
        """A document with no competition article emits no concept row."""
        store = _FakeStore(
            {
                "law_article_all_relevant": [_row("Article 3")],
                "eu_commisssion_decision_date": [_row("NA")],
            }
        )
        out = InfringementConceptModule(sql_store=store).forward(
            _params("CELEX-32003D0000")
        ).output
        self.assertEqual(out.items, [])

    def test_missing_article_fact_does_not_raise(self):
        """A missing article input is read as empty, never raises."""
        store = _FakeStore({})
        out = InfringementConceptModule(sql_store=store).forward(
            _params("CELEX-32003D0000")
        ).output
        self.assertEqual(out.items, [])

    def test_force_override_false_short_circuits_existing(self):
        """Without force_override an existing JSON fact is returned unchanged."""
        existing = [_row(value_json=[{_TARGET: "abuse_of_dominance"}])]
        store = _FakeStore({_TARGET: existing})
        module = InfringementConceptModule(sql_store=store)

        out = module.forward(
            _params("CELEX-31988D0000", force_override=False)
        ).output

        self.assertEqual(out.items, [{_TARGET: "abuse_of_dominance"}])


if __name__ == "__main__":
    unittest.main()
