"""Download-and-register orchestrator of the HTTP document downloader.

Planning runs on the calling thread through a per-run ``DecisionPlanner``
(key resolution and merge of duplicate rows, final ids, candidate ids,
EN-first selection, URL claims, target paths, decision registration BEFORE
any fetch) and the "already exists" check. The remaining downloads run in a
thread pool; their registration, master JSON upsert and audit happen back on
the calling thread. Every count and audit/registry row goes through the
``OutcomeRecorder``, the single dry-run write guard. The master store is
flushed once, in a ``finally`` so partial progress is kept.

A single bad row never aborts the run: unsafe ids become audited row
failures, registry rejections become failed candidates (plus a review
reason, after which the decision row is re-upserted), and any other
exception is logged with ``logger.exception``, audited when possible and
counted in ``RunSummary.decisions_errored``.

Identity: the planner's ``MasterIdentityIndex`` is built once per run from
the master snapshot plus the registry's PDF URLs of the master keys (alias
URLs, e.g. rows the de-duplication re-pointed to a master key), so a moved
URL keeps mapping to its master entry.

Content safety net (``OutcomeRecorder.content_owner``): a downloaded or
existing file whose sha256 another decision's source or selected document,
or the same decision's source under another id, already has is not
registered; it is audited ``not_selected`` with
``duplicate_content_of_<decision>`` and the decision gets a review reason.
Only a file just downloaded is removed, never one that existed before.

Not-selected candidates never claim a URL; their URL metadata rows are
written after all decisions are planned, and skipped (audit only) for a URL
another decision selected, so they cannot steal it.

Limits: an unexpected error while planning a decision discards that
decision's remaining planned jobs (e.g. the other candidates of an undecided
decision); review reasons are a per-run snapshot, so a re-run in which
everything is ``skipped_exists`` stores none of an earlier run's master
reasons (e.g. a ``url_pdf conflict``).
"""

import logging
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import UUID

from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.download.fetcher import HttpDocumentFetcher
from pipelines.download.local_pdf import (
    existing_file_result,
    is_valid_pdf,
    remove_new_file,
)
from pipelines.download.master_identity import MasterIdentityIndex
from pipelines.download.models import (
    DecisionRequest,
    FetchResult,
    Outcome,
    RowFailure,
)
from pipelines.download.outcome_recorder import (
    REASON_OWNED,
    REASON_REJECTED,
    REASON_UNEXPECTED,
    OutcomeRecorder,
    failed_result,
)
from pipelines.download.planner import DecisionPlanner, FetchJob, ResolvedDecision
from pipelines.download.run_summary import RunSummary
from pipelines.download.selector import CandidateSelector
from pipelines.rag.pgvector.document_registry_repository import (
    DocumentOwnershipConflict,
    DocumentRegistryRepository,
)
from pipelines.rag.pgvector.download_audit_repository import DownloadAuditRepository

__all__ = ["DownloadOrchestrator", "RunSummary"]

logger = logging.getLogger(__name__)

REASON_INVALID_FILE = "file_invalid_after_download"
REASON_FILE_MISSING_OFFLINE = "file_missing_offline"


