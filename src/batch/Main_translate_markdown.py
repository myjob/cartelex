"""Translate foreign-language decision Markdown into English.

Reads the translation queue (non-English source rows of ``document_registry``
with a Markdown), splits each Markdown with ``TokenTargetMarkdownSplitter``
(sentence-aware fallback), translates every chunk with the batch LLM profile
and writes ``<input dir>/<input stem>_EN.md``. A decision is committed only
when all chunks passed: the source row's ``local_file_md`` is switched to the
English file and the original is kept in ``original_file_md``; the master
entry gets the same switch. A failed decision is rolled back and skipped; the
run continues. One audit row per item goes to ``document_translation_audit``.
Logging is configured by ``config.settings`` on import.

Modes:
  lazy   (default) skip decisions with ``language_translated = 'en'``.
  force  re-translate every non-English source row from its original.

Options: ``--ids ID`` (repeatable) restricts to decisions, ``--limit N`` caps
the number of items, ``--dry-run`` lists what would be translated.

Exit codes: 0 all ok or nothing to do, 1 any item failed or the run aborted
on repeated LLM connection errors, 2 usage or migration error.

Design: docs/Design Translator.md

.venv/bin/python -m batch.Main_translate_markdown --dry-run
.venv/bin/python -m batch.Main_translate_markdown --mode force --ids CELEX-31964D0233
"""

import argparse
import logging
import signal
import sys
import uuid
from pathlib import Path
from types import FrameType

from config.llm_factory import batch_profile, create_lm
from config.settings import settings
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.translation_repository import (
    TranslationNotReady,
    TranslationRepository,
)
from pipelines.translate.orchestrator import TranslationMode, TranslationOrchestrator
from pipelines.translate.translator import MarkdownTranslator
from pipelines.utils.TokenSizeTargetMarkdownSplitter import TokenTargetMarkdownSplitter

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
_SPLIT_HEADERS = [("#", "Header 1"), ("##", "Header 2")]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        The parsed namespace.

    Raises:
        SystemExit: On a usage error (argparse exits with code 2).
    """
    p = argparse.ArgumentParser(description="Translate non-English decision Markdown to English.")
    p.add_argument("--mode", choices=[m.value for m in TranslationMode],
                   default=TranslationMode.LAZY.value,
                   help="lazy skips translated decisions; force re-translates from the original.")
    p.add_argument("--ids", action="append", metavar="ID",
                   help="Decision id to translate (repeatable).")
    p.add_argument("--limit", type=int, default=None, help="Maximum number of items.")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be translated; no LLM call, no writes.")
    return p.parse_args(argv)


def _raise_interrupt(signum: int, frame: FrameType | None) -> None:
    """SIGTERM handler: raise KeyboardInterrupt so the current item is rolled back.

    Args:
        signum: Signal number (unused).
        frame: Interrupted stack frame (unused).

    Raises:
        KeyboardInterrupt: Always.
    """
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    """Run the translation batch.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).

    Returns:
        0 on success or empty queue, 1 when items failed or the run aborted,
        2 on a usage or migration error.
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
    mode = TranslationMode(args.mode)
    run_id = uuid.uuid4()
    engine = make_engine()
    try:
        repository = TranslationRepository(engine)
        try:
            repository.probe()
        except TranslationNotReady as exc:
            logger.error("Translation schema not ready: %s", exc)
            return EXIT_USAGE
        items = repository.queue(args.ids, force=mode is TranslationMode.FORCE)
        if not items:
            logger.info("Run %s: nothing to translate (%s)", run_id, mode.value)
            return EXIT_OK
        profile = batch_profile()
        translator = MarkdownTranslator(
            create_lm(profile),
            retries=settings.TRANSLATE_CHUNK_RETRIES,
            ratio_min=settings.TRANSLATE_LENGTH_RATIO_MIN,
            ratio_max=settings.TRANSLATE_LENGTH_RATIO_MAX,
            language_min_chars=settings.TRANSLATE_CHUNK_LANGUAGE_MIN_CHARS,
        )
        splitter = TokenTargetMarkdownSplitter(
            headers_to_split_on=_SPLIT_HEADERS,
            chunk_size=settings.TRANSLATE_CHUNK_TOKENS,
            separators=settings.TRANSLATE_SPLIT_SEPARATORS,
        )
        orchestrator = TranslationOrchestrator(
            translator, splitter, repository,
            MasterDescriptorStore(Path(settings.MASTER_DOC_DESCRIPTORS_PATH)), run_id,
            model_id=profile.qualified_model_name,
            heartbeat_s=settings.TRANSLATE_HEARTBEAT_SECONDS,
            max_connection_errors=settings.TRANSLATE_MAX_CONSECUTIVE_CONNECTION_ERRORS,
        )
        summary = orchestrator.run(items, mode, args.dry_run, limit=args.limit)
        logger.info("Run %s summary: counts %s, failure reasons %s, aborted %s",
                    run_id, summary.counts, summary.failure_reasons, summary.aborted)
        return summary.exit_code()
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
