"""Tests for provenance-reference attachment in the QA retrieval pipeline.

Covers:
  1. ``_collect_references`` — dedupe, empty input, and DB-error resilience.
  2. ``_format_reference_block`` — label is ``ancestry_path_names``; chunk
     terminals embed an inline collapsible ``view`` section.
  3. ``QARetrievalPipeline.forward`` — reference appended for a single resolved
     celex (driven by the executor's typed ``fact_names``), and skipped when no
     ``lineage_repo`` is injected.

Uses unittest with MagicMock — no live DB or LLM required.
"""

import unittest
from unittest.mock import MagicMock

import dspy

from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep
from pipelines.agent_dspy.retrieval_modules import (
    QARetrievalPipeline,
)
from pipelines.agent_dspy.retrieval_provenance import (
    _collect_fallback_references,
    _collect_references,
    _compute_highlight_terms,
    _filter_grounded_fact_names,
    _format_reference_block,
    _resolve_question_fact_names,
)
from pipelines.rag.pgvector.lineage_repository import FactProvenanceRow
from tools.tests.celex_index_fixture import FakeCelexChecker


_FakeCelexChecker = FakeCelexChecker


def _row(ancestry: str | None, chunk_id: str | None = None, depth: int = 0):
    return FactProvenanceRow(
        ancestry_path_names=ancestry, chunk_id=chunk_id, depth=depth
    )


# ---------------------------------------------------------------------------
# _filter_grounded_fact_names
# ---------------------------------------------------------------------------


class TestFilterGroundedFactNames(unittest.TestCase):
    def test_empty_fact_names_returns_empty(self):
        self.assertEqual(_filter_grounded_fact_names([], "answer", {}), [])

    def test_name_absent_from_dump_is_kept(self):
        # court_list came from RAG/fallback, not the case_facts dump → keep it
        # regardless of literal overlap with the answer.
        out = _filter_grounded_fact_names(
            ["court_list"], "Court: European Commission", {}
        )
        self.assertEqual(out, ["court_list"])

    def test_dump_fact_grounded_in_answer_is_kept(self):
        values = {"authorities_canoncial": "European Commission"}
        out = _filter_grounded_fact_names(
            ["authorities_canoncial"],
            "Court: European Commission (European Community)",
            values,
        )
        self.assertEqual(out, ["authorities_canoncial"])

    def test_dump_fact_absent_from_answer_is_dropped(self):
        values = {
            "market_geographical": "Belgium",
            "companies_industry": "insurance",
        }
        out = _filter_grounded_fact_names(
            ["market_geographical", "companies_industry"],
            "Court: European Commission",
            values,
        )
        self.assertEqual(out, [])

    def test_table_value_dropped_when_answer_ignores_it(self):
        fines = "| company   | amount      |\n| La Poste  | 2.5 million |"
        out = _filter_grounded_fact_names(
            ["fines"], "The court is the European Commission.", {"fines": fines}
        )
        self.assertEqual(out, [])

    def test_empty_or_numeric_value_dropped(self):
        values = {"human_index_year": "", "decision_chapter_tokens": "- 310"}
        out = _filter_grounded_fact_names(
            ["human_index_year", "decision_chapter_tokens"],
            "some answer with words",
            values,
        )
        self.assertEqual(out, [])

    def test_mixed_dump_keeps_only_grounded(self):
        values = {
            "authorities_canoncial": "European Commission",
            "market_geographical": "Belgium",
        }
        out = _filter_grounded_fact_names(
            ["authorities_canoncial", "market_geographical", "court_list"],
            "Court: European Commission (European Community)",
            values,
        )
        # authorities grounded; market dropped; court_list kept (not in dump).
        self.assertEqual(out, ["authorities_canoncial", "court_list"])


# ---------------------------------------------------------------------------
# _collect_references
# ---------------------------------------------------------------------------


class TestCollectReferences(unittest.TestCase):
    def test_empty_fact_names_returns_empty(self):
        repo = MagicMock()
        self.assertEqual(_collect_references(repo, "CELEX-1", []), [])
        repo.get_fact_provenance.assert_not_called()

    def test_dedupes_identical_ancestry_paths(self):
        repo = MagicMock()
        repo.get_fact_provenance.side_effect = [
            [_row("a -> b", "chunk-1", 0), _row("a -> b", "chunk-1", 0)],
            [_row("a -> b", "chunk-1", 0), _row("c -> d", "chunk-2", 1)],
        ]
        rows = _collect_references(repo, "CELEX-1", ["fines", "court_list"])
        self.assertEqual(
            [r.ancestry_path_names for r in rows], ["a -> b", "c -> d"]
        )

    def test_db_error_is_swallowed(self):
        repo = MagicMock()
        repo.get_fact_provenance.side_effect = RuntimeError("connection lost")
        self.assertEqual(_collect_references(repo, "CELEX-1", ["fines"]), [])


