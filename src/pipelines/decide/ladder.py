"""Rule ladder: one undecided manifest decision -> a DecisionPlan (pure).

Order: documents the corpus already holds (by sha256 or decision identity)
are re-pointed to their owner; the remaining operative Commission decisions
are grouped by identity (case numbers and date; same subject, or a different
language for language versions); one group is promoted as the decision's
source, several are split into one decision each; anything else goes to
review.
"""

import datetime as dt
import logging
import re
from collections import Counter

from config.settings import settings
from pipelines.decide.models import (
    CandidateEvidence,
    DecisionPlan,
    DocumentKind,
    ExistingSource,
    Promotion,
    Repoint,
    TitleKey,
    UndecidedDecision,
    Verdict,
)
from pipelines.download.identifiers import decision_id_for

logger = logging.getLogger(__name__)

_ORIGIN_RANK = {"ec.europa.eu": 0, "eur-lex": 1, "db-comp": 2}
_OTHER_ORIGIN_RANK = 3
_ENGLISH = "EN"
_LANGUAGE_NA = "NA"
_SPLIT_CASE_PREFIX = "AT."
_CASE_DIGITS = re.compile(r"(\d{2})\.?(\d{3})")
_VALID_KINDS = frozenset({DocumentKind.DECISION, DocumentKind.COMBINED})
_REASON_NONE_MATCHES = "several decisions, none matches the row"
_REASON_MERGE_OTHER_CASES = "merged documents span other cases only"
_REASON_NO_OPERATIVE = "no operative commission decision"
_REASON_CONFLICT = "same case and date as an existing decision"
_REASON_UNRESOLVED_DECISION = "unresolved decision document beside known documents"
_REASON_BY_KIND = {
    DocumentKind.LETTER: "letters only",
    DocumentKind.SUMMARY: "summaries only",
}
_OUTCOME_KNOWN = "known: {owner}"
_OUTCOME_BETTER = "known, better copy becomes source of {owner}"
_OUTCOME_CONFLICT = "same case and date as existing {ids}"
_OUTCOME_NOT_OPERATIVE = "no operative ending"
_OUTCOME_COMBINED = "combined beside single-case decisions"
_OUTCOME_WINNER = "winner -> {decision}"
_OUTCOME_MEMBER = "version of {winner}"


def case_digits(case_id: str) -> str | None:
    """Return the five case digits of a case id (``IV/33.133-1`` -> ``33133``).

    Args:
        case_id: Case id as linked in ``competition_case_decision``.

    Returns:
        The digits, or None when the id has no five-digit case number.
    """
    m = _CASE_DIGITS.search(case_id)
    return m.group(1) + m.group(2) if m else None


def _same_text(a: frozenset[str], b: frozenset[str]) -> bool:
    """True when two vocabularies overlap by at least ``DECIDE_SAME_TEXT_MIN_OVERLAP`` (Jaccard)."""
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= settings.DECIDE_SAME_TEXT_MIN_OVERLAP


def _same_decision(a: CandidateEvidence, b: CandidateEvidence) -> bool:
    """True when two candidates are versions or mirrors of one Commission decision.

    Same case numbers and date are required; language versions (both
    languages known and different) need nothing more, same-language
    documents also need the same subject and the same vocabulary.
    """
    ka, kb = a.title_key, b.title_key
    if ka is None or kb is None or ka.case_numbers != kb.case_numbers or ka.doc_date != kb.doc_date:
        return False
    if a.language and b.language and a.language != b.language:
        return True
    return ka.subject == kb.subject and _same_text(a.vocabulary, b.vocabulary)


def _preference(e: CandidateEvidence) -> tuple[int, int, int, str]:
    """Sort key: English first, then origin, then longest text, then id."""
    return (0 if e.language == _ENGLISH else 1, _ORIGIN_RANK.get(e.origin, _OTHER_ORIGIN_RANK),
            -e.md_chars, e.document_id)


def _existing_cases(source: ExistingSource) -> set[str]:
    """Case digits of an existing source: its decision's links plus its title's cases."""
    cases = {d for d in (case_digits(c) for c in source.case_ids) if d}
    if source.title_key is not None:
        cases.update(source.title_key.case_numbers)
    return cases


def index_by_case_and_date(existing: list[ExistingSource]) -> dict[tuple[str, dt.date], list[ExistingSource]]:
    """Index existing sources by (case digits, date) from links, title and registry date.

    Args:
        existing: Every source document of the corpus.

    Returns:
        Sources per (case, date); a source appears under each of its cases
        and each of its dates (title date and registry date).
    """
    index: dict[tuple[str, dt.date], list[ExistingSource]] = {}
    for source in existing:
        dates = {d for d in (source.title_key.doc_date if source.title_key else None, source.decision_date) if d}
        for case in _existing_cases(source):
            for date in dates:
                index.setdefault((case, date), []).append(source)
    return index


