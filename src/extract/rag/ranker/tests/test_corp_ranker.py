import unittest
from unittest.mock import patch

from extract.rag.ranker.corp_ranker import CorpRanker, AliasClassificationResult
from extract.rag.models.chunk_vo import RankedChunkVo

# Mock data
COMPANY_STRINGS = [
    "Google", "Google Ireland", "Google LLC", "Amazon", "eBay", 
    "Fnac", "PriceMinister", "Conrad", "1PlusV", "Acheter moins cher", 
    "BDZV", "VDZ", "BEUC", "Ciao", "Microsoft", "Company AC", 
    "Deutsche Telekom", "ETTSA", "Euro-Cities", "Expedia", "FairSearch", 
    "Foundem", "Getty", "HolidayCheck", "Hot Maps", "ICOMP", "Kelkoo", 
    "MIH", "MoneySupermarket", "News Corp", "Nextag", "Guenstiger", 
    "OCU", "Odigeo", "OIP", "TradeComet", "TripAdvisor", "Trivago", 
    "Twenga", "VfT", "Visual Meta", "Yelp", "Axa", "Permira", "Opodo", 
    "Go Voyages", "eDreams", "Apple", "ECC Köln", "Raymond James", 
    "Seznam", "Yahoo", "America Online", "Bing."
]

class MockFlashRanker:
    def rank_chunkvo(self, query, chunks, top_k):
        # Mocking ranking behavior
        results = []
        if query == "Google":
            matches = [
                ("Google", 0.999),
                ("Google Ireland", 0.950),
                ("Google LLC", 0.940),
                ("Yahoo", 0.930),
                ("Amazon", 0.850)
            ]
            for chunk_str, score in matches:
                idx = next((i for i, c in enumerate(chunks) if c.chunk == chunk_str), 0)
                results.append(RankedChunkVo(id=str(idx), celex_id="", chunk=chunk_str, origin="mock", similarity=0.5,score=score, doc_pos=0))
        return results[:top_k]

class MockPredict:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, predefined_company, candidates):
        # Mocking LM extraction
        class MockExtraction:
            def __init__(self, aliases):
                self.extracted = AliasClassificationResult(aliases=aliases)
                
        if predefined_company == "Google":
            # the LLM correctly filters out Yahoo and Amazon, and keeps the true aliases
            return MockExtraction(aliases=["Google Ireland", "Google LLC"])
        
        return MockExtraction(aliases=[])

class TestCorpRanker(unittest.TestCase):

    def setUp(self):
        self.patcher = patch("extract.rag.ranker.corp_ranker.dspy.Predict", return_value=MockPredict())
        self.patcher.start()
        self.ranker = CorpRanker(ranker=MockFlashRanker(), top_k=5)

    def tearDown(self):
        self.patcher.stop()

    def test_corp_ranker_filtering(self):
        """
        Test that CorpRanker correctly uses FlashRanker to get candidates,
        then uses DSPy Module to extract the actual aliases, filtering out competitors.
        """
        predefined = ["Google"]
        
        results = self.ranker(predefined_companies=predefined, company_strings=COMPANY_STRINGS)
        
        self.assertEqual(len(results), 2)
        
        aliases = [r.alias for r in results]
        self.assertIn("Google Ireland", aliases)
        self.assertIn("Google LLC", aliases)
        self.assertNotIn("Yahoo", aliases)
        
        # Check that FlashRanker score was mapped correctly
        for r in results:
            if r.alias == "Google Ireland":
                self.assertEqual(r.score, 0.950)
            elif r.alias == "Google LLC":
                self.assertEqual(r.score, 0.940)

    def test_corp_ranker_empty_inputs(self):
        self.assertEqual(self.ranker([], COMPANY_STRINGS), [])
        self.assertEqual(self.ranker(["Google"], []), [])

if __name__ == '__main__':
    unittest.main()
