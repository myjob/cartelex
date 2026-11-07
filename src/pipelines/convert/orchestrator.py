"""Run loop of the MinerU PDF-to-Markdown conversion batch.

Items are decided before any submit (lazy skip, missing target, missing PDF),
the rest is sent in sequential tasks of ``files_per_task`` files. Each file
that completes is finished immediately: result placed, language decided,
registry and master JSON updated, audit row written. One task is in flight at
a time. A file's deadline runs from the first poll that sees it ``running``;
while it is still queued, a task-level queue-wait cap applies instead.
"""

import hashlib
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import BaseModel, Field

from pipelines.convert.language import decide_language, detect_markdown_language
from pipelines.convert.mineru_client import (
    MineruRequestError,
    MineruTaskClient,
    MineruTaskNotFound,
    UploadSpec,
)
from pipelines.convert.models import (
    ConversionAuditRecord,
    ConversionItem,
    ConversionOutcome,
    MineruFile,
)
from pipelines.convert.progress import ProgressTracker, format_duration
from pipelines.convert.result_unpacker import ResultInvalid, place_result
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.download.local_pdf import is_valid_pdf, markdown_path_for
from pipelines.rag.pgvector.conversion_repository import ConversionRepository

logger = logging.getLogger(__name__)

_MS_PER_SECOND = 1000
_STATUS_COMPLETED = "completed"
_STATUS_RUNNING = "running"
_STATUSES_FAILED = frozenset({"failed", "cancelled"})
_REASON_TASK_NOT_FOUND = "task_not_found"
_REASON_RESULT_INVALID = "result_invalid"
_REASON_CONNECTION = "connection_error"
_REASON_PDF_MISSING = "pdf_missing"
_REASON_FILE_MISSING = "file_missing"
_REASON_NO_TARGET = "no_target_path"
_COUNT_SHARES_SOURCE_PDF = "skipped_shares_source_pdf"
_REASON_TIMEOUT = "file_timeout"
_REASON_UNKNOWN_FILE_ERROR = "file_failed"
_REASON_REQUEST_FMT = "http_{status}"
_REASON_UNEXPECTED_FMT = "unexpected_error: {error!r}"
_FAILURE_OUTCOMES = frozenset({
    ConversionOutcome.FAILED,
    ConversionOutcome.TIMEOUT,
    ConversionOutcome.PDF_MISSING,
    ConversionOutcome.NO_TARGET_PATH,
})


def _conversion_target(item: ConversionItem) -> ConversionItem:
    """Point a translated source row at its original, never at the English file.

    A translated source row's ``local_file_md`` is the English translation;
    MinerU output belongs in ``original_file_md`` or, when that is unknown,
    in the default ``<pdf dir>/mineru/<pdf stem>.md``.

    Args:
        item: Queue item as read from the registry.

    Returns:
        The item, with ``local_file_md`` replaced for translated source rows.
    """
    if not (item.is_source and item.translated):
        return item
    target = item.original_file_md or str(markdown_path_for(Path(item.local_file_pdf)))
    return item.model_copy(update={"local_file_md": target})


class ConversionMode(StrEnum):
    """Which queue rows are converted."""

    LAZY = "lazy"
    FORCE = "force"


class ConversionSummary(BaseModel):
    """Result of a run."""

    counts: dict[str, int] = Field(description="Number of items per outcome.")
    failure_reasons: dict[str, int] = Field(description="Number of items per failure reason.")
    aborted: bool = Field(description="True when the run stopped on repeated connection errors.")

    def exit_code(self) -> int:
        """Return the process exit code of the run.

        Returns:
            1 when the run aborted or any item failed, timed out, lacked a
            PDF or a target path; 0 otherwise.
        """
        if self.aborted:
            return 1
        return int(any(self.counts.get(o.value, 0) for o in _FAILURE_OUTCOMES))


@dataclass
class _Job:
    """One submitted file with everything needed to finish it."""

    item: ConversionItem
    size: int
    sha256: str
    task_id: str
    file_id: str
    submitted_at: float
    started: float | None = None


class _Abort(Exception):
    """Too many consecutive connection errors; stop the run."""