# ---------------------------------------------------------------------------
# _format_reference_block
# ---------------------------------------------------------------------------


class TestFormatReferenceBlock(unittest.TestCase):
    def test_empty_rows_returns_empty_string(self):
        self.assertEqual(_format_reference_block([]), "")

    def test_renders_collapsed_details_with_label_as_summary(self):
        rows = [_row("court_list -> court_list_raw -> chunk 31", "chunk-abc", 0)]
        loader = MagicMock()
        loader.get_chunk_by_id.return_value = {
            "chunk_id": "chunk-abc",
            "celex_id": "CELEX-1",
            "doc_pos": 31,
            "document": "The undertaking agreed to fix prices.",
        }
        block = _format_reference_block(rows, chunk_loader=loader)
        # The provenance path is the always-visible summary; the chunk text is
        # embedded in a collapsed <details> body — no link, no new browser tab.
        # Label is HTML-escaped (``->`` -> ``-&gt;``, rendered as ``->``).
        self.assertIn(
            "<summary>court_list -&gt; court_list_raw -&gt; chunk 31 view</summary>",
            block,
        )
        self.assertIn("The undertaking agreed to fix prices.", block)
        # <details> with no `open` attribute starts collapsed.
        self.assertIn("<details>\n", block)
        self.assertNotIn("<details open", block)
        # Inline embed, not a markdown link to a separate page.
        self.assertNotIn("](", block)
        loader.get_chunk_by_id.assert_called_once_with("chunk-abc")

    def test_chunk_row_without_loader_renders_plain_label(self):
        rows = [_row("court_list -> chunk 31", "chunk-abc", 0)]
        block = _format_reference_block(rows)
        self.assertIn("court_list -> chunk 31", block)
        self.assertNotIn("<details>", block)

    def test_unresolved_chunk_renders_plain_label(self):
        rows = [_row("court_list -> chunk 31", "missing", 0)]
        loader = MagicMock()
        loader.get_chunk_by_id.return_value = None
        block = _format_reference_block(rows, chunk_loader=loader)
        self.assertIn("court_list -> chunk 31", block)
        self.assertNotIn("<details>", block)

    def test_loader_error_is_swallowed(self):
        rows = [_row("court_list -> chunk 31", "boom", 0)]
        loader = MagicMock()
        loader.get_chunk_by_id.side_effect = RuntimeError("connection lost")
        block = _format_reference_block(rows, chunk_loader=loader)
        self.assertIn("court_list -> chunk 31", block)
        self.assertNotIn("<details>", block)

    def test_chunkless_row_has_no_view_chunk_section(self):
        rows = [_row("csv_human -> manual entry", None, 0)]
        loader = MagicMock()
        block = _format_reference_block(rows, chunk_loader=loader)
        self.assertIn("csv_human -> manual entry", block)
        self.assertNotIn("<details>", block)
        loader.get_chunk_by_id.assert_not_called()

    def test_skips_rows_without_ancestry(self):
        self.assertEqual(_format_reference_block([_row(None, "chunk-1", 0)]), "")


# ---------------------------------------------------------------------------
# Pipeline-level wiring
# ---------------------------------------------------------------------------


