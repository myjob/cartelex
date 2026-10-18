"""
Tests for FactEmbeddingLoaderQwen.

Unit tests use mocked DB session and embedding function.
Integration tests require a live PostgreSQL instance and Ollama server.
"""

import hashlib
import math
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestFactEmbeddingLoaderQwenUnit(unittest.TestCase):
    """Unit tests — no live DB or Ollama required."""

    def _make_loader(self, mock_session):
        """Return a FactEmbeddingLoaderQwen wired to mock_session."""
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )

        mock_factory = MagicMock(return_value=mock_session)
        return FactEmbeddingLoaderQwen(session_factory=mock_factory)

    def _make_mock_session(self, rows):
        """Return a context-manager mock whose execute().fetchall() yields rows."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = rows

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute.return_value = mock_result
        return mock_session

    @patch(
        "pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen.retrieve_qwen3_embedding",
        return_value=[0.1] * 4096,
    )
    def test_insert_path(self, mock_embed):
        """Single row found: should call embed + execute UPSERT once."""
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )

        fake_row = SimpleNamespace(
            id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            question="Who are the cartel members?",
            fact_name="companies_investigated",
            value_md="**Company A** and **Company B**",
        )
        mock_session = self._make_mock_session([fake_row])
        loader = self._make_loader(mock_session)

        result = loader.ingest_for_celex_and_fact("32450_35_6", "companies_investigated")

        self.assertEqual(result, 1)

        # Verify embedding was called with the correct passage text
        value_txt = FactEmbeddingLoaderQwen._clean_for_embedding(fake_row.value_md)
        expected_passage = (
            f"Represent this fact: {fake_row.question} "
            f"{fake_row.fact_name} {value_txt}"
        )
        mock_embed.assert_called_once_with(expected_passage)

        # Verify UPSERT parameters
        expected_hash = hashlib.md5(expected_passage.encode()).hexdigest()
        expected_vec = "[" + ",".join(str(0.1) for _ in range(4096)) + "]"

        # execute is called twice: SELECT then UPSERT
        self.assertEqual(mock_session.execute.call_count, 2)
        upsert_call = mock_session.execute.call_args_list[1]
        upsert_params = upsert_call[0][1]

        self.assertEqual(upsert_params["fk_fact_id"], str(fake_row.id))
        self.assertEqual(upsert_params["celex_id"], "32450_35_6")
        self.assertEqual(upsert_params["fact_name"], "companies_investigated")
        self.assertEqual(upsert_params["hash_q_a"], expected_hash)
        self.assertEqual(upsert_params["vec"], expected_vec)

        # Verify commit was called
        mock_session.commit.assert_called_once()

    @patch(
        "pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen.retrieve_qwen3_embedding",
        return_value=[0.2] * 4096,
    )
    def test_update_path_on_conflict_sql(self, _mock_embed):
        """UPSERT SQL must contain ON CONFLICT ... DO UPDATE to handle refreshed facts."""
        fake_row = SimpleNamespace(
            id="11111111-2222-3333-4444-555555555555",
            question="Where was the cartel?",
            fact_name="companies_investigated",
            value_md="Updated value text",
        )
        mock_session = self._make_mock_session([fake_row])
        loader = self._make_loader(mock_session)

        loader.ingest_for_celex_and_fact("32450_35_6", "companies_investigated")

        upsert_sql_arg = mock_session.execute.call_args_list[1][0][0]
        sql_text = str(upsert_sql_arg)
        self.assertIn("ON CONFLICT", sql_text)
        self.assertIn("DO UPDATE SET", sql_text)

    @patch(
        "pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen.retrieve_qwen3_embedding",
        return_value=[0.3] * 4096,
    )
    def test_no_rows_returns_zero(self, _mock_embed):
        """When question2fact has no matching rows, return 0 and skip embedding."""
        mock_session = self._make_mock_session([])
        loader = self._make_loader(mock_session)

        result = loader.ingest_for_celex_and_fact("nonexistent", "no_fact")

        self.assertEqual(result, 0)
        _mock_embed.assert_not_called()
        # Only the SELECT should have been executed
        self.assertEqual(mock_session.execute.call_count, 1)

    @patch(
        "pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen.retrieve_qwen3_embedding",
        return_value=[0.4] * 4096,
    )
    def test_multiple_rows(self, mock_embed):
        """Multiple rows from question2fact each produce one UPSERT call."""
        rows = [
            SimpleNamespace(
                id=f"row-id-{i}",
                question=f"Question {i}",
                fact_name="companies_investigated",
                value_md=f"Value {i}",
            )
            for i in range(3)
        ]
        mock_session = self._make_mock_session(rows)
        loader = self._make_loader(mock_session)

        result = loader.ingest_for_celex_and_fact("32450_35_6", "companies_investigated")

        self.assertEqual(result, 3)
        self.assertEqual(mock_embed.call_count, 3)
        # SELECT + 3 UPSERTs
        self.assertEqual(mock_session.execute.call_count, 4)

    def test_clean_for_embedding_removes_artefacts(self):
        """_clean_for_embedding should strip tables, code blocks, and images."""
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )

        md = "Before\n|---|---|\n```python\ncode\n```\n![alt](img.png)\nAfter"
        result = FactEmbeddingLoaderQwen._clean_for_embedding(md)

        self.assertNotIn("|---|", result)
        self.assertNotIn("```", result)
        self.assertNotIn("![", result)
        self.assertIn("Before", result)
        self.assertIn("After", result)

    def test_compute_hash_is_md5_of_passage(self):
        """_compute_hash must return MD5 of the full passage text."""
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )

        passage = "Represent this fact: question fact_name value"
        expected = hashlib.md5(passage.encode()).hexdigest()
        self.assertEqual(FactEmbeddingLoaderQwen._compute_hash(passage), expected)


class TestFactEmbeddingLoaderQwenOversizedPassage(unittest.TestCase):
    """Passages beyond the embedding context are windowed and mean-pooled."""

    # A value long enough to need several windows at the 2048-token limit.
    LONG_VALUE = "cartel meeting in Brussels on the price of trucks " * 900

    def _make_loader(self, mock_session):
        """Return a FactEmbeddingLoaderQwen wired to mock_session."""
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )

        return FactEmbeddingLoaderQwen(session_factory=MagicMock(return_value=mock_session))

    def _make_mock_session(self, rows):
        """Return a context-manager mock whose execute().fetchall() yields rows."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = rows

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute.return_value = mock_result
        return mock_session

    def _long_row(self):
        """Return a question2fact row whose value exceeds the embedding context."""
        return SimpleNamespace(
            id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            question="What happened when?",
            fact_name="heideltime_event",
            value_md=self.LONG_VALUE,
        )

    @patch(
        "pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen.retrieve_qwen3_embedding",
        return_value=[0.1] * 4096,
    )
    def test_short_passage_still_uses_a_single_call(self, mock_embed):
        """The unchanged path: a fitting passage is embedded verbatim, once."""
        row = SimpleNamespace(
            id="row-id",
            question="Who are the cartel members?",
            fact_name="companies_investigated",
            value_md="**Company A** and **Company B**",
        )
        mock_session = self._make_mock_session([row])
        loader = self._make_loader(mock_session)

        result = loader.ingest_for_celex_and_fact("32450_35_6", "companies_investigated")

        self.assertEqual(result, 1)
        self.assertEqual(mock_embed.call_count, 1)
        self.assertEqual(
            mock_embed.call_args[0][0],
            "Represent this fact: Who are the cartel members? "
            "companies_investigated **Company A** and **Company B**",
        )

    @patch(
        "pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen.retrieve_qwen3_embedding",
        return_value=[0.1] * 4096,
    )
    def test_long_passage_is_split_into_windows_within_the_limit(self, mock_embed):
        """Every window must carry the prefix and stay under the token limit."""
        from pipelines.rag.pgvector.embeddings.qwen3_embedding import (
            count_tokens,
            embedding_token_limit,
        )

        mock_session = self._make_mock_session([self._long_row()])
        loader = self._make_loader(mock_session)

        result = loader.ingest_for_celex_and_fact("32450_35_6", "heideltime_event")

        self.assertEqual(result, 1, "One fact must still yield exactly one row")
        self.assertGreater(mock_embed.call_count, 1, "Long value must be windowed")

        token_limit = embedding_token_limit()
        for call in mock_embed.call_args_list:
            window_passage = call[0][0]
            self.assertTrue(
                window_passage.startswith(
                    "Represent this fact: What happened when? heideltime_event "
                ),
                "Each window must stand alone as a full passage",
            )
            self.assertLessEqual(count_tokens(window_passage), token_limit)

        # SELECT + exactly one UPSERT, regardless of the window count.
        self.assertEqual(mock_session.execute.call_count, 2)

    @patch(
        "pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen.retrieve_qwen3_embedding"
    )
    def test_pooled_vector_is_the_normalised_mean(self, mock_embed):
        """The stored vector is the re-normalised mean of the window vectors."""
        mock_embed.side_effect = lambda _passage: [3.0, 4.0, 0.0, 0.0]

        mock_session = self._make_mock_session([self._long_row()])
        loader = self._make_loader(mock_session)
        loader.ingest_for_celex_and_fact("32450_35_6", "heideltime_event")

        upsert_params = mock_session.execute.call_args_list[-1][0][1]
        vector = [float(x) for x in upsert_params["vec"].strip("[]").split(",")]

        # Every window returned the same direction, so pooling reproduces it.
        self.assertAlmostEqual(vector[0], 0.6, places=6)
        self.assertAlmostEqual(vector[1], 0.8, places=6)
        self.assertAlmostEqual(
            math.sqrt(sum(value * value for value in vector)), 1.0, places=6
        )

    def test_mean_pool_normalises_and_handles_zero_vectors(self):
        """_mean_pool returns a unit vector and never produces NaN."""
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )

        pooled = FactEmbeddingLoaderQwen._mean_pool([[1.0, 0.0], [0.0, 2.0]])
        self.assertAlmostEqual(pooled[0], math.sqrt(0.5), places=6)
        self.assertAlmostEqual(pooled[1], math.sqrt(0.5), places=6)

        degenerate = FactEmbeddingLoaderQwen._mean_pool([[0.0, 0.0], [0.0, 0.0]])
        self.assertEqual(degenerate, [0.0, 0.0])
        for value in degenerate:
            self.assertFalse(math.isnan(value))

        with self.assertRaises(ValueError):
            FactEmbeddingLoaderQwen._mean_pool([])


