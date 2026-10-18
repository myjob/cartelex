"""Integration tests for BgeReranker against the real CrossEncoder model.

Unlike ``test_bge_reranker.py`` (model mocked), these load the actual
``settings.RERANKER_MODEL_ID`` weights (``BAAI/bge-reranker-v2-m3``, ~2.2 GB on
first run) and assert *semantic* behaviour: that the cross-encoder ranks
genuinely relevant legal text above boilerplate, handles a non-English passage
(the multilingual upgrade over the English-only ms-marco model), and produces
sigmoid-scored values in ``(0, 1)``.

Cross-encoder inference is deterministic (no sampling), so exact orderings are
stable. Gated behind ``RUN_RERANKER_INTEGRATION=1`` so a routine
``unittest discover`` does not trigger the multi-gigabyte download.

Run with::

    RUN_RERANKER_INTEGRATION=1 .venv/bin/python -m unittest \\
        extract.rag.ranker.tests.test_bge_reranker_integration
"""

import os
import unittest

from config.settings import settings
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from pipelines.nace.nace_tree import NaceVo, RankedNaceVo

_RUN = os.getenv("RUN_RERANKER_INTEGRATION") == "1"


@unittest.skipUnless(
    _RUN,
    "set RUN_RERANKER_INTEGRATION=1 to run (downloads/loads the real "
    "bge-reranker-v2-m3, ~2.2 GB)",
)
class TestBgeRerankerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Real model via system config (settings.RERANKER_MODEL_ID). Loaded once
        # for the whole class — construction is the expensive step.
        from extract.rag.ranker.bge_reranker import BgeReranker

        cls.ranker = BgeReranker()

    def test_loads_configured_model(self):
        """The integration ranker uses the model id from settings."""
        self.assertEqual(self.ranker.model_name, settings.RERANKER_MODEL_ID)

    def test_fines_chunk_ranks_first_on_semantics(self):
        """With the position boost OFF, the fines chunk wins on content alone.

        Isolates the cross-encoder's semantic judgement from the Legal Climax
        depth boost — this is the capability the reranker upgrade targets
        (ms-marco could not tell operative fines text from body recitals).
        """
        chunks = [
            ChunkVo(
                id="fines",
                celex_id="32002D0759",
                chunk=(
                    "3.4.4. FINAL AMOUNT OF THE FINES\n"
                    "The final amounts of the fines imposed under Article 15(2) of "
                    "Regulation No 17 are: undertaking A EUR 400 000, undertaking B "
                    "EUR 24 000, undertaking C EUR 24 000."
                ),
                origin="test",
                similarity=0.5,
                doc_pos=42,
            ),
            ChunkVo(
                id="market",
                celex_id="32002D0759",
                chunk=(
                    "2.1. RELEVANT PRODUCT MARKET\n"
                    "The relevant product market comprises industrial and medical "
                    "gases supplied in cylinders and in bulk to downstream customers."
                ),
                origin="test",
                similarity=0.5,
                doc_pos=12,
            ),
            ChunkVo(
                id="boilerplate",
                celex_id="32002D0759",
                chunk=(
                    "Done at Brussels, 11 December 2001. For the Commission. "
                    "Mario MONTI, Member of the Commission. Address for service."
                ),
                origin="test",
                similarity=0.5,
                doc_pos=60,
            ),
        ]
        query = "What was the total amount of the fines imposed on the undertakings?"

        ranked = self.ranker.rank_chunkvo(
            query, chunks, apply_position_boost=False
        )

        self.assertIsInstance(ranked[0], RankedChunkVo)
        self.assertEqual(
            ranked[0].id, "fines", "the fines chunk must win on semantics"
        )
        score = {r.id: r.base_score for r in ranked}
        # The discriminating behaviour the upgrade buys: the directly-relevant
        # fines chunk scores near 1, while chunks irrelevant to a *fines-amount*
        # query (product-market text, signature boilerplate) score near 0. Their
        # relative order is meaningless noise, so it is not asserted. Observed:
        # fines ~0.97, others ~1.6e-05 (a ~60000x margin).
        self.assertGreater(score["fines"], 0.5)
        self.assertLess(score["market"], 0.01)
        self.assertLess(score["boilerplate"], 0.01)
        for value in score.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

    def test_multilingual_relevance(self):
        """A relevant German passage outranks an irrelevant English one.

        bge-reranker-v2-m3 is multilingual; ms-marco (English web-QA) is not.
        The query is English, the leniency passage is German.
        """
        chunks = [
            ChunkVo(
                id="de_leniency",
                celex_id="X",
                chunk=(
                    "Nach der Kronzeugenregelung wurde dem ersten Unternehmen, das "
                    "Beweismittel vorlegte, ein vollstaendiger Erlass der Geldbusse "
                    "gewaehrt."
                ),
                origin="test",
                similarity=0.5,
                doc_pos=1,
            ),
            ChunkVo(
                id="en_unrelated",
                celex_id="X",
                chunk=(
                    "The weather in the coastal region remained mild throughout the "
                    "spring, with occasional light rainfall."
                ),
                origin="test",
                similarity=0.5,
                doc_pos=1,
            ),
        ]
        query = "immunity from fines granted to the first leniency applicant"

        ranked = self.ranker.rank_chunkvo(
            query, chunks, apply_position_boost=False
        )
        self.assertEqual(
            ranked[0].id,
            "de_leniency",
            "multilingual reranker must place the German leniency passage first",
        )

    def test_rank_chunkvo_top_k_and_sorted(self):
        chunks = [
            ChunkVo(id=str(i), celex_id="X", chunk=text, origin="t", similarity=0.5, doc_pos=i)
            for i, text in enumerate(
                [
                    "cartel members agreed to fix prices at coordination meetings",
                    "the geographic market is the European Economic Area",
                    "an appeal was lodged before the General Court",
                ]
            )
        ]
        ranked = self.ranker.rank_chunkvo("price fixing cartel meetings", chunks, top_k=2)
        self.assertEqual(len(ranked), 2)
        self.assertGreaterEqual(ranked[0].score, ranked[1].score)

    def test_rank_nacevo_best_match(self):
        nodes = [
            NaceVo(type="group", code="26.1", title="Manufacture of electronic components and boards", isic_code="261", childs=[]),
            NaceVo(type="group", code="45.1", title="Sale of motor vehicles", isic_code="451", childs=[]),
            NaceVo(type="group", code="35.1", title="Electric power generation, transmission and distribution", isic_code="351", childs=[]),
        ]
        ranked = self.ranker.rank_nacevo("retail sale of new and used passenger cars", nodes)
        self.assertIsInstance(ranked[0], RankedNaceVo)
        self.assertEqual(ranked[0].code, "45.1")


if __name__ == "__main__":
    unittest.main()
