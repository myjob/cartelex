"""
Integration test for :class:`BookCrossCheckModule` against CELEX-31976D0353 / IV/26699.

Uses the live database and the real DSPy LLM judge (configured via settings).
Write repositories (lineage, mismatch) are stubbed so no DB mutations occur.
The test is skipped automatically when the database is unreachable or when the
book corpus does not contain an IV/26699 entry.

Run with::

    PYTHONPATH=src .venv/bin/python -m unittest \\
        pipelines.agent_dspy.tests.test_book_cross_check_integration -v
"""

import unittest
from unittest.mock import MagicMock

import dspy

from config.llm_thinking import thinking_kwargs
from config.settings import settings
from extract.rag.hybrid_book_search import HybridBookSearchToolkit
from extract.rag.ranker.flash_ranker import FlashRanker
from pipelines.agent_dspy.book_cross_check_module import (
    BookCrossCheckModule,
    BookReferenceType,
    CrossCheckAction,
)
from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository
from pipelines.rag.pgvector.fact_repository import FactRepository
from tools.celex_tools import CelexDatabaseToolkit

CELEX_ID = "CELEX-31976D0353"
CASE_ID = "IV/26.699"

# Fact names and expected DB values under validation.
EXPECTED_FACTS: dict[str, str] = {
    "infringement_category_abuse_of_dominance": "True",
}

# Facts that must NOT exist for this case. IV/26.699 is an Article 86 (abuse of
# dominance) decision: its operative part cites only Article 86, so the
# Article 101 agreement-category questions must never be asked. Both facts below
# were extracted before the operative-article gate existed -- the decision's
# narrative also mentions Article 85 -- and both were deleted on 2026-09-19 with
# the other 124 facts that lost their gate. Their reappearance means the gate
# has regressed.
FORBIDDEN_FACTS: tuple[str, ...] = (
    "infringement_category_hor_vert_lic_joint",
    "infringement_subcategory_horizontal_restraints",
)


