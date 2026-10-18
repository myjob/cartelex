import unittest
from unittest.mock import patch, MagicMock

# Import settings as required by the user instructions
from config.settings import settings
from pipelines.rag.pgvector.embeddings.qwen3_embedding import (
    EmbeddingInputTooLongError,
    Qwen3OllamaEmbeddings,
    count_tokens,
    embedding_token_limit,
    retrieve_qwen3_embedding,
)


class DummyEmbedResponse:
    """Mock for the response object normally returned by ollama.embed"""
    def __init__(self, embeddings):
        self.embeddings = embeddings


class TestQwen3Embedding(unittest.TestCase):

    @patch("pipelines.rag.pgvector.embeddings.qwen3_embedding.ollama.Client")
    def test_retrieve_qwen3_embedding_single(self, mock_client_class):
        # Setup mock
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.embed.return_value = DummyEmbedResponse(embeddings=[[0.1, 0.5, 0.9]])

        # Execute
        text = "Extract facts from this document."
        embedding = retrieve_qwen3_embedding(text)
        
        # Assert return types and structure
        self.assertIsInstance(embedding, list)
        self.assertEqual(len(embedding), 3)
        self.assertEqual(embedding, [0.1, 0.5, 0.9])
        
        # Assert the correct settings were used
        mock_client_class.assert_called_once_with(host=settings.OLLAMA_HOST)
        mock_client.embed.assert_called_once_with(
            model=settings.EMBEDDING_MODEL_qwen3,
            input=text
        )

    @patch("pipelines.rag.pgvector.embeddings.qwen3_embedding.ollama.Client")
    def test_retrieve_qwen3_embedding_batch(self, mock_client_class):
        # Setup mock
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.embed.return_value = DummyEmbedResponse(embeddings=[[0.1, 0.2], [0.3, 0.4]])

        # Execute
        texts = ["Extract facts from this document.", "Another text"]
        embeddings = retrieve_qwen3_embedding(texts)
        
        # Assert return types and structure
        self.assertIsInstance(embeddings, list)
        self.assertEqual(len(embeddings), 2)
        self.assertEqual(embeddings[0], [0.1, 0.2])
        
        # Assert
        mock_client_class.assert_called_once_with(host=settings.OLLAMA_HOST)
        mock_client.embed.assert_called_once_with(
            model=settings.EMBEDDING_MODEL_qwen3,
            input=texts
        )

    @patch("pipelines.rag.pgvector.embeddings.qwen3_embedding.ollama.Client")
    def test_qwen3_ollama_embeddings_class(self, mock_client_class):
        # Setup mock
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.embed.return_value = DummyEmbedResponse(embeddings=[[0.99, 0.88]])
        
        embedder = Qwen3OllamaEmbeddings()
        res = embedder.embed_query("query")
        
        # Assert
        mock_client_class.assert_called_once_with(host=settings.OLLAMA_HOST)
        self.assertEqual(res, [0.99, 0.88])
        mock_client.embed.assert_called_with(model=settings.EMBEDDING_MODEL_qwen3, input="query")

    @patch("pipelines.rag.pgvector.embeddings.qwen3_embedding.ollama.Client")
    def test_oversized_input_raises_typed_error(self, mock_client_class):
        """Input beyond the context window must not reach the Ollama client."""
        long_text = "cartel meeting on the price of trucks " * 1000

        with self.assertRaises(EmbeddingInputTooLongError) as ctx:
            retrieve_qwen3_embedding(long_text)

        self.assertEqual(ctx.exception.token_limit, embedding_token_limit())
        self.assertEqual(ctx.exception.tokens_num, count_tokens(long_text))
        self.assertIn(str(ctx.exception.tokens_num), str(ctx.exception))
        self.assertIn(str(ctx.exception.token_limit), str(ctx.exception))
        mock_client_class.assert_not_called()

    def test_oversized_error_is_still_a_value_error(self):
        """Existing `except ValueError` handlers must keep catching the guard."""
        self.assertTrue(issubclass(EmbeddingInputTooLongError, ValueError))

    def test_token_limit_comes_from_settings(self):
        """The guard must read the limit from settings, not a literal."""
        self.assertEqual(
            embedding_token_limit(),
            int(settings.OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT),
        )

    @patch("pipelines.rag.pgvector.embeddings.qwen3_embedding.ollama.Client")
    def test_input_at_the_limit_is_accepted(self, mock_client_class):
        """A passage exactly at the limit must still be embedded."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        mock_client.embed.return_value = DummyEmbedResponse(embeddings=[[0.1, 0.2]])

        token_limit = embedding_token_limit()
        text = "cartel " * token_limit
        while count_tokens(text) > token_limit:
            text = text[: text.rfind(" ")]

        self.assertEqual(retrieve_qwen3_embedding(text), [0.1, 0.2])
        mock_client.embed.assert_called_once()

    def test_retrieve_qwen3_embedding_integration(self):
        """
        True integration test that calls the live Ollama server.
        """
        import requests
        try:
            # Check if the Ollama server is reachable, skip otherwise
            response = requests.get(settings.OLLAMA_HOST)
            if response.status_code != 200:
                self.skipTest("Ollama server is not returning 200 OK.")
        except requests.exceptions.RequestException:
            self.skipTest("Ollama server is not accessible, skipping integration test.")
        
        # Execute actual individual call
        text = "This is a real integration test."
        embedding = retrieve_qwen3_embedding(text)
        
        # Assert return types and structure
        self.assertIsInstance(embedding, list)
        self.assertEqual(len(embedding), 4096)
        self.assertIsInstance(embedding[0], float)

        # Execute batch call
        texts = ["First document.", "Second document."]
        embeddings = retrieve_qwen3_embedding(texts)

        # Assert returns and structure
        self.assertIsInstance(embeddings, list)
        self.assertEqual(len(embeddings), 2)
        self.assertIsInstance(embeddings[0], list)
        self.assertEqual(len(embeddings[0]), 4096)
        
        # Test the class component Provisioner pattern
        embedder = Qwen3OllamaEmbeddings()
        res_query = embedder.embed_query("Integration query")
        self.assertIsInstance(res_query, list)
        self.assertEqual(len(res_query), 4096)

    def test_integration_insert_select_delete_qwen3(self):
        """
        Integration test: call Ollama embeddings, insert a dummy row into
        pg_embedding_qwen3, select it back, then delete it.

        Skipped if either the Ollama server or the database are not reachable.
        """
        import uuid
        import requests
        import psycopg

        # Skip if Ollama is not reachable
        try:
            resp = requests.get(settings.OLLAMA_HOST, timeout=5)
            if resp.status_code != 200:
                self.skipTest("Ollama server is not returning 200 OK.")
        except requests.exceptions.RequestException:
            self.skipTest("Ollama server is not accessible.")

        # Step 1: obtain embedding from Ollama
        sample_text = "Integration test: EU cartel decision fact extraction."
        embedding = retrieve_qwen3_embedding(sample_text) # len(embedding) should be 4096 for qwen3-embedding
        self.assertEqual(len(embedding), 4096)  # Ensure embedding length is as expected for qwen3-embedding
        self.assertIsInstance(embedding, list)
        self.assertGreater(len(embedding), 0)
        self.assertIsInstance(embedding[0], float)

        # Convert embedding list to pgvector literal  "[f1,f2,...]"
        vector_literal = "[" + ",".join(str(v) for v in embedding) + "]"

        test_id = f"test-{uuid.uuid4()}"
        db_url = settings.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")

        try:
            conn = psycopg.connect(db_url)
        except Exception as exc:
            self.skipTest(f"Database is not accessible: {exc}")

        # Check table exists before attempting DML
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'celex_facts' AND table_name = 'pg_embedding_qwen3'"
                )
                if cur.fetchone() is None:
                    conn.close()
                    self.skipTest("Table celex_facts.pg_embedding_qwen3 does not exist.")
        except Exception as exc:
            conn.close()
            self.skipTest(f"Could not verify table existence: {exc}")

        try:
            # Step 2: insert dummy row
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO celex_facts.langchain_pg_embedding_qwen3
                        (id, document, embedding, cmetadata, celex_id)
                    VALUES (%s, %s, %s::vector, %s::jsonb, %s)
                    """,
                    (test_id, sample_text, vector_literal, "{}", "test-integration"),
                )
            conn.commit()

            # Step 3: select the row back and verify content
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, document FROM celex_facts.langchain_pg_embedding_qwen3 WHERE id = %s",
                    (test_id,),
                )
                row = cur.fetchone()

            self.assertIsNotNone(row, "Inserted row was not found by SELECT.")
            self.assertEqual(row[0], test_id)
            self.assertEqual(row[1], sample_text)

        finally:
            # Step 4: delete the dummy row unconditionally
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM celex_facts.pg_embedding_qwen3 WHERE id = %s",
                    (test_id,),
                )
            conn.commit()
            conn.close()


if __name__ == '__main__':
    unittest.main()
