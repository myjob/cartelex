import unittest
from unittest.mock import MagicMock, patch

import dspy
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import FactCaseTitleModule
from pipelines.agent_dspy.dspy_protocols import FirstPageVO
from pipelines.agent_dspy.dspy_signature import FactCaseTitleOutput


class TestFactCaseTitleModule(unittest.TestCase):
    def setUp(self):
        self.mock_sql_store = MagicMock()
        
        # We need to patch dspy.Predict since it gets instantiated in __init__
        with patch('dspy.Predict') as MockPredict:
            self.mock_extract = MagicMock()
            MockPredict.return_value = self.mock_extract
            self.module = FactCaseTitleModule(sql_store=self.mock_sql_store)

        self.params = FactExtractorParamVO(
            celex_id="TEST-CELEX-001",
            target_fact_name="case_title",
            question="Extract case title",
            output_schema="",
            pipeline="case_title",
        )

    def test_sql_cache_hit_returns_cached_fact(self):
        """When a cached fact exists, return it without a DB/LLM lookup."""
        mock_fact = MagicMock()
        mock_fact.value_json = [{"case_title": ["COMP/B-2/37.766", "Dutch beer market"]}]
        self.mock_sql_store.select_fact4celex.return_value = [mock_fact]

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "facts")
        self.assertEqual(
            prediction.output.items,
            [{"case_title": ["COMP/B-2/37.766", "Dutch beer market"]}],
        )
        self.mock_sql_store.select_first_page4celex.assert_not_called()

    def test_lookup_returns_none_when_no_first_page(self):
        """If SQL query for first page returns None, module returns empty items."""
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_sql_store.select_first_page4celex.return_value = None

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "none")
        self.assertEqual(prediction.output.items, [])
        self.mock_sql_store.select_first_page4celex.assert_called_once_with(
            "TEST-CELEX-001"
        )
        self.mock_extract.assert_not_called()

    def test_successful_extraction(self):
        """When first page is found, DSPy is called and returns the title."""
        document = "Some document text with Case no. COMP/B-2/37.766 – Dutch beer market"
        chunk_id = "11111111-2222-3333-4444-555555555555"
        self.mock_sql_store.select_fact4celex.return_value = []
        self.mock_sql_store.select_first_page4celex.return_value = FirstPageVO(
            chunk_id=chunk_id, doc_pos=0, document=document
        )

        mock_extraction = MagicMock()
        mock_extraction.output = FactCaseTitleOutput(case_id="COMP/B-2/37.766", title="Dutch beer market")
        self.mock_extract.return_value = mock_extraction

        prediction = self.module.forward(self.params)

        self.assertEqual(prediction.output.source, "sql_doc_pos_0")
        self.assertEqual(
            prediction.output.items,
            [{"case_title": ("COMP/B-2/37.766", "Dutch beer market")}],
        )
        self.assertEqual(prediction.output.references_doc, [0])
        # Explicit lineage is emitted from the real first-page chunk identity.
        self.assertIsNotNone(prediction.output.lineage_sources)
        self.assertEqual(len(prediction.output.lineage_sources), 1)
        lineage = prediction.output.lineage_sources[0]
        self.assertEqual(lineage.source_type, "chunk")
        self.assertEqual(lineage.chunk_id, chunk_id)
        self.assertEqual(lineage.chunk_seq, [0])
        self.assertEqual(lineage.extraction_method, "case_title")
        self.mock_extract.assert_called_once_with(context=document)

class TestFactCaseTitleModuleIntegration(unittest.TestCase):
    def setUp(self):
        from config.llm_thinking import thinking_kwargs
        from config.settings import settings
        from tools.celex_tools import CelexDatabaseToolkit
        
        model_name = f"openai/{settings.LLM_MODEL_ID}"
        lm = dspy.LM(
            model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=True,
            **thinking_kwargs(model_name),
        )
        dspy.configure(lm=lm)
        self.toolkit = CelexDatabaseToolkit()
        self.module = FactCaseTitleModule(sql_store=self.toolkit.fact_repo)

    def test_integration_35814_17_6(self):
        celex_id = "35814_17_6"
        params = FactExtractorParamVO(
            target_fact_name="case_title",
            question="Extract case title",
            celex_id=celex_id,
            output_schema="",
            pipeline="case_title",
            force_override=True,  # Bypass cache to test full extraction
        )
        prediction = self.module(params)
        
        items = prediction.output.items
        import json
        print(f"\nIntegration test Case Title Result for {celex_id}: {json.dumps(items, indent=2)}")

        self.assertGreater(len(items), 0)
        # Using assertIn to be safe against slight formatting differences like brackets
        self.assertEqual("IV/35814", items[0]["case_title"][0].replace(" ", "").replace("-", "").replace("–", "").replace("(", "").replace(")", ""))
        self.assertIn("Extra alloy", items[0]["case_title"][1])

if __name__ == "__main__":
    unittest.main()