class TestBookCrossCheckIntegrationIV26699(unittest.TestCase):
    """Live cross-check of CELEX-31976D0353 / IV/26699 against the book corpus.

    Facts under test
    ----------------
    - ``infringement_category_abuse_of_dominance``: ``True`` -- corroborated
      from the book's "3 Abuse of dominance" chapter taxonomy.
    - The two Article 101 classification facts this case used to carry must now
      be absent; see :data:`FORBIDDEN_FACTS`.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Connect to the corpus; skip the whole class on any infrastructure failure.

        Steps performed once per class:

        1. Build ``FactRepository`` and ``BookChunkRepository`` against the live DB.
        2. Pre-load facts for ``CELEX_ID`` to validate the expected values exist.
        3. Assert ``CASE_ID`` is present in the book corpus (keyword gate).
        4. Configure DSPy LM from ``settings``.
        5. Build the module with stubbed write repositories.
        6. Run ``module.forward`` once; every test method shares ``cls.result``.
        """
        try:
            cls.fact_repo = FactRepository()
            cls.book_chunk_repo = BookChunkRepository()
            cls.facts_map = cls.fact_repo.select_facts4celex(CELEX_ID)
        except Exception as exc:
            raise unittest.SkipTest(f"celex_facts DB unavailable: {exc}") from exc

        if not cls.facts_map:
            raise unittest.SkipTest(f"No facts stored for {CELEX_ID} — skipping")

        in_book = cls.book_chunk_repo.keyword_exists(CASE_ID)
        if not in_book:
            raise unittest.SkipTest(
                f"{CASE_ID} not present in book corpus — skipping"
            )

        model_name = f"openai/{settings.LLM_MODEL_ID}"
        lm = dspy.LM(
            model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=False,
            num_retries=1,
            max_retries=0,
            timeout=settings.OLLAMA_TIMEOUT,
            **thinking_kwargs(model_name),
        )
        dspy.configure(lm=lm)

        tools = CelexDatabaseToolkit(fact_repo=cls.fact_repo)
        book_searcher = HybridBookSearchToolkit(tools=tools)

        try:
            ranker = FlashRanker()
        except Exception as exc:
            raise unittest.SkipTest(
                f"FlashRanker model unavailable: {exc}"
            ) from exc

        lineage_repo = MagicMock()
        lineage_repo.register_book_source.return_value = "stub-source-id"
        cls.lineage_repo = lineage_repo

        cls.check_repo = MagicMock()

        cls.module = BookCrossCheckModule(
            book_searcher=book_searcher,
            book_chunk_repo=cls.book_chunk_repo,
            lineage_repo=cls.lineage_repo,
            check_repo=cls.check_repo,
            fact_repo=cls.fact_repo,
            ranker=ranker,
        )

        cls.result = cls.module(celex_id=CELEX_ID, case_id=CASE_ID)

    # ------------------------------------------------------------------
    # DB pre-condition assertions
    # ------------------------------------------------------------------

    def test_expected_facts_present_in_db(self) -> None:
        """The three target facts must be stored with the expected values."""
        for name, expected in EXPECTED_FACTS.items():
            with self.subTest(fact=name):
                actual = self.facts_map.get(name, "")
                self.assertEqual(
                    actual,
                    expected,
                    f"Fact '{name}': expected {expected!r}, got {actual!r}",
                )

    # ------------------------------------------------------------------
    # Book corpus presence
    # ------------------------------------------------------------------

    def test_case_found_in_book(self) -> None:
        """The module must confirm IV/26699 is present in the book corpus."""
        self.assertTrue(
            self.result.in_book,
            f"Expected {CASE_ID} to be found in book corpus; got in_book=False",
        )

    def test_book_chunk_id_anchored(self) -> None:
        """A citable book chunk ID must be resolved when the case is in the book."""
        self.assertIsNotNone(
            self.result.book_chunk_id,
            "book_chunk_id must be set when in_book is True",
        )

    def test_book_path_set(self) -> None:
        """The chapter path must be non-empty when the case is found."""
        self.assertTrue(
            self.result.book_path,
            "book_path must be non-empty when in_book is True",
        )

    def test_reference_type_is_landmark(self) -> None:
        """Chiquita IV/26.699 is an 'F.1' landmark case — full validation applies."""
        self.assertEqual(
            self.result.reference_type,
            BookReferenceType.LANDMARK,
            f"Expected {CASE_ID} to be classified as a landmark reference; "
            f"got {self.result.reference_type}",
        )

    # ------------------------------------------------------------------
    # Verdict coverage for the target facts
    # ------------------------------------------------------------------

    def test_verdicts_produced_for_target_facts(self) -> None:
        """All three target facts must receive a verdict from the module."""
        verdict_names = {v.fact_name for v in self.result.verdicts}
        for name in EXPECTED_FACTS:
            with self.subTest(fact=name):
                self.assertIn(
                    name,
                    verdict_names,
                    f"No verdict produced for fact '{name}'",
                )

    def test_all_verdict_actions_are_valid(self) -> None:
        """Every verdict action for the target facts must be a valid CrossCheckAction."""
        target_verdicts = [
            v for v in self.result.verdicts if v.fact_name in EXPECTED_FACTS
        ]
        valid_actions = {a.value for a in CrossCheckAction}
        for verdict in target_verdicts:
            with self.subTest(fact=verdict.fact_name):
                self.assertIn(
                    verdict.action,
                    valid_actions,
                    f"Unexpected action {verdict.action!r} for '{verdict.fact_name}'",
                )

    def test_aggregate_counts_are_consistent(self) -> None:
        """Sum of corroborated + mismatch + skipped must equal total verdicts produced."""
        total = (
            self.result.corroborated_count
            + self.result.mismatch_count
            + self.result.skipped_count
        )
        self.assertEqual(
            total,
            len(self.result.verdicts),
            f"Aggregate counts {total} != verdict list length {len(self.result.verdicts)}",
        )

    def test_no_writes_to_lineage_on_read_only_stubs(self) -> None:
        """Any lineage writes must pass through the injected stubs, not a live repo."""
        # corroborated facts cause register_book_source + link_fact_to_source calls;
        # this test verifies the module uses the injected repo rather than constructing
        # its own connection (which would fail against a live DB without mutations).
        register_calls = self.lineage_repo.register_book_source.call_count
        link_calls = self.lineage_repo.link_fact_to_source.call_count
        self.assertEqual(
            register_calls,
            link_calls,
            f"register_book_source called {register_calls}x but "
            f"link_fact_to_source called {link_calls}x — must be 1:1",
        )

    # ------------------------------------------------------------------
    # Infringement-category specific assertions
    # ------------------------------------------------------------------

    def _get_verdict(self, fact_name: str):
        """Return the verdict for *fact_name* or None."""
        for v in self.result.verdicts:
            if v.fact_name == fact_name:
                return v
        return None

    def _assert_action(self, name: str, expected: CrossCheckAction):
        """Assert the verdict for *name* and return it for further checks."""
        verdict = self._get_verdict(name)
        self.assertIsNotNone(verdict, f"No verdict for '{name}'")
        self.assertEqual(
            verdict.action,
            expected,
            f"'{name}': expected {expected} but got "
            f"{verdict.action} (reason: {verdict.reason!r})",
        )
        return verdict

    def test_abuse_of_dominance_corroborated(self) -> None:
        """The book analyses IV/26.699 under Abuse of dominance — corroborated.

        The corroboration comes from the chapter taxonomy, so the recorded quote
        is the book section path rather than a prose sentence.
        """
        verdict = self._assert_action(
            "infringement_category_abuse_of_dominance",
            CrossCheckAction.CORROBORATED,
        )
        self.assertTrue(
            verdict.evidence_quote,
            "corroborated abuse_of_dominance must carry an exact supporting quote",
        )

    def test_article_101_facts_absent(self) -> None:
        """The agreement-category facts must not exist for this Article 86 case.

        They did until 2026-09-19. The category questions were gated on every
        article mentioned anywhere in the decision, and this decision discusses
        Article 85 while applying only Article 86, so the Article 101 questions
        fired and produced a horizontal classification for an abuse case. The
        gate now reads ``law_article_operative`` and the stale facts were
        deleted. The cross-check used to flag both as taxonomy mismatches; that
        contradiction logic is still covered by ``TestClassificationTaxonomy``
        in ``test_book_cross_check_module.py``, which needs no live data.
        """
        for name in FORBIDDEN_FACTS:
            with self.subTest(fact=name):
                self.assertNotIn(
                    name,
                    self.facts_map,
                    f"{name} exists for {CELEX_ID}: the operative-article gate "
                    "has regressed, or the fact was re-extracted",
                )

    def _persisted_actions(self) -> dict[str, str]:
        """Map persisted ``fact_id`` -> ``action`` from the check repo calls."""
        return {
            str(call.kwargs["fact_id"]): call.kwargs.get("action")
            for call in self.check_repo.upsert_check.call_args_list
        }

    def test_all_judged_facts_logged_to_check_repo(self) -> None:
        """Every verdict (not just mismatches) must be persisted to fact2book_checks."""
        persisted = self._persisted_actions()
        judged = [
            v for v in self.result.verdicts if v.reason != "already checked in fact2book_checks"
        ]
        for verdict in judged:
            with self.subTest(fact=verdict.fact_name):
                self.assertIn(
                    verdict.fact_id,
                    persisted,
                    f"no check row persisted for '{verdict.fact_name}'",
                )
                self.assertEqual(persisted[verdict.fact_id], verdict.action.value)

    def test_corroborated_fact_logged_with_corroborated_action(self) -> None:
        """The corroborated abuse-of-dominance fact is logged with action=corroborated."""
        verdict = self._get_verdict("infringement_category_abuse_of_dominance")
        self.assertIsNotNone(verdict)
        self.assertEqual(self._persisted_actions().get(verdict.fact_id), "corroborated")

    def test_no_verdict_for_removed_article_101_facts(self) -> None:
        """The removed Article 101 facts yield no verdict and no ledger row.

        Until 2026-09-19 both were judged and written as taxonomy mismatches,
        which is what this test used to assert. They no longer exist for this
        Article 86 case (see :data:`FORBIDDEN_FACTS`), so the module must not
        produce a verdict for them at all.
        """
        judged = {v.fact_name for v in self.result.verdicts}
        for name in FORBIDDEN_FACTS:
            with self.subTest(fact=name):
                self.assertNotIn(
                    name,
                    judged,
                    f"'{name}' was judged, so the fact still exists for {CELEX_ID}",
                )


if __name__ == "__main__":
    unittest.main()
