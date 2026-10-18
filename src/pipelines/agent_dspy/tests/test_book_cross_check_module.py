"""
Unit tests for :class:`BookCrossCheckModule`.

Fakes are injected at the DB and LLM boundaries only (CLAUDE.md testing rule):
a fake support judge returning ``supports`` / ``contradicts`` / ``insufficient``
asserts that each routing branch logs the verdict to the check ledger with the
right ``action`` (and records lineage on a corroboration).
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from config.settings import settings
from pipelines.agent_dspy.book_cross_check_module import (
    BookCrossCheckModule,
    BookReferenceType,
    CrossCheckAction,
    case_id_variants,
    classify_book_reference,
)
from pipelines.agent_dspy.evidence_support import EvidenceSupportResult, fact_statement

# Book chunk texts name the case (the keyword-hit chunks are, by construction,
# the ones containing the case surface); case-specific facts are only judged
# against such text.
_CASE_CHUNK_TEXT = "Acme ran a cartel in COMP/123."
_CASE_CHAPTER_TEXT = "Acme ran a cartel chapter (COMP/123)."

# A landmark section path (default for _build_module so existing tests keep the
# full-validation behaviour). A simple/example path and an index path are used
# by the reference-policy tests.
_LANDMARK_PATH = (
    "Infringements -> 2 Horizontal restrictions -> 2.1 Price fixing -> "
    "F Economic landmark cases -> F.1 Chiquita"
)
_SIMPLE_PATH = "Infringements -> 2 Horizontal restrictions -> 2.1 Price fixing -> A Introduction"
_INDEX_PATH = "Index -> Table of cases"
# Taxonomy placements: a landmark case filed under abuse of dominance
# (chapter 3), an example reference in the vertical chapter (5), and a merger
# placement (chapter 8) that never decides a classification on its own.
_ABUSE_LANDMARK_PATH = (
    "Infringements -> 3 Abuse of dominance -> 3.1 Excessive pricing -> "
    "F Economic landmark cases -> F.1 Chiquita"
)
_ABUSE_SIMPLE_PATH = (
    "Infringements -> 3 Abuse of dominance -> 3.2 Loyalty discounts -> A Introduction"
)
_VERTICAL_SIMPLE_PATH = (
    "Infringements -> 5 Vertical restrictions -> 5.2 Territorial exclusivity -> "
    "A Introduction"
)
_MERGER_PATH = (
    "Infringements -> 8 Mergers and acquisitions -> 8.1 Horizontal mergers -> "
    "A Introduction"
)


def _fact(name: str, *, fact_id: str = "f-1", value_md: str = "some value"):
    """Build a minimal fact row stand-in."""
    return SimpleNamespace(
        id=fact_id, celex_id="CELEX-1", name=name, value_md=value_md, value_json=None
    )


def _chunk(chunk_id: str = "book-chunk-1", text: str = _CASE_CHUNK_TEXT):
    """Build a book ``ChunkVo`` stand-in (keyword-hit anchor)."""
    return SimpleNamespace(id=chunk_id, chunk=text, doc_pos=0)


def _chapter_row(document: str, path: str = "Infringements -> 2 Cartels"):
    """Build a ``BookChapterVo`` stand-in row."""
    return SimpleNamespace(document=document, path=path, doc_pos=0)


def _verdict(**kwargs):
    """Wrap an ``EvidenceSupportResult`` as a DSPy prediction object."""
    return SimpleNamespace(output=EvidenceSupportResult(**kwargs))


def _action_of(check_repo) -> str | None:
    """Return the ``action`` recorded by the sole ``upsert_check`` call."""
    return check_repo.upsert_check.call_args.kwargs["action"]


def _build_module(
    facts,
    judge,
    *,
    in_book: bool = True,
    anchor=None,
    chapter=None,
    ranker=None,
    candidates=None,
    latest_question=None,
    reference_paths=None,
    overwrite: bool = True,
    fact_prefixes=None,
):
    """Assemble a module with all boundaries faked.

    Returns the module plus the lineage / check fakes so tests can assert on
    the writes performed. ``keyword_search`` returns the ``anchor`` (the
    case-level fallback anchor) and ``fetch_chapters_for_keyword`` returns the
    ``candidates`` (the chapter chunks the ranker reranks per fact).
    ``reference_paths`` defaults to a landmark section path so the module runs
    the full verifier (every fact, contradictions allowed).
    """
    book_searcher = MagicMock()
    book_searcher.keyword_exists.return_value = in_book
    book_searcher.search_keyword.return_value = (
        chapter if chapter is not None else [_chapter_row(_CASE_CHAPTER_TEXT)]
    )

    anchor_list = [anchor] if anchor is not None else [_chunk()]
    book_chunk_repo = MagicMock()
    book_chunk_repo.keyword_search.return_value = anchor_list
    book_chunk_repo.fetch_chapters_for_keyword.return_value = (
        candidates if candidates is not None else anchor_list
    )
    book_chunk_repo.select_reference_paths.return_value = (
        reference_paths if reference_paths is not None else [_LANDMARK_PATH]
    )

    fact_repo = MagicMock()
    fact_repo.select_all_facts_for_celex.return_value = facts
    fact_repo.select_latest_question.return_value = latest_question

    lineage_repo = MagicMock()
    lineage_repo.register_book_source.return_value = "source-1"
    check_repo = MagicMock()
    check_repo.select_checked_fact_ids.return_value = set()

    module = BookCrossCheckModule(
        book_searcher=book_searcher,
        book_chunk_repo=book_chunk_repo,
        lineage_repo=lineage_repo,
        check_repo=check_repo,
        fact_repo=fact_repo,
        support_judge=judge,
        ranker=ranker,
        overwrite=overwrite,
        fact_prefixes=fact_prefixes,
    )
    return module, lineage_repo, check_repo


class TestCasePresence(unittest.TestCase):
    """The book gate short-circuits cases absent from the corpus."""

    def test_case_not_in_book_returns_empty(self):
        judge = MagicMock()
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge, in_book=False
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertFalse(result.in_book)
        self.assertEqual(result.verdicts, [])
        judge.assert_not_called()
        lineage_repo.register_book_source.assert_not_called()
        check_repo.upsert_check.assert_not_called()

    def test_keyword_exists_but_no_anchor_chunk_returns_empty(self):
        judge = MagicMock()
        book_searcher = MagicMock()
        book_searcher.keyword_exists.return_value = True
        book_chunk_repo = MagicMock()
        book_chunk_repo.keyword_search.return_value = []  # no citable anchor
        fact_repo = MagicMock()
        module = BookCrossCheckModule(
            book_searcher=book_searcher,
            book_chunk_repo=book_chunk_repo,
            lineage_repo=MagicMock(),
            check_repo=MagicMock(),
            fact_repo=fact_repo,
            support_judge=judge,
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertFalse(result.in_book)
        fact_repo.select_all_facts_for_celex.assert_not_called()
        judge.assert_not_called()


class TestCaseIdVariants(unittest.TestCase):
    """Punctuation and prefix variants bridge stored vs book case-id spellings."""

    def test_undotted_number_gets_thousands_variant(self):
        self.assertEqual(
            case_id_variants("IV/26699"),
            ["IV/26699", "IV/26.699", "COMP/26699", "COMP/26.699"],
        )

    def test_dotted_number_gets_stripped_variant(self):
        self.assertEqual(
            case_id_variants("IV/26.699"),
            ["IV/26.699", "IV/26699", "COMP/26.699", "COMP/26699"],
        )

    def test_no_trailing_group_yields_only_prefix_swap(self):
        self.assertEqual(case_id_variants("COMP/123"), ["COMP/123", "IV/123"])

    def test_stored_prefix_variants_come_before_other_prefixes(self):
        variants = case_id_variants("IV/36545")
        self.assertEqual(variants[0], "IV/36545")
        self.assertLess(variants.index("IV/36.545"), variants.index("COMP/36.545"))

    def test_unknown_prefix_gets_no_prefix_swap(self):
        self.assertEqual(case_id_variants("M.1234"), ["M.1234", "M.1.234"])

    def test_leading_marker_dot_is_preserved(self):
        # The "AT." marker dot has a non-digit on its left and must survive.
        self.assertIn("AT.39610", case_id_variants("AT.39610"))


class TestCaseIdResolution(unittest.TestCase):
    """``forward`` resolves the stored case id to the book's punctuation."""

    def test_variant_lookup_finds_case_under_alternate_punctuation(self):
        judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=0.9,
                evidence_quote="Acme ran a cartel",
            )
        )
        module, lineage_repo, _ = _build_module([_fact("market_definition")], judge)
        # Book only contains the dotted surface; the stored id is undotted.
        module.book_searcher.keyword_exists.side_effect = (
            lambda keyword: keyword == "IV/26.699"
        )

        result = module.forward("CELEX-1", "IV/26699")

        self.assertTrue(result.in_book)
        # Book lookups use the resolved surface ...
        module.book_chunk_repo.keyword_search.assert_called_with(
            "IV/26.699", limit=settings.BOOK_CROSSCHECK_CANDIDATE_LIMIT
        )
        # ... but persistence keeps the canonical stored id.
        self.assertEqual(result.case_id, "IV/26699")

    def test_prefix_variant_finds_comp_era_case(self):
        judge = MagicMock(
            return_value=_verdict(label="insufficient", support_score=0.1)
        )
        module, _, check_repo = _build_module([_fact("market_definition")], judge)
        # celex_case_id stores "IV/"; the book writes the COMP-era surface.
        module.book_searcher.keyword_exists.side_effect = (
            lambda keyword: keyword == "COMP/36.545"
        )

        result = module.forward("CELEX-32001D0418", "IV/36545")

        self.assertTrue(result.in_book)
        module.book_chunk_repo.keyword_search.assert_called_with(
            "COMP/36.545", limit=settings.BOOK_CROSSCHECK_CANDIDATE_LIMIT
        )
        self.assertEqual(result.case_id, "IV/36545")
        self.assertEqual(check_repo.upsert_check.call_args.kwargs["case_id"], "IV/36545")

    def test_no_variant_present_is_not_in_book(self):
        judge = MagicMock()
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge
        )
        module.book_searcher.keyword_exists.side_effect = lambda keyword: False

        result = module.forward("CELEX-1", "IV/26699")

        self.assertFalse(result.in_book)
        judge.assert_not_called()
        lineage_repo.register_book_source.assert_not_called()
        check_repo.upsert_check.assert_not_called()


