"""Decide the source document of every undecided manifest decision.

After MinerU, each ``undecided`` decision without a source row is resolved
from its candidates' Markdown (``pipelines.decide``): documents the corpus
already holds (same sha256, or same title and vocabulary) are re-pointed to
their existing decision; the remaining operative Commission decisions are
grouped into decisions; one group is promoted as the decision's source,
several are split into one decision each; letters, summaries and anything
ambiguous are flagged for review. A promotion also creates the decision's
master JSON entry. Each decision is applied in one transaction with the
master flush inside it; a failure leaves that decision untouched and the
run continues.

Dry run by default (log + CSV report under ``settings.REPORT_PATH``);
``--apply`` writes. ``--ids ID`` (repeatable) restricts the run.

Exit codes: 0 ok, 1 any decision failed to apply, 2 usage error.

Design: docs/Design Source Decision.md

.venv/bin/python -m batch.Main_decide_source_document
.venv/bin/python -m batch.Main_decide_source_document --apply
"""

import argparse
import csv
import datetime as dt
import logging
import sys
import time
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from config.settings import settings
from pipelines.convert.language import detect_markdown_language
from pipelines.decide.evidence import extract_evidence
from pipelines.decide.ladder import index_by_case_and_date, plan_decision
from pipelines.decide.models import (
    CandidateEvidence,
    DecisionPlan,
    ExistingSource,
    UndecidedDecision,
    Verdict,
)
from pipelines.download.descriptor_store import MasterDescriptorStore, TranslationEntry
from pipelines.download.models import CandidateDocument, DecisionRequest
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.source_decision_repository import (
    CandidateRow,
    SourceDecisionRepository,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
_LOADER = "Main_decide_source_document"
_REPORT_NAME = "decide_source_document_{stamp}.csv"
_REPORT_COLUMNS = (
    "decision_id", "registry_date", "document_id", "origin", "sha256", "language", "kind", "case_numbers", "doc_date",
    "subject", "operative", "md_chars", "outcome", "verdict", "target",
)


class DecisionContext(BaseModel):
    """What the run knows about one manifest decision besides its plan."""

    decision: UndecidedDecision = Field(description="The manifest decision.")
    rows: dict[str, CandidateRow] = Field(description="Candidate rows by document id.")
    evidence: dict[str, CandidateEvidence] = Field(description="Candidate evidence by document id.")


Planned = tuple[DecisionPlan, DecisionContext]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        Namespace with ``apply`` and ``ids``.

    Raises:
        SystemExit: On a usage error.
    """
    p = argparse.ArgumentParser(description="Decide the source document of undecided decisions.")
    p.add_argument("--apply", action="store_true", help="Write; default is a dry run with a report.")
    p.add_argument("--ids", action="append", metavar="ID", help="Decision id to process (repeatable).")
    return p.parse_args(argv)


def _read(path: str | None) -> str | None:
    """Return a file's text, or None when the path is missing or not a file."""
    if not path:
        return None
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _existing_index(
    repository: SourceDecisionRepository,
) -> tuple[dict[str, ExistingSource], dict[tuple[str, dt.date], list[ExistingSource]]]:
    """Index the corpus's source documents by sha256 and by (case, date).

    A translated source is read from its original Markdown, so vocabulary and
    length describe the original text, not the translation.
    """
    sources: list[ExistingSource] = []
    for row in repository.existing_sources():
        path = row.original_file_md if _read(row.original_file_md) is not None else row.local_file_md
        text = _read(path)
        ev = extract_evidence(
            text, document_id=row.document_id, decision_id=row.decision_id, origin="", sha256=row.sha256,
            local_file_pdf="", local_file_md=path or "", language=None) if text else None
        sources.append(ExistingSource(
            decision_id=row.decision_id, document_id=row.document_id, sha256=row.sha256,
            title_key=ev.title_key if ev else None, case_ids=row.case_ids, decision_date=row.decision_date,
            language=(row.language or "").upper() or None, text_chars=ev.text_chars if ev else None,
            vocabulary=ev.vocabulary if ev else frozenset()))
    by_sha = {x.sha256: x for x in sources if x.sha256}
    return by_sha, index_by_case_and_date(sources)


def _evidence(rows: list[CandidateRow]) -> dict[str, CandidateEvidence]:
    """Build evidence for the candidates that have a Markdown file."""
    out: dict[str, CandidateEvidence] = {}
    for row in rows:
        text = _read(row.local_file_md)
        if text is None:
            logger.warning("%s: no MinerU markdown at %s", row.document_id, row.local_file_md)
            continue
        detected, _ = detect_markdown_language(text, row.document_id)
        out[row.document_id] = extract_evidence(
            text, document_id=row.document_id, decision_id=row.decision_id, origin=row.origin,
            sha256=row.sha256, local_file_pdf=row.local_file_pdf, local_file_md=row.local_file_md or "",
            language=detected.upper() if detected else None)
    return out


def run(
    repository: SourceDecisionRepository, master: MasterDescriptorStore, *, apply: bool,
    ids: list[str] | None,
) -> list[Planned]:
    """Plan every undecided decision and, with ``apply``, write the plans.

    Args:
        repository: Registry access.
        master: Master descriptor store.
        apply: Write the plans; otherwise only plan.
        ids: Restrict to these decisions; None for all.

    Returns:
        The plans with their context, in decision id order.
    """
    loaded = repository.load_undecided(ids)
    if not loaded:
        logger.info("no undecided decision without a source")
        return []
    by_sha, by_case = _existing_index(repository)
    taken = repository.taken_decision_ids()
    planned: list[Planned] = []
    for decision, rows in loaded:
        evidence = _evidence(rows)
        plan = plan_decision(decision, list(evidence.values()), by_sha, by_case, taken)
        taken.update(p.decision_id for p in plan.promotions if p.is_new)
        for p in plan.promotions:
            if not p.is_new and decision.decision_date != p.decision_date:
                logger.warning("%s: decision date %s -> %s (from the document %s)", decision.decision_id,
                               decision.decision_date, p.decision_date, p.winner.document_id)
        planned.append((plan, DecisionContext(decision=decision, rows={r.document_id: r for r in rows},
                                              evidence=evidence)))
    counts = Counter(p.verdict.value for p, _ in planned)
    reasons = Counter(p.review_reason for p, _ in planned if p.review_reason)
    logger.info("planned %d decisions: %s; review reasons %s%s", len(planned), dict(counts),
                dict(reasons), "" if apply else " [dry run]")
    if apply:
        apply_plans(repository, master, planned)
    return planned


def _master_hook(master: MasterDescriptorStore, plan: DecisionPlan, context: DecisionContext,
                 added: list[str], switched: list[tuple[str, str | None, TranslationEntry | None]]) -> None:
    """Create promotion entries, switch better-copy owners and flush (inside the DB transaction).

    ``added`` and ``switched`` record what to undo if the transaction fails.
    """
    for p in plan.promotions:
        row = context.rows[p.winner.document_id]
        key = p.winner.title_key
        title = context.decision.title if not p.is_new or key is None else f"{', '.join(key.case_numbers)} - {key.subject}"
        if not master.has_key(p.decision_id):
            added.append(p.decision_id)
        master.upsert(
            DecisionRequest(decision_id=p.decision_id, case_ids=context.decision.case_ids,
                            decision_date=p.decision_date, title=title, loader_file=_LOADER, loader_row=0),
            CandidateDocument(document_id=row.document_id, source_url=row.source_url,
                              file_format=row.file_format, language=p.winner.language, origin=row.origin),
            Path(row.local_file_pdf), p.language_init)
        master.set_markdown(p.decision_id, p.winner.local_file_md, p.language_init)
    for rp in plan.repoints:
        if not rp.make_source or not master.has_key(rp.owner_decision_id):
            continue
        row = context.rows[rp.document_id]
        ev = context.evidence.get(rp.document_id)
        language = ev.language.lower() if ev and ev.language else "NA"
        switched.append((rp.owner_decision_id, master.markdown_entry(rp.owner_decision_id)[0],
                         master.translation_entry(rp.owner_decision_id)))
        master.switch_source(rp.owner_decision_id, local_file_pdf=row.local_file_pdf,
                             txt_md_file=row.local_file_md or "", language_init=language)
    master.flush()


def apply_plans(repository: SourceDecisionRepository, master: MasterDescriptorStore,
                planned: list[Planned]) -> int:
    """Apply plans one decision at a time; a failure undoes only that decision.

    Args:
        repository: Registry access.
        master: Master descriptor store.
        planned: Plans with their context.

    Returns:
        The number of decisions that failed to apply.
    """
    failures = 0
    for plan, context in planned:
        added: list[str] = []
        switched: list[tuple[str, str | None, TranslationEntry | None]] = []
        try:
            repository.apply(plan, context.rows,
                             before_commit=lambda: _master_hook(master, plan, context, added, switched))
        except Exception:  # noqa: BLE001 - one decision must not stop the run
            failures += 1
            logger.exception("%s: apply failed, rolled back", plan.decision_id)
            for key in added:
                master.remove(key)
            for key, old_pdf, old_entry in switched:
                if old_entry is not None:
                    master.switch_source(key, local_file_pdf=old_pdf or "", txt_md_file=old_entry.txt_md_file,
                                         language_init=old_entry.language_init)
                    master.set_translation(key, old_entry)
            try:
                master.flush()
            except Exception:  # noqa: BLE001 - registry is rolled back; report only
                logger.exception("%s: master entries could not be removed on disk", plan.decision_id)
    logger.info("applied %d decisions, %d failed", len(planned) - failures, failures)
    return failures


def _verdict_text(plan: DecisionPlan) -> str:
    """Render a plan's verdict for the report."""
    return f"review: {plan.review_reason}" if plan.verdict is Verdict.REVIEW else plan.verdict.value


def _target(plan: DecisionPlan, document_id: str) -> str:
    """Return where a candidate ends up: promotion decision, owner or empty."""
    for p in plan.promotions:
        if document_id in p.members:
            return p.decision_id
    for r in plan.repoints:
        if r.document_id == document_id:
            return r.owner_decision_id
    return ""


def write_report(planned: list[Planned], directory: Path, stamp: str) -> Path:
    """Write one CSV row per candidate.

    Args:
        planned: Plans with their context.
        directory: Report directory (created when missing).
        stamp: Timestamp for the file name.

    Returns:
        The report path.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _REPORT_NAME.format(stamp=stamp)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_REPORT_COLUMNS)
        w.writeheader()
        for plan, context in planned:
            for doc, row in context.rows.items():
                ev = context.evidence.get(doc)
                key = ev.title_key if ev else None
                w.writerow({
                    "decision_id": plan.decision_id, "document_id": doc, "origin": row.origin,
                    "registry_date": context.decision.decision_date.isoformat()
                    if context.decision.decision_date else "",
                    "sha256": row.sha256 or "", "language": ev.language if ev else "",
                    "kind": ev.kind.value if ev else "no markdown",
                    "case_numbers": " ".join(key.case_numbers) if key else "",
                    "doc_date": key.doc_date.isoformat() if key else "", "subject": key.subject if key else "",
                    "operative": ev.operative if ev else "", "md_chars": ev.md_chars if ev else "",
                    "outcome": plan.candidate_outcomes.get(doc, ""), "verdict": _verdict_text(plan),
                    "target": _target(plan, doc)})
    return path


def main(argv: list[str] | None = None) -> int:
    """Run the step.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).

    Returns:
        0 ok, 1 any decision failed to apply, 2 usage error.
    """
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    engine = make_engine()
    try:
        repository = SourceDecisionRepository(engine)
        master = MasterDescriptorStore(Path(settings.MASTER_DOC_DESCRIPTORS_PATH))
        planned = run(repository, master, apply=False, ids=args.ids)
        if not planned:
            return EXIT_OK
        report = write_report(planned, Path(settings.REPORT_PATH), time.strftime("%Y%m%d-%H%M%S"))
        logger.info("report %s", report)
        if not args.apply:
            return EXIT_OK
        return EXIT_FAILED if apply_plans(repository, master, planned) else EXIT_OK
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
