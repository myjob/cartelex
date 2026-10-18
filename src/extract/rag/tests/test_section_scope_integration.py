"""Acceptance test for section-scoped retrieval (live database + models).

Review Phase 3 acceptance criterion: a fines question against
CELEX-32002D0759 restricted to the fines/operative sections returns the
doc_pos 42 chunk ("3.4.4. FINAL AMOUNT OF THE FINES") at rank 1.

Requires the section_type migration to be applied and
``Main_classify_sections.py`` to have classified CELEX-32002D0759.
"""

import unittest

from extract.rag.hybrid_search import HybridSearch
from extract.rag.models.chunk_vo import SectionType
from extract.rag.ranker.ranker_factory import create_ranker
from tools.celex_tools import CelexDatabaseToolkit

_CELEX_ID = "CELEX-32002D0759"
_FINES_DOC_POS = 42


class TestSectionScopeAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = CelexDatabaseToolkit()
        cls.search = HybridSearch(
            repo=cls.tools.chunk_repo,
            ranker=create_ranker(),
            embed_query=cls.tools.embed_query,
        )

    def test_fines_question_scoped_to_fines_operative(self) -> None:
        results = self.search.search(
            celex_id=_CELEX_ID,
            query="List all fines",
            top_k=10,
            section_types=["fines", "operative"],
        )

        self.assertGreater(len(results), 0, "scoped search returned nothing")
        for result in results:
            self.assertIn(
                result.section_type,
                {SectionType.FINES, SectionType.OPERATIVE},
                f"chunk doc_pos={result.doc_pos} leaked section {result.section_type}",
            )
        self.assertEqual(
            results[0].doc_pos,
            _FINES_DOC_POS,
            "the FINAL AMOUNT OF THE FINES chunk must rank first",
        )


    """  unreliable over multi calls
    def test_unscoped_search_still_works(self) -> None:
        results = self.search.search(celex_id=_CELEX_ID, query="List all fines", top_k=10)
        # getting 10 results in  debug more but not in batch test script
        self.assertGreater(len(results), 0)
    """

if __name__ == "__main__":
    unittest.main()
