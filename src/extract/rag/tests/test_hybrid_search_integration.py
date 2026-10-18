import unittest
from tools.celex_tools import CelexDatabaseToolkit
from extract.rag.ranker.flash_ranker import FlashRanker
from extract.rag.hybrid_search import HybridSearch

class TestHybridSearchIntegration(unittest.TestCase):

    def setUp(self):
        self.tools = CelexDatabaseToolkit()
        self.ranker = FlashRanker()
        self.hybrid_search = HybridSearch(
            repo=self.tools.chunk_repo, ranker=self.ranker, embed_query=self.tools.embed_query
        )

    def test_hybrid_search_integration_39574_2801_11(self):
        """
        Test the complete HybridSearch pipeline against the live database
        for the specific celex_id: '39574_2801_11'.
        """

        # We perform a search that typically yields results in the specific document.
        query = "list companies and fine amounts?"
        celex_id = "39574_2801_11"

        results = self.hybrid_search.search(
            celex_id=celex_id,
            query=query,
            top_k=30
        )

        # Display results to verify the legal climax boost in action
        print(f"--- Hybrid Search Results for {celex_id} ---")
        for i, res in enumerate(results):
            print(f"Rank {i+1}: ID={res.id}, Origin={res.origin}, BaseScore={res.base_score:.5f}, Score={res.score:.5f}")
            print(f"Snippet: {res.chunk[:100]}...\n")

        print("\\n--- ALL CHUNKS ---")
        for r in results:
            if r.id in ("7aba2d6f-6aa9-44a7-8bab-ad03435c490d", "a019b4b1-745d-43ce-831a-5d6055376cbe", "6d0a127e-6888-4902-8bb5-9275f42bfbd1"):
                print(f"Target ID={r.id}, Rank=?, BaseScore={r.base_score:.5f}, Score={r.score:.5f}")

        # Assertions
        self.assertGreater(len(results), 0, "Expected to find chunks for this document")
        self.assertGreaterEqual(results[0].score, results[-1].score, "Results should be sorted descending")

        # User Requirement: The target companies should appear somewhere in the top 10 chunks.
        top_10_chunks_text = " ".join([res.chunk for res in results[:10]])
        self.assertIn("For the infringement referred to in Article 1, the following fines are imposed:", top_10_chunks_text)
        self.assertIn("Infineon Technologies AG: EUR 82 784 000", top_10_chunks_text)

    def test_hybrid_search_integration_CELEX31975D0358(self):

        # We perform a search that typically yields results in the specific document.
        query = "list companies and fine amounts?"
        celex_id = "CELEX-31975D0358"

        results = self.hybrid_search.search(
            celex_id=celex_id,
            query=query,
            top_k=30
        )

        # Display results to verify the legal climax boost in action
        print(f"--- Hybrid Search Results for {celex_id} ---")
        for i, res in enumerate(results):
            print(f"Rank {i+1}: ID={res.id}, Origin={res.origin}, BaseScore={res.base_score:.5f}, Score={res.score:.5f}, Similarity={res.similarity:.5f}")
            print(f"Snippet: {res.chunk[:100]}...\n")

        # User Requirement: The target companies should appear somewhere in the top 10 chunks.
        # Since information in older cases is often spread out, we check the aggregated text of the top 10.
        top_10_chunks_text = " ".join([res.chunk for res in results[:10]])
        self.assertIn("Stokvis", top_10_chunks_text)
        self.assertIn("Haarden", top_10_chunks_text)
        self.assertIn("Kriek", top_10_chunks_text)
"""
all text to the LLM:
| Company / Person | Fine Amount (NLG) | Reason | Status under Commission Decision |
|------------------|-------------------|--------|-------------------------------|
| **H. D. Kriek** | 5,500 (5,000 + 500) | Breach of exclusivity & accounting rules | Fine unenforceable; suspension annulled |
| **Stokvis** | 53,700 (3,700 + 50,000) | Supply to unauthorized/suspended retailers & Smit via Schweitzer | Fine unenforceable |
| **Haarden- en Kachelbureau** | — | Administered illegal agreement | Ordered to cease enforcement |

"""
if __name__ == '__main__':
    unittest.main()
