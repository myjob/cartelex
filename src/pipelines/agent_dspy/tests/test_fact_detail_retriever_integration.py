"""Integration test for FactDetailRetriever against the live system.

Uses the system configs from config.settings (DATABASE_URL, LLM_MODEL_ID,
OLLAMA_HOST) per project convention for integration tests. Read-only: it calls
the module's forward() directly, so no facts are persisted.

Run explicitly (slow, needs Postgres + Ollama):
    PYTHONPATH=src .venv/bin/python -m unittest \
        pipelines.agent_dspy.tests.test_fact_detail_retriever_integration -v
"""

import json
import unittest

import dspy

from config.llm_thinking import thinking_kwargs
from config.settings import settings

CELEX_ID = "CELEX-32000D0146"
ROSTER_FACT = "companies_investigated"

# Known ground truth for CELEX-32000D0146 (Commission decision 2000/146/EC):
# company-name anchor -> stable address substrings (postal-code anchors are
# robust against LLM formatting variance).
#
# Only the two INVESTIGATED undertakings appear here, because the roster is the
# companies_investigated fact (['Anheuser-Busch Incorporated', 'Scottish &
# Newcastle plc']). Anheuser-Busch European Trade Limited (ABET) is listed in
# the addressee block but is not itself an investigated undertaking, so it is
# not part of the roster this pipeline enriches.
EXPECTED_ADDRESSES = {
    "Anheuser-Busch Incorporated": ("St Louis", "63188"),
    "Scottish & Newcastle": ("East Fettes", "EH4 1RR"),
}


class TestFactDetailRetrieverIntegration(unittest.TestCase):
    """End-to-end per-entity enrichment over the live DB and LLM."""

    @classmethod
    def setUpClass(cls):
        from extract.rag.hybrid_search import HybridSearch
        from extract.rag.key_word_search import KeywordSearch
        from extract.rag.ranker.ranker_factory import create_ranker
        from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
        from pipelines.agent_dspy.universal_modules import FactDetailRetriever
        from pipelines.rag.pgvector.fact_repository import FactRepository
        from tools.celex_tools import CelexDatabaseToolkit

        model_name = f"openai/{settings.LLM_MODEL_ID}"
        dspy.configure(
            lm=dspy.LM(
                model_name,
                api_base=f"{settings.OLLAMA_HOST}/v1",
                api_key="ollama",
                cache=False,
                timeout=settings.OLLAMA_TIMEOUT,
                **thinking_kwargs(model_name),
            )
        )

        cls.fact_repo = FactRepository()
        tools = CelexDatabaseToolkit(fact_repo=cls.fact_repo)
        ranker = create_ranker()
        cls.module = FactDetailRetriever(
            sql_store=cls.fact_repo,
            keyword_search=KeywordSearch(repo=tools.chunk_repo, ranker=ranker),
            hybrid_search=HybridSearch(
                repo=tools.chunk_repo, ranker=ranker, embed_query=tools.embed_query
            ),
        )
        cls.params = FactExtractorParamVO(
            celex_id=CELEX_ID,
            target_fact_name="companies_address",
            question="What is the registered address of this company ?",
            output_schema="companies, address",
            pipeline="detail_retriever",
            predecessors=[ROSTER_FACT],
            section_scope=["operative"],
            force_override=True,
        )

    def test_companies_address_for_celex_32000d0146(self):
        """Each investigated company is enriched with its documented address."""
        try:
            roster = self.fact_repo.select_fact4celex(CELEX_ID, ROSTER_FACT)
        except Exception as exc:  # pragma: no cover - environment guard
            self.skipTest(f"Database not reachable: {exc}")
        if not roster:
            self.skipTest(
                f"Roster fact '{ROSTER_FACT}' not present for {CELEX_ID}; "
                "seed it with the companies_investigated pipeline first."
            )

        prediction = self.module.forward(self.params)
        items = prediction.output.items

        roster_json = roster[-1].value_json
        if isinstance(roster_json, str):
            roster_json = json.loads(roster_json)

        self.assertEqual(prediction.output.source, "detail_retriever")
        self.assertTrue(items, "detail_retriever returned no rows")
        # One row per roster entity, exact schema keys.
        self.assertEqual(len(items), len(roster_json))
        for row in items:
            self.assertEqual(set(row.keys()), {"companies", "address"})

        for company_anchor, address_anchors in EXPECTED_ADDRESSES.items():
            matching = [
                row for row in items if company_anchor.lower() in row["companies"].lower()
            ]
            self.assertTrue(
                matching, f"No output row for expected company '{company_anchor}'"
            )
            address = matching[0]["address"]
            self.assertNotEqual(
                address, "NA", f"Address for '{company_anchor}' came back as NA"
            )
            self.assertTrue(
                any(anchor.lower() in address.lower() for anchor in address_anchors),
                f"Address for '{company_anchor}' missing anchors "
                f"{address_anchors}: got '{address}'",
            )


if __name__ == "__main__":
    unittest.main()