class TestPipelineReferenceAttachment(unittest.TestCase):
    def _build_pipeline(self, checker, lineage_repo=None, chunk_loader=None):
        sql_store = MagicMock()
        # No case_facts dump in these tests → retrieved names are treated as
        # targeted retrievals and pass the grounding filter unchanged.
        sql_store.select_facts4celex.return_value = {}
        pipe = QARetrievalPipeline(
            sql_store=sql_store,
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            celex_checker=checker,
            lineage_repo=lineage_repo,
            chunk_loader=chunk_loader,
        )
        plan = RetrievalPlan(
            question_type="factual",
            steps=[
                RetrievalStep(
                    step_id=1, tool="case_facts", query="q", rationale="r"
                )
            ],
        )
        pipe.planner = MagicMock(return_value=dspy.Prediction(plan=plan))
        pipe.executor = MagicMock(
            return_value=dspy.Prediction(
                evidence="[case_facts] celex_id=CELEX-32002D0180\n  fines: 2.5M\n",
                fact_ids=["fines-uuid"],
                chunk_ids=[],
                fact_names=["fines"],
                celex_ids=[],
            )
        )
        pipe.synthesizer = MagicMock(
            return_value=dspy.Prediction(answer="The fine is 2.5 million EUR.")
        )
        # Force the full pipeline (avoid the direct-fact passthrough firing).
        pipe._try_direct_fact_passthrough = MagicMock(return_value=None)
        return pipe

    def test_reference_appended_for_single_celex(self):
        checker = _FakeCelexChecker({"CELEX-32002D0180": "CELEX-32002D0180"})
        lineage_repo = MagicMock()
        lineage_repo.get_fact_provenance.return_value = [
            _row("fines -> chunk 12", "chunk-12", 0)
        ]
        chunk_loader = MagicMock()
        chunk_loader.get_chunk_by_id.return_value = {
            "chunk_id": "chunk-12",
            "celex_id": "CELEX-32002D0180",
            "doc_pos": 12,
            "document": "A fine of EUR 2.5 million was imposed.",
        }
        pipe = self._build_pipeline(
            checker, lineage_repo=lineage_repo, chunk_loader=chunk_loader
        )

        pred = pipe(question="which fine in CELEX-32002D0180")

        self.assertIn("The fine is 2.5 million EUR.", pred.answer)
        # Provenance path is the collapsed summary; chunk text is in the body,
        # embedded in place rather than linked to a new tab.
        self.assertIn(
            "<summary>fines -&gt; chunk 12 view</summary>", pred.answer
        )
        self.assertIn("A fine of EUR 2.5 million was imposed.", pred.answer)
        self.assertNotIn("<details open", pred.answer)
        self.assertEqual(len(pred.references), 1)
        self.assertEqual(pred.references[0].chunk_id, "chunk-12")
        lineage_repo.get_fact_provenance.assert_called_once_with(
            "CELEX-32002D0180", "fines"
        )

    def test_no_reference_when_lineage_repo_absent(self):
        checker = _FakeCelexChecker({"CELEX-32002D0180": "CELEX-32002D0180"})
        pipe = self._build_pipeline(checker, lineage_repo=None)

        pred = pipe(question="which fine in CELEX-32002D0180")

        self.assertEqual(pred.answer, "The fine is 2.5 million EUR.")
        self.assertNotIn("Reference (provenance)", pred.answer)
        self.assertEqual(pred.references, [])


class TestResolveQuestionFactNames(unittest.TestCase):
    """_resolve_question_fact_names scopes references to the question's facts."""

    def test_synonym_resolves_to_existing_fact(self):
        available = ["fines", "companies_investigated", "companies_country"]
        self.assertEqual(
            _resolve_question_fact_names("what fine did they pay", available),
            ["fines"],
        )

    def test_excludes_incidental_company_fact(self):
        result = _resolve_question_fact_names("list the fines", ["fines", "companies_investigated"])
        self.assertIn("fines", result)
        self.assertNotIn("companies_investigated", result)

    def test_exact_underscore_token_longest_first(self):
        available = ["court_list_raw", "court_list", "fines"]
        result = _resolve_question_fact_names("show court_list_raw please", available)
        self.assertEqual(result[0], "court_list_raw")

    def test_empty_when_no_target_or_no_available(self):
        self.assertEqual(
            _resolve_question_fact_names("tell me about it", ["fines"]), []
        )
        self.assertEqual(_resolve_question_fact_names("fines", []), [])


