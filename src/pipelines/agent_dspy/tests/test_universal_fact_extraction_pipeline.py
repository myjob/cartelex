import unittest
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

if __name__ == "__main__":
    unittest.main()
