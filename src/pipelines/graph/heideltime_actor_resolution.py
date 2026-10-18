"""Pure (DB-free) projection of ``heideltime_event`` facts onto graph companies.

The HeidelTime pipeline stores one ``heideltime_event`` fact per decision whose
``value_json`` is a bare JSON list of ``{event_date, actor, predicate_object}``
objects.  ``actor`` is free text written by an LLM; this module decides which of
those strings denote a company already present in the case's own graph
neighbourhood, and aggregates their dates into one edge payload per company.

Why this is not ``entity_graph_mapping.parse_rows``
---------------------------------------------------
``parse_rows`` emits one endpoint token plus an optional amount per list element.
A HeidelTime event carries *three* load-bearing fields, needs classification
before matching, and needs aggregation *across* elements because the edge
identity ``(src, dst, relation, source_fact_id)`` collapses every event of one
case onto a single edge (there is exactly one ``heideltime_event`` fact per
CELEX).  A dedicated parser is therefore correct; the "unparseable -> empty,
never raise" discipline and the empty-marker set are shared with that module.

Case-scoped matching, deliberately
----------------------------------
Actors are matched **only** against the company nodes already materialized for
the same decision.  They are never routed through
``EntityGraphRepository.resolve_company``: that performs a pg_trgm probe over
3.3M ``corp_lei`` legal names whose reliability for short needles is measured at
45% cross-source disagreement (ISSUES.md 20260830), and a wrong LEI here would
key a company node on the wrong legal entity.  Nor is a new node ever created
from an actor string - 9,616 distinct actor values include ``redacted``,
``parties`` and ``employee of non-addressee``, and node dedup is already weak
(1,972 of 6,531 company nodes carry an LEI).

Matching is on *token tuples* with legal-form tokens removed, never on raw
substrings, and an ambiguous actor is refused rather than guessed.  The
consequence is a deliberate conservative miss: ``AZ`` does not match
``AstraZeneca``.  Refusals are counted, never silently dropped.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from pipelines.graph.entity_graph_mapping import _EMPTY_JSON_MARKERS
from pipelines.graph.entity_graph_models import EdgeValue

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
_TOKEN_SPLIT_RE = re.compile(r"[^0-9a-z]+")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_POSSESSIVE_RE = re.compile(r"'s\b")

# Legal-form and corporate-suffix tokens dropped before matching, so that
# "Akzo Chemicals BV" and "Akzo Chemicals" share a stem.  Kept deliberately
# narrow: only tokens that carry no discriminating power on their own.
_LEGAL_FORM_TOKENS: frozenset[str] = frozenset(
    {
        "ab", "ag", "aps", "as", "asa", "bv", "co", "company", "corp",
        "corporation", "cv", "gmbh", "group", "holding", "holdings", "inc",
        "incorporated", "kft", "kg", "kgaa", "limited", "ltd", "llc", "lp",
        "nv", "oy", "oyj", "plc", "sa", "sarl", "sas", "se", "sl", "spa",
        "sp", "spolka", "srl", "zrt",
    }
)

# Actor strings that denote no party at all.  ``redacted`` alone accounts for
# 18,139 of 57,756 stored events (31%); these must never reach the matcher.
_PLACEHOLDER_ACTORS: frozenset[str] = frozenset(
    {
        "redacted", "[redacted]", "unknown", "na", "n/a", "none", "not specified",
        "unspecified", "parties", "the parties", "addressees", "the addressees",
        "third party", "third parties", "customers", "competitors",
        "employee of non-addressee", "employees of non-addressee",
    }
)

# Tokens/phrases marking an institution rather than an undertaking.  These get
# no node (see the module plan): "which cases did the Commission act in" is all
# of them, and there is no registry to dedup a treaty-era renaming against.
_INSTITUTION_MARKERS: tuple[str, ...] = (
    "commission",
    "court",
    "council",
    "parliament",
    "authority",
    "ombudsman",
    "bundeskartellamt",
    "office of fair trading",
    "dg comp",
    "ecj",
    "cfi",
)


class ActorClass(StrEnum):
    """What an ``actor`` string denotes, decided before any matching."""

    COMPANY_CANDIDATE = "company_candidate"
    INSTITUTION = "institution"
    PLACEHOLDER = "placeholder"


class MatchMethod(StrEnum):
    """How an actor was matched to a company node, in precedence order."""

    EXACT = "exact"
    ALIAS = "alias"
    STEM_ACTOR_IN_COMPANY = "stem_actor_in_company"
    STEM_COMPANY_IN_ACTOR = "stem_company_in_actor"


class HeidelTimeEventRow(BaseModel):
    """One parsed element of a ``heideltime_event`` fact's ``value_json``.

    ``event_date`` is kept verbatim: the extractor emits full ISO dates but also
    year-only (``1994``) and month precision (``1994-03``), and prose or ``NA``
    survives in older rows.  Normalisation happens at aggregation time so the
    raw value stays visible to the timeline view.
    """

    model_config = ConfigDict(frozen=True)

    event_date: str
    actor: str
    predicate_object: str | None = None


@dataclass(frozen=True)
class CompanyCandidate:
    """A company node already materialized for the case being processed.

    Attributes
    ----------
    entity_id:
        The node identity as returned by ``EntityGraphRepository.upsert_entity``
        (a ``uuid.UUID`` in production, a string in the batch unit tests), kept
        opaque so this module stays free of any persistence type.
    canonical_name:
        The node's display name, matched against after normalisation.
    """

    entity_id: Hashable
    canonical_name: str


@dataclass
class TemporalProjection:
    """The result of projecting one case's events onto its company nodes.

    Attributes
    ----------
    payloads:
        One aggregated :class:`EdgeValue` per resolved company node, ready to
        become a ``dated_in`` edge's ``value_json``.
    counters:
        Per-case tallies merged into the batch's stats dict.  Every event and
        every actor lands in exactly one terminal bucket, so a drop is always
        countable.
    ambiguous_actors:
        ``(actor, [candidate names])`` for actors that matched more than one
        company in a tier; the caller logs them.
    """

    payloads: dict[Hashable, EdgeValue] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    ambiguous_actors: list[tuple[str, list[str]]] = field(default_factory=list)


@dataclass
class _CompanyBucket:
    """Mutable accumulator for one company's events while grouping."""

    actors: set[str] = field(default_factory=set)
    dates: list[str] = field(default_factory=list)
    event_count: int = 0


