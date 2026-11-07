"""Unit tests for ``RecitalCountModule``.

The counting rules are covered by ``pipelines/legal/tests/test_recital_count.py``;
these tests cover source resolution (ingested markdown, conventional path,
chunk fallback), provenance fields, the chunk-gap downgrade, ``references_doc``
and the existing-fact short-circuit. Store and chunk repository are fakes at
the DB boundary; markdown lives in a temporary directory.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import RecitalCountModule
from pipelines.legal.recital_count import (
    REASON_CHUNK_GAP,
    REASON_OPERATIVE_NOT_SEEN,
    RecitalThresholds,
)

_TARGET = "recitals_max"
_OPERATIVE = "HAS ADOPTED THIS DECISION:\n\n"
_CELEX = "40099_9993_3"
T = RecitalThresholds(
    max_gap=3, max_start=3, min_chain=5, min_coverage=0.6, min_coverage_confident=0.9
)


def _seq(nums) -> str:
    """Render recital numbers as line-start ``(n)`` paragraphs."""
    return "\n\n".join(f"({n}) Text of recital {n}." for n in nums)


def _chunk(doc_pos: int, document: str) -> dict:
    """A chunk row as returned by ``select_chunks_for_sections``."""
    return {"id": f"c{doc_pos}", "doc_pos": doc_pos, "document": document, "section_type": None}


def _chunks(nums, doc_pos: list[int] | None = None) -> list[dict]:
    """Spread recitals evenly over chunks at the given positions (default one chunk)."""
    nums = list(nums)
    positions = doc_pos or [0]
    size = -(-len(nums) // len(positions))
    return [
        _chunk(pos, _seq(nums[i * size:(i + 1) * size])) for i, pos in enumerate(positions)
    ]


class _FakeStore:
    """``select_fact4celex`` stub; ``rows`` is returned for every fact name."""

    def __init__(self, rows: list | None = None):
        self.rows = rows or []

    def select_fact4celex(self, celex_id: str, fact_name: str) -> list:
        return self.rows


class _FakeChunkRepo:
    """Chunk repository stub serving fixed source paths and chunks."""

    def __init__(self, paths: list[str], chunks: list[dict]):
        self.paths = paths
        self.chunks = chunks
        self.calls = 0

    def select_source_paths(self, celex_id: str) -> list[str]:
        self.calls += 1
        return self.paths

    def select_chunks_for_sections(self, celex_id: str) -> list[dict]:
        self.calls += 1
        return self.chunks


def _params(force_override: bool = True) -> FactExtractorParamVO:
    """Extraction params for the recitals_max fact."""
    return FactExtractorParamVO(
        target_fact_name=_TARGET,
        question="Highest recital number",
        celex_id=_CELEX,
        output_schema="",
        output_type="dict",
        pipeline="recital_count",
        force_override=force_override,
    )


class TestRecitalCountModule(unittest.TestCase):
    """The module resolves the text, counts recitals and shapes the FactOutput."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.docs = Path(self._tmp.name)
        self.md = self.docs / "ingested.md"
        self.md.write_text(_seq(range(1, 31)), encoding="utf-8")
        patcher = patch(
            "pipelines.agent_dspy.universal_modules.settings.CELEX_DOCS_DIR", str(self.docs)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_conventional(self) -> Path:
        path = self.docs / _CELEX / "mineru" / "auto" / f"{_CELEX}.md"
        path.parent.mkdir(parents=True)
        path.write_text(_seq(range(1, 26)), encoding="utf-8")
        return path

    def _forward(self, paths, chunks, store=None, force_override=True):
        module = RecitalCountModule(
            sql_store=store or _FakeStore(), chunk_repo=_FakeChunkRepo(paths, chunks), thresholds=T
        )
        return module(_params(force_override))

    def _run(self, paths, chunks) -> dict:
        items = self._forward(paths, chunks).output.items
        self.assertEqual(len(items), 1)
        return items[0]

    def test_markdown_from_chunk_path_preferred(self):
        r = self._run([str(self.md)], _chunks(range(1, 21)))
        self.assertEqual(
            (r["recitals_max"], r["text_source"], r["source_path"]), (30, "markdown", str(self.md))
        )

    def test_missing_recorded_path_falls_back(self):
        r = self._run(["/nope/missing.md"], _chunks(range(1, 21)) + [_chunk(1, _OPERATIVE)])
        self.assertEqual((r["recitals_max"], r["text_source"], r["source_path"]), (20, "chunks", ""))

    def test_conventional_path_used_without_chunks(self):
        conventional = self._write_conventional()
        r = self._run([], [])
        self.assertEqual(
            (r["recitals_max"], r["text_source"], r["source_path"]),
            (25, "markdown", str(conventional)),
        )

    def test_chunk_gap_after_last_recital_downgrades(self):
        chunks = _chunks(range(1, 21), doc_pos=[0, 1, 2, 3]) + [_chunk(5, _OPERATIVE + "Article 1")]
        r = self._run([], chunks)
        self.assertEqual(
            (r["recitals_max"], r["status"], r["reason"], r["chunk_gaps"]),
            (20, "doubtful", REASON_CHUNK_GAP, 1),
        )

    def test_chunk_gap_after_last_recital_when_first_missing(self):
        # chain starts at (2): the downgrade must not depend on finding "(1)"
        chunks = _chunks(range(2, 21), doc_pos=[0, 1, 2, 3]) + [_chunk(5, _OPERATIVE + "Article 1")]
        r = self._run([], chunks)
        self.assertEqual((r["status"], r["reason"]), ("doubtful", REASON_CHUNK_GAP))

    def test_chunks_without_operative_part_not_confident(self):
        # lost tail chunks leave no gap; without the operative marker the max may be truncated
        r = self._run([], _chunks(range(1, 21), doc_pos=[0, 1]))
        self.assertEqual((r["status"], r["reason"]), ("doubtful", REASON_OPERATIVE_NOT_SEEN))

    def test_chunk_gap_before_last_recital_keeps_confident(self):
        chunks = _chunks(range(1, 11), doc_pos=[0]) + _chunks(range(11, 21), doc_pos=[2]) + [
            _chunk(3, _OPERATIVE + "Article 1")
        ]
        r = self._run([], chunks)
        self.assertEqual((r["status"], r["chunk_gaps"]), ("confident", 1))

    def test_no_source_text_is_undetermined(self):
        r = self._run([], [])
        self.assertEqual(
            (r["recitals_max"], r["status"], r["numbering"]), (None, "undetermined", "none")
        )

    def test_references_doc_points_at_first_and_last(self):
        pred = self._forward([str(self.md)], _chunks(range(1, 31), doc_pos=[0, 1, 2]))
        self.assertEqual(pred.output.references_doc, [0, 2])

    def test_references_doc_none_without_chunks(self):
        self._write_conventional()
        self.assertIsNone(self._forward([], []).output.references_doc)

    def test_existing_fact_short_circuits(self):
        repo = _FakeChunkRepo([str(self.md)], [])
        stored = SimpleNamespace(value_md="30", value_json=[{"recitals_max": 30}], name=_TARGET)
        module = RecitalCountModule(sql_store=_FakeStore([stored]), chunk_repo=repo, thresholds=T)
        module(_params(force_override=False))
        self.assertEqual(repo.calls, 0)


if __name__ == "__main__":
    unittest.main()