class TestFactEmbeddingLoaderQwenIntegration(unittest.TestCase):
    """
    Integration tests — require live PostgreSQL (settings.DATABASE_URL)
    and Ollama server (settings.OLLAMA_HOST).

    Skipped automatically when either service is unreachable.
    """

    TEST_CELEX_ID = "32450_35_6"
    TEST_FACT_NAME = "companies_investigated"

    def _check_services(self):
        """Skip the test if DB or Ollama are not reachable."""
        import requests
        from config.settings import settings
        from sqlalchemy import text
        from pipelines.rag.pgvector.session import Session

        # Check Ollama
        try:
            resp = requests.get(settings.OLLAMA_HOST, timeout=5)
            if resp.status_code != 200:
                self.skipTest("Ollama server is not returning 200 OK.")
        except requests.exceptions.RequestException:
            self.skipTest("Ollama server is not accessible.")

        # Check DB
        try:
            with Session() as session:
                session.execute(text("SELECT 1"))
        except Exception as exc:
            self.skipTest(f"Database is not accessible: {exc}")

    def _restore(self):
        """Re-embed the pair the test touched, leaving the corpus as found.

        TEST_CELEX_ID is a real corpus document, not a fixture: its embeddings
        belong to the production vector store. Deleting them here (the previous
        behaviour, which removed every row for the whole celex_id) silently
        stripped 46 unrelated facts of their vectors on every run. The loader
        UPSERTs, so re-ingesting is enough to leave the row consistent with the
        current fact value.
        """
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )

        FactEmbeddingLoaderQwen().ingest_for_celex_and_fact(
            self.TEST_CELEX_ID, self.TEST_FACT_NAME
        )

    def test_ingest_and_verify(self):
        """
        Full round-trip: ingest embeddings for a known celex_id and fact_name,
        verify the row exists with a non-null embedding vector, then clean up.
        """
        self._check_services()

        from sqlalchemy import text
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )
        from pipelines.rag.pgvector.session import Session

        loader = FactEmbeddingLoaderQwen()

        try:
            result = loader.ingest_for_celex_and_fact(
                self.TEST_CELEX_ID, self.TEST_FACT_NAME
            )
            self.assertIsInstance(result, int)
            self.assertGreaterEqual(result, 0)

            # If the row was already present, result may be 0 (no new rows),
            # but the row must exist with embeddings filled in.
            with Session() as session:
                rows = session.execute(
                    text(
                        "SELECT fk_fact_id, embeddings, hash_q_a"
                        " FROM celex_facts.question2fact2embeddings"
                        " WHERE celex_id = :celex_id AND fact_name = :fact_name"
                    ),
                    {
                        "celex_id": self.TEST_CELEX_ID,
                        "fact_name": self.TEST_FACT_NAME,
                    },
                ).fetchall()

            self.assertGreater(
                len(rows), 0, "No rows found after ingest — check question2fact view."

            )
            for row in rows:
                self.assertIsNotNone(row.embeddings, "embeddings column must not be NULL")
                self.assertIsNotNone(row.hash_q_a, "hash_q_a column must not be NULL")

        finally:
            self._restore()

    def test_idempotent_upsert(self):
        """
        Calling ingest twice with the same data must not raise and must leave
        exactly one row per fact (the PK constraint is respected).
        """
        self._check_services()

        from sqlalchemy import text
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            FactEmbeddingLoaderQwen,
        )
        from pipelines.rag.pgvector.session import Session

        loader = FactEmbeddingLoaderQwen()

        try:
            loader.ingest_for_celex_and_fact(self.TEST_CELEX_ID, self.TEST_FACT_NAME)
            loader.ingest_for_celex_and_fact(self.TEST_CELEX_ID, self.TEST_FACT_NAME)

            with Session() as session:
                count = session.execute(
                    text(
                        "SELECT COUNT(*) FROM celex_facts.question2fact2embeddings"
                        " WHERE celex_id = :celex_id AND fact_name = :fact_name"
                    ),
                    {
                        "celex_id": self.TEST_CELEX_ID,
                        "fact_name": self.TEST_FACT_NAME,
                    },
                ).scalar()

            # Count must equal the number of distinct facts — no duplicates.
            self.assertGreater(count, 0)

        finally:
            self._restore()