def _new_bucket() -> _CompanyBucket:
    """Return an empty accumulator (named so ``setdefault`` stays readable)."""
    return _CompanyBucket()


def parse_heideltime_events(value_json: str | None) -> list[HeidelTimeEventRow]:
    """Parse a ``heideltime_event`` fact's ``value_json`` into event rows.

    Tolerant by contract: an empty marker, malformed JSON, a non-list payload or
    an element without an ``actor`` yields no row rather than an exception, so a
    single bad document can never abort a corpus-wide materialization.

    Parameters
    ----------
    value_json:
        The raw ``facts.value_json`` string, expected to be a JSON list of
        objects.

    Returns
    -------
    list[HeidelTimeEventRow]
        One row per usable element, in stored order (the extractor writes them
        chronologically).
    """
    if value_json is None or value_json.strip().casefold() in _EMPTY_JSON_MARKERS:
        return []
    try:
        payload = json.loads(value_json)
    except (TypeError, ValueError):
        logger.debug("heideltime_event value_json is not JSON; skipping")
        return []
    if not isinstance(payload, list):
        return []

    rows: list[HeidelTimeEventRow] = []
    for element in payload:
        if not isinstance(element, Mapping):
            continue
        actor = element.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            continue
        event_date = element.get("event_date")
        predicate = element.get("predicate_object")
        rows.append(
            HeidelTimeEventRow(
                event_date=event_date if isinstance(event_date, str) else "",
                actor=actor.strip(),
                predicate_object=predicate if isinstance(predicate, str) else None,
            )
        )
    return rows


def normalize_actor(name: str) -> str:
    """Return an actor or company name reduced to a comparable form.

    NFKC-normalises (the roster was NFKC-normalised upstream), casefolds,
    strips quotes and possessives, and collapses whitespace.

    Parameters
    ----------
    name:
        Any actor or company display name.

    Returns
    -------
    str
        The normalised form, possibly empty.
    """
    text = unicodedata.normalize("NFKC", name)
    text = text.strip().strip("\"'[](){}").strip()
    text = _POSSESSIVE_RE.sub("", text.casefold())
    return _WHITESPACE_RE.sub(" ", text).strip()


