import unittest
from tools.celex_tools import CelexDatabaseToolkit
from extract.rag.ranker.flash_ranker import FlashRanker
from extract.rag.hybrid_search import HybridSearch


class TestHybridSearchCommissioner(unittest.TestCase):
    def setUp(self):
        self.tools = CelexDatabaseToolkit()
        self.ranker = FlashRanker()
        self.hybrid_search = HybridSearch(
            repo=self.tools.chunk_repo, ranker=self.ranker, embed_query=self.tools.embed_query
        )

    def test_hybrid_search_commissioner_CELEX32003D0570(self):
        """
        Test to investigate why the commissioner Mario MONTI at the end of doc was not found.
        """
        query = "Which commissioner acted for the commission?"
        keyword = "commissioner"
        celex_id = "CELEX-32003D0570"

        # 1. Gather via Keyword Logic
        sql_chunks = self.tools.chunk_repo.keyword_search(
            celex_id=celex_id, keyword=keyword
        )
        print(f"\\n--- SQL Keyword Search Chunks for {celex_id} ---")
        print(f"Found {len(sql_chunks)} chunks for keyword '{keyword}'")
        for chunk in sql_chunks:
            if "Monti" in chunk.chunk or "MONTI" in chunk.chunk:
                print(
                    f"Keyword search found Monti! ID: {chunk.id}, Pos: {chunk.doc_pos}"
                )

        # 2. Gather via Hybrid Search
        vector_chunks = self.hybrid_search.search(
            celex_id=celex_id, query=query, top_k=15
        )
        print(f"\\n--- Hybrid Search Chunks for {celex_id} ---")
        print(f"Found {len(vector_chunks)} chunks for query '{query}'")
        for chunk in vector_chunks:
            if "Monti" in chunk.chunk or "MONTI" in chunk.chunk:
                print(
                    f"Hybrid search found Monti! ID: {chunk.id}, Pos: {chunk.doc_pos}"
                )

        # Now do the actual hybrid search
        results = self.hybrid_search.search(
            celex_id=celex_id, query=query, top_k=30
        )

        print(f"\\n--- Hybrid Search Results for {celex_id} ---")
        for i, res in enumerate(results):
            print(
                f"Rank {i + 1}: ID={res.id}, Origin={res.origin}, BaseScore={res.base_score:.5f}, Score={res.score:.5f}, Pos={res.doc_pos}"
            )
            snippet = res.chunk[:100].replace("\\n", " ")
            print(f"Snippet: {snippet}...")
            if "Monti" in res.chunk or "MONTI" in res.chunk:
                print(">>> CONTAINS MONTI <<<")

        # Let's see if Monti is in the entire document just to be sure
        all_vector = self.tools.semantic_search_chunks_in_celex(
            celex_id=celex_id, query="Mario Monti", hits=100
        )
        print("\\n--- Search for 'Mario Monti' specifically ---")
        for chunk in all_vector:
            if "Monti" in chunk.chunk or "MONTI" in chunk.chunk:
                print(
                    f"Direct search found Monti! ID: {chunk.id}, Pos: {chunk.doc_pos}"
                )
                snippet = chunk.chunk[:100].replace("\\n", " ")
                print(f"Snippet: {snippet}... doc_pos {chunk.doc_pos}")
                # self.assertTrue(chunk.doc_pos == 66)

        # Assertions
        top_text = " ".join([res.chunk for res in results])
        self.assertIn(
            "MONTI",
            top_text.upper(),
            "Target commissioner Mario Monti should be found in the top chunks",
        )


if __name__ == "__main__":
    unittest.main()