class TestRouting(unittest.TestCase):
    """Each judge label routes to exactly one logged action (and lineage on support)."""

    def test_supports_records_corroboration(self):
        judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=0.9,
                evidence_quote="Acme ran a cartel",
                reason="The chapter states Acme ran a cartel.",
            )
        )
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge
        )

        result = module.forward("CELEX-1", "COMP/123")

        verdict = result.verdicts[0]
        self.assertEqual(verdict.action, CrossCheckAction.CORROBORATED)
        self.assertEqual(result.corroborated_count, 1)
        lineage_repo.register_book_source.assert_called_once()
        self.assertEqual(
            lineage_repo.register_book_source.call_args.kwargs["chunk_id"],
            "book-chunk-1",
        )
        link_kwargs = lineage_repo.link_fact_to_source.call_args.kwargs
        self.assertEqual(link_kwargs["relation_type"], "corroborated_by")
        self.assertEqual(link_kwargs["source_type"], "book_case")
        self.assertEqual(link_kwargs["source_id"], "source-1")
        # The corroboration is also logged to the check ledger.
        check_repo.upsert_check.assert_called_once()
        self.assertEqual(_action_of(check_repo), "corroborated")

    def test_supports_below_threshold_is_skipped(self):
        judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=settings.EVIDENCE_SUPPORT_MIN_SCORE - 0.1,
                evidence_quote="Acme ran a cartel",
            )
        )
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.SKIPPED)
        self.assertEqual(result.skipped_count, 1)
        lineage_repo.register_book_source.assert_not_called()
        # A sub-threshold support is logged as a skip, not dropped.
        check_repo.upsert_check.assert_called_once()
        self.assertEqual(_action_of(check_repo), "skipped")

    def test_supports_with_folded_quote_is_corroborated(self):
        """Whitespace and line-break hyphenation in the quote do not block support."""
        judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=0.95,
                evidence_quote="Acme   ran a car-\ntel",
            )
        )
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)
        lineage_repo.register_book_source.assert_called_once()
        self.assertEqual(_action_of(check_repo), "corroborated")

    def test_supports_without_substring_quote_is_skipped(self):
        judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=0.95,
                evidence_quote="text that is not in the chapter",
            )
        )
        module, lineage_repo, check_repo = _build_module([_fact("market_definition")], judge)

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.SKIPPED)
        lineage_repo.register_book_source.assert_not_called()
        self.assertEqual(_action_of(check_repo), "skipped")

    def test_contradicts_records_mismatch(self):
        judge = MagicMock(
            return_value=_verdict(
                label="contradicts",
                support_score=0.8,
                reason="The book says it was not a cartel.",
            )
        )
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition", fact_id="f-9")], judge
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.MISMATCH)
        self.assertEqual(result.mismatch_count, 1)
        check_repo.upsert_check.assert_called_once()
        kwargs = check_repo.upsert_check.call_args.kwargs
        self.assertEqual(kwargs["action"], "mismatch")
        self.assertEqual(kwargs["fact_id"], "f-9")
        self.assertEqual(kwargs["book_chunk_id"], "book-chunk-1")
        self.assertEqual(kwargs["case_id"], "COMP/123")
        self.assertEqual(kwargs["explanation"], "The book says it was not a cartel.")
        self.assertEqual(kwargs["support_label"], "contradicts")
        lineage_repo.register_book_source.assert_not_called()

    def test_insufficient_is_skipped(self):
        judge = MagicMock(
            return_value=_verdict(label="insufficient", support_score=0.2)
        )
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.SKIPPED)
        lineage_repo.register_book_source.assert_not_called()
        check_repo.upsert_check.assert_called_once()
        self.assertEqual(_action_of(check_repo), "skipped")