def company_stem(name: str) -> tuple[str, ...]:
    """Return the discriminating token tuple of a company or actor name.

    Splits the normalised name on non-alphanumerics and drops legal-form tokens
    so ``Akzo Chemicals BV`` and ``Akzo Chemicals`` share a stem.  Matching on
    this tuple - rather than on raw substrings - is what keeps ``AZ`` from
    matching ``AstraZeneca``.

    Parameters
    ----------
    name:
        Any actor or company display name.

    Returns
    -------
    tuple[str, ...]
        The remaining tokens in order; empty when the name was entirely
        legal-form noise.
    """
    tokens = [token for token in _TOKEN_SPLIT_RE.split(normalize_actor(name)) if token]
    # Single characters are dropped alongside the legal forms: they are almost
    # always fragments of a punctuated suffix ("A/S", "S.p.A.") and would
    # otherwise defeat subsequence matching for the name they trail.
    stem = tuple(
        token
        for token in tokens
        if token not in _LEGAL_FORM_TOKENS and len(token) > 1
    )
    return stem or tuple(tokens)


def classify_actor(actor: str) -> ActorClass:
    """Classify an actor string before any matching is attempted.

    Placeholders are tested first (they are exact strings), then institutional
    markers by token/phrase containment; anything else is a company candidate.

    Parameters
    ----------
    actor:
        The raw ``actor`` value from an event.

    Returns
    -------
    ActorClass
        The bucket this actor belongs to.
    """
    normalized = normalize_actor(actor)
    if not normalized or normalized in _PLACEHOLDER_ACTORS:
        return ActorClass.PLACEHOLDER
    tokens = set(_TOKEN_SPLIT_RE.split(normalized))
    for marker in _INSTITUTION_MARKERS:
        if " " in marker:
            if marker in normalized:
                return ActorClass.INSTITUTION
        elif marker in tokens:
            return ActorClass.INSTITUTION
    return ActorClass.COMPANY_CANDIDATE


