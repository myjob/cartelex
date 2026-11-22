"""Tests for the NA fallback that infers a fact from the stored case title.

When ``products`` (or any fact declaring ``na_fallback_fact``) comes back NA
from the regular extraction, ``UniversalFactModule`` reads the stored
``case_title`` fact and asks the LLM to infer the value from it. Mocks sit at
the SQL store, searcher and LLM boundaries only.
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import (
    TitleFallbackModule,
    UniversalFactModule,
    _items_are_na,
)

_CELEX = "CELEX-32003D0570"
_TITLE_FACT_ID = uuid.uuid4()


def _params(na_fallback_fact: str | None = "case_title") -> FactExtractorParamVO:
    """Build products params as loaded from FACT_EXTRACTOR.yaml."""
    struct = {
        "target_fact_name": "products",
        "question": "list all products",
        "output_schema": "products",
        "pipeline": "universal",
    }
    if na_fallback_fact is not None:
        struct["na_fallback_fact"] = na_fallback_fact
    return FactExtractorParamVO.from_yaml_struct(
        celex_id=_CELEX, struct=struct, force_override=True
    )


def _title_row(
    value_md: str = "('COMP/38.354', 'Industrial bags')",
    value_json: str = '[{"case_title": ["COMP/38.354", "Industrial bags"]}]',
) -> SimpleNamespace:
    """Return a stand-in for a stored ``case_title`` facts row."""
    return SimpleNamespace(id=_TITLE_FACT_ID, value_md=value_md, value_json=value_json)


def _extraction(items: list) -> SimpleNamespace:
    """Wrap rows the way a DSPy ChainOfThought prediction exposes them."""
    return SimpleNamespace(output=SimpleNamespace(extracted_facts=items))


class TestItemsAreNa(unittest.TestCase):
    """Verify which extraction results count as 'not found'."""

    def test_empty_list_is_na(self) -> None:
        self.assertTrue(_items_are_na([]))

    def test_sentinel_values_are_na(self) -> None:
        self.assertTrue(_items_are_na([{"products": "NA"}, {"products": "n/a"}]))

    def test_empty_list_value_is_na(self) -> None:
        self.assertTrue(_items_are_na([{"products": []}]))

    def test_real_value_is_not_na(self) -> None:
        self.assertFalse(_items_are_na([{"products": "NA"}, {"products": "bags"}]))


class TestTitleFallbackModule(unittest.TestCase):
    """Verify the focused module that infers a fact from the case title."""

    def _module(self, title_rows: list, inferred: list) -> TitleFallbackModule:
        sql_store = MagicMock()
        sql_store.select_fact4celex.return_value = title_rows
        module = TitleFallbackModule(sql_store=sql_store)
        module.infer = MagicMock(return_value=_extraction(inferred))
        return module

    def test_infers_product_and_records_title_lineage(self) -> None:
        module = self._module([_title_row()], [{"products": "industrial bags"}])

        prediction = module(params=_params())

        self.assertEqual(prediction.output.items, [{"products": "industrial bags"}])
        self.assertEqual(prediction.output.source, "title_fallback")
        kwargs = module.infer.call_args.kwargs
        self.assertIn("Industrial bags", kwargs["case_title"])
        self.assertEqual(kwargs["schema_description"], "products")
        (lineage,) = prediction.output.lineage_sources
        self.assertEqual(lineage.source_type, "fact")
        self.assertEqual(lineage.source_fact_id, _TITLE_FACT_ID)
        self.assertEqual(lineage.relation_type, "derived_from")
        self.assertEqual(lineage.description, "fact:case_title")

    def test_missing_title_returns_none_without_llm_call(self) -> None:
        module = self._module([], [{"products": "x"}])

        self.assertIsNone(module(params=_params()))
        module.infer.assert_not_called()

    def test_na_title_returns_none_without_llm_call(self) -> None:
        module = self._module([_title_row("NA")], [{"products": "x"}])

        self.assertIsNone(module(params=_params()))
        module.infer.assert_not_called()

    def test_title_rows_all_na_returns_none_without_llm_call(self) -> None:
        row = _title_row("('NA', 'NA')", '[{"case_title": ["NA", "NA"]}]')
        module = self._module([row], [{"products": "x"}])

        self.assertIsNone(module(params=_params()))
        module.infer.assert_not_called()

    def test_title_naming_no_product_returns_none(self) -> None:
        module = self._module([_title_row()], [{"products": "NA"}])

        self.assertIsNone(module(params=_params()))


class TestUniversalModuleNaFallback(unittest.TestCase):
    """Verify UniversalFactModule hands NA results to the title fallback."""

    def _module(self, extracted: list, chunks: list | None = None) -> UniversalFactModule:
        sql_store = MagicMock()
        sql_store.select_fact4celex.return_value = []
        searcher = MagicMock()
        searcher.search.return_value = (
            chunks
            if chunks is not None
            else [SimpleNamespace(doc_pos=3, chunk="The undertakings agreed prices.")]
        )
        chunk_repo = MagicMock()
        chunk_repo.sum_document_tokens.return_value = None
        module = UniversalFactModule(
            sql_store=sql_store, searcher=searcher, chunk_repo=chunk_repo
        )
        module.extract = MagicMock(return_value=_extraction(extracted))
        module.find_doc_references = MagicMock(
            return_value=SimpleNamespace(output=SimpleNamespace(doc_positions=[3]))
        )
        module.title_fallback = MagicMock(
            return_value=SimpleNamespace(
                output=SimpleNamespace(items=[{"products": "industrial bags"}])
            )
        )
        return module

    def test_na_extraction_uses_title_fallback(self) -> None:
        module = self._module([{"products": "NA"}])

        prediction = module(params=_params())

        module.title_fallback.assert_called_once()
        self.assertEqual(prediction.output.items, [{"products": "industrial bags"}])

    def test_empty_context_uses_title_fallback(self) -> None:
        module = self._module([], chunks=[])

        prediction = module(params=_params())

        module.extract.assert_not_called()
        self.assertEqual(prediction.output.items, [{"products": "industrial bags"}])

    def test_found_products_skip_fallback(self) -> None:
        module = self._module([{"products": "paper sacks"}])

        prediction = module(params=_params())

        module.title_fallback.assert_not_called()
        self.assertEqual(prediction.output.items, [{"products": "paper sacks"}])

    def test_fact_without_fallback_key_stays_na(self) -> None:
        module = self._module([])

        prediction = module(params=_params(na_fallback_fact=None))

        module.title_fallback.assert_not_called()
        self.assertEqual(prediction.output.items, [])

    def test_unresolved_fallback_keeps_original_na(self) -> None:
        module = self._module([])
        module.title_fallback.return_value = None

        prediction = module(params=_params())

        self.assertEqual(prediction.output.items, [])


class TestParamRoundTrip(unittest.TestCase):
    """Verify the YAML key survives load and export."""

    def test_na_fallback_fact_round_trips(self) -> None:
        params = _params()

        self.assertEqual(params.na_fallback_fact, "case_title")
        self.assertEqual(params.to_yaml_struct()["na_fallback_fact"], "case_title")

    def test_default_is_none(self) -> None:
        self.assertIsNone(_params(na_fallback_fact=None).na_fallback_fact)


if __name__ == "__main__":
    unittest.main()