def _known_owner(
    e: CandidateEvidence, by_sha: dict[str, ExistingSource],
    by_case: dict[tuple[str, dt.date], list[ExistingSource]], decision_id: str,
) -> tuple[ExistingSource | None, list[ExistingSource]]:
    """Find the existing decision a candidate belongs to.

    Identical bytes always match. Otherwise an existing source with the same
    case and date is the same decision when the languages differ (a language
    version), or when the subject (if the source has a title) is equal and
    the vocabularies overlap; any other same case-and-date source is a
    conflict: the candidate is never promoted.

    Returns:
        ``(owner, conflicts)``; owner None when no match, conflicts the
        same case-and-date sources that did not match.
    """
    hit = by_sha.get(e.sha256) if e.sha256 else None
    if hit is not None and hit.decision_id != decision_id:
        return hit, []
    if e.title_key is None:
        return None, []
    hits: dict[str, ExistingSource] = {}
    for case in e.title_key.case_numbers:
        for source in by_case.get((case, e.title_key.doc_date), []):
            if source.decision_id != decision_id:
                hits.setdefault(source.document_id, source)
    same = [h for h in hits.values()
            if (h.language and e.language and h.language != e.language)
            or ((h.title_key is None or h.title_key.subject == e.title_key.subject)
                and _same_text(h.vocabulary, e.vocabulary))]
    if same:
        return min(same, key=lambda h: h.decision_id), []
    return None, sorted(hits.values(), key=lambda h: h.decision_id)


def _is_better_copy(candidate: CandidateEvidence, owner: ExistingSource) -> bool:
    """True when a known candidate should replace the owner's source (user decision 2026-11-01)."""
    if candidate.kind not in _VALID_KINDS or not candidate.operative or candidate.title_key is None:
        return False
    if candidate.language == _ENGLISH and owner.language and owner.language != _ENGLISH:
        return True
    same_language = candidate.language == owner.language or owner.language is None
    return bool(same_language and owner.text_chars
                and candidate.text_chars >= settings.DECIDE_BETTER_COPY_MIN_RATIO * owner.text_chars)


def _known_key(owner: ExistingSource, evidence: CandidateEvidence) -> TitleKey | None:
    """Title key of a known document: the candidate's own, else the existing source's."""
    return evidence.title_key or owner.title_key


def _drop_covered_combined(
    valid: list[CandidateEvidence], known: list[tuple[ExistingSource, CandidateEvidence]],
) -> set[str]:
    """Ids of multi-case decisions whose cases are covered by single-case decisions of that date."""
    singles: set[tuple[str, dt.date]] = set()
    for key in [e.title_key for e in valid] + [_known_key(o, e) for o, e in known]:
        if key is not None and len(key.case_numbers) == 1:
            singles.add((key.case_numbers[0], key.doc_date))
    return {e.document_id for e in valid
            if e.kind is DocumentKind.COMBINED and e.title_key is not None
            and any((c, e.title_key.doc_date) in singles for c in e.title_key.case_numbers)}


def _group(valid: list[CandidateEvidence]) -> list[list[CandidateEvidence]]:
    """Group candidates into Commission decisions, each group sorted by preference."""
    groups: list[list[CandidateEvidence]] = []
    for e in sorted(valid, key=_preference):
        target = next((g for g in groups if any(_same_decision(e, m) for m in g)), None)
        if target is None:
            groups.append([e])
        else:
            target.append(e)
    for g in groups:
        g.sort(key=_preference)
    return groups


def _promotion(group: list[CandidateEvidence], decision_id: str, is_new: bool) -> Promotion:
    """Build the promotion of a group's preferred candidate."""
    winner = group[0]
    assert winner.title_key is not None
    return Promotion(
        decision_id=decision_id, is_new=is_new, winner=winner,
        members=[m.document_id for m in group], decision_date=winner.title_key.doc_date,
        language_init=winner.language.lower() if winner.language else _LANGUAGE_NA)


