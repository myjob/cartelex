"""Full integration test: universal pipeline for infringement_subcategory_vertical_restraints.

Exercises the complete live stack for CELEX-31990D0645:
  live document context (PostgreSQL + pgvector) -> UniversalFactModule DSPy
  extraction (live OLLAMA)

UniversalFactModule picks the context leg itself: hybrid retrieval (FTS +
pgvector fused by RRF, FlashRank rerank, source='hybrid') or the
small-document bypass that feeds every chunk when the whole document fits
settings.SMALL_DOC_CONTEXT_TOKEN_LIMIT and the fact is not section-scoped
(source='full_doc'). 31990D0645 is small and this fact carries no
section_scope, so the bypass is the expected leg here; the assertions accept
either live leg and reject the cached ('facts') and empty ('none') ones.

Compares qwen3-coder:30b (baseline) vs qwen3.6:35b (no-think candidate) and
asserts that real hybrid retrieval surfaces the expected evidence chunks and
that both models extract structurally valid vertical-restraint categories.

Requires:
  - OLLAMA running with both models pulled
  - PostgreSQL with celex_facts schema and CELEX-31990D0645 indexed
"""

import logging
import time
import unittest

import dspy
import yaml

from config.llm_thinking import THINKING_DISABLED, thinking_kwargs
from config.settings import settings
from extract.rag.hybrid_search import HybridSearch
from extract.rag.ranker.flash_ranker import FlashRanker
from pipelines.agent_dspy.dspy_protocols import FactOutput
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import UniversalFactModule
from pipelines.rag.pgvector.fact_repository import FactRepository
from tools.celex_tools import CelexDatabaseToolkit

logger = logging.getLogger(__name__)

# CELEX ids are stored WITH the "CELEX-" prefix in celex_facts.pg_embedding_qwen;
# stripping it makes every WHERE celex_id = ... match nothing.
CELEX_ID = "CELEX-31990D0645"
FACT_NAME = "infringement_subcategory_vertical_restraints"
MODEL_BASELINE = "qwen3-coder:30b"
MODEL_CANDIDATE = "qwen3.6:35b"

# doc_pos values hybrid retrieval is expected to surface as supporting evidence.
EXPECTED_DOC_POSITIONS: frozenset[int] = frozenset({10, 9, 7})

# FactOutput.source values that prove context came from the live document.
# 'facts' is the cached-SQL leg (excluded via force_override) and 'none' means
# zero context was assembled.
_LIVE_CONTEXT_SOURCES: frozenset[str] = frozenset({"hybrid", "full_doc"})

# Category tokens drawn verbatim from the FACT_EXTRACTOR.yaml question text.
# "NA" is the valid fallback when no restraint is found.
_VALID_CATEGORY_TOKENS: frozenset[str] = frozenset({
    "exclusive dealing",
    "market foreclosure",
    "territorial exclusivity",
    "parallel trade",
    "resale price maintenance",
    "rpm",
    "bundling",
    "tying",
    "selective distribution",
    "franchising",
    "discounts",
    "rebates",
    "na",
})


def _contains_valid_category(items: list[dict]) -> bool:
    """
    Return True when at least one extracted item value contains a recognised category token.

    Parameters
    ----------
    items:
        The FactOutput.items list.

    Returns
    -------
    bool
        True if any token from _VALID_CATEGORY_TOKENS appears (case-insensitive)
        in any item value.
    """
    flat = " ".join(str(v).lower() for item in items for v in item.values())
    return any(token in flat for token in _VALID_CATEGORY_TOKENS)


def _load_fact_param(celex_id: str, fact_name: str) -> FactExtractorParamVO:
    """
    Load a FactExtractorParamVO from settings.FACT_EXTRACTOR_CONFIG_DIR.

    Parameters
    ----------
    celex_id:
        CELEX document identifier injected at runtime.
    fact_name:
        target_fact_name to look up in the YAML config.

    Returns
    -------
    FactExtractorParamVO

    Raises
    ------
    KeyError
        If fact_name is absent from FACT_EXTRACTOR.yaml.
    """
    with settings.FACT_EXTRACTOR_CONFIG_DIR.open("r", encoding="utf-8") as fh:
        entries = yaml.safe_load(fh)
    struct = next(
        (e for e in entries if e.get("target_fact_name") == fact_name), None
    )
    if struct is None:
        raise KeyError(f"{fact_name!r} not found in FACT_EXTRACTOR.yaml")
    # force_override=True so the SQL existence check is bypassed each run.
    return FactExtractorParamVO.from_yaml_struct(celex_id, struct, force_override=True)