class ConversionOrchestrator:
    """Drives queue items through MinerU into Markdown files and the database."""

    def __init__(
        self,
        client: MineruTaskClient,
        repository: ConversionRepository,
        master: MasterDescriptorStore,
        run_id: UUID,
        *,
        api_url: str,
        tier: str,
        files_per_task: int,
        file_timeout_s: float,
        poll_interval_s: float,
        heartbeat_s: float,
        max_connection_errors: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Build the orchestrator.

        Args:
            client: MinerU Tasks API client.
            repository: Registry/audit repository.
            master: Master descriptor store to sync converted entries into.
            run_id: Id written to every audit row of this run.
            api_url: MinerU server URL, recorded in the audit rows.
            tier: Tier requested for every task.
            files_per_task: Maximum files per submitted task.
            file_timeout_s: Per-file budget measured from the first poll that
                sees the file running; files still queued are capped at
                ``files_per_task * file_timeout_s`` from task creation.
            poll_interval_s: Seconds between task polls.
            heartbeat_s: Minimum seconds between heartbeat log lines.
            max_connection_errors: Consecutive transport errors that abort the run.
            clock: Monotonic clock (injectable for tests).
            sleep: Sleep function (injectable for tests).
        """
        self._client = client
        self._repository = repository
        self._master = master
        self._run_id = run_id
        self._api_url = api_url
        self._tier = tier
        self._files_per_task = files_per_task
        self._file_timeout_s = file_timeout_s
        self._poll_interval_s = poll_interval_s
        self._heartbeat_s = heartbeat_s
        self._max_connection_errors = max_connection_errors
        self._clock = clock
        self._sleep = sleep
        self._counts: Counter[str] = Counter()
        self._reasons: Counter[str] = Counter()
        self._connection_errors = 0
        self._progress = ProgressTracker(0, clock)

    def run(
        self, items: list[ConversionItem], mode: ConversionMode, dry_run: bool,
        limit: int | None = None,
    ) -> ConversionSummary:
        """Convert the queue items.

        Args:
            items: Queue rows in processing order.
            mode: Lazy skips rows whose Markdown exists; force converts all.
            dry_run: Only classify and log; no submit, no writes, no audit.
            limit: Maximum number of items to submit, applied after
                classification: skips and items decided before submit
                (``no_target_path``, ``pdf_missing``) do not count. None
                means no cap.

        Returns:
            The run summary. The master store is flushed before returning,
            also when the run aborts or is interrupted (not on a dry run).

        Raises:
            Exception: A master flush failure when the run itself succeeded;
                when another error is already propagating the flush failure
                is only logged.
        """
        started = self._clock()
        self._counts.clear()
        self._reasons.clear()
        self._connection_errors = 0
        aborted = False
        completed = False
        try:
            queued = self._classify(items, mode, dry_run, limit)
            logger.info(
                "Conversion run %s: %d queued (%s), %d items, server %s, tier %s, "
                "%d files per task",
                self._run_id, len(queued), mode.value, len(items), self._api_url,
                self._tier, self._files_per_task,
            )
            if dry_run:
                for item in queued:
                    logger.info("Would convert %s (%s)", item.document_id, item.local_file_pdf)
                return self._summary(aborted)
            try:
                for start in range(0, len(queued), self._files_per_task):
                    self._process_task(queued[start:start + self._files_per_task])
            except _Abort:
                aborted = True
                logger.error("Run aborted after %d consecutive connection errors",
                             self._connection_errors)
            completed = True
        finally:
            if not dry_run:
                try:
                    self._master.flush()
                except Exception:
                    logger.exception("master descriptor flush failed")
                    if completed:
                        raise
        logger.info(
            "Conversion run %s finished in %s: counts %s, failure reasons %s",
            self._run_id, format_duration(self._clock() - started),
            dict(self._counts), dict(self._reasons),
        )
        return self._summary(aborted)

    def _summary(self, aborted: bool) -> ConversionSummary:
        """Build the summary from the counters."""
        return ConversionSummary(
            counts=dict(self._counts), failure_reasons=dict(self._reasons), aborted=aborted)

    def _classify(
        self, items: list[ConversionItem], mode: ConversionMode, dry_run: bool,
        limit: int | None = None,
    ) -> list[ConversionItem]:
        """Decide skips and pre-submit failures; return the items to submit.

        Lazy skips whose master entry is out of sync are reconciled (read-only
        under ``dry_run``). ``limit`` caps only the returned submit list.
        """
        decided: list[tuple[ConversionItem, ConversionOutcome, str]] = []
        queued: list[ConversionItem] = []
        for item in map(_conversion_target, items):
            if not item.is_source and item.local_file_pdf == item.source_pdf:
                # A candidate without a PDF of its own (copied from the source
                # row) would convert the source's language into its target.
                self._counts[_COUNT_SHARES_SOURCE_PDF] += 1
                logger.info("skip %s: shares the source PDF %s",
                            item.document_id, item.local_file_pdf)
            elif item.local_file_md is None:
                decided.append((item, ConversionOutcome.NO_TARGET_PATH, _REASON_NO_TARGET))
            elif mode is ConversionMode.LAZY and _non_empty(Path(item.local_file_md)):
                self._counts[ConversionOutcome.SKIPPED_EXISTS.value] += 1
                self._reconcile_master(item, dry_run)
            elif not is_valid_pdf(Path(item.local_file_pdf)):
                decided.append((item, ConversionOutcome.PDF_MISSING, _REASON_PDF_MISSING))
            else:
                queued.append(item)
        if limit is not None:
            queued = queued[:limit]
        if dry_run:
            for item, outcome, _ in decided:
                logger.info("Would audit %s as %s", item.document_id, outcome.value)
            return queued
        self._progress = ProgressTracker(len(queued) + len(decided), self._clock)
        for item, outcome, reason in decided:
            self._write(item, self._audit(item, outcome, failure_reason=reason), reason)
        return queued

    def _reconcile_master(self, item: ConversionItem, dry_run: bool) -> None:
        """Record an existing Markdown on a master entry that missed the update.

        A run killed after placing the file but before the master flush leaves
        the entry with ``mineru_flag`` explicitly False (a pending downloader entry);
        the next lazy run repairs it here. A missing flag marks a legacy entry
        and is left untouched.
        No MinerU call and no audit row.
        """
        decision_id = item.decision_id
        if (not item.is_source or item.translated
                or self._master.markdown_entry(decision_id)[0] != item.local_file_pdf
                or self._master.mineru_flag(decision_id) is not False):
            return
        if dry_run:
            logger.info("Would reconcile master for %s", decision_id)
            return
        self._master.set_markdown(decision_id, item.local_file_md or "", None)
        logger.info("reconciled master for %s", decision_id)

    def _audit(
        self, item: ConversionItem, outcome: ConversionOutcome, **fields: object
    ) -> ConversionAuditRecord:
        """Build an audit row with the fields every row shares."""
        return ConversionAuditRecord(
            run_id=self._run_id,
            outcome=outcome,
            decision_id=item.decision_id,
            document_id=item.document_id,
            local_file_pdf=item.local_file_pdf,
            local_file_md=item.local_file_md,
            mineru_api_url=self._api_url,
            language_stored=item.language,
            **fields,
        )

    def _write(self, item: ConversionItem, audit: ConversionAuditRecord, detail: str) -> None:
        """Persist an audit row, count it and log the progress line."""
        self._repository.record(audit)
        self._counts[audit.outcome.value] += 1
        if audit.failure_reason:
            self._reasons[audit.failure_reason] += 1
        logger.info(self._progress.finished(item, audit.outcome, detail))

    def _job_audit(
        self, job: _Job, outcome: ConversionOutcome, **fields: object
    ) -> ConversionAuditRecord:
        """Build an audit row for a submitted file."""
        return self._audit(
            job.item, outcome,
            mineru_task_id=job.task_id, mineru_file_id=job.file_id,
            pdf_size_bytes=job.size, pdf_sha256=job.sha256,
            wall_duration_ms=int((self._clock() - job.submitted_at) * _MS_PER_SECOND),
            **fields)

    def _fail_job(
        self, job: _Job, reason: str, message: str | None = None,
        outcome: ConversionOutcome = ConversionOutcome.FAILED,
    ) -> None:
        """Audit a submitted file as failed (or timed out)."""
        self._write(
            job.item,
            self._job_audit(job, outcome, failure_reason=reason, error_message=message), reason)

    def _fail_unsubmitted(self, item: ConversionItem, reason: str, message: str | None) -> None:
        """Audit a file whose task never reached the server."""
        self._write(
            item,
            self._audit(item, ConversionOutcome.FAILED, failure_reason=reason,
                        error_message=message), reason)

    def _connection_failed(self) -> None:
        """Count a transport error; raise _Abort at the threshold."""
        self._connection_errors += 1
        if self._connection_errors >= self._max_connection_errors:
            raise _Abort

    def _process_task(self, batch: list[ConversionItem]) -> None:
        """Submit one task, upload its files and poll until all are terminal."""
        specs: list[UploadSpec] = []
        sized: list[tuple[ConversionItem, int, str]] = []
        for item in batch:
            try:
                data = Path(item.local_file_pdf).read_bytes()
            except OSError as exc:
                self._fail_unsubmitted(item, _REASON_UNEXPECTED_FMT.format(error=exc), None)
                continue
            digest = hashlib.sha256(data).hexdigest()
            specs.append(UploadSpec(Path(item.local_file_pdf).name, data, digest))
            sized.append((item, len(data), digest))
        if not specs:
            return
        submitted_at = self._clock()
        try:
            task = self._client.create_task(specs, self._tier)
            self._connection_errors = 0
        except MineruRequestError as exc:
            self._connection_errors = 0
            reason = exc.error.code if exc.error else _REASON_REQUEST_FMT.format(status=exc.status)
            for item, _, _ in sized:
                self._fail_unsubmitted(item, reason, str(exc))
            return
        except httpx.TransportError as exc:
            for item, _, _ in sized:
                self._fail_unsubmitted(item, _REASON_CONNECTION, repr(exc))
            self._connection_failed()
            return
        for item, _, _ in sized[len(task.files):]:
            self._fail_unsubmitted(item, _REASON_FILE_MISSING, "server returned fewer files")
        pending = [
            (_Job(item, size, digest, task.task_id, mfile.file_id, submitted_at), mfile, spec)
            for (item, size, digest), mfile, spec in zip(sized, task.files, specs, strict=False)]
        jobs: dict[str, _Job] = {}
        for index, (job, mfile, spec) in enumerate(pending):
            if mfile.upload is not None:
                try:
                    self._client.upload(mfile.upload, spec.data)
                    self._connection_errors = 0
                except MineruRequestError as exc:
                    reason = (exc.error.code if exc.error
                              else _REASON_REQUEST_FMT.format(status=exc.status))
                    self._fail_job(job, reason, str(exc))
                    continue
                except httpx.TransportError as exc:
                    self._fail_job(job, _REASON_CONNECTION, repr(exc))
                    try:
                        self._connection_failed()
                    except _Abort:
                        for done in jobs.values():
                            self._fail_job(done, _REASON_CONNECTION, repr(exc))
                        for rest, _, _ in pending[index + 1:]:
                            self._fail_job(rest, _REASON_CONNECTION, repr(exc))
                        self._cancel(task.task_id)
                        raise
                    continue
            jobs[mfile.file_id] = job
        if jobs:
            self._poll(task.task_id, jobs)

    def _poll(self, task_id: str, jobs: dict[str, _Job]) -> None:
        """Poll a task until every job is finished, failed or timed out.

        A job's deadline starts at the first poll that sees it running; jobs
        not yet running are failed with ``timeout`` at the task queue-wait
        cap. The task is cancelled after the last job is terminal when any
        timed out, and best-effort on ``KeyboardInterrupt``.

        Args:
            task_id: Server task id.
            jobs: Unfinished jobs keyed by file id; emptied as they finish.

        Raises:
            KeyboardInterrupt: Re-raised after a best-effort task cancel.
        """
        try:
            self._poll_loop(task_id, jobs)
        except KeyboardInterrupt:
            self._cancel(task_id)
            raise

    def _poll_loop(self, task_id: str, jobs: dict[str, _Job]) -> None:
        """Body of ``_poll``; see there."""
        timed_out = False
        last_beat = self._clock()
        queue_cap = last_beat + self._files_per_task * self._file_timeout_s
        while jobs:
            snapshot: dict[str, MineruFile] | None = None
            try:
                task = self._client.get_task(task_id)
                self._connection_errors = 0
                snapshot = {f.file_id: f for f in task.files}
            except MineruTaskNotFound:
                for job in jobs.values():
                    self._fail_job(job, _REASON_TASK_NOT_FOUND)
                return
            except MineruRequestError as exc:
                logger.warning("Polling %s failed: %s", task_id, exc)
            except httpx.TransportError as exc:
                logger.warning("Polling %s failed: %r", task_id, exc)
                try:
                    self._connection_failed()
                except _Abort:
                    for job in jobs.values():
                        self._fail_job(job, _REASON_CONNECTION, repr(exc))
                    raise
            now = self._clock()
            if snapshot is not None:
                for file_id in list(jobs):
                    mfile = snapshot.get(file_id)
                    if mfile is None:
                        continue
                    if mfile.status == _STATUS_COMPLETED or mfile.status in _STATUSES_FAILED:
                        self._finish(jobs.pop(file_id), mfile)
                    elif mfile.status == _STATUS_RUNNING and jobs[file_id].started is None:
                        jobs[file_id].started = now
            now = self._clock()
            for file_id in list(jobs):
                started = jobs[file_id].started
                deadline = queue_cap if started is None else started + self._file_timeout_s
                if now >= deadline:
                    self._fail_job(jobs.pop(file_id), _REASON_TIMEOUT,
                                   outcome=ConversionOutcome.TIMEOUT)
                    timed_out = True
            if not jobs:
                break
            if now - last_beat >= self._heartbeat_s:
                states = Counter(
                    snapshot[f].status if snapshot and f in snapshot else "unknown" for f in jobs)
                logger.info(self._progress.heartbeat(task_id, states))
                last_beat = now
            self._sleep(self._poll_interval_s)
        if timed_out:
            self._cancel(task_id)

    def _cancel(self, task_id: str) -> None:
        """Cancel a task after its timed-out files; failures are only logged."""
        try:
            self._client.cancel_task(task_id)
        except (MineruRequestError, MineruTaskNotFound, httpx.TransportError) as exc:
            logger.warning("Cancel of %s failed: %r", task_id, exc)

    def _finish(self, job: _Job, mfile: MineruFile) -> None:
        """Finish one terminal file: place result, language, registry, master, audit."""
        try:
            if mfile.status != _STATUS_COMPLETED or mfile.result_url is None:
                code = mfile.error.code if mfile.error else _REASON_UNKNOWN_FILE_ERROR
                self._fail_job(job, code, mfile.error.message if mfile.error else None)
                return
            self._convert(job, mfile)
        except MineruRequestError as exc:
            reason = exc.error.code if exc.error else _REASON_REQUEST_FMT.format(status=exc.status)
            self._fail_job(job, reason, str(exc))
        except ResultInvalid as exc:
            self._fail_job(job, _REASON_RESULT_INVALID, str(exc))
        except Exception as exc:  # noqa: BLE001 - one file must not stop the run
            logger.exception("Unexpected error finishing %s", job.item.document_id)
            self._fail_job(job, _REASON_UNEXPECTED_FMT.format(error=exc), str(exc))

    def _convert(self, job: _Job, mfile: MineruFile) -> None:
        """Download and place a completed file and write registry, master, audit."""
        item = job.item
        target = Path(item.local_file_md or "")
        zip_bytes = self._client.download_result(mfile.result_url or "")
        metrics = place_result(zip_bytes, target)
        detected, confidence = detect_markdown_language(
            target.read_text(encoding="utf-8"), item.document_id)
        decision = decide_language(detected, confidence, item.language)
        if decision.registry_value is not None:
            self._repository.set_language(item.document_id, decision.registry_value)
        if (item.is_source and not item.translated
                and self._master.markdown_entry(item.decision_id)[0] == item.local_file_pdf):
            self._master.set_markdown(
                item.decision_id, item.local_file_md or "",
                decision.detected.lower() if decision.detected else None)
            self._master.flush()
        parse = mfile.parse
        tier = parse.tier if parse else self._tier
        audit = self._job_audit(
            job, ConversionOutcome.CONVERTED,
            tier=tier,
            parser_version=parse.parser_version if parse else None,
            server_duration_ms=parse.duration_ms if parse else None,
            pdf_pages=metrics.pdf_pages, md_chars=metrics.md_chars,
            image_count=metrics.image_count, table_count=metrics.table_count,
            formula_count=metrics.formula_count,
            language_detected=decision.detected, language_confidence=decision.confidence,
            language_action=decision.action)
        pages = metrics.pdf_pages if metrics.pdf_pages is not None else "?"
        wall = format_duration(self._clock() - job.submitted_at, precise=True)
        self._write(item, audit, f"{tier}, {pages} pages, {wall}")


def _non_empty(path: Path) -> bool:
    """Return True when ``path`` is an existing non-empty file."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False
