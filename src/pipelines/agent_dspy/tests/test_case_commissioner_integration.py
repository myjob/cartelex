"""Integration test replaying the ``case_commissioner`` extraction call.

Reproduces the ``/chat/completions`` request that
``batch/Main_ingest_generic_facts.py`` issued on 2026-08-15 12:32 for CELEX
``39824_6567_14`` (AT.39824 Trucks / Scania) and that failed with
``litellm.Timeout`` after ``settings.OLLAMA_TIMEOUT`` seconds:

    2026-08-15 12:38:07 - ERROR - batch.BatchJobRunner - Error processing
    39824_6567_14: [qwen3-coder:30b] litellm.Timeout: APITimeoutError

The call under test is ``dspy.ChainOfThought(GenericExtractionSignature)`` fed
with the four inputs ``UniversalFactModule.forward`` assembles: the hybrid
retrieval context, ``target_fact_name``, the resolved ``schema_description``
(``output_type: str`` -> the single-value schema) and the YAML question.

The LM is configured exactly as ``Main_ingest_generic_facts._configure_llm``
does (same model, temperature, retry and timeout settings), so a timeout here
is the same timeout the batch run hit.

Uses the system configs from ``config.settings`` (DATABASE_URL, LLM_MODEL_ID,
OLLAMA_HOST, OLLAMA_TIMEOUT) per project convention for integration tests.
Read-only: the module's ``forward()`` is called directly, so no facts are
persisted.

Run explicitly (slow, needs Postgres + Ollama):
    PYTHONPATH=src .venv/bin/python -m unittest \
        pipelines.agent_dspy.tests.test_case_commissioner_integration -v
"""

import logging
import time
import unittest

import dspy

from config.llm_thinking import thinking_kwargs
from config.settings import settings

logger = logging.getLogger(__name__)

CELEX_ID = "39824_6567_14"
TARGET_FACT_NAME = "case_commissioner"

# Commissioner for Competition who adopted decision AT.39824 Trucks (Scania),
# C(2017) 6467 final of 27 September 2017.
EXPECTED_COMMISSIONER_ANCHOR = "vestager"


