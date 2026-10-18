import unittest
from unittest.mock import MagicMock
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import FactContextModule

class TestFactContextModule(unittest.TestCase):
    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.module = FactContextModule(sql_store=self.mock_sql_store)

        # Mock out the internal extract DSPy component directly.
        self.mock_extract = MagicMock()
        self.module.extract = self.mock_extract

        self.params = FactExtractorParamVO(
            celex_id="TEST-123",
            target_fact_name="leniency_granted",
            question="Which companies_investigated got which amount fine reduction?",
            output_schema="company, amount",
            pipeline="fact_context",
            predecessors=["leniency_case", "company", "leniency_applicants"]
        )

    def test_sql_hit_returns_existing_true(self):
        # Arrange
        mock_fact = MagicMock()
        mock_fact.value_json = [{"company": "IMI plc", "amount": "EUR 48.30 million"}]
        self.mock_sql_store.select_fact4celex.return_value = [mock_fact]

        # Act
        prediction = self.module.forward(self.params)

        # Assert
        self.assertEqual(prediction.output.source, "facts_context")
        self.assertEqual(prediction.output.items, [{"company": "IMI plc", "amount": "EUR 48.30 million"}])
        self.mock_extract.assert_not_called()

    def test_missing_antecedents_raises_value_error(self):
        # Arrange
        # Force the target fact check to fail
        def side_effect(celex_id, fact_name):
            return []
            
        self.mock_sql_store.select_fact4celex.side_effect = side_effect

        # Act & Assert
        with self.assertRaises(ValueError) as context:
             self.module.forward(self.params)
             
        self.assertEqual(str(context.exception), "Missing prerequisite fact: 'leniency_case'")
        self.mock_extract.assert_not_called()

    def test_partial_antecedents_build_context_and_extract(self):
        # Arrange
        def side_effect(celex_id, fact_name):
            if fact_name == "leniency_case":
                fact = MagicMock()
                fact.value_md = "Applied."
                return [fact]
            elif fact_name == "leniency_applicants":
                fact = MagicMock()
                fact.value_md = "| company |\n| IMI |"
                return [fact]
            elif fact_name == "company":
                fact = MagicMock()
                fact.value_md = "IMI, Delta"
                return [fact]
            return []
            
        self.mock_sql_store.select_fact4celex.side_effect = side_effect
        
        mock_pred = MagicMock()
        mock_pred.output.extracted_facts = [{"company": "IMI", "amount": "Unknown"}]
        self.mock_extract.return_value = mock_pred

        # Act
        prediction = self.module.forward(self.params)

        # Assert
        self.assertEqual(prediction.output.source, "facts_context")
        self.assertEqual(prediction.output.items, [{"company": "IMI", "amount": "Unknown"}])
        self.mock_extract.assert_called_once()
        
        args, kwargs = self.mock_extract.call_args
        self.assertIn("--- Context from leniency_case ---", kwargs['context'])
        self.assertIn("--- Context from leniency_applicants ---", kwargs['context'])

    def test_companies_country_derives_iso_from_predecessor(self):
        """companies_country must derive the ISO code from companies_investigated.

        Regression for the empty-country failure: under the `universal`
        pipeline the declared predecessor was ignored and the company list was
        re-derived from a hybrid-search lottery, so a retrieval miss dropped the
        company (and its country) entirely. Routed through `fact_context`, the
        authoritative companies_investigated list is fed to the extractor and the
        country is preserved for every company.
        """
        # Arrange: companies_investigated holds the authoritative company list.
        def side_effect(celex_id, fact_name):
            if fact_name == "companies_investigated":
                fact = MagicMock()
                fact.value_md = "Deutsche Post AG"
                return [fact]
            return []

        self.mock_sql_store.select_fact4celex.side_effect = side_effect

        mock_pred = MagicMock()
        mock_pred.output.extracted_facts = [
            {"companies_investigated": "Deutsche Post AG", "country": "DE"}
        ]
        self.mock_extract.return_value = mock_pred

        params = FactExtractorParamVO(
            celex_id="38745_32_1",
            target_fact_name="companies_country",
            question="for all companies_investigated, what is their ISO country code ?",
            output_schema="companies_investigated, country",
            pipeline="fact_context",
            predecessors=["companies_investigated"],
            force_override=True,
        )

        # Act
        prediction = self.module.forward(params)

        # Assert: the country survives and the predecessor drove the context.
        self.assertEqual(
            prediction.output.items,
            [{"companies_investigated": "Deutsche Post AG", "country": "DE"}],
        )
        self.mock_extract.assert_called_once()
        _args, kwargs = self.mock_extract.call_args
        self.assertIn("--- Context from companies_investigated ---", kwargs["context"])
        self.assertIn("Deutsche Post AG", kwargs["context"])
        self.assertEqual(kwargs["schema_description"], "companies_investigated, country")


if __name__ == '__main__':
    unittest.main()
