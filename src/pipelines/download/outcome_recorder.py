"""Outcome recorder of the HTTP document downloader (dry-run write guard).

Counts every candidate result, row failure and review reason in the run's
``RunSummary`` and writes the matching decision rows, URL metadata rows and
audit rows. It is the single home of the dry-run guarantee: in a dry run the
counters are filled but no registry or audit writer is called.
"""

import logging
from uuid import UUID

from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    FetchResult,
    Outcome,
    RowFailure,
)
from pipelines.download.run_summary import RunSummary
from pipelines.download.selector import Selection
from pipelines.rag.pgvector.document_registry_repository import (
    DocumentOwnershipConflict,
    DocumentRegistryRepository,
)
from pipelines.rag.pgvector.download_audit_repository import DownloadAuditRepository

logger = logging.getLogger(__name__)

REASON_OWNED = "document_owned_by_other_decision"
REASON_REJECTED = "registry_rejected"
REASON_UNEXPECTED = "unexpected_error"
REASON_URL_CLAIMED = "url_claimed_by_"
REASON_DUPLICATE_CONTENT = "duplicate_content_of_"


def failed_result(candidate: CandidateDocument, reason: str) -> FetchResult:
    """Build a failed result for a candidate that was not (validly) fetched.

    Args:
        candidate: The candidate.
        reason: Failure reason (``<token>: <detail>``).

    Returns:
        A ``failed`` FetchResult with zero duration.
    """
    return FetchResult(document_id=candidate.document_id,
                       source_url=candidate.source_url, outcome=Outcome.FAILED,
                       failure_reason=reason, duration_ms=0)


def claimed_result(candidate: CandidateDocument, owner: str) -> FetchResult:
    """Build the ``not_selected`` result of a URL another decision selected.

    Args:
        candidate: The candidate whose URL is claimed.
        owner: Decision id that owns the URL in this run.

    Returns:
        A ``not_selected`` FetchResult with reason ``url_claimed_by_<owner>``.
    """
    return FetchResult(document_id=candidate.document_id,
                       source_url=candidate.source_url,
                       outcome=Outcome.NOT_SELECTED,
                       failure_reason=f"{REASON_URL_CLAIMED}{owner}", duration_ms=0)