class TestFactFiltering(unittest.TestCase):
    """Empty-valued facts and judge failures are logged as skips without judging."""

    def test_empty_value_fact_skipped_without_judging(self):
        judge = MagicMock()
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition", value_md="NA")], judge
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.SKIPPED)
        judge.assert_not_called()
        lineage_repo.register_book_source.assert_not_called()
        # No judge call, but the skip is still recorded on the ledger.
        check_repo.upsert_check.assert_called_once()
        self.assertEqual(_action_of(check_repo), "skipped")

    def test_judge_exception_skips_fact_without_crashing(self):
        judge = MagicMock(side_effect=RuntimeError("LLM timeout"))
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.SKIPPED)
        lineage_repo.register_book_source.assert_not_called()
        check_repo.upsert_check.assert_called_once()
        self.assertEqual(_action_of(check_repo), "skipped")

    def test_multiple_facts_counted_independently(self):
        def _route(**kwargs):
            statement = kwargs["fact_statement"]
            if statement.startswith("market_definition:"):
                return _verdict(
                    label="supports",
                    support_score=0.9,
                    evidence_quote="Acme ran a cartel",
                )
            return _verdict(label="contradicts", support_score=0.7, reason="nope")

        judge = MagicMock(side_effect=_route)
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition", fact_id="f-1"), _fact("fines", fact_id="f-2")], judge
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.corroborated_count, 1)
        self.assertEqual(result.mismatch_count, 1)
        self.assertEqual(result.skipped_count, 0)
        lineage_repo.register_book_source.assert_called_once()
        # Both verdicts are logged (one corroborated, one mismatch).
        self.assertEqual(check_repo.upsert_check.call_count, 2)
        actions = {
            call.kwargs["action"] for call in check_repo.upsert_check.call_args_list
        }
        self.assertEqual(actions, {"corroborated", "mismatch"})


