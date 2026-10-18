import unittest
from unittest.mock import MagicMock

from pipelines.agent_dspy.universal_modules import (
    UniversalFactModule,
    SQLFactStore,
    HybridSearcher
)
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from extract.rag.models.chunk_vo import RankedChunkVo

class TestFactReferenceExtraction(unittest.TestCase):
    def test_extract_reference_doc_pos_hybrid_scenario(self):
        # 1. Mock SQL Fact Store (empty to force hybrid)
        mock_sql_store = MagicMock(spec=SQLFactStore)
        mock_sql_store.select_fact4celex.return_value = []

        # 2. Mock Hybrid Searcher returning our target chunk with doc_pos 66
        mock_searcher = MagicMock(spec=HybridSearcher)
        mock_searcher.search.return_value = [
            RankedChunkVo(id="chunk1", celex_id="TEST", chunk="Mario Monti was the commissioner.", origin="hybrid", similarity=0.5, doc_pos=66, score=0.9, base_score=0.9),
            RankedChunkVo(id="chunk2", celex_id="TEST", chunk="Noise text.", origin="hybrid", similarity=0.5, doc_pos=67, score=0.1, base_score=0.1)
        ]

        # 3. Instantiate Module
        module = UniversalFactModule(sql_store=mock_sql_store, searcher=mock_searcher)

        # 4. Mock the DSPy inner chains
        # Mock extract
        mock_extract_pred = MagicMock()
        mock_extract_pred.output.extracted_facts = [{"commissioner": "Mario Monti"}]
        module.extract = MagicMock(return_value=mock_extract_pred)

        # Mock find_doc_references exactly matching CELEX-32003D0570 scenario
        mock_ref_pred = MagicMock()
        mock_ref_pred.output.doc_positions = [66]
        module.find_doc_references = MagicMock(return_value=mock_ref_pred)

        # 5. Build Parameter VO
        params = FactExtractorParamVO(
            celex_id="CELEX-32003D0570",
            question="Who was the commissioner?",
            target_fact_name="commissioner",
            output_schema="",
            force_override=True
        )

        # 6. Run forward
        result = module.forward(params)

        # 7. Assertions
        self.assertEqual(result.output.source, "hybrid")
        self.assertEqual(result.output.items, [{"commissioner": "Mario Monti"}])
        self.assertEqual(result.output.references_doc, [66], "The doc_pos 66 must be propagated to references_doc")

        # Verify context injection format
        call_args = module.find_doc_references.call_args[1]
        self.assertIn("[doc_pos: 66]", call_args['context'], "Formatter must inject [doc_pos: X] into context")

if __name__ == "__main__":
    unittest.main()
