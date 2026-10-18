import unittest
from unittest.mock import MagicMock
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import BookKeywordModule


class TestBookKeywordExistsModule(unittest.TestCase):
    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_book_searcher = MagicMock()
        self.module = BookKeywordModule(
            sql_store=self.mock_sql_store,
            book_searcher=self.mock_book_searcher,
        )

        self.params = FactExtractorParamVO(
            celex_id="TEST-456",
            target_fact_name="book_concept",
            question="object restriction",
            output_schema="",
            pipeline="book_keyword_exists",
        )

    def test_sql_hit_returns_true(self):
        """Cached SQL fact with value_md short-circuits to True."""
        mock_fact = MagicMock()
        mock_fact.value_json = None
        mock_fact.value_md = "Some text"
        self.mock_sql_store.select_fact4celex.return_value = [mock_fact]

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "facts")
        self.assertEqual(prediction.output.items, [{"book_concept": "True"}])
        self.mock_book_searcher.keyword_exists.assert_not_called()

    def test_sql_json_hit_returns_existing(self):
        """Cached SQL fact with value_json returns as-is."""
        mock_fact = MagicMock()
        mock_fact.value_json = [{"book_concept": "True"}]
        self.mock_sql_store.select_fact4celex.return_value = [mock_fact]

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "facts")
        self.assertEqual(prediction.output.items, [{"book_concept": "True"}])
        self.mock_book_searcher.keyword_exists.assert_not_called()


    def test_no_hits_returns_false(self):
        """When neither SQL nor book keyword finds a match, returns False."""
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_book_searcher.keyword_exists.return_value = False

        prediction = self.module.forward(self.params)

        # self.assertEqual(prediction.output.source, "book_keyword")
        self.assertEqual(prediction.output.items, [{"book_concept": "False"}])
        self.assertIsNone(prediction.output.references_doc)

    def test_force_override_skips_sql(self):
        """force_override=True bypasses the SQL cache entirely."""
        self.params.force_override = True
        self.mock_book_searcher.keyword_exists.return_value = False

        prediction = self.module.forward(self.params)

        self.mock_sql_store.select_fact4celex.assert_not_called()
        self.assertEqual(prediction.output.source, "book_keyword")
        self.assertEqual(prediction.output.items, [{"book_concept": "False"}])


if __name__ == "__main__":
    unittest.main()
