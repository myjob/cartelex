"""Run loop of the Markdown translation batch.

Each queue item is one decision's source row. The item is translated chunk
by chunk into ``<input stem>_EN.md.tmp``; only when every chunk passed and
the whole text is detected as English is the file swapped into place and the
registry (source row switched, original kept) and master entry updated in one
step: the master flush runs inside the registry transaction. Any failure
undoes that decision alone (temp file removed, previous target restored,
master entry restored) and the run continues with the next item. Only
repeated LLM connection errors abort the run.
"""

import logging
import os
import time
from collections import Counter
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from config.settings import settings
from pipelines.convert.language import detect_markdown_language
from pipelines.convert.progress import ProgressTracker, format_duration
from pipelines.download.descriptor_store import MasterDescriptorStore, TranslationEntry
from pipelines.translate.models import (
    ChunkRejected,
    TranslationAuditRecord,
    TranslationConnectionError,
    TranslationItem,
    TranslationOutcome,
)

logger = logging.getLogger(__name__)

_ENGLISH = "EN"
_UNSET_LANGUAGES = frozenset({"", "NA"})
_TRANSLATED_MARKER = "en"
_MD_SUFFIX = ".md"
_TMP_SUFFIX = ".tmp"
_BAK_SUFFIX = ".bak"
_CHUNK_JOIN = "\n\n"
_MS_PER_SECOND = 1000
_REASON_INPUT_MISSING = "input_missing"
_REASON_UNSUPPORTED = "unsupported_language"
_REASON_UNDETERMINED = "language_undetermined"
_REASON_NOT_ENGLISH = "not_english"
_REASON_CHUNK_REJECTED = "chunk_rejected"
_REASON_CONNECTION = "connection_error"
_REASON_COMMIT = "commit_failed"
_REASON_UNEXPECTED_FMT = "unexpected_error: {error!r}"
_OK_OUTCOMES: frozenset[StrEnum] = frozenset(
    {TranslationOutcome.TRANSLATED, TranslationOutcome.ALREADY_ENGLISH, TranslationOutcome.SKIPPED})


class TranslationMode(StrEnum):
    """Which queue rows are translated."""

    LAZY = "lazy"
    FORCE = "force"


class TranslationSummary(BaseModel):
    """Result of a run."""

    counts: dict[str, int] = Field(description="Number of items per outcome.")
    failure_reasons: dict[str, int] = Field(description="Number of failed items per reason.")
    aborted: bool = Field(description="True when the run stopped on repeated connection errors.")

    def exit_code(self) -> int:
        """Return the process exit code of the run.

        Returns:
            1 when the run aborted or any item failed; 0 otherwise.
        """
        return int(self.aborted or self.counts.get(TranslationOutcome.FAILED.value, 0) > 0)


class _ChunkTranslator(Protocol):
    """Callable translating one chunk (``MarkdownTranslator`` or a fake)."""

    def __call__(self, markdown_chunk: str, source_language: str) -> str: ...


class _Splitter(Protocol):
    """Splitter returning objects with ``page_content``."""

    def split_text(self, text: str) -> list: ...


class _Repository(Protocol):
    """The parts of ``TranslationRepository`` the run loop uses."""

    def set_language(self, document_id: str, language: str) -> None: ...

    def commit_translation(
        self, item: TranslationItem, target_md: str, original_md: str,
        before_commit: Callable[[], None]) -> None: ...

    def record(self, audit: TranslationAuditRecord) -> None: ...


