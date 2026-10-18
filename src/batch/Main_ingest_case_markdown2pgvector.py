import logging
import os

from batch.utils.Job_Utils import loadJsonFor
from batch.BatchJobRunner import BatchJobRunner
from pipelines.rag.pgvector.embeddings.celex_chunks2pgvector import (
    CelexMarkdownIngestor,
)
from config.settings import settings


def _build_ingestor() -> CelexMarkdownIngestor:
    """Construct a ``CelexMarkdownIngestor`` for the canonical embedding provider.

    The ingestor takes a required, injected ``Embeddings`` instance. Qwen3 is the
    sole provider, so the batch entry point mirrors the rest of the pipeline
    (``qwen3`` -> ``pg_embedding_qwen``).

    Returns:
        A ready-to-use ``CelexMarkdownIngestor``.
    """
    from pipelines.rag.pgvector.embeddings.qwen3_embedding import (
        Qwen3OllamaEmbeddings,
    )

    return CelexMarkdownIngestor(embeddings=Qwen3OllamaEmbeddings())


def _resolve_md_path(celex_id: str, master_data: dict) -> str:
    """Resolve the markdown source path for a CELEX ID.

    Prefers the ``txt_md_file`` recorded in the master descriptor JSON. Falls
    back to the conventional on-disk layout
    ``<CELEX_DOCS_DIR>/<celex_id>/mineru/auto/<celex_id>.md`` so a document that
    is missing from the master JSON can still be ingested directly.

    Args:
        celex_id: The CELEX document identifier.
        master_data: Parsed master descriptor JSON (may be empty).

    Returns:
        Absolute path to the candidate markdown file (existence not guaranteed).
    """
    doc_data = master_data.get("documents", {}).get(celex_id, {})
    filename = doc_data.get("txt_md_file", "NA")
    if filename and filename != "NA":
        return filename

    return os.path.join(
        settings.CELEX_DOCS_DIR, celex_id, "mineru", "auto", f"{celex_id}.md"
    )


def process_single_celex(celex_id, context, dry_run=False):
    ingestor, master_data = context
    logger = logging.getLogger(__name__)

    md_path = _resolve_md_path(celex_id, master_data)
    if not os.path.exists(md_path):
        logger.warning(
            "Skip load for %s: markdown not found at %s", celex_id, md_path
        )
        return False

    if dry_run:
        logger.info("[DRY RUN] Would ingest %s from %s", celex_id, md_path)
        return True

    ingestor.ingest_doc(celex_id, md_path)
    return True


def main():
    runner = BatchJobRunner(
        description="Batch processes Markdown to PGVector ingestion."
    )
    runner.parser.add_argument(
        "--celex-id",
        type=str,
        default=None,
        help=(
            "Ingest only this CELEX ID directly. The markdown path is resolved "
            "from the master JSON when present, otherwise from the conventional "
            "<CELEX_DOCS_DIR>/<id>/mineru/auto/<id>.md layout. Does not require "
            "the document to be listed in the master JSON."
        ),
    )

    # We need to load the master sheet to get filenames
    # We parse known args to get the json path
    args, _ = runner.parser.parse_known_args()

    try:
        master_data = loadJsonFor(args.json_fn)
    except Exception as e:
        runner.logger.warning(
            f"Failed to load master JSON from {args.json_fn}: {e}"
        )
        if not args.celex_id:
            runner.logger.critical("No master JSON and no --celex-id; nothing to do.")
            return
        # A direct single-ID ingest can still proceed via the conventional path.
        master_data = {}

    ingestor = _build_ingestor()

    if args.celex_id:
        ok = process_single_celex(
            args.celex_id, (ingestor, master_data), dry_run=args.dry_run
        )
        runner.logger.info(
            "Single-ID ingest for %s: %s", args.celex_id, "OK" if ok else "FAILED"
        )
        return

    # Pass both ingestor and master_data as context
    runner.run(process_single_celex, context=(ingestor, master_data))


if __name__ == "__main__":
    main()

# Ingest the full corpus:
#   python src/batch/Main_ingest_case_markdown2pgvector.py --json-fn ./config_main/master_doc_descriptors.json
# Ingest a single document (bypasses the master-JSON list):
#   python src/batch/Main_ingest_case_markdown2pgvector.py --celex-id 39731_801_8