class TestCaseCommissionerIntegration(unittest.TestCase):
    """Replay of the timed-out ``case_commissioner`` extraction for Scania."""

    @classmethod
    def setUpClass(cls) -> None:
        """Provision the live LM, hybrid searcher and universal module."""
        from extract.rag.hybrid_search import HybridSearch
        from extract.rag.ranker.ranker_factory import create_ranker
        from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
        from pipelines.agent_dspy.universal_modules import UniversalFactModule
        from pipelines.rag.pgvector.fact_repository import FactRepository
        from tools.celex_tools import CelexDatabaseToolkit

        # Mirror batch.Main_ingest_generic_facts._configure_llm exactly so the
        # request options (timeout, retries, temperature) match the failing run.
        model_name = f"openai/{settings.LLM_MODEL_ID}"
        dspy.configure(
            lm=dspy.LM(
                model_name,
                api_base=f"{settings.OLLAMA_HOST}/v1",
                api_key="ollama",
                temperature=settings.LLM_TEMPERATURE,
                cache=False,
                num_retries=1,
                max_retries=0,
                timeout=settings.OLLAMA_TIMEOUT,
                **thinking_kwargs(model_name),
            )
        )

        cls.fact_repo = FactRepository()
        tools = CelexDatabaseToolkit(fact_repo=cls.fact_repo)
        cls.module = UniversalFactModule(
            sql_store=cls.fact_repo,
            searcher=HybridSearch(
                repo=tools.chunk_repo,
                ranker=create_ranker(),
                embed_query=tools.embed_query,
            ),
            chunk_repo=tools.chunk_repo,
        )
        cls.params = cls._load_yaml_params(FactExtractorParamVO)

    @staticmethod
    def _load_yaml_params(param_cls):
        """Return the ``case_commissioner`` params from the live YAML catalogue.

        Loading the catalogue instead of hardcoding the question and schema
        keeps the replay faithful to whatever the batch run would send today.

        Args:
            param_cls: The ``FactExtractorParamVO`` class (injected to keep the
                import local to ``setUpClass``).

        Returns:
            The ``FactExtractorParamVO`` for ``case_commissioner``, or ``None``
            when the catalogue no longer declares that fact.
        """
        all_params = param_cls.load_from_yaml_file(
            celex_id=CELEX_ID,
            yaml_path=str(settings.FACT_EXTRACTOR_CONFIG_DIR),
            force_override=True,
        )
        for param in all_params:
            if param.target_fact_name == TARGET_FACT_NAME:
                return param
        return None

    def setUp(self) -> None:
        """Skip when the fact catalogue no longer declares the target fact."""
        if self.params is None:
            self.skipTest(
                f"'{TARGET_FACT_NAME}' is not declared in "
                f"{settings.FACT_EXTRACTOR_CONFIG_DIR}"
            )

    def _build_context(self) -> str:
        """Assemble the retrieval context exactly as ``forward()`` does.

        Returns:
            The ``[doc_pos: N]``-prefixed chunk context for the question.

        Raises:
            unittest.SkipTest: If the database is unreachable or the CELEX has
                no indexed chunks.
        """
        try:
            ranked_chunks = self.module.searcher.search(
                celex_id=CELEX_ID,
                query=self.params.question,
                top_k=settings.HYBRID_SEARCH_TOP_K,
                section_types=self.params.section_scope,
            )
        except Exception as exc:  # pragma: no cover - environment guard
            self.skipTest(f"Database not reachable: {exc}")

        if not ranked_chunks:
            self.skipTest(
                f"No chunks indexed for {CELEX_ID}; ingest the document first."
            )
        return "\n---\n".join(
            f"[doc_pos: {chunk.doc_pos}]\n{chunk.chunk}" for chunk in ranked_chunks
        )

    def test_retrieval_context_fits_the_prompt_budget(self):
        """The assembled context stays inside the configured token budget."""
        from pipelines.agent_dspy.universal_modules import _check_context_token_limit

        context = self._build_context()

        self.assertIn("[doc_pos:", context)
        # Raises ValueError when the context exceeds
        # settings.OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT.
        _check_context_token_limit(context, TARGET_FACT_NAME)

    def test_generic_extraction_call_completes_within_timeout(self):
        """The replayed chat/completions call returns a structured result.

        Fails with the observed error when the request times out, which is the
        regression this test guards: the batch run aborted after
        ``settings.OLLAMA_TIMEOUT`` seconds without producing a fact.
        """
        from pipelines.agent_dspy.universal_modules import _resolve_schema_description

        context = self._build_context()
        schema_description = _resolve_schema_description(self.params)

        started = time.perf_counter()
        try:
            prediction = self.module.extract(
                context=context,
                target_fact_name=self.params.target_fact_name,
                schema_description=schema_description,
                question=self.params.question,
            )
        except Exception as exc:  # pragma: no cover - reproduction path
            elapsed = time.perf_counter() - started
            self.fail(
                f"GenericExtractionSignature call for '{TARGET_FACT_NAME}' of "
                f"{CELEX_ID} failed after {elapsed:.1f}s "
                f"(timeout {settings.OLLAMA_TIMEOUT}s, model "
                f"{settings.LLM_MODEL_ID}): {exc}"
            )

        elapsed = time.perf_counter() - started
        logger.info(
            "Extraction for '%s' of %s took %.1fs", TARGET_FACT_NAME, CELEX_ID, elapsed
        )
        self.assertLess(
            elapsed,
            settings.OLLAMA_TIMEOUT,
            "Extraction ran past the configured request timeout",
        )

        facts = prediction.output.extracted_facts
        self.assertIsInstance(facts, list)
        self.assertTrue(facts, "Extraction returned an empty extracted_facts list")
        # output_type 'str' constrains the model to a single {'value': ...} row.
        self.assertEqual(len(facts), 1)
        self.assertEqual(set(facts[0].keys()), {"value"})
        self.assertIsInstance(facts[0]["value"], str)

    def test_universal_module_extracts_the_case_commissioner(self):
        """The full forward() pass names the Commissioner who adopted AT.39824.

        Regression guard for the closing reserve: the only chunk naming the
        Commissioner is ``doc_pos 170`` (the closing "For the Commission /
        Margrethe VESTAGER" block), which the ranker scores near zero for this
        question. Without ``settings.HYBRID_CLOSING_RESERVE`` it never reaches
        the context and the model answers "Not specified".
        """
        try:
            prediction = self.module.forward(self.params)
        except Exception as exc:  # pragma: no cover - reproduction path
            self.fail(
                f"UniversalFactModule.forward for '{TARGET_FACT_NAME}' of "
                f"{CELEX_ID} failed (model {settings.LLM_MODEL_ID}): {exc}"
            )

        items = prediction.output.items
        self.assertTrue(items, "forward() produced no items")
        self.assertEqual(prediction.output.source, "hybrid")

        values = " ".join(
            str(value) for item in items for value in item.values()
        ).lower()
        self.assertIn(
            EXPECTED_COMMISSIONER_ANCHOR,
            values,
            f"Expected the Competition Commissioner in the extracted value, got {items}",
        )


if __name__ == "__main__":
    unittest.main()
