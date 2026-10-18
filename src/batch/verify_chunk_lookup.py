"""Standalone verification for the provenance "view chunk" resolution path.

Given a chunk id (the ``{embedding_table}.id`` embedded in a ``view chunk``
provenance link), this script confirms the id resolves end-to-end:

1. **DB layer** — calls :meth:`ChunkRepository.get_chunk_by_id` directly and
   prints the resolved ``celex_id`` / ``doc_pos`` and a document preview.
2. **HTTP layer** (optional, ``--http``) — fetches
   ``{settings.CHUNK_VIEW_BASE_URL}/chunk/{chunk_id}`` exactly as a browser
   click would, confirming the running oi_dspy server serves the page.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/verify_chunk_lookup.py \
        b0aeee5b-1431-4d4a-a60f-4a595ede2bf6
    PYTHONPATH=src .venv/bin/python src/batch/verify_chunk_lookup.py \
        b0aeee5b-1431-4d4a-a60f-4a595ede2bf6 --http
"""

import argparse
import logging
import sys

from config.settings import settings
from pipelines.rag.pgvector.chunk_repository import ChunkRepository

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("verify_chunk_lookup")

# Default to the id the user reported clicking, so a bare run is a smoke test.
_DEFAULT_CHUNK_ID = "b0aeee5b-1431-4d4a-a60f-4a595ede2bf6"

# How much of the document body to echo as a preview.
_PREVIEW_CHARS = 500


def verify_db(chunk_id: str) -> bool:
    """Resolve *chunk_id* via the DB layer and print the outcome.

    Args:
        chunk_id: Stable chunk id to look up.

    Returns:
        ``True`` when a chunk row was found, ``False`` otherwise.
    """
    chunk_repo = ChunkRepository()
    logger.info(
        "DB lookup: table=celex_facts.%s id=%s",
        chunk_repo.embedding_table,
        chunk_id,
    )
    chunk = chunk_repo.get_chunk_by_id(chunk_id)
    if chunk is None:
        logger.error("NOT FOUND — no row in %s with id=%s",
                     chunk_repo.embedding_table, chunk_id)
        return False

    document = chunk["document"] or ""
    logger.info("FOUND — celex_id=%s doc_pos=%s document_chars=%d",
                chunk["celex_id"], chunk["doc_pos"], len(document))
    preview = document[:_PREVIEW_CHARS]
    suffix = " …" if len(document) > _PREVIEW_CHARS else ""
    logger.info("Document preview:\n%s%s", preview, suffix)
    return True


def verify_http(chunk_id: str) -> bool:
    """Fetch the chunk via the live oi_dspy endpoint and print the outcome.

    Args:
        chunk_id: Stable chunk id to request.

    Returns:
        ``True`` when the endpoint returns HTTP 200, ``False`` otherwise.
    """
    import httpx

    base = settings.CHUNK_VIEW_BASE_URL.rstrip("/")
    url = f"{base}/chunk/{chunk_id}"
    logger.info("HTTP GET %s", url)
    try:
        response = httpx.get(url, timeout=10.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        logger.error("HTTP request failed: %s", exc)
        return False

    logger.info("HTTP %d — content_type=%s body_chars=%d",
                response.status_code,
                response.headers.get("content-type", "?"),
                len(response.text))
    return response.status_code == 200


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Verify a provenance chunk id resolves to its source text"
    )
    parser.add_argument(
        "chunk_id",
        nargs="?",
        default=_DEFAULT_CHUNK_ID,
        help="Chunk id to resolve (defaults to the reported sample id)",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Also fetch the live oi_dspy GET /chunk/{id} endpoint",
    )
    args = parser.parse_args()

    db_ok = verify_db(args.chunk_id)
    http_ok = verify_http(args.chunk_id) if args.http else True

    if db_ok and http_ok:
        logger.info("OK — chunk id resolves%s", " (DB + HTTP)" if args.http else "")
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