class TestReferenceScoping(unittest.TestCase):
    """Pipeline attaches references only for the question's primary fact."""

    def _build(self, lineage_repo):
        sql_store = MagicMock()
        # The case has both a fines fact and an (incidental) companies_investigated fact.
        sql_store.select_facts4celex.return_value = {
            "fines": "Acme Corp fined EUR 2.5 million",
            "companies_investigated": "Acme Corp",
        }
        pipe = QARetrievalPipeline(
            sql_store=sql_store,
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            celex_checker=_FakeCelexChecker({"39952_1517_5": "39952_1517_5"}),
            lineage_repo=lineage_repo,
        )
        plan = RetrievalPlan(
            question_type="factual",
            steps=[RetrievalStep(step_id=1, tool="case_facts", query="q", rationale="r")],
        )
        pipe.planner = MagicMock(return_value=dspy.Prediction(plan=plan))
        pipe.executor = MagicMock(
            return_value=dspy.Prediction(
                evidence=(
                    "[case_facts] celex_id=39952_1517_5\n"
                    "  fines: Acme Corp fined EUR 2.5 million\n"
                    "  companies_investigated: Acme Corp\n"
                ),
                fact_ids=["fines-uuid", "companies_investigated-uuid"],
                chunk_ids=[],
                fact_names=["fines", "companies_investigated"],
                celex_ids=[],
            )
        )
        pipe.synthesizer = MagicMock(
            return_value=dspy.Prediction(answer="Acme Corp was fined EUR 2.5 million.")
        )
        pipe._try_direct_fact_passthrough = MagicMock(return_value=None)
        return pipe

    def test_only_primary_fact_traced(self):
        lineage_repo = MagicMock()
        lineage_repo.get_fact_provenance.return_value = [
            _row("fines → [score=1, doc, pos=28]", "chunk-28", 0)
        ]
        pipe = self._build(lineage_repo)

        pipe(question="what fines were imposed in 39952_1517_5")

        # 'companies_investigated' value ("Acme Corp") is grounded in the answer, but the
        # question targets fines, so only the fines provenance is fetched.
        traced = [c.args for c in lineage_repo.get_fact_provenance.call_args_list]
        self.assertIn(("39952_1517_5", "fines"), traced)
        self.assertNotIn(("39952_1517_5", "companies_investigated"), traced)


# ---------------------------------------------------------------------------
# _compute_highlight_terms
# ---------------------------------------------------------------------------


class TestComputeHighlightTerms(unittest.TestCase):
    def test_keeps_only_terms_present_in_answer(self):
        case_fact_values = {"fines": "| Acme Corporation | 1000000 |"}
        # "Acme Corporation" appears in the answer; the numeric cell does not.
        terms = _compute_highlight_terms(
            ["fines"], case_fact_values, "The answer mentions Acme Corporation."
        )
        self.assertIn("Acme Corporation", terms)

    def test_no_overlap_returns_empty(self):
        case_fact_values = {"fines": "| Acme Corporation | 1000000 |"}
        self.assertEqual(
            _compute_highlight_terms(["fines"], case_fact_values, "unrelated text"),
            [],
        )

    def test_missing_fact_value_is_skipped(self):
        self.assertEqual(_compute_highlight_terms(["fines"], {}, "Acme"), [])


# ---------------------------------------------------------------------------
# _collect_fallback_references
# ---------------------------------------------------------------------------


class _FakeChunk:
    def __init__(self, chunk_id: str) -> None:
        self.id = chunk_id


class _FakeSearcher:
    """Records search calls and returns canned chunks (HybridSearcher shape)."""

    def __init__(self, chunks_by_query: dict[str, list[_FakeChunk]] | None = None):
        self.calls: list[dict] = []
        self._chunks = chunks_by_query or {}

    def search(self, celex_id, query, keyword, top_k):
        self.calls.append(
            {"celex_id": celex_id, "query": query, "keyword": keyword, "top_k": top_k}
        )
        return self._chunks.get(query, [])


class TestCollectFallbackReferences(unittest.TestCase):
    def test_builds_synthetic_rows_with_fallback_suffix(self):
        searcher = _FakeSearcher({"the summary text": [_FakeChunk("chunk-1")]})
        rows = _collect_fallback_references(
            "CELEX-1",
            ["case_summary"],
            {"case_summary": "the summary text"},
            searcher,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].chunk_id, "chunk-1")
        self.assertEqual(
            rows[0].ancestry_path_names, "case_summary (fallback search)"
        )

    def test_dedupes_chunk_ids_across_facts(self):
        shared = [_FakeChunk("chunk-shared")]
        searcher = _FakeSearcher(
            {
                "first long fact value": shared,
                "second long fact value": shared,
            }
        )
        rows = _collect_fallback_references(
            "CELEX-1",
            ["fa", "fb"],
            {"fa": "first long fact value", "fb": "second long fact value"},
            searcher,
        )
        self.assertEqual(len(rows), 1)

    def test_skips_facts_without_values(self):
        searcher = _FakeSearcher()
        rows = _collect_fallback_references("CELEX-1", ["fines"], {}, searcher)
        self.assertEqual(rows, [])
        self.assertEqual(searcher.calls, [])

    def test_search_error_is_swallowed(self):
        class _BoomSearcher:
            def search(self, **kwargs):
                raise RuntimeError("boom")

        rows = _collect_fallback_references(
            "CELEX-1", ["fines"], {"fines": "x"}, _BoomSearcher()
        )
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
