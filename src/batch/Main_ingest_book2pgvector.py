"""
Ingest the infringement book (MinerU markdown) into ``pg_embedding_book_qwen``.

The book is one document, so this is a thin CLI rather than a
``BatchJobRunner`` job (which iterates CELEX ids). It gives the book load the
same operator surface as the other batches -- explicit arguments with
settings-derived defaults and a ``--dry-run`` -- so renaming or moving the
markdown cannot silently break the loader (the path used to be a literal in
the ingestor's ``__main__`` block).

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_book2pgvector.py --dry-run
    PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_book2pgvector.py
    PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_book2pgvector.py \
        --book-name My_Book --md-file /path/to/book-mineru.md
"""

import argparse
import logging
import os
import sys

from config.settings import settings
from pipelines.rag.pgvector.embeddings.book_chunks2pgvector import (
    BOOK_COLLECTION_NAME,
    BookMarkdownIngestor,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("ingest_book2pgvector")


def run(book_name: str, md_file: str, dry_run: bool = False) -> bool:
    """Ingest one book markdown file, or report what would be ingested.

    Parameters
    ----------
    book_name:
        ``book_name`` key the sections are stored under.
    md_file:
        Absolute path to the MinerU markdown of the book.
    dry_run:
        When ``True``, only checks that the file exists and logs the plan; no
        embedding call, no write.

    Returns
    -------
    bool
        ``False`` when the markdown file does not exist, else ``True``.
    """
    if not os.path.exists(md_file):
        logger.error("Book markdown not found: %s", md_file)
        return False
    if dry_run:
        logger.info("[DRY-RUN] Would ingest %s as book_name=%r", md_file, book_name)
        return True

    from pipelines.rag.pgvector.embeddings.qwen3_embedding import Qwen3OllamaEmbeddings

    ingestor = BookMarkdownIngestor(
        embeddings=Qwen3OllamaEmbeddings(), collection_name=BOOK_COLLECTION_NAME
    )
    ingestor.ingest_book(book_name=book_name, md_file=md_file)
    logger.info("Book ingestion complete for %s", book_name)
    return True


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Ingest the infringement book markdown into pg_embedding_book_qwen"
    )
    parser.add_argument(
        "--book-name",
        default=settings.BOOK_NAME,
        help="book_name key for the stored sections (default: settings.BOOK_NAME)",
    )
    parser.add_argument(
        "--md-file",
        default=settings.BOOK_MD_PATH,
        help="Path to the book's MinerU markdown (default: settings.BOOK_MD_PATH)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check the file and log the plan without embedding or writing",
    )
    args = parser.parse_args()
    ok = run(book_name=args.book_name, md_file=args.md_file, dry_run=args.dry_run)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
