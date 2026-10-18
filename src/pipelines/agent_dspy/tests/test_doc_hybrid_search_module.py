import unittest
from unittest.mock import MagicMock
from config.settings import settings
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import DocHybridSearchModule
from extract.rag.models.chunk_vo import RankedChunkVo

class TestDocHybridSearchModule(unittest.TestCase):
    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_searcher = MagicMock()
        self.module = DocHybridSearchModule(sql_store=self.mock_sql_store, searcher=self.mock_searcher)

        self.params = FactExtractorParamVO(
            celex_id="TEST-123",
            target_fact_name="leniency_case",
            question="Did companies apply for leniency?",
            output_schema="",
            pipeline="hybrid_search"
        )

    def test_sql_hit_returns_true(self):
        # Arrange
        mock_fact = MagicMock()
        mock_fact.value_json = None
        mock_fact.value_md = "Some text indicating presence"
        self.mock_sql_store.select_fact4celex.return_value = [mock_fact]

        # Act
        prediction = self.module.forward(self.params)

        # Assert
        self.assertEqual(prediction.output.source, "facts")
        self.assertEqual(prediction.output.items, [{"leniency_case": "True"}])
        self.mock_searcher.search.assert_not_called()

    def test_sql_json_hit_returns_existing(self):
        # Arrange
        mock_fact = MagicMock()
        mock_fact.value_json = [{"leniency_case": "True"}]
        self.mock_sql_store.select_fact4celex.return_value = [mock_fact]

        # Act
        prediction = self.module.forward(self.params)

        # Assert
        self.assertEqual(prediction.output.source, "facts")
        self.assertEqual(prediction.output.items, [{"leniency_case": "True"}])
        self.mock_searcher.search.assert_not_called()

    def test_searcher_hit_returns_true_with_docs(self):
        # Arrange
        self.mock_sql_store.select_fact4celex.return_value = []
        
        mock_chunk = RankedChunkVo(
            id="chunk-42",
            celex_id="TEST-123",
            origin="test_source",
            doc_pos=42,
            chunk="Company X applied for leniency.",
            similarity=0.5,
            score=0.9
        )
        self.mock_searcher.search.return_value = [mock_chunk]

        # Act
        prediction = self.module.forward(self.params)

        # Assert
        self.assertEqual(prediction.output.source, "hybrid")
        self.assertEqual(prediction.output.items, [{"leniency_case": "True"}])
        self.assertEqual(prediction.output.references_doc, [42])
        self.mock_searcher.search.assert_called_once_with(
            celex_id="TEST-123",
            query="Did companies apply for leniency?",
            top_k=settings.DOC_SEARCH_TOP_K,
            section_types=None,
        )

    def test_no_hits_returns_empty(self):
        # Arrange
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_searcher.search.return_value = []

        # Act
        prediction = self.module.forward(self.params)

        # Assert
        self.assertEqual(prediction.output.source, "hybrid")
        self.assertEqual(prediction.output.items, [])
        self.assertIsNone(prediction.output.references_doc)

    def test_force_override_skips_sql(self):
        # Arrange
        self.params.force_override = True
        self.mock_searcher.search.return_value = []

        # Act
        prediction = self.module.forward(self.params)

        # Assert
        self.mock_sql_store.select_fact4celex.assert_not_called()
        self.assertEqual(prediction.output.source, "hybrid")
        self.assertEqual(prediction.output.items, [])

if __name__ == '__main__':
    unittest.main()