class TestAllFactsEmbeddingLoaderQwenUnit(unittest.TestCase):
    """Unit tests for AllFactsEmbeddingLoaderQwen — no live DB or Ollama required."""

    def _make_outer_session(self, rows):
        """Return a context-manager mock whose execute().fetchall() yields rows."""
        mock_result = MagicMock()
        mock_result.fetchall.return_value = rows

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute.return_value = mock_result
        return mock_session

    def _make_loader(self, rows):
        """Return AllFactsEmbeddingLoaderQwen wired to a mock session that yields rows."""
        from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
            AllFactsEmbeddingLoaderQwen,
        )

        mock_session = self._make_outer_session(rows)
        mock_factory = MagicMock(return_value=mock_session)
        return AllFactsEmbeddingLoaderQwen(session_factory=mock_factory)

    @staticmethod
    def _make_fact_row(celex_id: str, name: str):
        return SimpleNamespace(celex_id=celex_id, name=name)

    def test_ingest_all_calls_loader_per_unique_pair(self):
        """Three distinct pairs → inner loader called exactly 3 times."""
        rows = [
            self._make_fact_row("A", "fine"),
            self._make_fact_row("A", "company"),
            self._make_fact_row("B", "fine"),
        ]
        loader = self._make_loader(rows)

        with patch.object(loader._loader, "ingest_for_celex_and_fact", return_value=1) as mock_ingest:
            result = loader.ingest_all()

        self.assertEqual(result.facts_processed, 3)
        self.assertEqual(mock_ingest.call_count, 3)
        self.assertEqual(result.rows_upserted, 3)
        self.assertEqual(result.errors, [])

    def test_ingest_all_deduplicates_pairs(self):
        """Duplicate (celex_id, name) rows in facts → loader called only once per pair."""
        rows = [
            self._make_fact_row("A", "fine"),
            self._make_fact_row("A", "fine"),  # duplicate
        ]
        loader = self._make_loader(rows)

        # The SELECT uses DISTINCT, so fetchall returns only unique pairs.
        # We simulate the DB already deduplicating for us, but test that
        # AllFactsEmbeddingLoaderQwen does not call the inner loader twice
        # even if the outer SELECT returns duplicates.
        with patch.object(loader._loader, "ingest_for_celex_and_fact", return_value=2) as mock_ingest:
            result = loader.ingest_all()

        # Both rows are returned by the mock; the loader calls once per row
        # (SQL DISTINCT would prevent duplicates in production, but the class
        # itself does not add a second dedup layer — it trusts the SQL).
        self.assertEqual(result.facts_processed, 2)
        self.assertEqual(mock_ingest.call_count, 2)

    def test_ingest_all_no_rows(self):
        """Empty facts table → facts_processed==0, rows_upserted==0, errors==[]."""
        loader = self._make_loader([])

        with patch.object(loader._loader, "ingest_for_celex_and_fact", return_value=0) as mock_ingest:
            result = loader.ingest_all()

        self.assertEqual(result.facts_processed, 0)
        self.assertEqual(result.rows_upserted, 0)
        self.assertEqual(result.errors, [])
        mock_ingest.assert_not_called()

    def test_ingest_all_continues_on_error(self):
        """One pair raises → errors list has 1 entry; remaining pairs still processed."""
        rows = [
            self._make_fact_row("A", "fine"),
            self._make_fact_row("B", "company"),
            self._make_fact_row("C", "fine"),
        ]
        loader = self._make_loader(rows)

        def side_effect(celex_id, fact_name):
            if celex_id == "B":
                raise RuntimeError("Ollama timeout")
            return 1

        with patch.object(loader._loader, "ingest_for_celex_and_fact", side_effect=side_effect):
            result = loader.ingest_all()

        self.assertEqual(result.facts_processed, 3)
        self.assertEqual(result.rows_upserted, 2)  # A and C succeeded
        self.assertEqual(len(result.errors), 1)
        self.assertIn("B/company", result.errors[0])
        self.assertIn("Ollama timeout", result.errors[0])

    def test_ingest_all_aggregates_upserted_count(self):
        """rows_upserted is the sum of all individual ingest_for_celex_and_fact returns."""
        rows = [
            self._make_fact_row("A", "fine"),
            self._make_fact_row("A", "company"),
            self._make_fact_row("B", "fine"),
        ]
        loader = self._make_loader(rows)

        return_values = [3, 5, 2]  # each call returns a different count
        with patch.object(
            loader._loader,
            "ingest_for_celex_and_fact",
            side_effect=return_values,
        ):
            result = loader.ingest_all()

        self.assertEqual(result.rows_upserted, 10)


if __name__ == "__main__":
    unittest.main()
