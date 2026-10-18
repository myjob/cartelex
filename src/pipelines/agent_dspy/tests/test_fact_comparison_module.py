import dspy
import unittest
from config.llm_thinking import thinking_kwargs
from config.settings import settings
from pipelines.rag.pgvector.db import FactTable
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import FactComparisonModule

class TestFactComparisonModuleIntegration(unittest.TestCase):
    """
    Integration test for the FactComparisonModule acting on 'companies_country'
    for celex 'CELEX-31975D0358'. Connects to the real database to pull both
    the legacy 'rule_facts.facts' row and the 'celex_facts.facts' row.
    Requires external LLM capabilities (Ollama/OpenAi depending on configuration).
    """

    @classmethod
    def setUpClass(cls):
        # 1. Init Database Connection
        cls.sql_store = FactTable()

        # 2. Init LLM
        model_name = f"openai/{settings.LLM_MODEL_ID}"
        lm = dspy.LM(
            model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=True,
            **thinking_kwargs(model_name),
        )
        dspy.configure(lm=lm)

        # 3. Target setup
        cls.celex_id = "CELEX-31975D0358"
        cls.target_fact_name = "companies_country"

    def test_forward_reads_dbs_and_extracts_comparison(self):
        # Arrange
        module = FactComparisonModule(sql_store=self.sql_store)
        params = FactExtractorParamVO(
            celex_id=self.celex_id,
            target_fact_name=self.target_fact_name,
            question="What is the country of the company?",
            output_schema="companies_country_match",
            pipeline="fact_comparison"
        )

        # Act
        prediction = module.forward(params)
        
        # Assert
        items = prediction.output.items
        source = prediction.output.source
        
        self.assertEqual(source, "comparison")
        self.assertEqual(len(items), 1)

        # Output format verification
        item = items[0]
        self.assertIn("companies_country_match", item)
        self.assertIn("reasoning", item)
        
        # Depending on local database state, this will either be True or False,
        # but the boolean flag and reasoning must be present.
        match_value = item["companies_country_match"]
        self.assertIn(match_value, ["True", "False"])
        
        print("\n--- INTEGRATION TEST RESULT ---")
        print(f"Target Fact: {self.target_fact_name}")
        print(f"Match      : {match_value}")
        print(f"Reasoning  : {item['reasoning']}")
        print("-------------------------------\n")

if __name__ == '__main__':
    unittest.main()