class OutcomeRecorder:
    """Counts outcomes and writes decision, metadata and audit rows."""

    def __init__(
        self,
        registry: DocumentRegistryRepository,
        audit_repo: DownloadAuditRepository,
        run_id: UUID,
        dry_run: bool,
        summary: RunSummary,
    ) -> None:
        """Inject the writers and the run's counters.

        Args:
            registry: Case / decision / document registry repository.
            audit_repo: Download audit repository.
            run_id: Id of the batch run (written to every audit row).
            dry_run: When True, count only and call no writer.
            summary: Counters being filled.
        """
        self._registry = registry
        self._audit_repo = audit_repo
        self._run_id = run_id
        self.dry_run = dry_run
        self.summary = summary
        self._content: dict[str, list[tuple[str, str, bool]]] = {}

    def register_decision(self, decision: DecisionRequest, selection: Selection) -> None:
        """Upsert the decision row and its case links (not in a dry run).

        Args:
            decision: Decision with its final id and review reasons.
            selection: Its candidate selection.
        """
        if not self.dry_run:
            self._registry.upsert_decision(decision, selection)
            self._registry.upsert_cases_and_links(decision)

    def fail(
        self, decision: DecisionRequest, selection: Selection, failed: FetchResult
    ) -> None:
        """Audit a failed candidate and record it as a decision review reason.

        Args:
            decision: Owning decision (already registered).
            selection: Its selection (for the decision re-upsert).
            failed: The failed result.
        """
        self.audit(decision, failed)
        self.add_reasons(decision, selection, [
            f"document {failed.document_id or failed.source_url} failed: "
            f"{failed.failure_reason}"])

    def reject(
        self, decision: DecisionRequest, selection: Selection,
        result: FetchResult, token: str, exc: Exception,
    ) -> None:
        """Turn a registry rejection into a failed candidate.

        Args:
            decision: Owning decision.
            selection: Its selection.
            result: The result the registry rejected.
            token: Failure reason token.
            exc: The rejection.
        """
        logger.warning("decision %s: %s not registered: %s", decision.decision_id,
                       result.document_id, exc)
        self.fail(decision, selection, result.model_copy(update={
            "outcome": Outcome.FAILED, "failure_reason": f"{token}: {exc}"}))

    def add_reasons(
        self, decision: DecisionRequest, selection: Selection, reasons: list[str]
    ) -> None:
        """Append review reasons and re-upsert the already registered decision.

        Args:
            decision: Decision (modified in place).
            selection: Its selection.
            reasons: Reasons to add; nothing happens when empty.
        """
        if not reasons:
            return
        decision.review_reasons.extend(reasons)
        self.summary.review_reasons += len(reasons)
        if not self.dry_run:
            self._registry.upsert_decision(decision, selection)

    def content_owner(
        self, decision_id: str, document_id: str, sha256: str | None
    ) -> str | None:
        """Find the decision that already holds these bytes (content net).

        A holder is another decision's source or selected document, or the
        same decision's source document under another document id. The
        documents registered earlier in this run are checked first, then
        the registry.

        Args:
            decision_id: Decision the file is for.
            document_id: Document the file is for (never its own holder).
            sha256: File hash; None finds nothing.

        Returns:
            The holding decision id (possibly ``decision_id`` itself), or None.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: If the registry lookup fails.
        """
        if not sha256:
            return None
        for owner, doc, source in self._content.get(sha256, []):
            if doc != document_id and (owner != decision_id or source):
                return owner
        return self._registry.decision_of_content(sha256, decision_id, document_id)

    def remember_content(
        self, decision_id: str, document_id: str, sha256: str | None, is_source: bool
    ) -> None:
        """Record a document registered in this run for ``content_owner``.

        Args:
            decision_id: Owning decision.
            document_id: Registered document.
            sha256: File hash; None records nothing.
            is_source: Whether it became the decision's source.
        """
        if sha256:
            self._content.setdefault(sha256, []).append(
                (decision_id, document_id, is_source))

    def duplicate(
        self, decision: DecisionRequest, selection: Selection,
        result: FetchResult, owner: str, removed: bool,
    ) -> None:
        """Audit a file whose bytes another document already holds.

        The document is not registered.

        Args:
            decision: Owning decision (already registered).
            selection: Its selection (for the decision re-upsert).
            result: The download or existing-file result (its sha256
                identifies the bytes).
            owner: Decision id holding the same content.
            removed: The caller removed the file (its path is not audited).
        """
        logger.warning("decision %s: %s has the content of %s; not registered",
                       decision.decision_id, result.document_id, owner)
        update: dict[str, object] = {
            "outcome": Outcome.NOT_SELECTED,
            "failure_reason": f"{REASON_DUPLICATE_CONTENT}{owner}"}
        if removed:
            update["local_path"] = None
        self.audit(decision, result.model_copy(update=update))
        self.add_reasons(decision, selection, [
            f"document {result.document_id} not registered: same content as "
            f"decision {owner}"])

    def row_failure(self, decision_id: str | None, failure: RowFailure) -> None:
        """Count and audit a row failure.

        Args:
            decision_id: Decision id, None when no id could be built.
            failure: The row failure.
        """
        self.summary.row_failures += 1
        self.record(decision_id, failure, failure.loader_file, failure.loader_row)

    def audit(self, decision: DecisionRequest, result: FetchResult) -> None:
        """Count and audit one candidate result of a decision.

        Args:
            decision: Owning decision.
            result: Candidate result.
        """
        self.summary.count(result)
        self.record(decision.decision_id, result, decision.loader_file,
                    decision.loader_row)

    def record(
        self, decision_id: str | None, result: FetchResult | RowFailure,
        loader_file: str, loader_row: int,
    ) -> None:
        """Append an audit row, except in a dry run.

        Args:
            decision_id: Decision id or None.
            result: Candidate result or row failure.
            loader_file: Loader file name.
            loader_row: Loader row number.
        """
        if not self.dry_run:
            self._audit_repo.record(self._run_id, decision_id, result, loader_file,
                                    loader_row)

    def not_selected(
        self, decision: DecisionRequest, candidate: CandidateDocument
    ) -> None:
        """Register a not-selected candidate's URL metadata and audit it.

        Args:
            decision: Owning decision.
            candidate: Not-selected candidate.
        """
        result = FetchResult(document_id=candidate.document_id,
                             source_url=candidate.source_url,
                             outcome=Outcome.NOT_SELECTED, duration_ms=0)
        if not self.dry_run:
            try:
                self._registry.upsert_document(candidate, None, False,
                                               decision_id=decision.decision_id,
                                               is_source=False)
            except DocumentOwnershipConflict as exc:
                # Metadata-only row of a URL another decision registered: the
                # candidate is not downloaded anyway, so audit, do not fail.
                logger.warning("decision %s: not-selected %s not registered: %s",
                               decision.decision_id, candidate.document_id, exc)
                result = result.model_copy(
                    update={"failure_reason": f"{REASON_OWNED}: {exc}"})
        self.audit(decision, result)

    def register_not_selected(
        self,
        pending: list[tuple[DecisionRequest, CandidateDocument]],
        claimed: dict[str, str],
    ) -> None:
        """Register and audit the planned not-selected candidates.

        Runs after all decisions are planned, so a URL another decision
        selected is known: such a candidate is audited only (no metadata row
        that would own the URL). Errors are logged and counted per item.

        Args:
            pending: (decision, candidate) pairs; cleared afterwards.
            claimed: source_url -> owning decision id of the run.
        """
        for decision, candidate in pending:
            try:
                owner = claimed.get(candidate.source_url)
                if owner is not None and owner != decision.decision_id:
                    self.audit(decision, claimed_result(candidate, owner))
                else:
                    self.not_selected(decision, candidate)
            except Exception:
                logger.exception("not-selected candidate %s of %s failed",
                                 candidate.document_id, decision.decision_id)
                self.summary.decisions_errored += 1
        pending.clear()
