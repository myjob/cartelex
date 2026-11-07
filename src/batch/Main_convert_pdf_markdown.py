"""Convert registry PDFs to Markdown through a MinerU server.

Reads the conversion queue (source and selected registry rows that have a PDF)
and sends the PDFs to the MinerU Tasks API. Target paths are read from
``document_registry.local_file_md``, never built here. Each converted file is
placed on disk, its language is decided, the registry and the master descriptor
JSON are updated and one audit row is written to
``document_conversion_audit``. Logging is configured by ``config.settings`` on
import.

Modes:
  lazy   (default) skip rows whose Markdown file already exists; they are
         counted as ``skipped_exists`` and get no audit row.
  force  convert every queued row again and overwrite the Markdown.

Options: ``--ids ID`` (repeatable) restricts to decisions, ``--limit N`` caps
the number of files submitted (skips and rows decided before submit do not
count), ``--dry-run`` classifies and logs without submitting or writing.
The server and tier are verified (preflight) before the queue is read, also
for ``--dry-run``.

Exit codes: 0 all ok or nothing to do, 1 any file failed or timed out or the
run aborted, 2 usage, preflight (server unreachable, tier not offered) or
migration error.

Run book: docs/claude/plans/2026-10-31-mineru-pdf-markdown-conversion-design.md
(section 13).

start first `mineru-api --tier advanced` in `~/a/mineru/MinerU`   

.venv/bin/python -m batch.Main_convert_pdf_markdown --dry-run`
| :---------------------------------------------------------------------------------------------------------------------- |
| `.venv/bin/python -m batch.Main_convert_pdf_markdown --limit 3`                                                         |
.venv/bin/python -m batch.Main_convert_pdf_markdown

"""

import argparse
import logging
import signal
import sys
import uuid
from pathlib import Path
from types import FrameType

from config.settings import settings
from pipelines.convert.mineru_client import MineruUnavailable, make_mineru_client
from pipelines.convert.orchestrator import ConversionMode, ConversionOrchestrator
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.rag.pgvector.conversion_repository import (
    ConversionNotReady,
    ConversionRepository,
)
from pipelines.rag.pgvector.session import make_engine

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        The parsed namespace.

    Raises:
        SystemExit: On a usage error (argparse exits with code 2).
    """
    p = argparse.ArgumentParser(description="Convert registry PDFs to Markdown with MinerU.")
    p.add_argument("--mode", choices=[m.value for m in ConversionMode],
                   default=ConversionMode.LAZY.value,
                   help="lazy skips existing Markdown; force converts all.")
    p.add_argument("--ids", action="append", metavar="ID",
                   help="Decision id to convert (repeatable).")
    p.add_argument("--limit", type=int, default=None,
                   help="Maximum number of files to submit (after lazy skips).")
    p.add_argument("--dry-run", action="store_true",
                   help="Classify and log; submit and write nothing.")
    return p.parse_args(argv)


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    """SIGTERM handler: raise KeyboardInterrupt so ``finally`` blocks run.

    Args:
        signum: Signal number (unused).
        frame: Interrupted stack frame (unused).

    Raises:
        KeyboardInterrupt: Always.
    """
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    """Run the conversion batch.

    SIGTERM is turned into ``KeyboardInterrupt`` for the duration of the run
    (the previous handler is restored on exit) so the master store is flushed.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).

    Returns:
        0 on success or empty queue, 1 when files failed or the run aborted,
        2 on a usage, preflight or migration error.
    """
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        return _run(args)
    finally:
        signal.signal(signal.SIGTERM, previous)


def _run(args: argparse.Namespace) -> int:
    """Execute one run for parsed arguments.

    Args:
        args: Parsed command line.

    Returns:
        The process exit code.
    """
    mode = ConversionMode(args.mode)
    run_id = uuid.uuid4()
    client = make_mineru_client()
    engine = make_engine()
    try:
        try:
            client.preflight(settings.MINERU_TIER)
            repository = ConversionRepository(engine)
            repository.probe()
        except MineruUnavailable as exc:
            logger.error("MinerU preflight failed: %s", exc)
            return EXIT_USAGE
        except ConversionNotReady as exc:
            logger.error("Conversion schema not ready: %s", exc)
            return EXIT_USAGE
        items = repository.queue(args.ids, None)
        logger.info(
            "Run %s: server %s, tier %s, mode %s, %d queue rows%s",
            run_id, settings.MINERU_API_URL, settings.MINERU_TIER, mode.value,
            len(items), " [dry run]" if args.dry_run else "")
        if not items:
            logger.info("Nothing to convert")
            return EXIT_OK
        master = MasterDescriptorStore(Path(settings.MASTER_DOC_DESCRIPTORS_PATH))
        orchestrator = ConversionOrchestrator(
            client, repository, master, run_id,
            api_url=settings.MINERU_API_URL,
            tier=settings.MINERU_TIER,
            files_per_task=settings.MINERU_FILES_PER_TASK,
            file_timeout_s=settings.MINERU_FILE_TIMEOUT_SECONDS,
            poll_interval_s=settings.MINERU_POLL_INTERVAL_SECONDS,
            heartbeat_s=settings.MINERU_PROGRESS_HEARTBEAT_SECONDS,
            max_connection_errors=settings.MINERU_MAX_CONSECUTIVE_CONNECTION_ERRORS,
        )
        summary = orchestrator.run(items, mode, args.dry_run, limit=args.limit)
        logger.info("Run %s summary: counts %s, failure reasons %s, aborted %s",
                    run_id, summary.counts, summary.failure_reasons, summary.aborted)
        return summary.exit_code()
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
