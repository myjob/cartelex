"""Integration tests for the Phase 1 FTS / trigram retrieval methods.

Exercises ``ChunkRepository.websearch_search_in_celex``,
``fulltext_search_all_cases`` and ``trigram_search`` against the live
``celex_facts`` database. Requires:
  - PostgreSQL with the celex_facts schema populated.
  - migrate_fts_extensions.sql applied (unaccent + pg_trgm + f_unaccent +
    trigram GIN index).

These assert the behaviours the Phase 1 lexical overhaul is meant to deliver:
English stemming (plural queries), multi-word matching where a single ILIKE
matched nothing, OR-vs-AND lexeme combination, and accent-insensitive trigram
lookup.
"""

import unittest

from pipelines.rag.pgvector.chunk_repository import ChunkRepository

# Known fixtures in the corpus.
_BREWING_CELEX = "37766_610_5"      # Dutch beer market — many brewery/brewing mentions
_LENIENCY_PRESENT = "CELEX-32000D0146"
_LENIENCY_ABSENT = "CELEX-31994D0601"  # cartonboard — no literal "leniency"


class TestChunkRepositoryFts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = ChunkRepository()

    def test_plural_query_matches_via_stemming(self) -> None:
        """'breweries' must match stored 'brewery'/'brewing' via English stemming."""
        hits = self.repo.websearch_search_in_celex(_BREWING_CELEX, "breweries", hits=5)
        self.assertTrue(hits, "Plural 'breweries' should stem and match brewing text")
        self.assertTrue(all(h.origin == "pgvector_fts" for h in hits))

    def test_multi_word_query_matches_where_ilike_is_dead(self) -> None:
        """A multi-word query that ILIKE '%...%' cannot match still finds cases."""
        multi = "list all cartel fines"
        # The legacy single-substring ILIKE returns nothing for a multi-word phrase.
        ilike = self.repo.keyword_search(_LENIENCY_ABSENT, multi)
        self.assertEqual(ilike, [], "Sanity: multi-word ILIKE substring matches nothing")
        fts = self.repo.fulltext_search_all_cases(multi, hits=10)
        self.assertTrue(fts, "Cross-case FTS should surface cases for a multi-word query")

    def test_or_semantics_recall_vs_and_precision(self) -> None:
        """OR keeps the salient-term recall; AND requires every lexeme present."""
        court_q = (
            "Is this case involves any court with assessment, order, ruling, "
            "decision or judgement?"
        )
        or_hits = self.repo.websearch_search_in_celex(
            "38344_5856_3", court_q, hits=10, use_or=True
        )
        and_hits = self.repo.websearch_search_in_celex(
            "38344_5856_3", court_q, hits=10, use_or=False
        )
        self.assertTrue(or_hits, "OR semantics must surface 'court' mentions")
        # ANDing every lexeme of a long question is far stricter (usually empty).
        self.assertLessEqual(len(and_hits), len(or_hits))

    def test_and_existence_check_true_when_present(self) -> None:
        hits = self.repo.websearch_search_in_celex(
            _LENIENCY_PRESENT, "leniency case", hits=5, use_or=False
        )
        self.assertTrue(hits)

    def test_and_existence_check_false_when_absent(self) -> None:
        hits = self.repo.websearch_search_in_celex(
            _LENIENCY_ABSENT, "leniency case", hits=5, use_or=False
        )
        self.assertEqual(hits, [], "AND existence check must not match on 'case' alone")

    def test_trigram_accent_insensitive_lookup(self) -> None:
        """Trigram lookup returns accent-folded matches with word_similarity scores."""
        hits = self.repo.trigram_search(_BREWING_CELEX, "brewery", hits=3)
        self.assertTrue(hits, "Trigram should find 'brewery' surface variants")
        self.assertTrue(all(h.origin == "pgvector_trgm" for h in hits))
        self.assertTrue(all(0.0 <= h.similarity <= 1.0 for h in hits))

    def test_blank_query_returns_empty(self) -> None:
        self.assertEqual(self.repo.websearch_search_in_celex(_BREWING_CELEX, "  "), [])
        self.assertEqual(self.repo.fulltext_search_all_cases(""), [])
        self.assertEqual(self.repo.trigram_search(_BREWING_CELEX, ""), [])


if __name__ == "__main__":
    unittest.main()