class TestContextAssembly(unittest.TestCase):
    """The chapter context is concatenated, ordered and char-capped."""

    def test_context_is_capped_to_budget(self):
        long_rows = [_chapter_row("x" * 5000), _chapter_row("y" * 5000)]
        captured = {}

        def _capture(**kwargs):
            captured["chunk_text"] = kwargs["chunk_text"]
            return _verdict(label="insufficient", support_score=0.1)

        judge = MagicMock(side_effect=_capture)
        module, _, _ = _build_module(
            [_fact("market_definition")], judge, chapter=long_rows
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertLessEqual(
            len(captured["chunk_text"]), settings.BOOK_CROSSCHECK_CONTEXT_CHARS
        )

    def test_empty_chapter_falls_back_to_anchor_text(self):
        captured = {}

        def _capture(**kwargs):
            captured["chunk_text"] = kwargs["chunk_text"]
            return _verdict(label="insufficient", support_score=0.1)

        judge = MagicMock(side_effect=_capture)
        module, _, _ = _build_module(
            [_fact("market_definition")],
            judge,
            anchor=_chunk(text="anchor fallback text"),
            chapter=[],
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertTrue(result.in_book)
        self.assertEqual(captured["chunk_text"], "anchor fallback text")


def _ranked(chunk_id: str, text: str):
    """Build a ``RankedChunkVo`` stand-in returned by the fake ranker."""
    return SimpleNamespace(id=chunk_id, chunk=text)


class TestPerFactContext(unittest.TestCase):
    """With a ranker injected, context and citation anchor are chosen per fact."""

    def test_ranked_chunk_is_anchor_and_context(self):
        """The reranked case chunk drives both the judged text and the cited id."""
        per_fact_text = "The book confirms Acme ran a cartel in 1990."
        ranker = MagicMock()
        ranker.rank_chunkvo.return_value = [
            _ranked("book-chunk-PERFACT", per_fact_text)
        ]

        captured = {}

        def _capture(**kwargs):
            captured["chunk_text"] = kwargs["chunk_text"]
            return _verdict(
                label="supports",
                support_score=0.95,
                evidence_quote="Acme ran a cartel",
            )

        judge = MagicMock(side_effect=_capture)
        module, lineage_repo, _ = _build_module(
            [_fact("market_definition")],
            judge,
            ranker=ranker,
            candidates=[_chunk("cand-1"), _chunk("cand-2")],
            latest_question="Which market_definition ran the cartel?",
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)
        self.assertEqual(captured["chunk_text"], per_fact_text)
        self.assertEqual(
            lineage_repo.register_book_source.call_args.kwargs["chunk_id"],
            "book-chunk-PERFACT",
        )

    def test_per_fact_query_uses_latest_question(self):
        """The fact's latest extraction question is the rerank query."""
        captured = {}

        def _rank(query, chunks, top_k=None):
            captured["query"] = query
            return [_ranked("book-chunk-PERFACT", "text with quote")]

        ranker = MagicMock()
        ranker.rank_chunkvo.side_effect = _rank
        judge = MagicMock(
            return_value=_verdict(label="insufficient", support_score=0.1)
        )
        module, _, _ = _build_module(
            [_fact("market_definition")],
            judge,
            ranker=ranker,
            candidates=[_chunk("cand-1")],
            latest_question="Which market_definition ran the cartel?",
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertEqual(captured["query"], "Which market_definition ran the cartel?")
        module_fact_repo = module.fact_repo
        module_fact_repo.select_latest_question.assert_called_with(
            "CELEX-1", "market_definition"
        )

    def test_falls_back_to_fact_statement_when_no_question(self):
        """With no stored question, the fact statement becomes the rerank query."""
        captured = {}

        def _rank(query, chunks, top_k=None):
            captured["query"] = query
            return [_ranked("book-chunk-PERFACT", "text with quote")]

        ranker = MagicMock()
        ranker.rank_chunkvo.side_effect = _rank
        judge = MagicMock(
            return_value=_verdict(label="insufficient", support_score=0.1)
        )
        fact = _fact("market_definition", value_md="Acme, Beta")
        module, _, _ = _build_module(
            [fact],
            judge,
            ranker=ranker,
            candidates=[_chunk("cand-1")],
            latest_question=None,
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertEqual(captured["query"], fact_statement(fact))

    def test_no_candidates_falls_back_to_case_chapter(self):
        """No case candidates -> judge against the case-chapter context + anchor."""
        captured = {}

        def _capture(**kwargs):
            captured["chunk_text"] = kwargs["chunk_text"]
            return _verdict(
                label="supports",
                support_score=0.95,
                evidence_quote="Acme ran a cartel",
            )

        ranker = MagicMock()
        ranker.rank_chunkvo.return_value = []
        judge = MagicMock(side_effect=_capture)
        module, lineage_repo, _ = _build_module(
            [_fact("market_definition")],
            judge,
            ranker=ranker,
            candidates=[],
            chapter=[_chapter_row("Acme ran a cartel chapter.")],
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertEqual(captured["chunk_text"], "Acme ran a cartel chapter.")
        self.assertEqual(
            lineage_repo.register_book_source.call_args.kwargs["chunk_id"],
            "book-chunk-1",
        )

    def test_no_ranker_uses_case_chapter(self):
        """Without a ranker the module judges every fact against the case chapter."""
        captured = {}

        def _capture(**kwargs):
            captured["chunk_text"] = kwargs["chunk_text"]
            return _verdict(label="insufficient", support_score=0.1)

        judge = MagicMock(side_effect=_capture)
        module, _, _ = _build_module(
            [_fact("market_definition")],
            judge,
            chapter=[_chapter_row("Acme ran a cartel chapter.")],
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertEqual(captured["chunk_text"], "Acme ran a cartel chapter.")

    def test_multi_fact_distinct_anchors(self):
        """Each fact cites the chunk that best matches its own question."""
        anchors_by_question = {
            "market_definition question": _ranked("chunk-market_definition", "Acme ran a cartel"),
            "fines question": _ranked("chunk-fines", "The fine in COMP/123 was 10 million"),
        }
        question_by_fact = {
            "market_definition": "market_definition question",
            "fines": "fines question",
        }

        def _rank(query, chunks, top_k=None):
            return [anchors_by_question[query]]

        ranker = MagicMock()
        ranker.rank_chunkvo.side_effect = _rank

        def _route(**kwargs):
            return _verdict(
                label="supports",
                support_score=0.95,
                evidence_quote=kwargs["chunk_text"],
            )

        judge = MagicMock(side_effect=_route)
        module, lineage_repo, _ = _build_module(
            [_fact("market_definition", fact_id="f-1"), _fact("fines", fact_id="f-2")],
            judge,
            ranker=ranker,
            candidates=[_chunk("cand-1")],
        )
        module.fact_repo.select_latest_question.side_effect = (
            lambda _celex, name: question_by_fact[name]
        )

        module.forward("CELEX-1", "COMP/123")

        cited = {
            call.kwargs["chunk_id"]
            for call in lineage_repo.register_book_source.call_args_list
        }
        self.assertEqual(cited, {"chunk-market_definition", "chunk-fines"})


class TestCaseScopedPool(unittest.TestCase):
    """Case-specific facts are judged only against chunks naming the case (F-5)."""

    def _module_with_pools(self, fact, ranker, judge, case_chunks, chapter_chunks):
        module, lineage_repo, check_repo = _build_module(
            [fact],
            judge,
            ranker=ranker,
            candidates=chapter_chunks,
            latest_question="question",
        )
        module.book_chunk_repo.keyword_search.return_value = case_chunks
        return module, lineage_repo, check_repo

    def test_case_specific_fact_reranks_only_case_chunks(self):
        """A decision-date fact never sees the rest of the chapter."""
        captured = {}

        def _rank(query, chunks, top_k=None):
            captured["ids"] = [c.id for c in chunks]
            return [chunks[0]]

        ranker = MagicMock()
        ranker.rank_chunkvo.side_effect = _rank
        judge = MagicMock(return_value=_verdict(label="insufficient", support_score=0.1))
        case_chunks = [_chunk("case-1"), _chunk("case-2")]
        chapter_chunks = [_chunk("chap-1", "Another case, decided 28 July 1978.")]
        module, _, _ = self._module_with_pools(
            _fact("eu_commisssion_decision_date", value_md="2001-12-02"),
            ranker, judge, case_chunks, chapter_chunks,
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertEqual(captured["ids"], ["case-1", "case-2"])

    def test_other_fact_reranks_case_chunks_then_chapter_deduplicated(self):
        """Non-specific facts see the case chunks first, then the chapter, once each."""
        captured = {}

        def _rank(query, chunks, top_k=None):
            captured["ids"] = [c.id for c in chunks]
            return [chunks[0]]

        ranker = MagicMock()
        ranker.rank_chunkvo.side_effect = _rank
        judge = MagicMock(return_value=_verdict(label="insufficient", support_score=0.1))
        case_chunks = [_chunk("case-1")]
        chapter_chunks = [_chunk("case-1"), _chunk("chap-2"), _chunk("chap-3")]
        module, _, _ = self._module_with_pools(
            _fact("market_definition"), ranker, judge, case_chunks, chapter_chunks
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertEqual(captured["ids"], ["case-1", "chap-2", "chap-3"])

    def test_case_specific_fact_without_case_surface_is_skipped_unjudged(self):
        """Context that does not name the case is never judged for a case-specific fact."""
        ranker = MagicMock()
        ranker.rank_chunkvo.return_value = [
            _ranked("chap-1", "Another case, decided 28 July 1978.")
        ]
        judge = MagicMock()
        module, lineage_repo, check_repo = self._module_with_pools(
            _fact("eu_commisssion_decision_date", value_md="2001-12-02"),
            ranker, judge, [_chunk("case-1")], [_chunk("chap-1")],
        )

        result = module.forward("CELEX-1", "COMP/123")

        judge.assert_not_called()
        self.assertEqual(result.verdicts[0].action, CrossCheckAction.SKIPPED)
        self.assertIn("names the case", check_repo.upsert_check.call_args.kwargs["explanation"])
        lineage_repo.register_book_source.assert_not_called()

    def test_case_surface_is_matched_case_insensitively(self):
        """The resolved surface came from an ILIKE lookup, so letter case is folded."""
        ranker = MagicMock()
        ranker.rank_chunkvo.return_value = [
            _ranked("case-1", "Decided 2 December 2001 (Comp/123).")
        ]
        judge = MagicMock(
            return_value=_verdict(
                label="supports", support_score=0.9, evidence_quote="2 December 2001"
            )
        )
        module, _, _ = self._module_with_pools(
            _fact("eu_commisssion_decision_date", value_md="2001-12-02"),
            ranker, judge, [_chunk("case-1")], [],
        )

        result = module.forward("CELEX-1", "COMP/123")

        judge.assert_called_once()
        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)

    def test_long_chunk_is_windowed_around_the_case_surface(self):
        """A case surface past the character budget stays inside the judged context."""
        filler = "x" * (settings.BOOK_CROSSCHECK_CONTEXT_CHARS + 500)
        long_text = filler + " Decided 2 December 2001 in COMP/123. " + "y" * 300
        ranker = MagicMock()
        ranker.rank_chunkvo.return_value = [_ranked("case-1", long_text)]
        captured = {}

        def _capture(**kwargs):
            captured["chunk_text"] = kwargs["chunk_text"]
            return _verdict(
                label="supports", support_score=0.9, evidence_quote="2 December 2001"
            )

        judge = MagicMock(side_effect=_capture)
        module, _, _ = self._module_with_pools(
            _fact("eu_commisssion_decision_date", value_md="2001-12-02"),
            ranker, judge, [_chunk("case-1", long_text)], [],
        )

        result = module.forward("CELEX-1", "COMP/123")

        judge.assert_called_once()
        self.assertIn("COMP/123", captured["chunk_text"])
        self.assertLessEqual(len(captured["chunk_text"]), settings.BOOK_CROSSCHECK_CONTEXT_CHARS)
        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)

    def test_other_facts_keep_the_head_of_a_long_chunk(self):
        """Non-specific facts are still judged on the head of the chunk."""
        long_text = "head " + "x" * (settings.BOOK_CROSSCHECK_CONTEXT_CHARS + 500) + " COMP/123"
        ranker = MagicMock()
        ranker.rank_chunkvo.return_value = [_ranked("case-1", long_text)]
        captured = {}

        def _capture(**kwargs):
            captured["chunk_text"] = kwargs["chunk_text"]
            return _verdict(label="insufficient", support_score=0.1)

        judge = MagicMock(side_effect=_capture)
        module, _, _ = self._module_with_pools(
            _fact("market_definition"), ranker, judge, [_chunk("case-1", long_text)], []
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertTrue(captured["chunk_text"].startswith("head "))
        self.assertEqual(len(captured["chunk_text"]), settings.BOOK_CROSSCHECK_CONTEXT_CHARS)

    def test_judge_receives_resolved_case_reference(self):
        """The book's own case surface is passed to the judge as case_reference."""
        judge = MagicMock(return_value=_verdict(label="insufficient", support_score=0.1))
        module, _, _ = _build_module([_fact("market_definition")], judge)
        module.book_searcher.keyword_exists.side_effect = (
            lambda keyword: keyword == "IV/26.699"
        )

        module.forward("CELEX-1", "IV/26699")

        self.assertEqual(judge.call_args.kwargs["case_reference"], "IV/26.699")


class TestClassificationTaxonomy(unittest.TestCase):
    """Classification facts are decided on the book's chapter taxonomy, without an LLM."""

    def test_contradictory_classification_records_mismatch(self):
        """A horizontal claim for a case the book files under abuse is a MISMATCH."""
        support_judge = MagicMock()  # must NOT be consulted on a contradiction
        module, lineage_repo, check_repo = _build_module(
            [_fact("infringement_category_hor_vert_lic_joint", value_md="horizontal")],
            support_judge,
            reference_paths=[_ABUSE_LANDMARK_PATH],
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.MISMATCH)
        self.assertEqual(result.mismatch_count, 1)
        check_repo.upsert_check.assert_called_once()
        kwargs = check_repo.upsert_check.call_args.kwargs
        self.assertEqual(kwargs["action"], "mismatch")
        self.assertEqual(kwargs["support_label"], "contradicts")
        self.assertIn("3 Abuse of dominance", kwargs["explanation"])
        support_judge.assert_not_called()
        lineage_repo.register_book_source.assert_not_called()

    def test_matching_classification_corroborates_on_section_path(self):
        """A category the book taxonomy confirms is corroborated, citing the path.

        A boolean category label has no verbatim surface in chapter prose, so the
        section path the book files the case under is the recorded quote.
        """
        support_judge = MagicMock()  # must NOT be consulted on a taxonomy match
        module, lineage_repo, check_repo = _build_module(
            [_fact("infringement_category_abuse_of_dominance", value_md="True")],
            support_judge,
            reference_paths=[_ABUSE_LANDMARK_PATH],
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)
        self.assertEqual(result.corroborated_count, 1)
        self.assertEqual(result.verdicts[0].evidence_quote, _ABUSE_LANDMARK_PATH)
        support_judge.assert_not_called()
        self.assertEqual(_action_of(check_repo), "corroborated")
        lineage_repo.register_book_source.assert_called_once()

    def test_case_in_several_chapters_matches_any_of_them(self):
        """F-3: a case cited in chapters 2 and 5 corroborates a vertical claim."""
        module, _, check_repo = _build_module(
            [_fact("infringement_category_hor_vert_lic_joint", value_md="vertical")],
            MagicMock(),
            reference_paths=[_SIMPLE_PATH, _VERTICAL_SIMPLE_PATH],
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)
        self.assertEqual(result.verdicts[0].evidence_quote, _VERTICAL_SIMPLE_PATH)
        self.assertEqual(_action_of(check_repo), "corroborated")

    def test_landmark_placement_outranks_example_mentions(self):
        """A landmark analysis under abuse decides, despite an example in chapter 2."""
        module, _, check_repo = _build_module(
            [_fact("infringement_category_hor_vert_lic_joint", value_md="horizontal")],
            MagicMock(),
            reference_paths=[_SIMPLE_PATH, _ABUSE_LANDMARK_PATH],
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.MISMATCH)
        self.assertEqual(_action_of(check_repo), "mismatch")

    def test_inconclusive_taxonomy_falls_through_to_support_judge(self):
        """A merger-only placement decides nothing; chapter prose may corroborate."""
        support_judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=0.95,
                evidence_quote="Acme ran a cartel",
            )
        )
        module, lineage_repo, check_repo = _build_module(
            [_fact("infringement_category_hor_vert_lic_joint", value_md="horizontal")],
            support_judge,
            reference_paths=[_MERGER_PATH],
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)
        support_judge.assert_called_once()
        self.assertEqual(_action_of(check_repo), "corroborated")
        lineage_repo.register_book_source.assert_called_once()

    def test_prose_judge_cannot_contradict_a_classification(self):
        """An inconclusive taxonomy plus a prose contradiction is only a skip."""
        support_judge = MagicMock(
            return_value=_verdict(
                label="contradicts", support_score=0.9, reason="Prose disagrees."
            )
        )
        module, _, check_repo = _build_module(
            [_fact("infringement_category_hor_vert_lic_joint", value_md="horizontal")],
            support_judge,
            reference_paths=[_MERGER_PATH],
        )

        result = module.forward("CELEX-1", "COMP/123")

        verdict = result.verdicts[0]
        self.assertEqual(verdict.action, CrossCheckAction.SKIPPED)
        self.assertEqual(verdict.label, "contradicts")
        self.assertEqual(_action_of(check_repo), "skipped")

    def test_classification_without_rule_uses_support_judge(self):
        """A classification fact with no taxonomy rule is judged on prose only."""
        support_judge = MagicMock(
            return_value=_verdict(label="insufficient", support_score=0.1)
        )
        module, _, check_repo = _build_module(
            [_fact("infringement_category_procedural", value_md="True")],
            support_judge,
            reference_paths=[_ABUSE_LANDMARK_PATH],
        )

        result = module.forward("CELEX-1", "COMP/123")

        support_judge.assert_called_once()
        self.assertEqual(result.verdicts[0].action, CrossCheckAction.SKIPPED)
        self.assertEqual(_action_of(check_repo), "skipped")

    def test_extraction_question_reaches_support_judge(self):
        """The fact's latest extraction question is passed as the judge rubric."""
        question = "Does the decision concern an abuse of a dominant position?"
        support_judge = MagicMock(
            return_value=_verdict(label="insufficient", support_score=0.1)
        )
        module, _, _ = _build_module(
            [_fact("fines", value_md="10 million")],
            support_judge,
            latest_question=question,
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertEqual(
            support_judge.call_args.kwargs["extraction_question"], question
        )


class TestReferenceClassifier(unittest.TestCase):
    """``classify_book_reference`` maps section paths to a reference type."""

    def test_landmark_path_is_landmark(self):
        self.assertEqual(
            classify_book_reference([_LANDMARK_PATH]), BookReferenceType.LANDMARK
        )

    def test_prose_path_is_simple(self):
        self.assertEqual(
            classify_book_reference([_SIMPLE_PATH]), BookReferenceType.SIMPLE
        )

    def test_chronological_listing_is_simple(self):
        chronological = (
            "Infringements -> 2 Horizontal restrictions -> 2.1 Price fixing -> "
            "E Commission decisions in chronological order"
        )
        self.assertEqual(
            classify_book_reference([chronological]), BookReferenceType.SIMPLE
        )

    def test_index_only_is_index(self):
        self.assertEqual(
            classify_book_reference([_INDEX_PATH, "Annex -> Cases"]),
            BookReferenceType.INDEX,
        )

    def test_landmark_wins_over_other_references(self):
        self.assertEqual(
            classify_book_reference([_SIMPLE_PATH, _INDEX_PATH, _LANDMARK_PATH]),
            BookReferenceType.LANDMARK,
        )

    def test_empty_paths_default_to_simple(self):
        self.assertEqual(classify_book_reference([]), BookReferenceType.SIMPLE)


class TestReferencePolicy(unittest.TestCase):
    """The reference type governs which facts may be contradicted / skipped."""

    def test_simple_non_infringement_contradiction_is_downgraded(self):
        """On a simple reference a non-infringement contradiction is skipped."""
        judge = MagicMock(
            return_value=_verdict(
                label="contradicts",
                support_score=0.8,
                reason="The book says it was not a cartel.",
            )
        )
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge, reference_paths=[_SIMPLE_PATH]
        )

        result = module.forward("CELEX-1", "COMP/123")

        verdict = result.verdicts[0]
        self.assertEqual(verdict.action, CrossCheckAction.SKIPPED)
        self.assertEqual(verdict.label, "contradicts")
        self.assertEqual(result.reference_type, BookReferenceType.SIMPLE)
        # Downgraded contradiction is logged as a skip (label retained).
        check_repo.upsert_check.assert_called_once()
        kwargs = check_repo.upsert_check.call_args.kwargs
        self.assertEqual(kwargs["action"], "skipped")
        self.assertEqual(kwargs["support_label"], "contradicts")
        lineage_repo.register_book_source.assert_not_called()

    def test_simple_non_infringement_corroboration_is_allowed(self):
        """Corroboration still happens for non-infringement facts on a simple ref."""
        judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=0.95,
                evidence_quote="Acme ran a cartel",
            )
        )
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge, reference_paths=[_SIMPLE_PATH]
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)
        lineage_repo.register_book_source.assert_called_once()
        check_repo.upsert_check.assert_called_once()
        self.assertEqual(_action_of(check_repo), "corroborated")

    def test_simple_infringement_contradiction_is_recorded(self):
        """Infringement facts may still be contradicted on a simple reference."""
        module, _, check_repo = _build_module(
            [_fact("infringement_category_hor_vert_lic_joint", value_md="horizontal")],
            MagicMock(),
            reference_paths=[_ABUSE_SIMPLE_PATH],
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.MISMATCH)
        check_repo.upsert_check.assert_called_once()
        kwargs = check_repo.upsert_check.call_args.kwargs
        self.assertEqual(kwargs["action"], "mismatch")
        self.assertEqual(kwargs["reference_type"], "simple")

    def test_landmark_non_infringement_contradiction_is_recorded(self):
        """The full verifier (landmark) records non-infringement contradictions."""
        judge = MagicMock(
            return_value=_verdict(
                label="contradicts", support_score=0.8, reason="nope"
            )
        )
        module, _, check_repo = _build_module(
            [_fact("market_definition")], judge, reference_paths=[_LANDMARK_PATH]
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.MISMATCH)
        self.assertEqual(result.reference_type, BookReferenceType.LANDMARK)
        check_repo.upsert_check.assert_called_once()
        kwargs = check_repo.upsert_check.call_args.kwargs
        self.assertEqual(kwargs["action"], "mismatch")
        self.assertEqual(kwargs["reference_type"], "landmark")

    def test_index_only_reference_skips_all_facts(self):
        """An index-only case produces no verdicts, no judging, no writes."""
        judge = MagicMock()
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition")], judge, reference_paths=[_INDEX_PATH]
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertTrue(result.in_book)
        self.assertEqual(result.reference_type, BookReferenceType.INDEX)
        self.assertEqual(result.verdicts, [])
        judge.assert_not_called()
        lineage_repo.register_book_source.assert_not_called()
        check_repo.upsert_check.assert_not_called()


class TestSkipExisting(unittest.TestCase):
    """``overwrite=False`` leaves already-checked facts untouched."""

    def test_already_checked_fact_is_not_rejudged_or_written(self):
        judge = MagicMock()
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition", fact_id="f-1")], judge, overwrite=False
        )
        check_repo.select_checked_fact_ids.return_value = {"f-1"}

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.SKIPPED)
        # Nothing was judged or written, so it is not a skipped verdict.
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.already_checked_count, 1)
        check_repo.select_checked_fact_ids.assert_called_once_with("CELEX-1")
        judge.assert_not_called()
        check_repo.upsert_check.assert_not_called()
        lineage_repo.register_book_source.assert_not_called()

    def test_unchecked_fact_is_judged_in_skip_mode(self):
        judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=0.95,
                evidence_quote="Acme ran a cartel",
            )
        )
        module, _, check_repo = _build_module(
            [_fact("market_definition", fact_id="f-2")], judge, overwrite=False
        )
        check_repo.select_checked_fact_ids.return_value = {"other-fact"}

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(result.verdicts[0].action, CrossCheckAction.CORROBORATED)
        judge.assert_called_once()
        check_repo.upsert_check.assert_called_once()

    def test_overwrite_mode_does_not_query_existing(self):
        judge = MagicMock(
            return_value=_verdict(label="insufficient", support_score=0.1)
        )
        module, _, check_repo = _build_module(
            [_fact("market_definition")], judge, overwrite=True
        )

        module.forward("CELEX-1", "COMP/123")

        check_repo.select_checked_fact_ids.assert_not_called()


