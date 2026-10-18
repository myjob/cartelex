"""Tests for the two prompt-size budgets guarding UniversalFactModule.

Short decisions (e.g. negative-clearance notifications) embed poorly against
loaded questions, so hybrid ranking can drop the very chunks that name the
parties. When the whole document fits ``SMALL_DOC_CONTEXT_TOKEN_LIMIT`` and the
fact is not section-scoped, the module feeds every chunk to the LLM instead of
retrieving. These tests exercise that decision and the resulting context path
with mocks at every boundary (SQL store, chunk repo, searcher, LLM calls).

The second budget is the circuit breaker ``_check_context_token_limit``
enforces before any request is sent, which every extraction path shares.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import tiktoken

from config.settings import settings
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import (
    UniversalFactModule,
    _check_context_token_limit,
)


def _context_of_tokens(token_count: int) -> str:
    """Return text that tokenises to exactly *token_count* cl100k tokens.

    Repeating a word would leave the count at the mercy of the tokeniser, and
    the whole point of these two tests is where the boundary sits.

    Args:
        token_count: Number of tokens the returned text must encode to.

    Returns:
        Text of exactly that token length under ``cl100k_base``.
    """
    encoding = tiktoken.get_encoding("cl100k_base")
    return encoding.decode([encoding.encode(" undertaking")[0]] * token_count)


def _params(section_scope=None) -> FactExtractorParamVO:
    struct = {
        "target_fact_name": "companies_investigated",
        "question": "list companies under investigation",
        "output_schema": "companies",
        "pipeline": "universal",
        "force_override": True,
    }
    if section_scope is not None:
        struct["section_scope"] = section_scope
    return FactExtractorParamVO.from_yaml_struct(
        celex_id="CELEX-31985D0564", struct=struct, force_override=True
    )


class TestSmallDocBypass(unittest.TestCase):
    """Verify when the module bypasses retrieval and feeds the full document."""

    def _build_module(self, total_tokens: int) -> tuple[UniversalFactModule, MagicMock]:
        sql_store = MagicMock()
        sql_store.select_fact4celex.return_value = []

        chunk_repo = MagicMock()
        chunk_repo.sum_document_tokens.return_value = total_tokens
        chunk_repo.select_chunks_for_sections.return_value = [
            {"doc_pos": 0, "document": "London Cocoa Terminal Market Association Limited"},
            {"doc_pos": 1, "document": "The LCTMA is one of many commodity markets."},
        ]

        searcher = MagicMock()

        module = UniversalFactModule(
            sql_store=sql_store, searcher=searcher, chunk_repo=chunk_repo
        )
        # Stub the LLM-backed steps so no model is called.
        module.extract = MagicMock(
            return_value=SimpleNamespace(
                output=SimpleNamespace(
                    extracted_facts=[
                        {"companies": "London Cocoa Terminal Market Association Limited"}
                    ]
                )
            )
        )
        module.find_doc_references = MagicMock(
            return_value=SimpleNamespace(output=SimpleNamespace(doc_positions=[]))
        )
        return module, searcher

    def test_small_doc_uses_full_context_and_skips_search(self) -> None:
        """Under the token limit and unscoped -> full-document context, no search."""
        module, searcher = self._build_module(
            total_tokens=settings.SMALL_DOC_CONTEXT_TOKEN_LIMIT - 1
        )
        pred = module(params=_params())

        searcher.search.assert_not_called()
        self.assertEqual(pred.output.source, "full_doc")
        self.assertEqual(
            pred.output.items,
            [{"companies": "London Cocoa Terminal Market Association Limited"}],
        )
        # The full-document context was assembled from every chunk.
        context_arg = module.extract.call_args.kwargs["context"]
        self.assertIn("[doc_pos: 0]", context_arg)
        self.assertIn("[doc_pos: 1]", context_arg)

    def test_large_doc_falls_back_to_hybrid_search(self) -> None:
        """Over the token limit -> hybrid retrieval, no full-document fetch."""
        module, searcher = self._build_module(
            total_tokens=settings.SMALL_DOC_CONTEXT_TOKEN_LIMIT + 1
        )
        searcher.search.return_value = [
            SimpleNamespace(doc_pos=3, chunk="some retrieved chunk text")
        ]

        pred = module(params=_params())

        searcher.search.assert_called_once()
        module.chunk_repo.select_chunks_for_sections.assert_not_called()
        self.assertEqual(pred.output.source, "hybrid")

    def test_section_scoped_fact_never_bypasses(self) -> None:
        """A section-scoped fact keeps hybrid retrieval even for a small doc."""
        module, searcher = self._build_module(
            total_tokens=settings.SMALL_DOC_CONTEXT_TOKEN_LIMIT - 1
        )
        searcher.search.return_value = [
            SimpleNamespace(doc_pos=3, chunk="scoped chunk text")
        ]

        self.assertFalse(module._use_full_document(_params(section_scope=["operative"])))
        pred = module(params=_params(section_scope=["operative"]))
        searcher.search.assert_called_once()
        self.assertEqual(pred.output.source, "hybrid")


if __name__ == "__main__":
    unittest.main()


class TestContextTokenCircuitBreaker(unittest.TestCase):
    """The pre-send guard on ``OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT``.

    The limit is a circuit breaker, not a share of the context window: it has
    to clear every legitimate context and still trip long before a prompt
    reaches the size where the server's prefill turns quadratic and costs
    minutes. Both bounds are pinned here because only one of them is obvious.
    """

    def test_measured_worst_case_context_passes(self) -> None:
        """The largest real context must not be rejected.

        Corpus maxima over 744 documents: the 50 largest chunks of a document
        (``HYBRID_SEARCH_TOP_K``) sum to 24,987 tokens, and every fact of one
        CELEX concatenated for the summary reaches ~24,800. Rounded up to
        25,000, that is the number the default has to accommodate.
        """
        _check_context_token_limit(_context_of_tokens(25_000), "worst_case_fact")

    def test_runaway_context_is_rejected_before_the_request(self) -> None:
        """Above the limit the chain breaks in milliseconds, not six minutes."""
        oversized = _context_of_tokens(
            settings.OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT + 1_000
        )

        with self.assertRaises(ValueError) as raised:
            _check_context_token_limit(oversized, "runaway_fact")

        self.assertIn(
            str(settings.OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT), str(raised.exception)
        )
