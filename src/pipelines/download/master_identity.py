"""Identity of a mapped decision against the master descriptor entries.

A mapped decision is the same as master entry K when the first of these
rules holds (design: ``docs/claude/plans/2026-10-30-master-identity-dedupe.md``):

1. ``celex``: the decision's own key (its CELEX key, or for a loader without
   CELEX its provisional id) is a master key;
2. ``url``, in two tiers, URLs compared with
   :func:`~pipelines.download.identifiers.pdf_url_key` against K's
   ``webDocDescriptor.url_pdf`` or one of K's registry alias URLs (the PDF
   URLs ``document_registry`` holds under K, including the rows the
   de-duplication re-pointed to K): (2a) the SELECTED candidates' URLs; only
   when they find no key, (2b) any PDF candidate's URL. When a tier finds
   several keys, only those sharing a case number with the decision are
   kept; exactly one left is the match (rule ``url+case``). Date guard: a
   URL tier first drops every key whose master date is known and differs
   from a known decision date (blank dates never exclude); a tier left
   without keys finds nothing;
3. ``case+date``: a case number of the decision and its decision date equal
   one of K's case numbers (from ``case_ids`` and ``webDocDescriptor.case``)
   and K's ``webDocDescriptor.decision_date`` (compared as ``YYYY-MM-DD``).
   K is excluded when both sides carry a CELEX number and they differ, or
   both carry a sub-file suffix (``IV/34179-1``) for that case number and
   they differ.

The first rule that finds any key wins. When it finds more than one key the
match is ambiguous: no key is reused and the keys are reported. URL keys
dropped by the date guard are reported by :meth:`MasterIdentityIndex.
match_detail` as ``DateConflict`` items.

Rule 3 is a hint, not proof (master decision dates can belong to a later
amending decision of the case): :func:`resolution` never reuses a
case+date key; it adds ``possible duplicate of master entry <K>
(case+date)`` and leaves true duplicates to the content safety net.

Case numbers, sub-file suffixes and master dates: see
:mod:`pipelines.download.identifiers`.
"""

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

from pipelines.download.identifiers import (
    case_number,
    case_suffix,
    celex_of,
    master_date,
    pdf_url_key,
)
from pipelines.download.models import CandidateDocument
from pipelines.download.selector import PDF_FORMAT

logger = logging.getLogger(__name__)

IdentityRule = Literal["celex", "url", "url+case", "case+date"]

_WEB_KEY = "webDocDescriptor"
REASON_AMBIGUOUS_MASTER = "ambiguous master match: "


class DateConflict(BaseModel):
    """A master key a URL tier found but dropped because the dates differ."""

    key: str = Field(description="Master key owning the URL.")
    master_date: str = Field(description="K's decision date (YYYY-MM-DD).")
    decision_date: str = Field(description="The decision's date (YYYY-MM-DD).")


class IdentityMatch(BaseModel):
    """Result of matching a decision against the master entries."""

    key: str | None = Field(
        description="Matched master key; None when the winning rule is ambiguous.")
    rule: IdentityRule = Field(description="Rule that matched (celex, url, url+case, case+date).")
    ambiguous: list[str] = Field(
        default_factory=list,
        description="Sorted master keys the winning rule found when more than one.")


def pdf_urls(candidates: Iterable[CandidateDocument]) -> list[str]:
    """Return the URLs of the PDF candidates.

    Args:
        candidates: Candidates.

    Returns:
        Their source URLs, PDF format only.
    """
    return [c.source_url for c in candidates if c.file_format == PDF_FORMAT]


def resolution(
    match: IdentityMatch | None, conflicts: list[DateConflict], own_key: str,
    celex_number: str | None,
) -> tuple[str, list[str]]:
    """Turn a match into the decision's key and review reasons.

    Args:
        match: The identity match, or None.
        conflicts: URL keys dropped by the date guard.
        own_key: The decision's own key.
        celex_number: The decision's CELEX number, or None.

    Returns:
        The key and the reasons: ``url matches master entry <K> but date
        differs (<d_master> vs <d_decision>)`` per conflict; then, for a
        case+date match, the own key with ``possible duplicate of master
        entry <K> (case+date)`` per key (never reused); for an ambiguous
        match the own key with ``ambiguous master match: <keys>``; for
        another key K (celex, url, url+case) K with ``matched master entry
        <K> by <rule>`` and, for a CELEX decision, ``CELEX <x> differs from
        master key <K>``; else the own key.
    """
    reasons = [f"url matches master entry {c.key} but date differs "
               f"({c.master_date} vs {c.decision_date})" for c in conflicts]
    if match is None or match.key == own_key:
        return own_key, reasons
    if match.rule == "case+date":
        keys = match.ambiguous or [match.key or ""]
        return own_key, [*reasons, *(f"possible duplicate of master entry {k} "
                                     "(case+date)" for k in keys)]
    if match.ambiguous:
        reason = f"{REASON_AMBIGUOUS_MASTER}{', '.join(match.ambiguous)}"
        logger.warning("decision %s: %s by %s", own_key, reason, match.rule)
        return own_key, [*reasons, reason]
    reasons.append(f"matched master entry {match.key} by {match.rule}")
    if celex_number:
        reasons.append(f"CELEX {celex_number} differs from master key {match.key}")
    return match.key, reasons


