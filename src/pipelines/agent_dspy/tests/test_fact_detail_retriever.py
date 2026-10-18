"""Unit tests for FactDetailRetriever (pipeline: detail_retriever).

Mocks are injected only at the system boundaries (SQL store, search legs,
DSPy predictor) so the tests are runnable in isolation.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from config.settings import settings
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import FactDetailRetriever


def _chunk(doc_pos: int, text: str) -> SimpleNamespace:
    """Build a minimal ranked-chunk stand-in (only .doc_pos/.chunk are read)."""
    return SimpleNamespace(doc_pos=doc_pos, chunk=text)


def _roster_fact(value_json, value_md: str = "| companies |\n| A |") -> MagicMock:
    """Build a fake Facts ORM row for the roster predecessor."""
    fact = MagicMock()
    fact.value_json = value_json
    fact.value_md = value_md
    return fact


class TestFactDetailRetriever(unittest.TestCase):
    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_keyword_search = MagicMock()
        self.mock_hybrid_search = MagicMock()
        self.module = FactDetailRetriever(
            sql_store=self.mock_sql_store,
            keyword_search=self.mock_keyword_search,
            hybrid_search=self.mock_hybrid_search,
        )
        self.mock_extract = MagicMock()
        self.module.extract = self.mock_extract

        self.params = FactExtractorParamVO(
            celex_id="TEST-123",
            target_fact_name="companies_address",
            question="What is the registered address of this company ?",
            output_schema="companies, address",
            pipeline="detail_retriever",
            predecessors=["companies_investigated"],
        )

    def _wire_roster(self, roster_rows, target_facts=None):
        """Route select_fact4celex: [] for the target fact, roster otherwise."""

        def side_effect(celex_id, fact_name):
            if fact_name == "companies_investigated":
                return roster_rows
            if fact_name == "companies_address":
                return target_facts or []
            return []

        self.mock_sql_store.select_fact4celex.side_effect = side_effect

    def _extract_returns(self, dicts_per_call):
        """Queue extraction results, one list of dicts per per-entity call."""
        predictions = []
        for extracted in dicts_per_call:
            pred = MagicMock()
            pred.output.extracted_facts = extracted
            predictions.append(pred)
        self.mock_extract.side_effect = predictions

    def test_happy_path_two_entities(self):
        self._wire_roster(
            [
                _roster_fact(
                    [{"companies": "Anheuser-Busch"}, {"companies": "Scottish & Newcastle"}]
                )
            ]
        )
        self.mock_keyword_search.search.side_effect = [
            [_chunk(3, "AB address text"), _chunk(5, "more")],
            [_chunk(7, "SN address text")],
        ]
        self._extract_returns(
            [
                [{"companies": "Anheuser-Busch", "address": "One Busch Place"}],
                [{"companies": "Scottish & Newcastle", "address": "50 East Fettes Avenue"}],
            ]
        )

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "detail_retriever")
        self.assertEqual(
            prediction.output.items,
            [
                {"companies": "Anheuser-Busch", "address": "One Busch Place"},
                {"companies": "Scottish & Newcastle", "address": "50 East Fettes Avenue"},
            ],
        )
        self.assertEqual(prediction.output.references_doc, [3, 7])
        self.assertEqual(self.mock_extract.call_count, 2)
        entities_passed = [
            call.kwargs["entity"] for call in self.mock_extract.call_args_list
        ]
        self.assertEqual(entities_passed, ["Anheuser-Busch", "Scottish & Newcastle"])
        self.mock_hybrid_search.search.assert_not_called()

    def test_keyword_empty_falls_back_to_hybrid(self):
        params = self.params.model_copy(update={"section_scope": ["parties"]})
        self._wire_roster([_roster_fact([{"companies": "Interbrew"}])])
        self.mock_keyword_search.search.return_value = []
        self.mock_hybrid_search.search.return_value = [_chunk(2, "Interbrew text")]
        self._extract_returns([[{"companies": "Interbrew", "address": "Brussels"}]])

        prediction = self.module.forward(params)

        self.assertEqual(
            prediction.output.items, [{"companies": "Interbrew", "address": "Brussels"}]
        )
        kwargs = self.mock_hybrid_search.search.call_args.kwargs
        self.assertIn("Interbrew", kwargs["query"])
        self.assertIn(params.question, kwargs["query"])
        self.assertEqual(kwargs["top_k"], settings.DETAIL_RETRIEVER_TOP_K)
        self.assertEqual(kwargs["section_types"], ["parties"])
        keyword_kwargs = self.mock_keyword_search.search.call_args.kwargs
        self.assertEqual(keyword_kwargs["query"], "Interbrew")
        self.assertEqual(keyword_kwargs["top_k"], settings.DETAIL_RETRIEVER_TOP_K)
        self.assertEqual(keyword_kwargs["section_types"], ["parties"])

    def test_no_chunks_yields_na_row(self):
        self._wire_roster([_roster_fact([{"companies": "Ghost Corp"}])])
        self.mock_keyword_search.search.return_value = []
        self.mock_hybrid_search.search.return_value = []

        prediction = self.module.forward(self.params)

        self.assertEqual(
            prediction.output.items, [{"companies": "Ghost Corp", "address": "NA"}]
        )
        self.assertIsNone(prediction.output.references_doc)
        self.mock_extract.assert_not_called()

    def test_existing_target_fact_short_circuits(self):
        existing = MagicMock()
        existing.value_json = [{"companies": "Cached Co", "address": "Cached Street"}]
        self._wire_roster([], target_facts=[existing])

        prediction = self.module.forward(self.params)

        self.assertEqual(
            prediction.output.items,
            [{"companies": "Cached Co", "address": "Cached Street"}],
        )
        self.assertEqual(prediction.output.source, "detail_retriever")
        self.mock_keyword_search.search.assert_not_called()
        self.mock_hybrid_search.search.assert_not_called()
        self.mock_extract.assert_not_called()

    def test_missing_roster_raises(self):
        self._wire_roster([])

        with self.assertRaises(ValueError) as context:
            self.module.forward(self.params)

        self.assertEqual(
            str(context.exception),
            "Missing prerequisite fact: 'companies_investigated'",
        )
        self.mock_extract.assert_not_called()

    def test_na_roster_short_circuits_to_empty(self):
        self._wire_roster([_roster_fact(None, value_md="NA")])

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.items, [])
        self.assertEqual(prediction.output.source, "detail_retriever")
        self.mock_extract.assert_not_called()

    def test_llm_extra_and_missing_keys_are_normalized(self):
        params = self.params.model_copy(
            update={"output_schema": "companies, address, country"}
        )
        self._wire_roster([_roster_fact([{"companies": "Interbrew"}])])
        self.mock_keyword_search.search.return_value = [_chunk(1, "text")]
        # LLM returns an extra key, a wrong entity name, and no country.
        self._extract_returns(
            [[{"companies": "INTERBREW SA", "address": "Brussels", "Role": "brewer"}]]
        )

        prediction = self.module.forward(params)

        self.assertEqual(
            prediction.output.items,
            [{"companies": "Interbrew", "address": "Brussels", "country": "NA"}],
        )

    def test_placeholder_detail_values_become_na(self):
        self._wire_roster([_roster_fact([{"companies": "Interbrew"}])])
        self.mock_keyword_search.search.return_value = [_chunk(1, "text")]
        self._extract_returns([[{"companies": "Interbrew", "address": "None"}]])

        prediction = self.module.forward(self.params)

        self.assertEqual(
            prediction.output.items, [{"companies": "Interbrew", "address": "NA"}]
        )

    def test_no_predecessors_raises(self):
        self._wire_roster([])
        params = self.params.model_copy(update={"predecessors": []})

        with self.assertRaises(ValueError):
            self.module.forward(params)

    def test_roster_without_value_json_raises(self):
        self._wire_roster([_roster_fact(None)])

        with self.assertRaises(ValueError) as context:
            self.module.forward(self.params)

        self.assertIn("no structured value_json", str(context.exception))

    def test_roster_value_json_string_is_parsed(self):
        self._wire_roster(
            [_roster_fact('[{"companies": "Interbrew"}, {"companies": "Interbrew"}]')]
        )
        self.mock_keyword_search.search.return_value = [_chunk(4, "text")]
        self._extract_returns([[{"companies": "Interbrew", "address": "Brussels"}]])

        prediction = self.module.forward(self.params)

        # Duplicate roster entries are deduplicated: one row, one extract call.
        self.assertEqual(
            prediction.output.items, [{"companies": "Interbrew", "address": "Brussels"}]
        )
        self.assertEqual(self.mock_extract.call_count, 1)

    def test_references_doc_deduped_and_capped_at_three(self):
        roster = [{"companies": name} for name in ["A", "B", "C", "D"]]
        self._wire_roster([_roster_fact(roster)])
        self.mock_keyword_search.search.side_effect = [
            [_chunk(1, "a")],
            [_chunk(1, "b")],
            [_chunk(2, "c")],
            [_chunk(3, "d")],
        ]
        self._extract_returns(
            [[{"companies": name, "address": f"{name} street"}] for name in
             ["A", "B", "C", "D"]]
        )

        prediction = self.module.forward(self.params)

        self.assertEqual(len(prediction.output.items), 4)
        self.assertEqual(prediction.output.references_doc, [1, 2, 3])

    def test_empty_output_schema_raises(self):
        params = self.params.model_copy(update={"output_schema": "companies"})
        self._wire_roster([_roster_fact([{"companies": "Interbrew"}])])

        with self.assertRaises(ValueError) as context:
            self.module.forward(params)

        self.assertIn("output_schema", str(context.exception))


if __name__ == "__main__":
    unittest.main()
