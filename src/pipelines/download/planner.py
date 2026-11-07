"""Decision planner of the HTTP document downloader (calling thread only).

Turns mapped decisions into fetch jobs, in three steps:

1. ``resolve``: the final key of every decision before any write, by the
   injected ``MasterIdentityIndex`` (see ``master_identity``). Rows that
   resolve to the same non-``DEC-`` key are MERGED into the first row's
   decision: one decision, one selection, one master entry.
2. ``assign``: per-run collision suffix (``_2``; only ``DEC-`` ids can still
   collide after the merge); unsafe ids become audited row failures.
3. ``plan``: candidate ids re-derived from the final id, EN-first selection,
   URL claims for SELECTED candidates (first loader row wins), the target
   path with the stored-file language guard, then the decision and case
   links are registered BEFORE any fetch and the bad / claimed / no-PDF
   candidates are audited.
"""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.download.identifiers import (
    DEC_PREFIX,
    UnsafeIdentifier,
    celex_key,
    decision_id_for,
    document_id_for,
    pdf_url_key,
)
from pipelines.download.local_pdf import target_path
from pipelines.download.master_identity import (
    MasterIdentityIndex,
    pdf_urls,
    resolution,
)
from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    FetchResult,
    Outcome,
    RowFailure,
    SelectionStatus,
)
from pipelines.download.outcome_recorder import (
    OutcomeRecorder,
    claimed_result,
    failed_result,
)
from pipelines.download.selector import LANGUAGE_NA, CandidateSelector, Selection

logger = logging.getLogger(__name__)

REASON_UNSAFE_DECISION = "unsafe_decision_id"
REASON_UNSAFE_DOCUMENT = "unsafe_document_id"
REASON_DUPLICATE_DOCUMENT = "duplicate_document_id"
REASON_UNDECIDED_RESOLVED = "undecided resolved by master entry"


class FetchJob(BaseModel):
    """One selected candidate of a decision with its target path."""

    decision: DecisionRequest = Field(description="Owning decision (final id).")
    selection: Selection = Field(description="Selection of the decision.")
    candidate: CandidateDocument = Field(description="Candidate (final id).")
    target: Path = Field(description="Target PDF path.")
    to_master: bool = Field(
        description="True when the file becomes (or is) the decision's master "
                    "entry PDF (master JSON merge).")
    is_source: bool = Field(
        description="True when the document becomes the registry's is_source "
                    "document; False for a candidate reusing the stored master "
                    "file under another URL (the original source row stays).")


class ResolvedDecision(BaseModel):
    """A (possibly merged) decision with its resolved key, before the suffix."""

    decision: DecisionRequest = Field(description="Decision (provisional id).")
    key: str | None = Field(default=None, description="Resolved safe key.")
    reasons: list[str] = Field(
        default_factory=list, description="Review reasons of the resolution.")
    error: str | None = Field(
        default=None, description="Why no safe key exists (row failure).")


