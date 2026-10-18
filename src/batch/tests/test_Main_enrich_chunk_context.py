"""Unit tests for the contextual-enrichment batch (DB, LLM, embedder mocked).

``build_embedding_input`` is pure and tested directly; ``_enrich_celex`` is
exercised with a MagicMock repository, a stub prefix module and a patched
embedding function.
"""

import unittest
from unittest.mock import MagicMock, patch

import dspy
import tiktoken

from batch.Main_enrich_chunk_context import (
    _enrich_celex,
    build_embedding_input,
)

_ENCODING = tiktoken.get_encoding("cl100k_base")


def _token_count(text: str) -> int:
    return len(_ENCODING.encode(text))


class TestBuildEmbeddingInput(unittest.TestCase):
    def test_under_limit_is_untruncated(self) -> None:
        result = build_embedding_input("Case X, fines section.", "The fine is EUR 30m.", 2048)
        self.assertEqual(result, "Case X, fines section.\n\nThe fine is EUR 30m.")

    def test_empty_prefix_returns_document(self) -> None:
        self.assertEqual(build_embedding_input("", "chunk text", 2048), "chunk text")

    def test_over_limit_truncates_document_tail_only(self) -> None:
        prefix = "Case X, fines section, final amounts per addressee."
        document = "word " * 3000  # far over any small limit
        limit = 200
        result = build_embedding_input(prefix, document, limit)
        self.assertLessEqual(_token_count(result), limit)
        self.assertTrue(result.startswith(f"{prefix}\n\n"))  # prefix survives intact

    def test_pathological_oversized_prefix_falls_back_to_document(self) -> None:
        prefix = "prefix " * 500
        document = "the actual chunk content"
        limit = 20
        result = build_embedding_input(prefix, document, limit)
        self.assertLessEqual(_token_count(result), limit)
        self.assertIn("the actual chunk", result)


class _StubContextModule:
    """ChunkContextModule stand-in returning a canned prefix."""

    def __init__(self, prefix: str = "Case X, fines section: sets the amounts.") -> None:
        self.calls: list[dict] = []
        self._prefix = prefix

    def __call__(self, **kwargs) -> dspy.Prediction:
        self.calls.append(kwargs)
        return dspy.Prediction(context_prefix=self._prefix)


def _chunk(chunk_id: str, doc_pos: int, document: str, section_type: str | None = None) -> dict:
    return {
        "id": chunk_id,
        "doc_pos": doc_pos,
        "document": document,
        "section_type": section_type,
    }


def _stats() -> dict[str, int]:
    return {"enriched": 0, "skipped_existing": 0, "errors": 0}


class TestEnrichCelex(unittest.TestCase):
    def _repo(self, chunks: list[dict], missing_ids: set[str]) -> MagicMock:
        repo = MagicMock()
        repo.select_chunks_for_sections.return_value = chunks
        repo.select_chunks_missing_context.return_value = [
            c for c in chunks if c["id"] in missing_ids
        ]
        repo.select_first_page.return_value = "COMMISSION DECISION of 5 June 2002 (Case X)"
        return repo

    def test_dry_run_writes_nothing_and_calls_no_model(self) -> None:
        chunks = [_chunk("c0", 0, "cover"), _chunk("c1", 1, "body")]
        repo = self._repo(chunks, missing_ids={"c0", "c1"})
        stats = _stats()
        with patch("batch.Main_enrich_chunk_context.retrieve_qwen3_embedding") as embed:
            _enrich_celex(repo, None, "CELEX-TEST", dry_run=True, limit=None, stats=stats)
        repo.update_chunk_context.assert_not_called()
        embed.assert_not_called()
        self.assertEqual(stats["enriched"], 2)

    def test_only_missing_chunks_are_enriched(self) -> None:
        """Resumability: rows with an existing prefix are never rewritten."""
        chunks = [
            _chunk("c0", 0, "cover"),
            _chunk("c1", 1, "# I. THE FACTS\nfacts text", "recitals_facts"),
            _chunk("c2", 2, "fines text", "fines"),
        ]
        repo = self._repo(chunks, missing_ids={"c2"})
        module = _StubContextModule()
        stats = _stats()
        with patch(
            "batch.Main_enrich_chunk_context.retrieve_qwen3_embedding",
            return_value=[0.1, 0.2],
        ):
            _enrich_celex(repo, module, "CELEX-TEST", dry_run=False, limit=None, stats=stats)

        self.assertEqual(repo.update_chunk_context.call_count, 1)
        written = repo.update_chunk_context.call_args
        self.assertEqual(written.args[0], "c2")
        self.assertEqual(written.args[1], "Case X, fines section: sets the amounts.")
        self.assertEqual(written.args[2], [0.1, 0.2])
        self.assertEqual(stats["enriched"], 1)
        self.assertEqual(stats["skipped_existing"], 2)

    def test_prompt_receives_section_and_preceding_heading(self) -> None:
        chunks = [
            _chunk("c0", 0, "# COMMISSION DECISION\ncover"),
            _chunk("c1", 1, "# I. THE FACTS\nfacts text", "recitals_facts"),
            _chunk("c2", 2, "unheaded fines text", "fines"),
        ]
        repo = self._repo(chunks, missing_ids={"c2"})
        module = _StubContextModule()
        with patch(
            "batch.Main_enrich_chunk_context.retrieve_qwen3_embedding",
            return_value=[0.1],
        ):
            _enrich_celex(repo, module, "CELEX-TEST", dry_run=False, limit=None, stats=_stats())

        call = module.calls[0]
        self.assertEqual(call["section_label"], "fines")
        self.assertEqual(call["preceding_heading"], "I. THE FACTS")
        self.assertEqual(call["celex_id"], "CELEX-TEST")
        self.assertTrue(call["case_hint"].startswith("COMMISSION DECISION"))

    def test_module_failure_counts_error_and_writes_nothing(self) -> None:
        chunks = [_chunk("c0", 0, "text")]
        repo = self._repo(chunks, missing_ids={"c0"})
        failing_module = MagicMock(side_effect=RuntimeError("LM down"))
        stats = _stats()
        with patch("batch.Main_enrich_chunk_context.retrieve_qwen3_embedding"):
            _enrich_celex(
                repo, failing_module, "CELEX-TEST", dry_run=False, limit=None, stats=stats
            )
        repo.update_chunk_context.assert_not_called()
        self.assertEqual(stats["errors"], 1)

    def test_limit_caps_enriched_chunks(self) -> None:
        chunks = [_chunk(f"c{i}", i, f"text {i}") for i in range(5)]
        repo = self._repo(chunks, missing_ids={c["id"] for c in chunks})
        module = _StubContextModule()
        stats = _stats()
        with patch(
            "batch.Main_enrich_chunk_context.retrieve_qwen3_embedding",
            return_value=[0.1],
        ):
            _enrich_celex(repo, module, "CELEX-TEST", dry_run=False, limit=2, stats=stats)
        self.assertEqual(repo.update_chunk_context.call_count, 2)
        self.assertEqual(stats["enriched"], 2)

    def test_no_missing_chunks_is_a_noop(self) -> None:
        chunks = [_chunk("c0", 0, "text")]
        repo = self._repo(chunks, missing_ids=set())
        _enrich_celex(repo, None, "CELEX-TEST", dry_run=False, limit=None, stats=_stats())
        repo.select_chunks_for_sections.assert_not_called()
        repo.update_chunk_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