def _is_token_subsequence(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    """Return ``True`` when ``needle`` appears as a contiguous run in ``haystack``."""
    if not needle or len(needle) > len(haystack):
        return False
    for start in range(len(haystack) - len(needle) + 1):
        if haystack[start : start + len(needle)] == needle:
            return True
    return False


def _stem_is_too_short(stem: tuple[str, ...], min_stem_len: int) -> bool:
    """Return ``True`` for single-token stems below the length floor.

    A one-token stem such as ``az`` carries too little signal for subsequence
    matching; requiring a floor is what makes the ``AstraZeneca`` miss
    deliberate rather than accidental.
    """
    return len(stem) == 1 and len(stem[0]) < min_stem_len


def match_actor(
    actor: str,
    candidates: Iterable[CompanyCandidate],
    min_stem_len: int,
    alias_resolver: Callable[[str], str | None] | None = None,
) -> tuple[CompanyCandidate | None, MatchMethod | None, list[str]]:
    """Match one actor against the case's company nodes, refusing ambiguity.

    Tiers are tried in precedence order and the first tier producing exactly one
    distinct node wins.  A tier producing two or more distinct nodes ends the
    search with no match, so an ambiguous actor is never guessed.

    Parameters
    ----------
    actor:
        The raw actor string (already classified as a company candidate).
    candidates:
        The company nodes materialized for this case.
    min_stem_len:
        Minimum length for a single-token stem to take part in stem matching.
    alias_resolver:
        Optional callable mapping an alias to a canonical company name, injected
        by the caller so this module performs no database access.

    Returns
    -------
    tuple[CompanyCandidate | None, MatchMethod | None, list[str]]
        The matched candidate and the method used, or ``(None, None, names)``
        where ``names`` lists the colliding candidates when the refusal was
        caused by ambiguity (empty when simply unmatched).
    """
    ordered = sorted(candidates, key=lambda candidate: candidate.canonical_name)
    if not ordered:
        return None, None, []

    actor_normalized = normalize_actor(actor)
    actor_stem = company_stem(actor)

    alias_normalized: str | None = None
    if alias_resolver is not None:
        alias = alias_resolver(actor)
        if alias:
            alias_normalized = normalize_actor(alias)

    tiers: list[tuple[MatchMethod, Callable[[CompanyCandidate], bool]]] = [
        (
            MatchMethod.EXACT,
            lambda candidate: normalize_actor(candidate.canonical_name) == actor_normalized,
        ),
        (
            MatchMethod.ALIAS,
            lambda candidate: alias_normalized is not None
            and normalize_actor(candidate.canonical_name) == alias_normalized,
        ),
        (
            MatchMethod.STEM_ACTOR_IN_COMPANY,
            lambda candidate: _is_token_subsequence(
                actor_stem, company_stem(candidate.canonical_name)
            ),
        ),
        (
            MatchMethod.STEM_COMPANY_IN_ACTOR,
            lambda candidate: _is_token_subsequence(
                company_stem(candidate.canonical_name), actor_stem
            ),
        ),
    ]

    stem_blocked = _stem_is_too_short(actor_stem, min_stem_len)
    for method, predicate in tiers:
        if stem_blocked and method in (
            MatchMethod.STEM_ACTOR_IN_COMPANY,
            MatchMethod.STEM_COMPANY_IN_ACTOR,
        ):
            continue
        hits = [candidate for candidate in ordered if predicate(candidate)]
        distinct = {candidate.entity_id for candidate in hits}
        if len(distinct) == 1:
            return hits[0], method, []
        if len(distinct) > 1:
            return None, None, [candidate.canonical_name for candidate in hits]
    return None, None, []


def project_case_events(
    events: Iterable[HeidelTimeEventRow],
    candidates: Iterable[CompanyCandidate],
    min_stem_len: int,
    alias_resolver: Callable[[str], str | None] | None = None,
) -> TemporalProjection:
    """Project one case's events onto its company nodes as edge payloads.

    Every distinct actor is classified and matched once; the matched events are
    then aggregated per company because the edge unique key collapses all events
    of a case onto one edge per company.  ``first_date`` / ``last_date`` are
    computed over full ISO dates only, while ``event_count`` counts every
    attached event including partial precisions, so a year-only period stays
    visible without inventing a day.

    Parameters
    ----------
    events:
        Parsed events for one decision.
    candidates:
        The company nodes already materialized for that decision.
    min_stem_len:
        Minimum single-token stem length for stem matching.
    alias_resolver:
        Optional alias -> canonical name callable.

    Returns
    -------
    TemporalProjection
        Payloads keyed by company entity id, plus counters and ambiguity report.
    """
    event_list = list(events)
    candidate_list = list(candidates)
    counters: dict[str, int] = {
        "heideltime_events_total": len(event_list),
        "heideltime_events_attached": 0,
        "heideltime_events_dropped": 0,
        "heideltime_actors_total": 0,
        "heideltime_actor_placeholder": 0,
        "heideltime_actor_institution": 0,
        "heideltime_actor_ambiguous": 0,
        "heideltime_actor_unresolved": 0,
        "heideltime_event_partial_date": 0,
    }
    for method in MatchMethod:
        counters[f"heideltime_actor_{method.value}"] = 0

    resolved: dict[str, CompanyCandidate] = {}
    ambiguous_actors: list[tuple[str, list[str]]] = []

    for actor in sorted({event.actor for event in event_list}):
        counters["heideltime_actors_total"] += 1
        actor_class = classify_actor(actor)
        if actor_class is ActorClass.PLACEHOLDER:
            counters["heideltime_actor_placeholder"] += 1
            continue
        if actor_class is ActorClass.INSTITUTION:
            counters["heideltime_actor_institution"] += 1
            continue
        candidate, method, colliding = match_actor(
            actor, candidate_list, min_stem_len, alias_resolver
        )
        if candidate is None:
            if colliding:
                counters["heideltime_actor_ambiguous"] += 1
                ambiguous_actors.append((actor, colliding))
            else:
                counters["heideltime_actor_unresolved"] += 1
            continue
        resolved[actor] = candidate
        counters[f"heideltime_actor_{method.value}"] += 1

    grouped: dict[Hashable, _CompanyBucket] = {}
    for event in event_list:
        candidate = resolved.get(event.actor)
        if candidate is None:
            counters["heideltime_events_dropped"] += 1
            continue
        counters["heideltime_events_attached"] += 1
        bucket = grouped.setdefault(candidate.entity_id, _new_bucket())
        bucket.actors.add(event.actor)
        bucket.event_count += 1
        if _ISO_DATE_RE.match(event.event_date):
            bucket.dates.append(event.event_date)
        else:
            counters["heideltime_event_partial_date"] += 1

    payloads: dict[Hashable, EdgeValue] = {}
    for entity_id, bucket in grouped.items():
        dates = sorted(bucket.dates)
        payloads[entity_id] = EdgeValue(
            actors=tuple(sorted(bucket.actors)),
            first_date=dates[0] if dates else None,
            last_date=dates[-1] if dates else None,
            event_count=bucket.event_count,
        )

    return TemporalProjection(
        payloads=payloads, counters=counters, ambiguous_actors=ambiguous_actors
    )