class DecisionPlanner:
    """Resolves, merges, names and plans decisions; holds the per-run state."""

    def __init__(
        self, selector: CandidateSelector, store: MasterDescriptorStore,
        docs_dir: Path, identity: MasterIdentityIndex,
    ) -> None:
        """Inject the collaborators; one planner per run.

        Args:
            selector: EN-first candidate selector.
            store: Master descriptor store (read only here).
            docs_dir: Document store root.
            identity: Master identity index, a snapshot of ``store`` taken
                before the run's first write.
        """
        self._selector = selector
        self._store = store
        self._identity = identity
        self._docs_dir = Path(docs_dir)
        self.taken: set[str] = set()
        self.claimed: dict[str, str] = {}
        self.pending: list[tuple[DecisionRequest, CandidateDocument]] = []

    def resolve(self, decisions: list[DecisionRequest]) -> list[ResolvedDecision]:
        """Resolve the keys and merge rows that share a non-``DEC-`` key.

        The first row's fields win; its blank fields are filled from later
        rows; case ids, candidates (by URL) and review reasons are united,
        plus ``merged duplicate row <row> of <file> (<id>)``.

        Args:
            decisions: Mapped decisions in loader order (not modified).

        Returns:
            One entry per final decision, in order of first appearance.
        """
        out: list[ResolvedDecision] = []
        by_key: dict[str, ResolvedDecision] = {}
        for decision in decisions:
            try:
                key, reasons = self._resolve_key(decision)
            except UnsafeIdentifier as exc:
                out.append(ResolvedDecision(decision=decision, error=str(exc)))
                continue
            first = None if key.startswith(DEC_PREFIX) else by_key.get(key)
            if first is None:
                item = ResolvedDecision(decision=decision.model_copy(deep=True),
                                        key=key, reasons=reasons)
                out.append(item)
                if not key.startswith(DEC_PREFIX):
                    by_key[key] = item
                continue
            _merge_into(first, decision, reasons)
        return out

    def _resolve_key(self, decision: DecisionRequest) -> tuple[str, list[str]]:
        """Resolve the safe key of one decision (no collision suffix).

        Args:
            decision: Mapped decision.

        Returns:
            Key and review reasons (see ``master_identity.resolution``).

        Raises:
            UnsafeIdentifier: If the CELEX or the resulting key is unusable.
        """
        own = (celex_key(decision.celex_number) if decision.celex_number
               else decision.decision_id)
        match, conflicts = self._identity.match_detail(
            own, pdf_urls(self._selector.select(decision).selected),
            pdf_urls(decision.candidates), decision.case_ids,
            decision.decision_date)
        key, reasons = resolution(match, conflicts, own, decision.celex_number)
        first_case = decision.case_ids[0] if decision.case_ids else None
        return decision_id_for(None, key, first_case, decision.decision_date,
                               set())[0], reasons

    def assign(
        self, resolved: ResolvedDecision, recorder: OutcomeRecorder
    ) -> DecisionRequest | None:
        """Assign the final decision id; None after an audited row failure.

        Args:
            resolved: Resolved (possibly merged) decision.
            recorder: Outcome recorder (row failure, skip counter).

        Returns:
            A deep copy with the final id and the review reasons, or None.
        """
        decision = resolved.decision
        first_case = decision.case_ids[0] if decision.case_ids else None
        try:
            if resolved.key is None:
                raise UnsafeIdentifier(resolved.error or "no decision key")
            decision_id, reasons = decision_id_for(
                None, resolved.key, first_case, decision.decision_date, self.taken)
        except UnsafeIdentifier as exc:
            logger.warning("decision %s (%s row %d): %s", decision.decision_id,
                           decision.loader_file, decision.loader_row, exc)
            recorder.summary.decisions_skipped += 1
            recorder.row_failure(None, RowFailure(
                loader_file=decision.loader_file, loader_row=decision.loader_row,
                reason=f"{REASON_UNSAFE_DECISION}: {exc}"))
            return None
        self.taken.add(decision_id)
        return decision.model_copy(deep=True, update={
            "decision_id": decision_id,
            "review_reasons": [*decision.review_reasons, *resolved.reasons,
                               *reasons],
        })

    def plan(
        self, decision: DecisionRequest, recorder: OutcomeRecorder
    ) -> list[FetchJob]:
        """Register and audit a decision; return its selected-document jobs.

        Args:
            decision: Decision with its final id (modified in place).
            recorder: Outcome recorder (registration and audit writes).

        Returns:
            One job per selected candidate (existence not yet checked).
        """
        bad = self._rederive(decision)
        selection, claimed = self._claim(
            decision, self._master_resolved(decision, self._selector.select(decision)))
        to_master = selection.status is SelectionStatus.SELECTED
        targets = [self._target(decision, c, to_master) for c in selection.selected]
        recorder.summary.review_reasons += len(decision.review_reasons)
        recorder.register_decision(decision, selection)
        for candidate, reason in bad:
            recorder.fail(decision, selection, failed_result(candidate, reason))
        if selection.status is SelectionStatus.NO_PDF_SOURCE:
            recorder.audit(decision, FetchResult(
                document_id="", source_url="", outcome=Outcome.NO_PDF_SOURCE,
                duration_ms=0))
        for candidate, owner in claimed:
            recorder.audit(decision, claimed_result(candidate, owner))
        self.pending.extend((decision, c) for c in selection.not_selected)
        return [FetchJob(decision=decision, selection=selection, candidate=c,
                         target=target, to_master=master, is_source=source)
                for c, (target, master, source) in zip(selection.selected, targets,
                                               strict=True)]

    def _master_resolved(
        self, decision: DecisionRequest, selection: Selection
    ) -> Selection:
        """Resolve an undecided selection by the master entry's own file.

        A candidate URL equal to the entry's ``url_pdf`` (``pdf_url_key``)
        becomes the only selected one (the master holds exactly that file),
        with a review reason. No language is guessed.

        Args:
            decision: Decision with its final id (review reason appended).
            selection: Its selection.

        Returns:
            The resolved selection, or the input when nothing applies.
        """
        if selection.status is not SelectionStatus.UNDECIDED:
            return selection
        key = decision.decision_id
        stored_url = self._store.url_pdf(key) if self._store.has_key(key) else None
        if stored_url is None:
            return selection
        wanted = pdf_url_key(stored_url)
        match = next((c for c in selection.selected
                      if pdf_url_key(c.source_url) == wanted), None)
        if match is None:
            return selection
        decision.review_reasons.append(REASON_UNDECIDED_RESOLVED)
        return Selection(
            status=SelectionStatus.SELECTED, selected=[match],
            not_selected=[*[c for c in selection.selected if c is not match],
                          *selection.not_selected],
            language_init=self._store.language_init(key) or LANGUAGE_NA)

    def _target(
        self, decision: DecisionRequest, candidate: CandidateDocument,
        to_master: bool,
    ) -> tuple[Path, bool, bool]:
        """Choose the target path of a selected candidate.

        An existing master entry's stored ``local_file_pdf`` is reused only
        when it holds the selected language (candidate language unknown,
        stored ``language_init`` unknown or equal, or the stored ``url_pdf``
        is the selected URL). Otherwise the file is staged under
        ``candidates/`` and the master entry is left untouched; a review
        reason is appended to the decision. A reused stored file whose
        stored ``url_pdf`` is another URL is not made the ``is_source``
        document (the original source row stays; the master merge never
        re-points ``local_file_pdf``).

        Args:
            decision: Decision with its final id.
            candidate: Selected candidate (final id).
            to_master: True for a SELECTED (not undecided) decision.

        Returns:
            The target path, whether the file goes to the master entry and
            whether the document becomes the registry source.
        """
        key = decision.decision_id
        stored = self._store.local_file_pdf(key) if to_master else None
        if stored is None:
            return target_path(self._docs_dir, key, candidate.document_id,
                               selected=to_master), to_master, to_master
        stored_lang = self._store.language_init(key)
        stored_url = self._store.url_pdf(key)
        same_url = (stored_url is not None
                    and pdf_url_key(stored_url) == pdf_url_key(candidate.source_url))
        if (candidate.language is None or stored_lang is None
                or stored_lang.upper() in (candidate.language, LANGUAGE_NA) or same_url):
            return Path(stored), True, stored_url is None or same_url
        decision.review_reasons.append(
            f"master entry {key} holds {stored_lang.lower()} file; selected "
            f"{candidate.language.lower()} staged in candidates")
        return target_path(self._docs_dir, key, candidate.document_id,
                           selected=False), False, False

    def _rederive(
        self, decision: DecisionRequest
    ) -> list[tuple[CandidateDocument, str]]:
        """Rebuild candidate ids from (URL, final decision id, format) in place.

        Args:
            decision: Decision with its final id.

        Returns:
            Candidates whose id cannot be built, or that duplicate an earlier
            candidate's id, with their failure reason (removed from the
            decision).
        """
        kept: list[CandidateDocument] = []
        bad: list[tuple[CandidateDocument, str]] = []
        for candidate in decision.candidates:
            try:
                doc_id = document_id_for(candidate.source_url, decision.decision_id,
                                         candidate.file_format)
            except UnsafeIdentifier as exc:
                bad.append((candidate.model_copy(update={"document_id": ""}),
                            f"{REASON_UNSAFE_DOCUMENT}: {exc}"))
                continue
            rebuilt = candidate.model_copy(update={"document_id": doc_id})
            if any(k.document_id == doc_id for k in kept):
                bad.append((rebuilt, f"{REASON_DUPLICATE_DOCUMENT}: {doc_id}"))
            else:
                kept.append(rebuilt)
        decision.candidates = kept
        return bad

    def _claim(
        self, decision: DecisionRequest, selection: Selection
    ) -> tuple[Selection, list[tuple[CandidateDocument, str]]]:
        """Claim the selected URLs; drop those an earlier decision selected.

        Dropped candidates get a review reason on the decision (before its
        first registry write). When every selected candidate was dropped the
        selection becomes ``no_pdf_source``. A claim stays in place if the
        decision's planning later raises (that run then exits with 1).
        Not-selected candidates never claim.

        Args:
            decision: Decision with its final id.
            selection: Its selection.

        Returns:
            The (possibly reduced) selection and the dropped candidates with
            the owning decision id.
        """
        kept: list[CandidateDocument] = []
        claimed: list[tuple[CandidateDocument, str]] = []
        for candidate in selection.selected:
            owner = self.claimed.setdefault(candidate.source_url, decision.decision_id)
            if owner == decision.decision_id:
                kept.append(candidate)
                continue
            claimed.append((candidate, owner))
            decision.review_reasons.append(
                f"source URL {candidate.source_url} already claimed by {owner}")
        if not claimed:
            return selection, claimed
        if not kept:
            return Selection(status=SelectionStatus.NO_PDF_SOURCE,
                             not_selected=selection.not_selected,
                             language_init=LANGUAGE_NA), claimed
        return selection.model_copy(update={"selected": kept}), claimed


def _merge_into(
    first: ResolvedDecision, later: DecisionRequest, reasons: list[str]
) -> None:
    """Merge a later row resolving to the same key into the first decision.

    Args:
        first: The first row's resolved decision (modified in place).
        later: The later row's decision (not modified).
        reasons: The later row's resolution reasons.
    """
    target = first.decision
    logger.info("decision %s: merged duplicate row %d of %s (%s)", first.key,
                later.loader_row, later.loader_file, later.decision_id)
    target.case_ids = list(dict.fromkeys([*target.case_ids, *later.case_ids]))
    urls = {c.source_url for c in target.candidates}
    target.candidates.extend(c.model_copy() for c in later.candidates
                             if c.source_url not in urls)
    for name in ("celex_number", "decision_date", "title", "parties", "document_class"):
        if not getattr(target, name) and getattr(later, name):
            setattr(target, name, getattr(later, name))
    merged = [*later.review_reasons, *reasons,
              f"merged duplicate row {later.loader_row} of {later.loader_file} "
              f"({later.decision_id})"]
    first.reasons.extend(r for r in merged
                         if r not in target.review_reasons and r not in first.reasons)
