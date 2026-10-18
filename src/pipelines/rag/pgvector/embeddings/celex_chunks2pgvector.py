import logging
import os
import tiktoken
import hashlib
from typing import List, Tuple
from sqlalchemy import text
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from pipelines.utils.TokenSizeTargetMarkdownSplitter import TokenTargetMarkdownSplitter
from pipelines.rag.pgvector.celex_pgvector import CelexPGVector, CelexPGVectorPgQwen
from pipelines.rag.pgvector.vector_config import (
    DATABASE_URL,
    COLLECTION_NAME,
    SEARCH_PATH,
)
from config.settings import settings

logger = logging.getLogger(__name__)


class MarkdownIngestorBase:
    """
    Shared machinery for ingesting markdown into a pgvector store.

    Subclasses name the store class, the key column that identifies one
    source (``celex_id`` for decisions, ``book_name`` for the book) and the
    table the verification counts read; they add their own persist step. The
    embedding provider is injected at construction time, making the ingestor
    provider-agnostic. Pass any LangChain-compatible ``Embeddings`` instance --
    ``Qwen3OllamaEmbeddings`` is the canonical one.

    Args:
        embeddings: A LangChain ``Embeddings`` implementation used to embed
            document chunks before storage.
        db_url: SQLAlchemy connection string. Defaults to ``DATABASE_URL``
            from ``vector_config``.
        collection_name: pgvector collection name. Use a distinct name per
            embedding provider so dimension-mismatched vectors are never
            mixed in the same collection.
    """

    #: The ``CelexPGVector`` subclass backing this ingestor.
    _STORE_CLASS: type[CelexPGVector] = CelexPGVector
    #: Column identifying one source in the store's table.
    _KEY_COLUMN: str = "celex_id"
    #: Fully qualified table the verification counts read.
    _TABLE: str = f"celex_facts.{settings.EMBEDDING_TABLE}"

    def __init__(
        self,
        embeddings: Embeddings,
        db_url: str = DATABASE_URL,
        collection_name: str = COLLECTION_NAME,
    ):
        self.embeddings = embeddings
        self.db_url = db_url
        self.collection_name = collection_name
        self.vector_store = self._setup_pgvector()

    def _setup_pgvector(self) -> CelexPGVector:
        """
        Initialise the backing store named by ``_STORE_CLASS``.

        Returns:
            A configured store instance.
        """
        return self._STORE_CLASS(
            embeddings=self.embeddings,
            collection_name=self.collection_name,
            connection=self.db_url,
            use_jsonb=True,
            engine_args={"connect_args": {"options": f"-csearch_path={SEARCH_PATH}"}},
        )

    def _split_markdown(self, md_file: str) -> Tuple[List[Document], int]:
        """
        Split a markdown file into token-bounded chunks.

        Args:
            md_file: Absolute path to the markdown file.

        Returns:
            A tuple of (chunks, doc_total_tokens) where chunks is a list of
            ``Document`` objects and doc_total_tokens is the total token count
            of the original document.
        """
        headers = [("#", "Header 1"), ("##", "Header 2")]

        try:
            with open(md_file, "r", encoding="utf-8") as f:
                markdown_text = f.read()
        except FileNotFoundError:
            logger.warning("%s not found, skipping ingestion", md_file)
            return [], 0

        splitter = TokenTargetMarkdownSplitter(
            headers_to_split_on=headers, chunk_size=500
        )
        doc_total_tokens = len(
            tiktoken.encoding_for_model("gpt-3.5-turbo").encode(markdown_text)
        )
        chunks = splitter.split_text(markdown_text)
        return chunks, doc_total_tokens

    def _verify_load_by_token_count(self, key: str, chunk_token_sum: int) -> None:
        """
        Assert that the DB token sum matches the expected value after ingestion.

        Args:
            key: The source identifier to query (value of ``_KEY_COLUMN``).
            chunk_token_sum: Expected sum of token counts across all chunks.

        Raises:
            AssertionError: If the stored token sum does not match.
        """
        logger.info("--- Verifying Token Count (Expected: %d) ---", chunk_token_sum)
        with self.vector_store._make_sync_session() as session:
            result = session.execute(
                text(
                    f"SELECT SUM(token_count) FROM {self._TABLE}"
                    f" WHERE {self._KEY_COLUMN} = :key"
                ),
                {"key": key},
            ).scalar()
            logger.info("--- DB Token Sum: %s ---", result)

            db_sum = result if result is not None else 0
            assert db_sum == chunk_token_sum, (
                f"Token sum mismatch! DB: {db_sum}, Expected: {chunk_token_sum}"
            )
            logger.info("--- Ingestion verification passed: sizes match ---")

    def _verify_load_by_chunk_count(self, key: str, chunk_count: int) -> None:
        """
        Assert that the DB row count matches the expected chunk count.

        Args:
            key: The source identifier to query (value of ``_KEY_COLUMN``).
            chunk_count: Expected number of chunk rows.

        Raises:
            AssertionError: If the stored row count does not match.
        """
        logger.info("--- Verifying Chunk Count (Expected: %d) ---", chunk_count)
        with self.vector_store._make_sync_session() as session:
            result = session.execute(
                text(
                    f"SELECT COUNT(*) FROM {self._TABLE}"
                    f" WHERE {self._KEY_COLUMN} = :key"
                ),
                {"key": key},
            ).scalar()
            logger.info("--- DB Row Count: %s ---", result)

            db_sum = result if result is not None else 0
            assert db_sum == chunk_count, (
                f"Row count mismatch! DB: {db_sum}, Expected: {chunk_count}"
            )
            logger.info("--- Ingestion verification passed: chunk count match ---")


