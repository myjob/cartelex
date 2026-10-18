"""
Unit tests for the Phase 1 entity repositories.

These tests validate repository instantiation and method signatures
without requiring a live database.  Integration tests that hit a real
PostgreSQL instance belong in separate ``test_*_integration.py`` files.
"""

import unittest
from unittest.mock import MagicMock, patch

# All repositories share the same instantiation pattern — we patch
# ``make_engine`` globally so no real PG connections are attempted.
_ENGINE_PATCH = "pipelines.rag.pgvector.session.make_engine"


class TestQueryEmbeddingRepository(unittest.TestCase):
    """Verify QueryEmbeddingRepository instantiation and API surface."""

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_instantiation_defaults(self, mock_engine):
        from pipelines.rag.pgvector.query_embedding_repository import (
            QueryEmbeddingRepository,
        )

        repo = QueryEmbeddingRepository()
        self.assertIsNotNone(repo.engine)
        self.assertIsNotNone(repo.Session)
        self.assertEqual(repo._fq_table, "celex_facts.query_embeddings_qwen")

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_custom_cache_table(self, mock_engine):
        from pipelines.rag.pgvector.query_embedding_repository import (
            QueryEmbeddingRepository,
        )

        repo = QueryEmbeddingRepository(cache_table="query_embeddings")
        self.assertEqual(repo._fq_table, "celex_facts.query_embeddings")

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_has_required_methods(self, mock_engine):
        from pipelines.rag.pgvector.query_embedding_repository import (
            QueryEmbeddingRepository,
        )

        repo = QueryEmbeddingRepository()
        self.assertTrue(callable(getattr(repo, "get_cached", None)))
        self.assertTrue(callable(getattr(repo, "cache", None)))
        self.assertTrue(callable(getattr(repo, "get_or_cache", None)))


class TestCelexIdentityRepository(unittest.TestCase):
    """Verify CelexIdentityRepository instantiation and API surface."""

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_instantiation(self, mock_engine):
        from pipelines.rag.pgvector.celex_identity_repository import (
            CelexIdentityRepository,
        )

        repo = CelexIdentityRepository()
        self.assertIsNotNone(repo.engine)

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_has_required_methods(self, mock_engine):
        from pipelines.rag.pgvector.celex_identity_repository import (
            CelexIdentityRepository,
        )

        repo = CelexIdentityRepository()
        for method_name in [
            "check_celex_or_case_id",
            "select_case_ids_for_celex",
            "select_celex_ids_for_case",
            "upsert_case_celex_mapping",
            "bulk_upsert_case_celex_mappings",
            "exists",
        ]:
            self.assertTrue(
                callable(getattr(repo, method_name, None)),
                f"Missing method: {method_name}",
            )


class TestCompanyRepository(unittest.TestCase):
    """Verify CompanyRepository instantiation and API surface."""

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_instantiation(self, mock_engine):
        from pipelines.rag.pgvector.company_repository import CompanyRepository

        repo = CompanyRepository()
        self.assertIsNotNone(repo.engine)

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_has_required_methods(self, mock_engine):
        from pipelines.rag.pgvector.company_repository import CompanyRepository

        repo = CompanyRepository()
        for method_name in [
            "upsert_canonical_company",
            "insert_alias",
            "insert_canonical_company_and_aliases",
            "insert_standard_alias",
        ]:
            self.assertTrue(
                callable(getattr(repo, method_name, None)),
                f"Missing method: {method_name}",
            )


class TestChunkRepository(unittest.TestCase):
    """Verify ChunkRepository instantiation and API surface."""

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_instantiation_defaults(self, mock_engine):
        from pipelines.rag.pgvector.chunk_repository import ChunkRepository

        repo = ChunkRepository()
        self.assertIsNotNone(repo.engine)
        self.assertEqual(repo.embedding_table, "pg_embedding_qwen")

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_has_required_methods(self, mock_engine):
        from pipelines.rag.pgvector.chunk_repository import ChunkRepository

        repo = ChunkRepository()
        for method_name in [
            "keyword_search",
            "keyword_search_all_cases",
            "keyword_count_all_cases",
            "semantic_search_in_celex",
            "semantic_search",
            "semantic_search_with_meta",
            "semantic_search_facts",
            "select_distinct_celex_for_keyword",
            "regex_retriever",
            "select_first_page",
        ]:
            self.assertTrue(
                callable(getattr(repo, method_name, None)),
                f"Missing method: {method_name}",
            )


class TestBookChunkRepository(unittest.TestCase):
    """Verify BookChunkRepository instantiation and API surface."""

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_instantiation(self, mock_engine):
        from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository

        repo = BookChunkRepository()
        self.assertIsNotNone(repo.engine)

    @patch(_ENGINE_PATCH, return_value=MagicMock())
    def test_has_required_methods(self, mock_engine):
        from pipelines.rag.pgvector.book_chunk_repository import BookChunkRepository

        repo = BookChunkRepository()
        for method_name in [
            "keyword_search",
            "semantic_search",
            "fetch_chapter_text",
            "keyword_exists",
        ]:
            self.assertTrue(
                callable(getattr(repo, method_name, None)),
                f"Missing method: {method_name}",
            )


class TestChunkRepositoryRegexScope(unittest.TestCase):
    """``regex_retriever`` restricts to classified sections only when asked."""

    def _captured_sql(self, section_types):
        from pipelines.rag.pgvector.chunk_repository import ChunkRepository

        with patch(_ENGINE_PATCH, return_value=MagicMock()):
            repo = ChunkRepository()
        repo.engine = MagicMock()
        conn = repo.engine.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = []
        repo.regex_retriever("CELEX-1", r"Article \\d+", section_types=section_types)
        statement, params = conn.execute.call_args.args
        return str(statement), params

    def test_unscoped_scans_the_whole_document(self):
        sql, params = self._captured_sql(None)
        self.assertNotIn("section_type", sql)
        self.assertNotIn("section_types", params)

    def test_scoped_adds_the_section_clause(self):
        sql, params = self._captured_sql(["operative"])
        self.assertIn("section_type = ANY(:section_types)", sql)
        self.assertEqual(params["section_types"], ["operative"])


if __name__ == "__main__":
    unittest.main()
