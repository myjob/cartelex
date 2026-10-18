import unittest
from tools.celex_tools import CelexDatabaseToolkit
from extract.rag.key_word_search import KeywordSearch
from extract.rag.ranker.ranker_factory import create_ranker

class TestKeywordSearchIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The cross-encoder is a ~2.2 GB model: build it once per class and
        # inject it (Provisioner pattern). Loading it per test method exhausted
        # GPU memory after two tests.
        cls.tools = CelexDatabaseToolkit()
        cls.ranker = create_ranker()
        cls.keyword_search = KeywordSearch(
            repo=cls.tools.chunk_repo, ranker=cls.ranker
        )

    def test_leniency_case_keyword_integration_CELEX_32000D0146(self):
        """Test the KeywordSearch class for CELEX-32000D0146 with leniency_case."""
        celex_id = "CELEX-32000D0146"
        query = "leniency case"

        result = self.keyword_search.search(celex_id=celex_id, query=query)

        print(f"\\nKeyword Search Result for {celex_id} targeting '{query}': {result}")

        # It's perfectly normal for this to be False if "leniency" doesn't literally exist there.
        self.assertTrue(result)



    def test_fine_keyword_integration(self):
        """Test the KeywordSearch class against the live DB using a term we know exists."""
        celex_id = "CELEX-31994D0601"
        query = "fine amounts"

        result = self.keyword_search.search(celex_id=celex_id, query=query)

        print(f"\\nKeyword Search Result for {celex_id} targeting '{query}': {result}")

        # We know "fine" has 20 chunks in CELEX-31994D0601
        self.assertTrue(result)

    def test_leniency_case_keyword_integration(self):
        """Test the KeywordSearch class for CELEX-31994D0601 with leniency_case."""
        celex_id = "CELEX-31994D0601"
        # In reality 'leniency' as a string is not present in document CELEX-31994D0601
        query = "leniency case"

        result = self.keyword_search.search(celex_id=celex_id, query=query)

        print(f"\\nKeyword Search Result for {celex_id} targeting '{query}': {result}")

        # It's perfectly normal for this to be False if "leniency" doesn't literally exist there.
        self.assertFalse(result)




if __name__ == '__main__':
    unittest.main()