class VerticalRestraintsUniversalPipelineTest(unittest.TestCase):
    """
    Integration test: UniversalFactModule on infringement_subcategory_vertical_restraints.

    Runs real retrieval (hybrid or the small-document full-context bypass) plus
    LLM extraction for CELEX-31990D0645 and compares qwen3-coder:30b (baseline)
    vs qwen3.6:35b (no-think).
    """

    agent: UniversalFactModule
    hybrid_search: HybridSearch
    lm_baseline: dspy.LM
    lm_candidate: dspy.LM
    params: FactExtractorParamVO

    @classmethod
    def setUpClass(cls) -> None:
        """
        Provision DB connections, configure both LMs, build the shared pipeline.

        Skips the entire test class if PostgreSQL is unavailable.
        """
        try:
            cls.fact_repo = FactRepository()
        except Exception as exc:
            raise unittest.SkipTest(f"PostgreSQL (celex_facts) unavailable: {exc}")

        # Both arms are pinned to no-think explicitly rather than through
        # settings.LLM_THINKING_MODE, so the comparison does not shift with the
        # environment the test happens to run in.
        baseline_model_name = f"openai/{MODEL_BASELINE}"
        candidate_model_name = f"openai/{MODEL_CANDIDATE}"
        cls.lm_baseline = dspy.LM(
            baseline_model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=False,
            timeout=settings.OLLAMA_TIMEOUT,
            **thinking_kwargs(baseline_model_name, mode=THINKING_DISABLED),
        )
        cls.lm_candidate = dspy.LM(
            candidate_model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=False,
            timeout=settings.OLLAMA_TIMEOUT,
            **thinking_kwargs(candidate_model_name, mode=THINKING_DISABLED),
        )

        toolkit = CelexDatabaseToolkit(fact_repo=cls.fact_repo)
        cls.hybrid_search = HybridSearch(
            repo=toolkit.chunk_repo,
            ranker=FlashRanker(),
            embed_query=toolkit.embed_query,
        )
        cls.agent = UniversalFactModule(
            sql_store=cls.fact_repo,
            searcher=cls.hybrid_search,
        )
        cls.params = _load_fact_param(CELEX_ID, FACT_NAME)

    def test_expected_chunks_retrieved(self) -> None:
        """
        Assert real hybrid retrieval surfaces the expected evidence chunks.

        This is deterministic (FTS + semantic vector search fused via RRF, then
        FlashRank rerank, no LLM), so it isolates the retrieval layer from
        extraction quality. The lexical leg runs Postgres FTS on the raw
        question the same way as production.
        """
        ranked = self.hybrid_search.search(
            celex_id=CELEX_ID,
            query=self.params.question,
            top_k=settings.HYBRID_SEARCH_TOP_K,
        )
        retrieved_positions = {c.doc_pos for c in ranked}
        logger.info(
            "Hybrid retrieval for %s surfaced doc_pos=%s",
            CELEX_ID, sorted(retrieved_positions, reverse=True),
        )
        self.assertTrue(
            ranked,
            f"Hybrid retrieval returned no chunks for {CELEX_ID}. "
            "Check the celex_id format (stored WITH the 'CELEX-' prefix).",
        )
        missing = EXPECTED_DOC_POSITIONS - retrieved_positions
        self.assertFalse(
            missing,
            f"Hybrid retrieval did not surface expected doc_pos {sorted(missing)}. "
            f"Got: {sorted(retrieved_positions, reverse=True)}",
        )

    def _run_pipeline(self, lm: dspy.LM, model_id: str) -> tuple[FactOutput, float]:
        """
        Configure the DSPy LM globally, invoke the universal pipeline, return the result.

        Parameters
        ----------
        lm:
            The DSPy LM instance to activate via dspy.configure before the call.
        model_id:
            Human-readable model name used in log messages.

        Returns
        -------
        tuple[FactOutput, float]
            Extracted output and wall-clock elapsed time in seconds.
        """
        dspy.configure(lm=lm)
        t0 = time.perf_counter()
        prediction = self.agent(params=self.params)
        elapsed = round(time.perf_counter() - t0, 2)
        output: FactOutput = prediction.output
        logger.info(
            "[%s] source=%s  items=%d  references=%s  elapsed=%.1fs",
            model_id, output.source, len(output.items), output.references_doc, elapsed,
        )
        return output, elapsed

    def test_extraction_and_comparison(self) -> None:
        """
        Run both models through the full universal pipeline, print a comparison
        report, and assert each model returns structurally valid output backed by
        real retrieval.
        """
        logger.info("Running baseline %s for %s / %s ...", MODEL_BASELINE, CELEX_ID, FACT_NAME)
        baseline_output, baseline_s = self._run_pipeline(self.lm_baseline, MODEL_BASELINE)

        logger.info("Running candidate %s for %s / %s ...", MODEL_CANDIDATE, CELEX_ID, FACT_NAME)
        candidate_output, candidate_s = self._run_pipeline(self.lm_candidate, MODEL_CANDIDATE)

        self._print_comparison(baseline_output, baseline_s, candidate_output, candidate_s)

        self._assert_output(baseline_output, MODEL_BASELINE)
        self._assert_output(candidate_output, MODEL_CANDIDATE)

    @staticmethod
    def _item_summary(output: FactOutput, max_items: int = 3) -> str:
        """
        Return a compact one-line summary of extracted items.

        Parameters
        ----------
        output:
            The FactOutput to summarise.
        max_items:
            Maximum number of items shown before appending a count suffix.

        Returns
        -------
        str
            Human-readable summary string.
        """
        if not output.items:
            return f"(no items — source: {output.source})"
        head = output.items[:max_items]
        values = "; ".join(
            str(next(iter(item.values()), "(empty)")) for item in head
        )
        suffix = (
            f" ... (+{len(output.items) - max_items} more)"
            if len(output.items) > max_items
            else ""
        )
        return values + suffix

    def _print_comparison(
        self,
        b_out: FactOutput,
        b_s: float,
        c_out: FactOutput,
        c_s: float,
    ) -> None:
        """
        Print a formatted side-by-side comparison table to stdout.

        Parameters
        ----------
        b_out:
            Baseline model FactOutput.
        b_s:
            Baseline elapsed time in seconds.
        c_out:
            Candidate model FactOutput.
        c_s:
            Candidate elapsed time in seconds.
        """
        sep = "-" * 80
        print(f"\n{sep}")
        print(f"INTEGRATION  {CELEX_ID} / {FACT_NAME}")
        print(sep)
        print(f"  Baseline  {MODEL_BASELINE:<24}  {b_s:>6.1f}s  source={b_out.source}  refs={b_out.references_doc}")
        print(f"  Answer:   {self._item_summary(b_out)}")
        print(f"  Candidate {MODEL_CANDIDATE:<24}  {c_s:>6.1f}s  source={c_out.source}  refs={c_out.references_doc}")
        print(f"  Answer:   {self._item_summary(c_out)}")
        if b_out.items or c_out.items:
            faster = MODEL_BASELINE if b_s <= c_s else MODEL_CANDIDATE
            print(f"  Faster model: {faster}")
        print(f"{sep}\n")

    def _assert_output(self, output: FactOutput, model_id: str) -> None:
        """
        Assert that a single model's FactOutput meets structural quality constraints.

        Parameters
        ----------
        output:
            The FactOutput to validate.
        model_id:
            Model name included in assertion messages.

        Raises
        ------
        AssertionError
            If retrieval found no context, no items were extracted, or the items
            contain no recognised vertical-restraint category token.
        """
        self.assertIsInstance(
            output, FactOutput, f"{model_id} did not return a FactOutput"
        )
        self.assertIn(
            output.source,
            _LIVE_CONTEXT_SOURCES,
            f"{model_id} expected source in {sorted(_LIVE_CONTEXT_SOURCES)} "
            f"(live document context); got '{output.source}'. source='none' means "
            "zero chunks were retrieved; source='facts' means a cached SQL fact "
            "was returned instead of a live run.",
        )
        self.assertTrue(
            len(output.items) > 0,
            f"{model_id} extracted no items. Expected at least one vertical-restraint "
            "subcategory or 'NA'.",
        )
        self.assertTrue(
            _contains_valid_category(output.items),
            f"{model_id} items contain no recognised vertical-restraint category "
            f"or 'NA': {output.items}",
        )


if __name__ == "__main__":
    unittest.main()
