"""
Tests for EmbeddingMigrationQwen3.

Unit tests use mocked DB session and embedding function.
Integration tests require a live PostgreSQL instance and Ollama server.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


@unittest.skip("Obsolete: the OpenAI-to-Qwen3 embedding migration it tests was pruned in Aug 2026; kept as reference, skipped explicitly since 2026-09-17.")
class TestEmbeddingMigrationQwen3Unit(unittest.TestCase):
    """Unit tests — no live DB or Ollama required."""

    def _make_mock_session(self, source_rows, existing_hashes=None):
        """Return a context-manager mock session.

        ``execute()`` is configured to return:
          - call 1 → existing hashes from target table
          - call 2 → source rows from langchain_pg_embedding
          - subsequent calls → INSERT results (no return needed)
        """
        if existing_hashes is None:
            existing_hashes = []

        hash_result = MagicMock()
        hash_result.fetchall.return_value = [
            SimpleNamespace(chunk_hash=h) for h in existing_hashes
        ]

        source_result = MagicMock()
        source_result.fetchall.return_value = source_rows

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        # First execute → hashes, second → source, rest → inserts
        mock_session.execute.side_effect = [hash_result, source_result] + [
            MagicMock() for _ in range(len(source_rows))
        ]
        return mock_session

    def _make_source_row(
        self,
        row_id="r1",
        celex_id="32450_35_6",
        document="some text",
        chunk_hash="abc123",
    ):
        return SimpleNamespace(
            id=row_id,
            collection_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            document=document,
            cmetadata={"doc_type": "full_md"},
            celex_id=celex_id,
            doc_type="full_md",
            token_count=42,
            path="/some/path.md",
            chunk_hash=chunk_hash,
            doc_pos=0,
        )

    def _make_migrator(self, mock_session, batch_size=50):
        from pipelines.rag.pgvector.embeddings.utils.embedding_migration_qwen3 import (
            EmbeddingMigrationQwen3,
        )

        mock_factory = MagicMock(return_value=mock_session)
        return EmbeddingMigrationQwen3(
            session_factory=mock_factory, batch_size=batch_size
        )

    @patch(
        "pipelines.rag.pgvector.embeddings.utils.embedding_migration_qwen3.retrieve_qwen3_embedding",
        return_value=[0.1] * 4096,
    )
    def test_migrate_single_row(self, mock_embed):
        """One source row → embedding called → inserted into target."""
        row = self._make_source_row()
        mock_session = self._make_mock_session([row])
        migrator = self._make_migrator(mock_session)

        result = migrator.migrate()

        self.assertEqual(result.source_rows, 1)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.errors, [])

        mock_embed.assert_called_once_with("some text")
        mock_session.commit.assert_called()

    @patch(
        "pipelines.rag.pgvector.embeddings.utils.embedding_migration_qwen3.retrieve_qwen3_embedding",
        return_value=[0.2] * 4096,
    )
    def test_skip_existing(self, mock_embed):
        """Row with matching chunk_hash in target → skipped, no embedding call."""
        row = self._make_source_row(chunk_hash="already_there")
        mock_session = self._make_mock_session([row], existing_hashes=["already_there"])
        migrator = self._make_migrator(mock_session)

        result = migrator.migrate()

        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.skipped, 1)
        mock_embed.assert_not_called()

    @patch(
        "pipelines.rag.pgvector.embeddings.utils.embedding_migration_qwen3.retrieve_qwen3_embedding",
        return_value=[0.3] * 4096,
    )
    def test_empty_source(self, mock_embed):
        """Zero source rows → result counts are 0, no errors."""
        mock_session = self._make_mock_session([])
        migrator = self._make_migrator(mock_session)

        result = migrator.migrate()

        self.assertEqual(result.source_rows, 0)
        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.errors, [])
        mock_embed.assert_not_called()

    @patch(
        "pipelines.rag.pgvector.embeddings.utils.embedding_migration_qwen3.retrieve_qwen3_embedding",
    )
    def test_error_resilience(self, mock_embed):
        """Embedding failure on one row → logged in errors, others still processed."""
        rows = [
            self._make_source_row(row_id="r1", chunk_hash="h1", document="text1"),
            self._make_source_row(row_id="r2", chunk_hash="h2", document="text2"),
            self._make_source_row(row_id="r3", chunk_hash="h3", document="text3"),
        ]
        mock_session = self._make_mock_session(rows)
        migrator = self._make_migrator(mock_session)

        def embed_side_effect(text):
            if text == "text2":
                raise RuntimeError("Ollama timeout")
            return [0.5] * 4096

        mock_embed.side_effect = embed_side_effect

        result = migrator.migrate()

        self.assertEqual(result.inserted, 2)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("r2", result.errors[0])
        self.assertIn("Ollama timeout", result.errors[0])

    @patch(
        "pipelines.rag.pgvector.embeddings.utils.embedding_migration_qwen3.retrieve_qwen3_embedding",
        return_value=[0.4] * 4096,
    )
    def test_batch_commit(self, _mock_embed):
        """Commit called at each batch boundary."""
        rows = [
            self._make_source_row(row_id=f"r{i}", chunk_hash=f"h{i}", document=f"t{i}")
            for i in range(7)
        ]
        mock_session = self._make_mock_session(rows)
        migrator = self._make_migrator(mock_session, batch_size=3)

        result = migrator.migrate()

        self.assertEqual(result.inserted, 7)
        # batch_size=3 → commits at row 3, 6, then final commit for row 7
        # That's 3 commit() calls
        self.assertEqual(mock_session.commit.call_count, 3)

    @patch(
        "pipelines.rag.pgvector.embeddings.utils.embedding_migration_qwen3.retrieve_qwen3_embedding",
        return_value=[0.6] * 4096,
    )
    def test_skip_empty_document(self, mock_embed):
        """Rows with blank document text should be skipped."""
        row = self._make_source_row(document="   ", chunk_hash="blank_hash")
        mock_session = self._make_mock_session([row])
        migrator = self._make_migrator(mock_session)

        result = migrator.migrate()

        self.assertEqual(result.inserted, 0)
        self.assertEqual(result.skipped, 1)
        mock_embed.assert_not_called()


@unittest.skip("Obsolete: the OpenAI-to-Qwen3 embedding migration it tests was pruned in Aug 2026; kept as reference, skipped explicitly since 2026-09-17.")
class TestEmbeddingMigrationQwen3Integration(unittest.TestCase):
    """Integration test — requires live PostgreSQL and Ollama.

    Verifies that the destination table ``pg_embedding_qwen`` is filled
    after running the migration on a small subset.
    """

    def _check_services(self):
        """Skip if DB or Ollama are unreachable."""
        import requests
        from config.settings import settings
        from sqlalchemy import text as sa_text
        from pipelines.rag.pgvector.session import Session

        try:
            resp = requests.get(settings.OLLAMA_HOST, timeout=5)
            if resp.status_code != 200:
                self.skipTest("Ollama server is not returning 200 OK.")
        except requests.exceptions.RequestException:
            self.skipTest("Ollama server is not accessible.")

        try:
            with Session() as session:
                session.execute(sa_text("SELECT 1"))
        except Exception as exc:
            self.skipTest(f"Database is not accessible: {exc}")

    def test_migration_fills_target_table(self):
        """Migrate a small subset and confirm rows appear in pg_embedding_qwen."""
        self._check_services()

        from sqlalchemy import text as sa_text
        from pipelines.rag.pgvector.embeddings.utils.embedding_migration_qwen3 import (
            EmbeddingMigrationQwen3,
        )
        from pipelines.rag.pgvector.session import Session

        migrator = EmbeddingMigrationQwen3(batch_size=5)
        result = migrator.migrate(limit=3)

        self.assertIsInstance(result.inserted, int)
        self.assertGreaterEqual(result.inserted, 0)

        # Verify rows exist in the target table
        with Session() as session:
            count = session.execute(
                sa_text("SELECT COUNT(*) FROM celex_facts.pg_embedding_qwen")
            ).scalar()

        self.assertGreater(
            count,
            0,
            "Target table pg_embedding_qwen should have at least one row "
            "after migration.",
        )

        # Verify embeddings are non-null
        with Session() as session:
            null_count = session.execute(
                sa_text(
                    "SELECT COUNT(*) FROM celex_facts.pg_embedding_qwen "
                    "WHERE embedding IS NULL"
                )
            ).scalar()

        # !! therea
        self.assertEqual(
            null_count,
            0,
            "All migrated rows must have a non-null embedding.",
        )


if __name__ == "__main__":
    unittest.main()
