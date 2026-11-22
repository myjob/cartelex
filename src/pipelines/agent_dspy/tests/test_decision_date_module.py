"""Unit tests for ``DecisionDateModule``.

The date rules are covered by ``pipelines/legal/tests/test_decision_date.py``;
these tests cover the slick pipe (date line, title, LLM fallback), text source
resolution (markdown file, chunk fallback), ``references_doc`` and the
existing-fact short-circuit. Store, chunk repository and the fallback module
are fakes at the DB / LLM boundary; markdown lives in a temporary directory.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dspy

from pipelines.agent_dspy.dspy_protocols import FactOutput
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import DecisionDateModule

_TARGET = "eu_commission_decision_date"
_CELEX = "DEC-AT39168-20110331"
_TITLE = "# COMMISSION DECISION\n\nof 31 March 2011\n\nrelating to a proceeding (Case COMP/39.168)\n\n"
_BODY = "In its Decision of 19 September 2002 (Closures) the Commission found ...\n\n"
_CLOSING = "Done at Brussels, 31 March 2011.\n\nFor the Commission\n"


def _chunk(doc_pos: int, document: str) -> dict:
    """A chunk row as returned by ``select_chunks_for_sections``."""
    return {"id": f"c{doc_pos}", "doc_pos": doc_pos, "document": document, "section_type": None}


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

    def select_source_paths(self, celex_id: str) -> list[str]:
        return self.paths

    def select_chunks_for_sections(self, celex_id: str) -> list[dict]:
        return self.chunks


class _FakeFallback:
    """LLM fallback stub recording the params it was called with."""

    def __init__(self, items: list[dict] | None = None, error: Exception | None = None):
        self.items = items or []
        self.error = error
        self.calls: list[FactExtractorParamVO] = []

    def __call__(self, params: FactExtractorParamVO) -> dspy.Prediction:
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        return dspy.Prediction(output=FactOutput(items=self.items, source="facts_context"))


def _params(force_override: bool = True) -> FactExtractorParamVO:
    """Extraction params as built from the catalog entry."""
    return FactExtractorParamVO(
        target_fact_name=_TARGET,
        question="When did the EU commission decided this case?",
        celex_id=_CELEX,
        output_schema="str",
        pipeline="decision_date",
        context_facts=["heideltime_event_agg"],
        force_override=force_override,
    )


class TestDecisionDateModule(unittest.TestCase):
    """The module reads the date from the text and calls the LLM only as fallback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.docs = Path(self._tmp.name)
        patcher = patch(
            "pipelines.agent_dspy.universal_modules.settings.CELEX_DOCS_DIR", str(self.docs)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _forward(self, paths, chunks, fallback=None, store=None, force_override=True):
        self.fallback = fallback or _FakeFallback()
        module = DecisionDateModule(
            sql_store=store or _FakeStore(),
            chunk_repo=_FakeChunkRepo(paths, chunks),
            fallback=self.fallback,
        )
        return module(_params(force_override)).output

    def test_date_line_from_markdown_with_chunk_reference(self):
        md = self.docs / "decision.md"
        md.write_text(_TITLE + _BODY + _CLOSING, encoding="utf-8")
        chunks = [_chunk(0, _TITLE), _chunk(1, _BODY), _chunk(2, _CLOSING)]
        out = self._forward([str(md)], chunks)
        self.assertEqual(out.items, [{_TARGET: "2011-03-31"}])
        self.assertEqual(out.source, "decision_date:date_line")
        self.assertEqual(out.references_doc, [2])
        self.assertEqual(self.fallback.calls, [])

    def test_chunks_used_when_no_markdown_file(self):
        chunks = [_chunk(0, _TITLE), _chunk(1, _BODY), _chunk(2, _CLOSING)]
        out = self._forward([str(self.docs / "missing.md")], chunks)
        self.assertEqual(out.items, [{_TARGET: "2011-03-31"}])

    def test_title_date_when_date_line_is_blank(self):
        chunks = [_chunk(0, _TITLE), _chunk(1, _BODY), _chunk(2, "Done at Brussels,\n\nFor the Commission\n")]
        out = self._forward([], chunks)
        self.assertEqual(out.items, [{_TARGET: "2011-03-31"}])
        self.assertEqual(out.source, "decision_date:title")
        self.assertEqual(out.references_doc, [0])

    def test_split_date_line_has_no_reference(self):
        chunks = [_chunk(0, _BODY + "Done at Brussels,"), _chunk(1, "31 March 2011.\n")]
        out = self._forward([], chunks)
        self.assertEqual(out.items, [{_TARGET: "2011-03-31"}])
        self.assertIsNone(out.references_doc)

    def test_fallback_only_without_text_date(self):
        fallback = _FakeFallback(items=[{"value": "The decision was adopted on 2011-03-31."}])
        out = self._forward([], [_chunk(0, _BODY)], fallback=fallback)
        self.assertEqual(out.items, [{_TARGET: "2011-03-31"}])
        self.assertEqual(len(fallback.calls), 1)
        self.assertEqual(fallback.calls[0].predecessors, ["heideltime_event_agg"])
        self.assertTrue(fallback.calls[0].force_override)

    def test_fallback_without_date_or_input_is_na(self):
        for fallback in (_FakeFallback(items=[{"value": "NA"}]), _FakeFallback(error=ValueError("missing"))):
            with self.subTest(fallback=fallback):
                out = self._forward([], [_chunk(0, _BODY)], fallback=fallback)
                self.assertEqual(out.items, [])

    def test_existing_fact_short_circuits(self):
        row = SimpleNamespace(value_json=[{_TARGET: "2011-03-31"}], value_md="2011-03-31")
        out = self._forward([], [], store=_FakeStore([row]), force_override=False)
        self.assertEqual(out.items, [{_TARGET: "2011-03-31"}])
        self.assertEqual(self.fallback.calls, [])


if __name__ == "__main__":
    unittest.main()