class _ItemFailed(Exception):
    """A decision failed; ``reason`` goes to the audit."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ItemSkipped(_ItemFailed):
    """Nothing to translate and nothing to fix by hand; audited as skipped."""


class _Abort(Exception):
    """Too many consecutive connection errors; stop the run."""


def target_path_for(source_md: Path) -> Path:
    """Return the English output path of a source Markdown.

    Args:
        source_md: Foreign-language Markdown.

    Returns:
        ``<source dir>/<source stem>_EN.md``.
    """
    return source_md.with_name(f"{source_md.stem}{settings.TRANSLATE_TARGET_SUFFIX}{_MD_SUFFIX}")


def _with_suffix(path: Path, suffix: str) -> Path:
    """Append a suffix to the full file name (``x.md`` -> ``x.md.tmp``)."""
    return path.with_name(path.name + suffix)


class TranslationOrchestrator:
    """Drives queue items through the LLM into English Markdown files."""

    def __init__(
        self,
        translator: _ChunkTranslator,
        splitter: _Splitter,
        repository: _Repository,
        master: MasterDescriptorStore,
        run_id: UUID,
        *,
        model_id: str,
        heartbeat_s: float,
        max_connection_errors: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build the orchestrator.

        Args:
            translator: Chunk translator.
            splitter: Markdown splitter.
            repository: Registry/audit repository.
            master: Master descriptor store.
            run_id: Id written to every audit row of this run.
            model_id: LLM model id, recorded in the audit rows.
            heartbeat_s: Minimum seconds between in-document progress lines.
            max_connection_errors: Consecutive connection errors that abort the run.
            clock: Monotonic clock (injectable for tests).
        """
        self._translator = translator
        self._splitter = splitter
        self._repository = repository
        self._master = master
        self._run_id = run_id
        self._model_id = model_id
        self._heartbeat_s = heartbeat_s
        self._max_connection_errors = max_connection_errors
        self._clock = clock
        self._counts: Counter[str] = Counter()
        self._reasons: Counter[str] = Counter()
        self._connection_errors = 0
        self._progress = ProgressTracker(0, clock)

    def run(
        self, items: list[TranslationItem], mode: TranslationMode, dry_run: bool,
        limit: int | None = None,
    ) -> TranslationSummary:
        """Translate the queue items.

        Args:
            items: Queue rows in processing order (already filtered by mode).
            mode: Logged only; the queue query applies lazy/force.
            dry_run: Only log what would be translated; no LLM call, no writes.
            limit: Maximum number of items to process; None for all.

        Returns:
            The run summary.

        Raises:
            KeyboardInterrupt: Re-raised after the current item was rolled back.
        """
        started = self._clock()
        self._counts.clear()
        self._reasons.clear()
        self._connection_errors = 0
        selected = items if limit is None else items[:limit]
        logger.info("Translation run %s: %d items (%s), model %s%s",
                    self._run_id, len(selected), mode.value, self._model_id,
                    " [dry run]" if dry_run else "")
        if dry_run:
            for item in selected:
                source = Path(item.original_file_md or item.local_file_md)
                logger.info("Would translate %s (%s): %s -> %s", item.document_id,
                            item.language or "language unknown", source, target_path_for(source))
            return self._summary(aborted=False)
        self._progress = ProgressTracker(
            len(selected), self._clock, ok_outcomes=_OK_OUTCOMES, timeout_outcomes=frozenset())
        aborted = False
        try:
            for item in selected:
                self._process(item)
        except _Abort:
            aborted = True
            logger.error("Run aborted after %d consecutive connection errors",
                         self._connection_errors)
        logger.info("Translation run %s finished in %s: counts %s, failure reasons %s",
                    self._run_id, format_duration(self._clock() - started),
                    dict(self._counts), dict(self._reasons))
        return self._summary(aborted)

    def _summary(self, aborted: bool) -> TranslationSummary:
        """Build the summary from the counters."""
        return TranslationSummary(
            counts=dict(self._counts), failure_reasons=dict(self._reasons), aborted=aborted)

    def _process(self, item: TranslationItem) -> None:
        """Translate one item and audit it; never raises except to abort or interrupt."""
        started = self._clock()
        source = Path(item.original_file_md or item.local_file_md)
        audit = TranslationAuditRecord(
            run_id=self._run_id, decision_id=item.decision_id, document_id=item.document_id,
            source_file_md=str(source), source_language=item.language,
            model_id=self._model_id, outcome=TranslationOutcome.FAILED)
        try:
            outcome = self._translate(item, source, audit)
            self._connection_errors = 0
        except _ItemSkipped as exc:
            outcome = TranslationOutcome.SKIPPED
            audit.reason = exc.reason
        except _ItemFailed as exc:
            outcome = TranslationOutcome.FAILED
            audit.reason = exc.reason
            if exc.reason == _REASON_CONNECTION:
                self._connection_errors += 1
            else:
                self._connection_errors = 0
        audit.outcome = outcome
        audit.duration_ms = int((self._clock() - started) * _MS_PER_SECOND)
        self._repository.record(audit)
        self._counts[outcome.value] += 1
        if audit.reason and outcome is TranslationOutcome.FAILED:
            self._reasons[audit.reason] += 1
        logger.info(self._progress.finished(
            item, outcome, audit.reason or f"{audit.chunk_count or 0} chunks"))
        if self._connection_errors >= self._max_connection_errors:
            raise _Abort

    def _translate(
        self, item: TranslationItem, source: Path, audit: TranslationAuditRecord,
    ) -> TranslationOutcome:
        """Run the per-item pipeline; filesystem effects are undone on failure.

        Raises:
            _ItemFailed: With the audit reason.
            KeyboardInterrupt: After cleanup.
        """
        if not source.is_file():
            raise _ItemFailed(_REASON_INPUT_MISSING)
        text = source.read_text(encoding="utf-8")
        if item.original_file_md is None and self._is_english(text, item.document_id):
            # Old-pipeline rows point at an English file under a foreign
            # registry language; translating it would record English as the
            # original for good.
            audit.source_language = _ENGLISH
            if (item.language or "").upper() in _UNSET_LANGUAGES:
                self._repository.set_language(item.document_id, _ENGLISH)
            return TranslationOutcome.ALREADY_ENGLISH
        language = self._language(item, text)
        audit.source_language = language
        if language == _ENGLISH:
            return TranslationOutcome.ALREADY_ENGLISH
        language_name = settings.TRANSLATE_LANGUAGE_NAMES.get(language)
        if language_name is None:
            raise _ItemFailed(_REASON_UNSUPPORTED)
        target = target_path_for(source)
        tmp = _with_suffix(target, _TMP_SUFFIX)
        try:
            self._write_translation(item, text, language_name, tmp, audit)
            self._commit(item, source, target, tmp, language)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        audit.target_file_md = str(target)
        return TranslationOutcome.TRANSLATED

    @staticmethod
    def _is_english(text: str, document_id: str) -> bool:
        """Return True when the text is detected as English."""
        detected, _ = detect_markdown_language(text, document_id)
        return (detected or "").upper() == _ENGLISH

    def _language(self, item: TranslationItem, text: str) -> str:
        """Return the upper-case source language, detecting and storing an unset one."""
        stored = (item.language or "").upper()
        if stored not in _UNSET_LANGUAGES:
            return stored
        detected, _ = detect_markdown_language(text, item.document_id)
        if detected is None:
            raise _ItemSkipped(_REASON_UNDETERMINED)
        language = detected.upper()
        self._repository.set_language(item.document_id, language)
        return language

    def _write_translation(
        self, item: TranslationItem, text: str, language_name: str, tmp: Path,
        audit: TranslationAuditRecord,
    ) -> None:
        """Translate chunk by chunk into ``tmp`` and check the whole result."""
        chunks = [d.page_content for d in self._splitter.split_text(text)]
        audit.chunk_count = len(chunks)
        last_beat = self._clock()
        with tmp.open("w", encoding="utf-8") as out:
            for index, chunk in enumerate(chunks):
                try:
                    translated = self._translator(markdown_chunk=chunk, source_language=language_name)
                except ChunkRejected as exc:
                    logger.warning("%s chunk %d/%d rejected: %s",
                                   item.document_id, index + 1, len(chunks), exc)
                    raise _ItemFailed(_REASON_CHUNK_REJECTED) from exc
                except TranslationConnectionError as exc:
                    logger.warning("%s: LLM unreachable: %s", item.document_id, exc)
                    raise _ItemFailed(_REASON_CONNECTION) from exc
                except Exception as exc:  # noqa: BLE001 - one decision must not stop the run
                    logger.exception("%s chunk %d failed", item.document_id, index + 1)
                    raise _ItemFailed(_REASON_UNEXPECTED_FMT.format(error=exc)) from exc
                out.write((_CHUNK_JOIN if index else "") + translated.strip("\n"))
                if self._clock() - last_beat >= self._heartbeat_s:
                    last_beat = self._clock()
                    logger.info("%s: chunk %d/%d translated", item.document_id,
                                index + 1, len(chunks))
            out.write("\n")
        detected, _ = detect_markdown_language(tmp.read_text(encoding="utf-8"), item.document_id)
        if (detected or "").upper() != _ENGLISH:
            raise _ItemFailed(_REASON_NOT_ENGLISH)

    def _commit(
        self, item: TranslationItem, source: Path, target: Path, tmp: Path, language: str,
    ) -> None:
        """Swap the file in and switch registry and master together, or undo all."""
        backup = _with_suffix(target, _BAK_SUFFIX)
        if target.exists():
            os.replace(target, backup)
        os.replace(tmp, target)
        snapshot = self._master.translation_entry(item.decision_id)
        try:
            self._repository.commit_translation(
                item, str(target), str(source),
                before_commit=lambda: self._switch_master(item, source, target, language))
        except BaseException as exc:
            self._undo(item, target, backup, snapshot)
            if isinstance(exc, Exception):
                logger.exception("%s: commit failed, rolled back", item.document_id)
                raise _ItemFailed(_REASON_COMMIT) from exc
            raise
        backup.unlink(missing_ok=True)

    def _switch_master(self, item: TranslationItem, source: Path, target: Path, language: str) -> None:
        """Point the master entry at the English file and flush (inside the DB transaction)."""
        if not self._master.has_key(item.decision_id):
            logger.warning("%s: no master entry, registry only", item.decision_id)
            return
        self._master.set_translation(item.decision_id, TranslationEntry(
            txt_md_file=str(target), original_file_md=str(source),
            language_init=language.lower(), language_translated=_TRANSLATED_MARKER))
        self._master.flush()

    def _undo(
        self, item: TranslationItem, target: Path, backup: Path,
        snapshot: TranslationEntry | None,
    ) -> None:
        """Restore the previous target file and master entry after a failed commit."""
        if backup.exists():
            os.replace(backup, target)
        else:
            target.unlink(missing_ok=True)
        if snapshot is None:
            return
        try:
            self._master.set_translation(item.decision_id, snapshot)
            self._master.flush()
        except Exception:  # noqa: BLE001 - report, the registry is already rolled back
            logger.exception("%s: master entry could not be restored on disk; "
                             "in-memory entry restored", item.decision_id)
