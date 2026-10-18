import unittest
from unittest.mock import MagicMock

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import CelexCaseIdModule


class TestCelexCaseIdModule(unittest.TestCase):
    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.module = CelexCaseIdModule(sql_store=self.mock_sql_store)

        self.params = FactExtractorParamVO(
            celex_id="TEST-CELEX-001",
            target_fact_name="celex_case_id",
            question="ingest celex_id and case_id",
            output_schema="",
            pipeline="celex_case_id",
        )

    def test_sql_cache_hit_returns_cached_fact(self):
        """When a cached fact with value_json exists, return it without a DB lookup."""
        mock_fact = MagicMock()
        mock_fact.value_json = [{"celex_id": "TEST-CELEX-001", "case_id": "AT_999"}]
        self.mock_sql_store.select_fact4celex.return_value = [mock_fact]

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "facts")
        self.assertEqual(
            prediction.output.items,
            [{"celex_id": "TEST-CELEX-001", "case_id": "AT_999"}],
        )
        self.mock_sql_store.select_case_ids_for_celex.assert_not_called()

    def test_lookup_returns_one_item_per_case_id(self):
        """Cache miss: select_case_ids_for_celex results are mapped to dicts."""
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_sql_store.select_case_ids_for_celex.return_value = [
            "AT_123",
            "AT_456",
        ]

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "sql_lookup")
        self.assertEqual(
            prediction.output.items,
            [
                {"celex_id": "TEST-CELEX-001", "case_id": "AT_123"},
                {"celex_id": "TEST-CELEX-001", "case_id": "AT_456"},
            ],
        )
        self.assertIsNone(prediction.output.references_doc)
        self.mock_sql_store.select_case_ids_for_celex.assert_called_once_with(
            "TEST-CELEX-001"
        )

    def test_empty_result_when_no_case_ids(self):
        """Cache miss with no case_ids in DB returns empty items list."""
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_sql_store.select_case_ids_for_celex.return_value = []

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "sql_lookup")
        self.assertEqual(prediction.output.items, [])

    def test_force_override_skips_cache(self):
        """force_override=True bypasses the SQL cache check."""
        self.params.force_override = True
        self.mock_sql_store.select_case_ids_for_celex.return_value = ["AT_789"]

        prediction = self.module.forward(self.params)

        self.mock_sql_store.select_fact4celex.assert_not_called()
        self.assertEqual(
            prediction.output.items,
            [{"celex_id": "TEST-CELEX-001", "case_id": "AT_789"}],
        )


if __name__ == "__main__":
    unittest.main()