class TestLineageRetraction(unittest.TestCase):
    """Book corroboration links follow the current verdict and anchor."""

    @staticmethod
    def _retract_kwargs(lineage_repo) -> dict:
        """Return the kwargs of the sole ``retract_fact_links`` call."""
        lineage_repo.retract_fact_links.assert_called_once()
        return lineage_repo.retract_fact_links.call_args.kwargs

    def _assert_scoped_to_module(self, kwargs: dict) -> None:
        """Retraction must only touch this module's book corroboration links."""
        self.assertEqual(kwargs["relation_type"], "corroborated_by")
        self.assertEqual(kwargs["extraction_method"], "book_cross_check")
        self.assertEqual(kwargs["source_type"], "book_case")

    def test_corroboration_retracts_links_to_other_anchors(self):
        judge = MagicMock(
            return_value=_verdict(
                label="supports", support_score=0.9, evidence_quote="Acme ran a cartel"
            )
        )
        module, lineage_repo, _ = _build_module(
            [_fact("market_definition", fact_id="f-1")], judge
        )

        module.forward("CELEX-1", "COMP/123")

        kwargs = self._retract_kwargs(lineage_repo)
        self._assert_scoped_to_module(kwargs)
        self.assertEqual(kwargs["fact_id"], "f-1")
        self.assertEqual(kwargs["keep_source_id"], "source-1")

    def test_skipped_verdict_retracts_all_book_links(self):
        judge = MagicMock(
            return_value=_verdict(label="insufficient", support_score=0.2)
        )
        module, lineage_repo, _ = _build_module(
            [_fact("market_definition", fact_id="f-2")], judge
        )

        module.forward("CELEX-1", "COMP/123")

        kwargs = self._retract_kwargs(lineage_repo)
        self._assert_scoped_to_module(kwargs)
        self.assertEqual(kwargs["fact_id"], "f-2")
        self.assertIsNone(kwargs["keep_source_id"])
        lineage_repo.link_fact_to_source.assert_not_called()

    def test_mismatch_retracts_all_book_links(self):
        judge = MagicMock(
            return_value=_verdict(
                label="contradicts", support_score=0.8, reason="Not a cartel."
            )
        )
        module, lineage_repo, _ = _build_module(
            [_fact("market_definition", fact_id="f-3")], judge
        )

        module.forward("CELEX-1", "COMP/123")

        self.assertIsNone(self._retract_kwargs(lineage_repo)["keep_source_id"])

    def test_empty_value_skip_retracts_all_book_links(self):
        judge = MagicMock()
        module, lineage_repo, _ = _build_module(
            [_fact("market_definition", value_md="n/a")], judge
        )

        module.forward("CELEX-1", "COMP/123")

        judge.assert_not_called()
        self.assertIsNone(self._retract_kwargs(lineage_repo)["keep_source_id"])

    def test_already_checked_fact_keeps_its_links(self):
        judge = MagicMock()
        module, lineage_repo, check_repo = _build_module(
            [_fact("market_definition", fact_id="f-1")], judge, overwrite=False
        )
        check_repo.select_checked_fact_ids.return_value = {"f-1"}

        module.forward("CELEX-1", "COMP/123")

        lineage_repo.retract_fact_links.assert_not_called()

    def test_case_not_in_book_touches_no_links(self):
        judge = MagicMock()
        module, lineage_repo, _ = _build_module(
            [_fact("market_definition")], judge, in_book=False
        )

        module.forward("CELEX-1", "COMP/123")

        lineage_repo.retract_fact_links.assert_not_called()


class TestFactPrefixFilter(unittest.TestCase):
    """``fact_prefixes`` restricts which facts are checked."""

    def test_only_matching_prefix_facts_are_checked(self):
        judge = MagicMock(
            return_value=_verdict(
                label="supports",
                support_score=0.95,
                evidence_quote="The fine was 10 million",
            )
        )
        module, _, check_repo = _build_module(
            [
                _fact("market_definition", fact_id="f-1"),
                _fact("fines", fact_id="f-2", value_md="The fine was 10 million"),
            ],
            judge,
            fact_prefixes=("fines",),
        )

        result = module.forward("CELEX-1", "COMP/123")

        self.assertEqual(len(result.verdicts), 1)
        self.assertEqual(result.verdicts[0].fact_name, "fines")
        judge.assert_called_once()
        check_repo.upsert_check.assert_called_once()
        self.assertEqual(check_repo.upsert_check.call_args.kwargs["fact_id"], "f-2")


if __name__ == "__main__":
    unittest.main()