class CelexMarkdownIngestor(MarkdownIngestorBase):
    """
    Ingests Celex markdown documents into ``pg_embedding_qwen``.

    See :class:`MarkdownIngestorBase` for the constructor arguments.
    """

    _STORE_CLASS = CelexPGVectorPgQwen
    _KEY_COLUMN = "celex_id"
    _TABLE = f"celex_facts.{settings.EMBEDDING_TABLE}"

    def _persist_md_chunks(
        self, celex_id: str, md_file: str, chunks: List[Document], doc_total_tokens: int
    ) -> int:
        """
        Embed and persist document chunks in the vector store.

        Args:
            celex_id: The Celex document identifier.
            md_file: Source file path stored as metadata.
            chunks: List of ``Document`` chunks produced by the splitter.
            doc_total_tokens: Total token count of the source document used
                for a sanity-check assertion.

        Returns:
            The sum of token counts across all persisted chunks.
        """
        chunk_token_sum = 0
        doc_chunks = []
        logger.info("--- %s created %d chunks ---", celex_id, len(chunks))
        for i, chunk in enumerate(chunks):
            doc_chunks.append(
                Document(
                    page_content=chunk.page_content,
                    metadata={
                        "id": i,
                        "celex_id": celex_id,
                        "token_count": chunk.metadata["token_count"],
                        "doc_type": "full_md",
                        "path": md_file,
                        "chunk_hash": hashlib.md5(
                            chunk.page_content.encode("utf-8")
                        ).hexdigest(),
                    },
                )
            )
            chunk_token_sum += chunk.metadata["token_count"]

        assert (chunk_token_sum + (chunk_token_sum * 0.1)) > doc_total_tokens or (
            chunk_token_sum - (chunk_token_sum * 0.1)
        ) < doc_total_tokens

        self.vector_store.add_documents(doc_chunks)
        return chunk_token_sum

    def ingest_doc(self, celex_id: str, md_file: str) -> None:
        """
        Full ingestion pipeline: split, embed, persist, and verify.

        Args:
            celex_id: The Celex document identifier.
            md_file: Absolute path to the markdown file to ingest.
        """
        chunks_of_langchain_doc, doc_total_tokens = self._split_markdown(md_file)
        if not chunks_of_langchain_doc:
            logger.warning("No chunks created for %s", md_file)
            return

        chunk_token_sum = self._persist_md_chunks(
            celex_id, md_file, chunks_of_langchain_doc, doc_total_tokens
        )
        self._verify_load_by_token_count(celex_id, chunk_token_sum)
        self._verify_load_by_chunk_count(celex_id, len(chunks_of_langchain_doc))


if __name__ == "__main__":
    # --- Qwen3 / Ollama ---
    # Reads settings.OLLAMA_HOST and settings.EMBEDDING_MODEL_qwen3 automatically.
    # Override via env vars or pass explicitly:
    #   Qwen3OllamaEmbeddings(host="http://localhost:11434", model="qwen3-embedding:8b")
    from pipelines.rag.pgvector.embeddings.qwen3_embedding import Qwen3OllamaEmbeddings

    qwen3_ingestor = CelexMarkdownIngestor(
        embeddings=Qwen3OllamaEmbeddings(),
        collection_name="celex_chunks_qwen3",
    )

    qwen3_ingestor.ingest_doc(
        "36756_74_4",
        os.path.join(settings.CELEX_DOCS_DIR, "36756_74_4/mineru/auto/36756_74_4.md"),
    )