class MasterIdentityIndex:
    """Lookup of master keys by own key, PDF URL and (case number, date)."""

    def __init__(
        self, entries: Iterable[tuple[str, dict[str, Any]]],
        aliases: Iterable[tuple[str, str]] = (),
    ) -> None:
        """Index a snapshot of the master entries (built once per run).

        Args:
            entries: (master key, entry) pairs; a key may repeat (its data
                is united).
            aliases: (master key, PDF URL) pairs read from the registry; a
                URL is indexed exactly like the key's ``url_pdf`` (same date
                guard and tie-break). Pairs of keys not in ``entries`` are
                ignored. Empty: the master alone.
        """
        self._keys: set[str] = set()
        self._by_url: dict[str, set[str]] = {}
        self._by_case_date: dict[tuple[str, str], set[str]] = {}
        self._cases_of: dict[str, set[str]] = {}
        self._suffixes_of: dict[tuple[str, str], set[str | None]] = {}
        self._date_of: dict[str, str] = {}
        self._celex_of: dict[str, str] = {}
        for key, entry in entries:
            self._add(key, entry)
        for key, url in aliases:
            if key in self._keys and isinstance(url, str) and url.strip():
                self._by_url.setdefault(pdf_url_key(url), set()).add(key)
        logger.info("master identity index: %d keys, %d urls, %d case+date pairs",
                    len(self._keys), len(self._by_url), len(self._by_case_date))

    def _add(self, key: str, entry: dict[str, Any]) -> None:
        """Index one master entry.

        Args:
            key: Master key.
            entry: Master entry (not modified).
        """
        self._keys.add(key)
        web = entry.get(_WEB_KEY)
        web = web if isinstance(web, dict) else {}
        url = web.get("url_pdf")
        if isinstance(url, str) and url.strip():
            self._by_url.setdefault(pdf_url_key(url), set()).add(key)
        celex = celex_of(key) or celex_of(web.get("case"))
        if celex is not None:
            self._celex_of[key] = celex
        raw_cases = [r for r in [*(entry.get("case_ids") or []), web.get("case")]
                     if isinstance(r, str)]
        numbers = {n for raw in raw_cases for n in [case_number(raw)] if n is not None}
        self._cases_of.setdefault(key, set()).update(numbers)
        for raw in raw_cases:
            number = case_number(raw)
            if number is not None:
                parts = case_suffix(raw)
                self._suffixes_of.setdefault((key, number), set()).add(
                    parts[1] if parts else None)
        date = master_date(web.get("decision_date"))
        if date is None:
            return
        self._date_of[key] = date
        for number in numbers:
            self._by_case_date.setdefault((number, date), set()).add(key)

    def _keys_of_urls(self, urls: Iterable[str]) -> set[str]:
        """Return the master keys owning any of the URLs.

        Args:
            urls: PDF URLs.

        Returns:
            The keys whose ``url_pdf`` or alias URL equals a URL under
            ``pdf_url_key``.
        """
        keys: set[str] = set()
        for url in urls:
            keys |= self._by_url.get(pdf_url_key(url), set())
        return keys

    def has_key(self, key: str) -> bool:
        """Report whether the snapshot holds a master key.

        Args:
            key: Decision id.

        Returns:
            True if indexed.
        """
        return key in self._keys

    def match(
        self, own_key: str | None, selected_urls: Iterable[str],
        candidate_urls: Iterable[str], case_ids: Iterable[str],
        decision_date: dt.date | None,
    ) -> IdentityMatch | None:
        """Match a decision to a master key (see :meth:`match_detail`).

        Args:
            own_key: The decision's own key, or None.
            selected_urls: The selected PDF candidates' URLs (tier 2a).
            candidate_urls: All PDF candidate URLs (tier 2b).
            case_ids: The decision's case ids.
            decision_date: The decision date, or None.

        Returns:
            The match, or None.
        """
        return self.match_detail(own_key, selected_urls, candidate_urls,
                                 case_ids, decision_date)[0]

    def match_detail(
        self, own_key: str | None, selected_urls: Iterable[str],
        candidate_urls: Iterable[str], case_ids: Iterable[str],
        decision_date: dt.date | None,
    ) -> tuple[IdentityMatch | None, list[DateConflict]]:
        """Match a decision to a master key (celex, then url, then case+date).

        Args:
            own_key: The decision's own key (CELEX key, or the provisional id
                of a loader without CELEX), or None. A ``CELEX-`` own key is
                also the decision's CELEX number for the case+date guard.
            selected_urls: The selected PDF candidates' URLs (tier 2a).
            candidate_urls: All PDF candidate URLs (tier 2b).
            case_ids: The decision's case ids.
            decision_date: The decision date, or None.

        Returns:
            The match of the first rule that finds a key (``key`` None and
            ``ambiguous`` filled when it finds several and, for a URL tier,
            the case-number tie-break leaves not exactly one) or None, and
            the URL keys the date guard dropped (sorted by key).
        """
        if own_key is not None and own_key in self._keys:
            return IdentityMatch(key=own_key, rule="celex"), []
        case_ids = list(case_ids)
        numbers = {n for raw in case_ids for n in [case_number(raw)] if n is not None}
        date = decision_date.isoformat() if decision_date else None
        conflicts: dict[str, DateConflict] = {}
        for urls in (selected_urls, candidate_urls):
            found = self._keys_of_urls(urls)
            by_url = {k for k in found if not self._date_differs(k, date)}
            for k in found - by_url:
                conflicts[k] = DateConflict(key=k, master_date=self._date_of[k],
                                            decision_date=date or "")
            if len(by_url) > 1:
                same_case = {k for k in by_url if self._cases_of.get(k, set()) & numbers}
                if len(same_case) == 1:
                    return (IdentityMatch(key=same_case.pop(), rule="url+case"),
                            _sorted(conflicts))
            if by_url:
                return _result(by_url, "url"), _sorted(conflicts)
        by_case = self._case_date_keys(own_key, case_ids, date)
        return (_result(by_case, "case+date") if by_case else None), _sorted(conflicts)

    def _date_differs(self, key: str, date: str | None) -> bool:
        """Report whether K's known date differs from a known decision date.

        Args:
            key: Master key.
            date: Decision date (ISO) or None.

        Returns:
            True only when both dates are known and differ.
        """
        stored = self._date_of.get(key)
        return date is not None and stored is not None and stored != date

    def _case_date_keys(
        self, own_key: str | None, case_ids: list[str], date: str | None
    ) -> set[str]:
        """Return the keys of rule 3, without the CELEX / sub-file mismatches.

        Args:
            own_key: The decision's own key (its CELEX when ``CELEX-``).
            case_ids: The decision's case ids.
            date: Decision date (ISO) or None.

        Returns:
            The matching keys (empty without a date).
        """
        if date is None:
            return set()
        celex = celex_of(own_key) if own_key else None
        keys: set[str] = set()
        for raw in case_ids:
            number = case_number(raw)
            if number is None:
                continue
            parts = case_suffix(raw)
            for key in self._by_case_date.get((number, date), set()):
                stored_celex = self._celex_of.get(key)
                if celex and stored_celex and celex != stored_celex:
                    continue
                stored = self._suffixes_of.get((key, number), set())
                if parts and stored and None not in stored and parts[1] not in stored:
                    continue
                keys.add(key)
        return keys


def _sorted(conflicts: dict[str, DateConflict]) -> list[DateConflict]:
    """Return the date conflicts sorted by key.

    Args:
        conflicts: Key -> conflict.

    Returns:
        The conflicts in key order.
    """
    return [conflicts[k] for k in sorted(conflicts)]


def _result(keys: set[str], rule: IdentityRule) -> IdentityMatch:
    """Build the match of a rule that found at least one key.

    Args:
        keys: Keys found (non-empty).
        rule: The rule.

    Returns:
        A single-key match, or an ambiguous one listing the sorted keys.
    """
    if len(keys) == 1:
        return IdentityMatch(key=next(iter(keys)), rule=rule)
    return IdentityMatch(key=None, rule=rule, ambiguous=sorted(keys))
