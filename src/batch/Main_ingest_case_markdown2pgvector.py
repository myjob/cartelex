import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine

from batch.utils.Job_Utils import loadJsonFor
from batch.BatchJobRunner import BatchJobRunner
from pipelines.download.local_pdf import conventional_markdown_path
from pipelines.rag.pgvector.source_document_resolver import SourceDocumentResolver
from pipelines.rag.pgvector.embeddings.celex_chunks2pgvector import (
    CelexMarkdownIngestor,
)
from pipelines.rag.pgvector.session import engine
from config.settings import settings


MODE_LAZY = "lazy"
MODE_FORCE = "force"
_SELECT_INGESTED_PATHS = (
    "SELECT celex_id, array_agg(DISTINCT path) FROM {schema}.{table} "
    "WHERE celex_id IS NOT NULL GROUP BY celex_id"
)


class IngestState(BaseModel):
    """What is already in the embedding table (lazy mode) or nothing (force mode)."""

    ingested_paths: dict[str, set[str]] | None = Field(
        description="Markdown paths per celex_id that already have chunks; None in force mode.")

    @classmethod
    def force(cls) -> "IngestState":
        """Return the state that re-ingests every decision.

        Returns:
            A state without an ingested set.
        """
        return cls(ingested_paths=None)


def load_ingested_paths(db_engine: Engine) -> dict[str, set[str]]:
    """Read the Markdown paths per celex_id that already have chunks.

    Args:
        db_engine: SQLAlchemy engine.

    Returns:
        ``{celex_id: {path, ...}}``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors.
    """
    sql = text(_SELECT_INGESTED_PATHS.format(schema=settings.DB_SCHEMA, table=settings.EMBEDDING_TABLE))
    with db_engine.connect() as conn:
        return {r[0]: {p for p in r[1] if p} for r in conn.execute(sql).fetchall()}


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

    return str(conventional_markdown_path(Path(settings.CELEX_DOCS_DIR), celex_id))


def process_single_celex(
    celex_id: str,
    context: tuple[CelexMarkdownIngestor, dict, SourceDocumentResolver, IngestState],
    dry_run: bool = False,
) -> bool:
    """Ingest one decision's markdown, stamping its source document.

    In lazy mode a decision that already has chunks is skipped: the ingest
    only adds chunks whose hash is new, so re-chunking an ingested decision
    with a changed splitter would leave both chunkings in the table. A
    skipped decision whose master markdown is another file than the one its
    chunks came from is reported (it needs a deliberate replace).

    Args:
        celex_id: The decision id (``celex_id`` == ``decision_id``).
        context: ``(ingestor, master_data, source_documents, state)``; the
            resolver caches one lookup per decision, the state holds the
            already ingested decisions (lazy) or nothing (force).
        dry_run: Log what would be ingested without writing.

    Returns:
        True if ingested, skipped as already ingested (or would be in a dry
        run), False if the markdown file is missing.
    """
    ingestor, master_data, source_documents, state = context
    logger = logging.getLogger(__name__)

    md_path = _resolve_md_path(celex_id, master_data)
    if state.ingested_paths is not None and celex_id in state.ingested_paths:
        stored = state.ingested_paths[celex_id]
        if stored and Path(md_path).name not in {Path(p).name for p in stored}:
            logger.warning(
                "Skip %s: already ingested, but markdown changed since ingest (chunks from %s, master %s); "
                "not re-chunked", celex_id, ", ".join(sorted(stored)), md_path)
        else:
            logger.info("Skip %s: already ingested (lazy mode)", celex_id)
        return True
    if not os.path.exists(md_path):
        logger.warning(
            "Skip load for %s: markdown not found at %s", celex_id, md_path
        )
        return False

    if dry_run:
        logger.info("[DRY RUN] Would ingest %s from %s", celex_id, md_path)
        return True

    ingestor.ingest_doc(
        celex_id, md_path, document_id=source_documents.resolve(celex_id)
    )
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

    runner.parser.add_argument(
        "--mode",
        choices=[MODE_LAZY, MODE_FORCE],
        default=MODE_LAZY,
        help=(
            "lazy (default) skips decisions that already have chunks; force re-ingests them "
            "(new chunks are added beside existing ones when the chunking changed)."
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
    source_documents = SourceDocumentResolver.from_engine(engine)
    if args.mode == MODE_LAZY:
        state = IngestState(ingested_paths=load_ingested_paths(engine))
        runner.logger.info("Lazy mode: %d decisions already ingested are skipped", len(state.ingested_paths))
    else:
        state = IngestState.force()
    context = (ingestor, master_data, source_documents, state)

    if args.celex_id:
        ok = process_single_celex(args.celex_id, context, dry_run=args.dry_run)
        runner.logger.info(
            "Single-ID ingest for %s: %s", args.celex_id, "OK" if ok else "FAILED"
        )
        return

    runner.run(process_single_celex, context=context)


if __name__ == "__main__":
    main()

# Ingest the decisions that have no chunks yet (lazy, default):
#   .venv/bin/python -m batch.Main_ingest_case_markdown2pgvector --json-fn ./config_main/master_doc_descriptors.json
# Re-ingest everything (adds new chunks beside existing ones if the chunking changed):
#   .venv/bin/python -m batch.Main_ingest_case_markdown2pgvector --mode force
# Ingest a single document (bypasses the master-JSON list):
#   python src/batch/Main_ingest_case_markdown2pgvector.py --celex-id 39731_801_8