def plan_decision(
    decision: UndecidedDecision,
    candidates: list[CandidateEvidence],
    existing_by_sha: dict[str, ExistingSource],
    existing_by_case: dict[tuple[str, dt.date], list[ExistingSource]],
    taken_ids: set[str],
) -> DecisionPlan:
    """Decide what happens to one undecided manifest decision.

    Args:
        decision: The manifest decision.
        candidates: Evidence of its candidates.
        existing_by_sha: Existing source documents by PDF sha256.
        existing_by_case: Existing source documents by (case digits, date),
            see :func:`index_by_case_and_date`.
        taken_ids: Decision ids already in use (registry and earlier plans of
            this run); not modified -- the caller adds new promotion ids.

    Returns:
        The plan; ``candidate_outcomes`` explains every candidate.
    """
    outcomes: dict[str, str] = {}
    repoints: list[Repoint] = []
    known: list[tuple[ExistingSource, CandidateEvidence]] = []
    rest: list[CandidateEvidence] = []
    conflicts: list[CandidateEvidence] = []
    for e in candidates:
        owner, clashes = _known_owner(e, existing_by_sha, existing_by_case, decision.decision_id)
        if owner is not None:
            known.append((owner, e))
        elif clashes:
            conflicts.append(e)
            outcomes[e.document_id] = _OUTCOME_CONFLICT.format(ids=", ".join(c.decision_id for c in clashes))
        else:
            rest.append(e)
    # A mirror or language version of a known document is known as well.
    for e in list(rest):
        twin = next(((o, k) for o, k in known if _same_decision(e, k)), None)
        if twin is not None:
            known.append((twin[0], e))
            rest.remove(e)
    better: dict[str, str] = {}
    for owner, e in sorted(known, key=lambda oe: _preference(oe[1])):
        if owner.decision_id not in better and _is_better_copy(e, owner):
            better[owner.decision_id] = e.document_id
    for owner, e in known:
        switch = better.get(owner.decision_id) == e.document_id
        repoints.append(Repoint(document_id=e.document_id, owner_decision_id=owner.decision_id,
                                make_source=switch))
        outcomes[e.document_id] = (_OUTCOME_BETTER if switch else _OUTCOME_KNOWN).format(owner=owner.decision_id)

    valid: list[CandidateEvidence] = []
    for e in rest:
        if e.kind not in _VALID_KINDS:
            outcomes[e.document_id] = e.kind.value
        elif not e.operative or e.title_key is None:
            outcomes[e.document_id] = _OUTCOME_NOT_OPERATIVE
        else:
            valid.append(e)
    covered = _drop_covered_combined(valid, known)
    for doc in covered:
        outcomes[doc] = _OUTCOME_COMBINED
    valid = [e for e in valid if e.document_id not in covered]

    plan = DecisionPlan(decision_id=decision.decision_id, verdict=Verdict.REVIEW,
                        repoints=repoints, candidate_outcomes=outcomes)
    if not valid:
        if conflicts:
            plan.review_reason = _REASON_CONFLICT
            return plan
        return _without_valid(plan, decision, rest, known)

    groups = _group(valid)
    own_cases = {d for d in (case_digits(c) for c in decision.case_ids) if d}
    if len(groups) == 1:
        plan.promotions = [_promotion(groups[0], decision.decision_id, is_new=False)]
        plan.verdict = Verdict.PROMOTE
    else:
        matching = [g for g in groups if g[0].title_key is not None
                    and own_cases & set(g[0].title_key.case_numbers)
                    and g[0].title_key.doc_date == decision.decision_date]
        if not matching:
            plan.review_reason = _REASON_NONE_MATCHES
            return plan
        keeper = min(matching, key=lambda g: (_ORIGIN_RANK.get(g[0].origin, _OTHER_ORIGIN_RANK),
                                              g[0].document_id))
        taken = set(taken_ids)
        plan.promotions = [_promotion(keeper, decision.decision_id, is_new=False)]
        for g in groups:
            if g is keeper:
                continue
            key = g[0].title_key
            assert key is not None
            new_id, _ = decision_id_for(None, None, _SPLIT_CASE_PREFIX + key.case_numbers[0], key.doc_date, taken)
            taken.add(new_id)
            plan.promotions.append(_promotion(g, new_id, is_new=True))
        plan.verdict = Verdict.SPLIT
    for p in plan.promotions:
        plan.candidate_outcomes[p.winner.document_id] = _OUTCOME_WINNER.format(decision=p.decision_id)
        for m in p.members[1:]:
            plan.candidate_outcomes[m] = _OUTCOME_MEMBER.format(winner=p.winner.document_id)
    return plan


def _without_valid(
    plan: DecisionPlan, decision: UndecidedDecision, rest: list[CandidateEvidence],
    known: list[tuple[ExistingSource, CandidateEvidence]],
) -> DecisionPlan:
    """Merge into an existing decision of the row's case, or send to review."""
    if plan.repoints:
        own_cases = {d for d in (case_digits(c) for c in decision.case_ids) if d}
        owners: list[tuple[int, str]] = []
        for owner, e in known:
            linked = {d for d in (case_digits(c) for c in owner.case_ids) if d}
            key = _known_key(owner, e)
            titled = set(key.case_numbers) if key is not None else set()
            cases = linked or titled
            if own_cases & cases:
                owners.append((len(cases), owner.decision_id))
        if any(e.kind in _VALID_KINDS for e in rest):
            plan.review_reason = _REASON_UNRESOLVED_DECISION
            return plan
        if owners:
            plan.verdict = Verdict.MERGE
            plan.merge_into = min(owners)[1]
            for e in rest:
                plan.repoints.append(Repoint(document_id=e.document_id, owner_decision_id=plan.merge_into))
            return plan
        plan.review_reason = _REASON_MERGE_OTHER_CASES
        return plan
    kinds = Counter(e.kind for e in rest)
    if kinds and len(kinds) == 1:
        plan.review_reason = _REASON_BY_KIND.get(next(iter(kinds)), _REASON_NO_OPERATIVE)
    else:
        plan.review_reason = _REASON_NO_OPERATIVE
    return plan
