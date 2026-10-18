import logging
import hashlib
from langchain_core.documents import Document
from pipelines.rag.pgvector.embeddings.book_tree import BookNode, InfringementTreeBuilder
from pipelines.rag.pgvector.embeddings.celex_chunks2pgvector import MarkdownIngestorBase
from pipelines.rag.pgvector.celex_pgvector import CelexPGVectorPgBookQwen3
from config.settings import settings

logger = logging.getLogger(__name__)

#: pgvector collection the book sections live in.
BOOK_COLLECTION_NAME = "book_chunks_qwen3"


class BookMarkdownIngestor(MarkdownIngestorBase):
    """
    Ingests the infringement book (converted to markdown) into
    ``pg_embedding_book_qwen``, one row per chapter/section node.

    See :class:`MarkdownIngestorBase` for the constructor arguments; the
    batch entry point is ``src/batch/Main_ingest_book2pgvector.py``.
    """

    _STORE_CLASS = CelexPGVectorPgBookQwen3
    _KEY_COLUMN = "book_name"
    _TABLE = "celex_facts.pg_embedding_book_qwen"

    def _persist_md_chapters(
        self, book_name: str, md_file: str, tree: BookNode, doc_total_tokens: int
    ) -> int:
        """
        Embed and persist the book's section nodes in the vector store.

        Args:
            book_name: The name of the book.
            md_file: Source file path stored as metadata.
            tree: The hierarchical tree of book sections.
            doc_total_tokens: Total token count of the source document used
                for a sanity-check assertion.

        Returns:
            The sum of token counts across all persisted chunks.
        """
        def _flatten(node: BookNode) -> list[BookNode]:
            nodes: list[BookNode] = []
            if node.level > 0:
                nodes.append(node)
            for child in node.children:
                nodes.extend(_flatten(child))
            return nodes

        all_nodes = _flatten(tree)
        chunk_token_sum = 0
        doc_chunks = []
        logger.info("--- %s created %d nodes ---", book_name, len(all_nodes))
        for i, node in enumerate(all_nodes):
            doc_chunks.append(
                Document(
                    page_content=node.raw_text,
                    metadata={
                        "id": i,
                        "book_name": book_name,
                        "doc_type": "book_section",
                        "path": node.path,
                        "level": node.level,
                        "title": node.title,
                        "text_line": node.text_line,
                        "token_count": node.token_count,
                        "source": md_file,
                        "chunk_hash": hashlib.md5(
                            node.raw_text.encode("utf-8")
                        ).hexdigest(),
                        "doc_pos": i,
                    },
                )
            )
            chunk_token_sum += node.token_count

        assert (chunk_token_sum + (chunk_token_sum * 0.1)) > doc_total_tokens or (
            chunk_token_sum - (chunk_token_sum * 0.1)
        ) < doc_total_tokens

        self.vector_store.add_documents(doc_chunks)
        return chunk_token_sum

    def ingest_book(self, book_name: str, md_file: str) -> None:
        """
        Full ingestion pipeline: build the section tree, embed, persist, verify.

        Args:
            book_name: The name of the book.
            md_file: Absolute path to the markdown file to ingest.
        """

        tree = InfringementTreeBuilder().build(md_file)

        if not tree.children:
            logger.error("No tree created for %s", md_file)
            return

        _, doc_total_tokens = self._split_markdown(md_file)
        chunk_token_sum = self._persist_md_chapters(book_name, md_file, tree, doc_total_tokens)
        self._verify_load_by_token_count(book_name, chunk_token_sum)


if __name__ == "__main__":
    # Prefer the batch entry point (src/batch/Main_ingest_book2pgvector.py),
    # which adds --dry-run and argument handling; this block mirrors it.
    from pipelines.rag.pgvector.embeddings.qwen3_embedding import Qwen3OllamaEmbeddings

    BookMarkdownIngestor(
        embeddings=Qwen3OllamaEmbeddings(),
        collection_name=BOOK_COLLECTION_NAME,
    ).ingest_book(book_name=settings.BOOK_NAME, md_file=settings.BOOK_MD_PATH)