class DownloadOrchestrator:
    """Maps decisions to downloads, registry rows, master entries and audit."""

    def __init__(
        self,
        selector: CandidateSelector,
        fetcher: HttpDocumentFetcher,
        store: MasterDescriptorStore,
        registry: DocumentRegistryRepository,
        audit: DownloadAuditRepository,
        docs_dir: Path,
    ) -> None:
        """Inject the collaborators (Provisioner pattern).

        Args:
            selector: EN-first candidate selector.
            fetcher: Validated PDF fetcher (thread-safe; used in the pool).
            store: Master descriptor store (written on the calling thread).
            registry: Case / decision / document registry repository.
            audit: Download audit repository.
            docs_dir: Document store root (``settings.CELEX_DOCS_DIR``).
        """
        self._selector = selector
        self._fetcher = fetcher
        self._store = store
        self._registry = registry
        self._audit_repo = audit
        self._docs_dir = Path(docs_dir)

    def run(
        self,
        decisions: list[DecisionRequest],
        *,
        run_id: UUID,
        force: bool,
        dry_run: bool,
        max_workers: int,
        failures: Sequence[RowFailure] = (),
        offline: bool = False,
    ) -> RunSummary:
        """Download and register the decisions' selected documents.

        Args:
            decisions: Mapped decisions (provisional ids; not modified).
            run_id: Id of this run, written to every audit row.
            force: Re-download even valid, registered files.
            dry_run: Map, select and check existence, log the plan, but call
                no writer (no file, master JSON or DB row, audit included).
                Registry / store reads (existence check) still happen.
            max_workers: HTTP thread pool size (at least 1 is used).
            failures: Mapper row failures to audit and count.
            offline: Never fetch. A selected job whose target is a valid PDF
                is ``registered_existing`` (``skipped_exists`` when already
                registered); a missing or invalid file is ``failed`` with
                ``file_missing_offline``. ``force`` is ignored (warning).

        Returns:
            The run summary; ``exit_code`` is 1 when any candidate failed or
            any decision errored (row failures are counted, not fatal).

        Raises:
            OSError: If the final master JSON flush fails and no other
                exception is already propagating (that one is then logged).
        """
        if offline and force:
            logger.warning("--force ignored in offline mode: nothing is fetched")
            force = False
        summary = RunSummary(run_id=run_id, dry_run=dry_run)
        recorder = OutcomeRecorder(self._registry, self._audit_repo, run_id,
                                   dry_run, summary)
        planner = DecisionPlanner(self._selector, self._store, self._docs_dir,
                                  self._identity_index())
        jobs: list[FetchJob] = []
        completed = False
        try:
            for failure in failures:
                recorder.row_failure(None, failure)
            for resolved in planner.resolve(decisions):
                jobs.extend(self._guarded_plan(resolved, planner, recorder, force,
                                                    offline))
            recorder.register_not_selected(planner.pending, planner.claimed)
            if offline:
                if jobs:
                    raise RuntimeError(f"{len(jobs)} jobs left to fetch in offline mode")
            elif dry_run:
                for job in jobs:
                    logger.info("dry run: download %s -> %s",
                                job.candidate.source_url, job.target)
                    summary.outcomes[Outcome.DOWNLOADED] += 1
            else:
                self._fetch_all(jobs, recorder, max_workers)
            completed = True
        finally:
            if not dry_run:
                try:
                    self._store.flush()
                except Exception:
                    logger.exception("master descriptor flush failed")
                    if completed:
                        raise
        logger.info("download run %s summary: %s", run_id, summary.model_dump_json())
        return summary

    def _identity_index(self) -> MasterIdentityIndex:
        """Snapshot the master and its registry alias URLs (one read-only query).

        Returns:
            The identity index over the master entries plus the PDF URLs the
            registry holds under each master key (see ``master_identity``).

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the alias read fails.
        """
        entries = self._store.entries()
        aliases = self._registry.pdf_urls_of(k for k, _ in entries)
        return MasterIdentityIndex(entries, aliases)

    def _guarded_plan(
        self, resolved: ResolvedDecision, planner: DecisionPlanner,
        recorder: OutcomeRecorder, force: bool, offline: bool,
    ) -> list[FetchJob]:
        """Plan one decision; an unexpected exception is logged, audited, counted.

        Args:
            resolved: Resolved (possibly merged) decision.
            planner: The run's planner.
            recorder: The run's outcome recorder.
            force: Re-download existing files.
            offline: Never leave a job for download (see ``run``).

        Returns:
            The jobs that still need a download (always empty offline).
        """
        current = resolved.decision
        try:
            final = planner.assign(resolved, recorder)
            if final is None:
                return []
            current = final
            jobs = [job for job in planner.plan(final, recorder)
                    if not self._use_existing(job, recorder, force, offline)]
            recorder.summary.decisions_processed += 1
            return jobs
        except Exception as exc:
            logger.exception("decision %s (%s row %d) failed", current.decision_id,
                             current.loader_file, current.loader_row)
            recorder.summary.decisions_errored += 1
            failure = RowFailure(loader_file=current.loader_file,
                                 loader_row=current.loader_row,
                                 reason=f"{REASON_UNEXPECTED}: {exc!r}")
            try:
                recorder.record(current.decision_id, failure,
                                failure.loader_file, failure.loader_row)
            except Exception:
                logger.exception("audit of failed decision %s failed",
                                 current.decision_id)
            return []

    def _use_existing(
        self, job: FetchJob, recorder: OutcomeRecorder, force: bool,
        offline: bool = False,
    ) -> bool:
        """Apply the "already exists" rule; False when a download is needed.

        Valid file + registry row (+ master key for a master document) is
        ``skipped_exists``; valid file with missing registration is
        ``registered_existing`` (no HTTP). Invalid files and ``--force``
        fall through to a download, except offline, where a missing or
        invalid file is a ``file_missing_offline`` failure.

        Args:
            job: Planned job.
            recorder: The run's outcome recorder.
            force: Re-download existing files.
            offline: Fail instead of downloading.

        Returns:
            True when the job is handled without a download.

        Raises:
            OSError: If an existing file cannot be hashed.
        """
        valid = is_valid_pdf(job.target)
        if offline and not valid:
            recorder.fail(job.decision, job.selection, failed_result(
                job.candidate, f"{REASON_FILE_MISSING_OFFLINE}: {job.target}"))
            return True
        if force or not valid:
            return False
        doc = job.candidate
        registered = self._registry.is_registered(doc.document_id) and (
            not job.to_master or self._store.has_key(job.decision.decision_id))
        outcome = Outcome.SKIPPED_EXISTS if registered else Outcome.REGISTERED_EXISTING
        if recorder.dry_run:
            logger.info("dry run: %s %s", outcome.value, job.target)
            recorder.summary.outcomes[outcome] += 1
            return True
        result = existing_file_result(doc.document_id, doc.source_url, job.target,
                                      outcome, with_hash=not registered)
        owner = None if registered else recorder.content_owner(
            job.decision.decision_id, doc.document_id, result.sha256)
        if registered:
            recorder.audit(job.decision, result)
        elif owner is not None:  # pre-existing file: audit only, never removed
            recorder.duplicate(job.decision, job.selection, result, owner, False)
        else:
            self._register(job, result, recorder)
        return True

    def _fetch_all(
        self, jobs: list[FetchJob], recorder: OutcomeRecorder, max_workers: int
    ) -> None:
        """Fetch in a thread pool; register each result on the calling thread.

        Args:
            jobs: Jobs that need a download.
            recorder: The run's outcome recorder.
            max_workers: Pool size (at least 1 is used).
        """
        if not jobs:
            return
        existed = {j.target for j in jobs if j.target.exists()}
        pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
        try:
            futures: dict[Future[FetchResult], FetchJob] = {
                pool.submit(self._fetcher.fetch, j.candidate.document_id,
                            j.candidate.source_url, j.target): j
                for j in jobs
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    self._complete(job, future.result(), recorder,
                                   job.target in existed)
                except Exception as exc:
                    logger.exception("candidate %s of %s failed",
                                     job.candidate.document_id, job.decision.decision_id)
                    try:
                        recorder.fail(job.decision, job.selection, failed_result(
                            job.candidate, f"{REASON_UNEXPECTED}: {exc!r}"))
                    except Exception:
                        logger.exception("recording failed candidate %s failed",
                                         job.candidate.document_id)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def _complete(
        self, job: FetchJob, result: FetchResult, recorder: OutcomeRecorder,
        existed: bool,
    ) -> None:
        """Register a fetched file that is valid on disk, else audit the failure.

        A valid file whose content another decision holds is not registered
        (content safety net, see the module docstring).

        Args:
            job: The fetched job.
            result: Fetcher result.
            recorder: The run's outcome recorder.
            existed: The target existed before the run (never removed).

        Raises:
            OSError: If the duplicate file cannot be removed.
        """
        if result.outcome is Outcome.DOWNLOADED and is_valid_pdf(job.target):
            owner = recorder.content_owner(job.decision.decision_id,
                                           job.candidate.document_id, result.sha256)
            if owner is None:
                self._register(job, result, recorder)
                return
            if not existed:
                remove_new_file(job.target, self._docs_dir)
            recorder.duplicate(job.decision, job.selection, result, owner,
                               not existed)
            return
        if result.outcome is Outcome.FAILED:
            recorder.audit(job.decision, result)
        else:
            recorder.fail(job.decision, job.selection,
                          failed_result(job.candidate, REASON_INVALID_FILE))

    def _register(
        self, job: FetchJob, result: FetchResult, recorder: OutcomeRecorder
    ) -> None:
        """Register a valid stored file: document row, master entry, audit.

        Only reached in a real run (a dry run stops before any registration).
        A job's ``is_source`` document becomes the decision's source;
        staged, undecided and other-URL reuses of the stored file do not.

        Args:
            job: The job whose file is valid on disk.
            result: Downloaded or registered_existing result.
            recorder: The run's outcome recorder.
        """
        try:
            self._registry.upsert_document(job.candidate, result, True,
                                           decision_id=job.decision.decision_id,
                                           is_source=job.is_source)
        except ValueError as exc:  # DocumentOwnershipConflict is a ValueError
            token = (REASON_OWNED if isinstance(exc, DocumentOwnershipConflict)
                     else REASON_REJECTED)
            recorder.reject(job.decision, job.selection, result, token, exc)
            return
        if job.to_master:
            reasons = self._store.upsert(job.decision, job.candidate, job.target,
                                         job.selection.language_init)
            recorder.add_reasons(job.decision, job.selection, reasons)
        recorder.remember_content(job.decision.decision_id, job.candidate.document_id,
                                  result.sha256, job.is_source)
        recorder.audit(job.decision, result)
