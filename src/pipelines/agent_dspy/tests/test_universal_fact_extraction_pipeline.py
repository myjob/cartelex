import asyncio
import unittest
import uuid
from unittest.mock import MagicMock

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_fact_extraction_pipeline import FactRagPipeline
from pipelines.agent_dspy.universal_modules import GenericExtractionSignature, GenericVerificationSignature

class TestUniversalFactExtractionPipeline(unittest.TestCase):
    def test_extraction_signature_docstring_is_clean(self):
        """Verify that GenericExtractionSignature docstring doesn't contain templated strings or prompt engineering."""
        doc = GenericExtractionSignature.__doc__
        if doc:
            self.assertNotIn("{target_fact_name}", doc, "Docstring should not contain prompt engineering templates.")
            self.assertNotIn("{schema_description}", doc, "Docstring should not contain prompt engineering templates.")

    def test_verification_signature_docstring_is_clean(self):
        """Verify that GenericVerificationSignature docstring doesn't contain prompt engineering."""
        doc = GenericVerificationSignature.__doc__
        if doc:
            self.assertNotIn("DO NOT add extra metadata keys", doc, "Rules should be moved to InputField desc=.")


class TestCrossDocumentStaleness(unittest.TestCase):
    """An existing fact is skipped unless a source fact of another document is newer."""

    def setUp(self):
        self.ingestor = MagicMock()
        self.ingestor.exists.return_value = True
        self.ingestor.save_fact.return_value = uuid.uuid4()
        self.fact_repo = MagicMock()
        self.fact_repo.select_newer_cross_document_sources.return_value = []
        self.pipeline = FactRagPipeline(
            target_fact_name="cartel_period",
            ingestor=self.ingestor,
            fact_embedding_loader=MagicMock(),
            fact_repo=self.fact_repo,
        )
        self.agent = MagicMock()
        self.agent.return_value.output.items = [{"date_range": "1987-02-24 : 2000-09-26"}]
        self.agent.return_value.output.references_doc = None
        self.agent.return_value.output.lineage_sources = None

    def _run(self, force_override: bool = False) -> dict:
        params = FactExtractorParamVO(
            target_fact_name="cartel_period", question="q", celex_id="DEC-AT38589-20160629_2",
            output_schema="date_range", pipeline="heideltime_cartel_period",
            force_override=force_override,
        )
        return asyncio.run(self.pipeline.process(params, self.agent))

    def test_fresh_existing_fact_is_skipped(self):
        self.assertEqual(self._run()["status"], "skipped")
        self.agent.assert_not_called()
        self.fact_repo.select_newer_cross_document_sources.assert_called_once_with(
            "DEC-AT38589-20160629_2", "cartel_period"
        )

    def test_newer_donor_forces_re_extraction(self):
        self.fact_repo.select_newer_cross_document_sources.return_value = ["38589_4669_7"]
        self.assertEqual(self._run()["status"], "success")
        self.assertTrue(self.agent.call_args.kwargs["params"].force_override)

    def test_missing_fact_needs_no_staleness_probe(self):
        self.ingestor.exists.return_value = False
        self.assertEqual(self._run()["status"], "success")
        self.fact_repo.select_newer_cross_document_sources.assert_not_called()
        self.assertFalse(self.agent.call_args.kwargs["params"].force_override)

    def test_forced_run_needs_no_staleness_probe(self):
        self.assertEqual(self._run(force_override=True)["status"], "success")
        self.fact_repo.select_newer_cross_document_sources.assert_not_called()


if __name__ == "__main__":
    unittest.main()
