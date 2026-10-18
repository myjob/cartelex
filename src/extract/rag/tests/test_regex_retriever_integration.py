import unittest
from tools.celex_tools import CelexDatabaseToolkit
from extract.rag.regex_retriever import RegexRetriever

class TestRegexRetrieverIntegration(unittest.TestCase):

    def setUp(self):
        self.tools = CelexDatabaseToolkit()
        self.retriever = RegexRetriever(repo=self.tools.chunk_repo)

    def test_regex_retriever_articles(self):
        celex_id = '32450_35_6'
        regex = r'Article \d+'

        results = self.retriever.regex_retriever(
            celex_id=celex_id,
            regex=regex
        )
        
        expected_articles = ["Article 81", "Article 82", "Article 85", "Article 86"]
        for article in expected_articles:
            expected_dict = {"law_article": article}
            self.assertIn(expected_dict, results, f"'{expected_dict}' not found in results: {results}")

if __name__ == '__main__':
    unittest.main()
