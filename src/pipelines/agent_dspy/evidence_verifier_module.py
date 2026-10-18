"""
Hybrid evidence verifier — does a cited chunk actually *support* a fact?

A fact is currently trusted because it was extracted and carries provenance,
not because any chunk was shown to support its claim. This module closes that
gap with a **deterministic-first** pipeline (Opus design): a fully hardwired
strategy router picks the cheapest sufficient verifier per fact, and the LLM
support judge runs only for concept/relation facts where word-presence does not
imply support.

Strategies (see :class:`VerificationStrategy`):
  * ``excluded``               — infrastructure fact names, skipped entirely.
  * ``trusted_human``          — ``human_index_*`` facts, trusted as authored.
  * ``derived``                — verified transitively from predecessor facts.
  * ``deterministic_entity``   — SQL surface match (companies, courts, …).
  * ``deterministic_temporal`` — HeidelTime ``timex_value`` match (dates).
  * ``boolean``                — True/False facts; deterministic marker, then a
                                 polarity-aware LLM judge (False asserts absence).
  * ``llm_support``            — concept/relation facts; LLM judge, last resort.
  * ``no_provenance``          — nothing to verify against; left ``unverified``.

Every non-excluded fact ends with at least one ``fact_evidence`` ledger row and
a populated ``facts.verification_status`` (the complete-ledger guarantee).

Design rules honoured: declarative DSPy signature (Rule 1), all orchestration in
``forward``/helpers (Rule 2), Pydantic structured judge output (Rule 3),
Provisioner pattern for injected DB clients.
"""

import json
import logging
import re
from enum import StrEnum
from typing import Any

import dspy
from pydantic import BaseModel, Field

from config.settings import settings
from pipelines.agent_dspy.evidence_support import (
    EvidenceSupportResult,
    EvidenceSupportSignature,
    fact_statement,
    judge_support,
    snippet,
)
from pipelines.rag.pgvector.embeddings.qwen3_embedding import retrieve_qwen3_embedding
from pipelines.rag.pgvector.fact_evidence_repository import FactEvidenceRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository

# Re-exported for backward compatibility: callers and tests import these names
# from this module. The canonical definitions now live in ``evidence_support``.
__all__ = ["EvidenceSupportResult", "EvidenceSupportSignature"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and value objects
# ---------------------------------------------------------------------------


class VerificationStrategy(StrEnum):
    """The verification path chosen for a fact by the deterministic router."""

    EXCLUDED = "excluded"
    TRUSTED_HUMAN = "trusted_human"
    DERIVED = "derived"
    DETERMINISTIC_ENTITY = "deterministic_entity"
    DETERMINISTIC_TEMPORAL = "deterministic_temporal"
    BOOLEAN = "boolean"
    LLM_SUPPORT = "llm_support"
    NO_PROVENANCE = "no_provenance"


class VerificationStatus(StrEnum):
    """The per-fact verdict written to ``facts.verification_status``."""

    VERIFIED_SUPPORTED = "verified_supported"
    WEAK_SUPPORT = "weak_support"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONTRADICTED = "contradicted"
    MISSING_REQUIRED_ENTITY = "missing_required_entity"
    MISSING_REQUIRED_TIME = "missing_required_time"
    MANUAL_APPROVED = "manual_approved"
    UNVERIFIED = "unverified"
    MANUAL_REJECTED = "manual_rejected"


class CandidateEvidence(BaseModel):
    """A scored candidate chunk considered during verification."""

    chunk_id: str = Field(description="Stable pg_embedding_qwen.id of the chunk.")
    doc_pos: int = Field(default=0, description="Structural chunk index.")
    chunk_text: str = Field(description="Full chunk text.")
    entity_score: float = Field(default=0.0)
    keyword_score: float = Field(default=0.0)
    semantic_score: float = Field(default=0.0)
    temporal_score: float = Field(default=0.0)
    provenance_score: float = Field(default=0.0)
    final_score: float = Field(default=0.0, description="Weighted combined score.")
    candidate_source: str = Field(
        default="", description="Retrieval leg that surfaced this chunk."
    )


class FactVerificationResult(BaseModel):
    """The outcome of verifying one fact (also persisted to fact_evidence)."""

    fact_id: str
    celex_id: str
    fact_name: str
    strategy: VerificationStrategy
    status: VerificationStatus
    best_chunk_id: str | None = None
    final_score: float | None = None
    support_label: str | None = None
    evidence_quote: str | None = None
    missing_elements: list[str] = Field(default_factory=list)
    candidates: list[CandidateEvidence] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# DSPy signature — used only by the boolean branch (Rule 1 / Rule 3)
# ---------------------------------------------------------------------------
# ``EvidenceSupportSignature`` / ``EvidenceSupportResult`` are defined in
# ``evidence_support`` and imported above (re-exported for callers/tests).


class BooleanPolaritySignature(dspy.Signature):
    """Decide whether a chunk supports a yes/no fact at its ASSERTED value.

    The fact was answered True or False. Judge the chunk against that value:
      - ``supports``    : the chunk states or entails the asserted value is
        correct, with an EXACT supporting quote.
      - ``contradicts`` : the chunk asserts the OPPOSITE of the asserted value.
      - ``insufficient``: the chunk does not settle the value either way.

    A False / absence value usually cannot be proven from positive text: return
    ``insufficient``, NOT ``supports``, unless the chunk explicitly states the
    negative. Use no outside knowledge. Give an exact quote only for
    ``supports``. When ``extraction_question`` is given, treat it as the rubric
    that defines what the claim is asking about.
    """

    claim: str = dspy.InputField(desc="The yes/no claim stated in natural language.")
    asserted_value: str = dspy.InputField(
        desc="The extracted answer being verified: 'True' or 'False'."
    )
    extraction_question: str = dspy.InputField(
        desc=(
            "The original question that defined this fact - the criteria the "
            "chunk must satisfy. May be empty."
        )
    )
    chunk_text: str = dspy.InputField(
        desc="The single candidate chunk of document text."
    )
    output: EvidenceSupportResult = dspy.OutputField(
        desc="Polarity-aware support verdict with an exact quote when supported."
    )


# ---------------------------------------------------------------------------
# Hardwired routing tables and weights
# ---------------------------------------------------------------------------

# Substring markers that classify a fact name into a verification strategy.
# Order of evaluation in ``_route_strategy`` is: human → concept → entity →
# temporal → derived → no_provenance.
_CONCEPT_MARKERS: tuple[str, ...] = (
    "infringement",
    "leniency",
    "exemption",
    "dominance",
    "abuse_of_dominance",
    "gravity",
    "remedies",
    "recidivism",
    "cartel",
)
_ENTITY_MARKERS: tuple[str, ...] = (
    "compan",  # companies_investigated / companies_country / companies_industry
    "court",
    "authorit",
    "product",
    "service",
    "nace",
    "law_article",
    "market_geographical",
    "commissioner",
    "case_title",
)
_TEMPORAL_MARKERS: tuple[str, ...] = (
    "decision_date",
    "period",
    "heideltime",
    "_date",
)

# Concept synonym dictionary for the deterministic keyword pre-gate that filters
# candidates before the LLM judge (discussion §2 — reduce LLM calls).
_CONCEPT_DICTIONARY: dict[str, tuple[str, ...]] = {
    "leniency": ("leniency", "immunity", "reduction of fine", "cooperation"),
    "infringement": (
        "infringement",
        "violation",
        "breach",
        "anti-competitive",
        "cartel",
    ),
    "exemption": ("exemption", "exempt", "article 85(3)", "article 101(3)"),
    "dominance": ("dominant", "dominance", "market power", "abuse"),
    "gravity": ("gravity", "serious", "severity"),
    "remedies": ("remedy", "cease", "desist", "divest", "commitment"),
    "recidivism": ("recidivism", "repeat", "previously", "prior infringement"),
}

# Boolean fact -> (natural-language positive claim, literal marker phrases).
# The claim is the polarity judge's criterion; markers feed the deterministic
# fast-path. Facts absent here fall back to a name-derived claim with no markers.
_BOOLEAN_CLAIMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "case_commission_decision": (
        "The European Commission has adopted a formal decision in this case.",
        ("has adopted this decision", "hereby adopted"),
    ),
    "case_commission_no_action": (
        "The Commission need not take action; the articles are declared "
        "inapplicable.",
        ("need not take action", "declared inapplicable"),
    ),
    "case_commission_info_request": (
        "The Commission issued a request for information in this case.",
        ("request for information", "requested information"),
    ),
    "is_court_involved": (
        "A court (Court of Justice or General Court) was involved in this case.",
        ("court of justice", "general court", "judgment", "on appeal"),
    ),
    "leniency_case": (
        "This case involved a leniency or immunity application.",
        ("leniency", "immunity from fines"),
    ),
    "leniency_case_match": (
        "This case involved a leniency or immunity application.",
        ("leniency", "immunity from fines"),
    ),
    "exemption_case": (
        "An exemption under Article 101(3) / 85(3) applies in this case.",
        ("exemption", "article 101(3)", "article 85(3)"),
    ),
}

# Per-strategy weight profile for combining candidate sub-scores into the
# ranking ``final_score`` (weights sum to 1.0). The deterministic branches lean
# on their own dominant signal; the concept branch ranks for the LLM judge.
_WEIGHT_PROFILES: dict[str, dict[str, float]] = {
    "concept": {
        "provenance": 0.25,
        "entity": 0.15,
        "keyword": 0.10,
        "semantic": 0.40,
        "temporal": 0.10,
    },
}

# Values that carry no verifiable claim.
_EMPTY_VALUES: frozenset[str] = frozenset({"", "na", "n/a", "none", "null", "[]"})

# A fact is a boolean (polarity) claim when its value is exactly one of these:
# the data defines boolean-ness, not a hardcoded name list (equivalent to
# ``WHERE lower(value_md) IN ('true', 'false')``).
_BOOLEAN_LITERALS: frozenset[str] = frozenset({"true", "false"})


def _is_empty_scalar(value: str | None) -> bool:
    """Return True for a null/blank/placeholder scalar (``NA``, ``null``, ...)."""
    return value is None or value.strip().lower() in _EMPTY_VALUES


def _has_verifiable_leaf(node: Any) -> bool:
    """Return True when a parsed JSON value holds any non-placeholder content.

    Recurses dicts and lists; a leaf counts as content when it is a non-empty,
    non-placeholder string, or any number/bool. This catches the case where a
    structurally non-empty payload (``[{"x": "NA"}]``) is *semantically* empty —
    every scalar leaf is a placeholder — which the raw-string check misses.
    """
    if isinstance(node, dict):
        return any(_has_verifiable_leaf(child) for child in node.values())
    if isinstance(node, list):
        return any(_has_verifiable_leaf(child) for child in node)
    if isinstance(node, str):
        return not _is_empty_scalar(node)
    return node is not None  # numbers / bools carry meaning; null does not


def _json_has_no_verifiable_value(value_json: str | None) -> bool:
    """Return True when ``value_json`` is null, blank, or all-placeholder.

    Unlike a raw-string check, this deserializes the payload so a JSON structure
    whose scalar leaves are all ``NA``/empty (e.g. ``[{"x": "NA"}]``) is treated
    as having nothing to verify.
    """
    if _is_empty_scalar(value_json):
        return True
    try:
        parsed = json.loads(value_json)
    except (json.JSONDecodeError, TypeError):
        # Not JSON: fall back to treating it as a plain scalar string.
        return _is_empty_scalar(value_json)
    return not _has_verifiable_leaf(parsed)


def _has_no_verifiable_value(value_md: str | None, value_json: str | None) -> bool:
    """Return True when a fact carries no verifiable value in either column.

    A fact is skipped before routing only when BOTH its markdown value and its
    JSON value are null or a trivial placeholder (``NA``, ``[]``, ``[{"x":
    "NA"}]``, ...): there is nothing to match against. A fact that still has
    structured content in ``value_json`` is verified even when ``value_md`` is
    null. The JSON column is parsed so a semantically-empty payload (all scalar
    leaves are placeholders) is recognised, not just the literal ``[]`` / ``NA``.

    Parameters
    ----------
    value_md:
        The fact's ``value_md`` column (may be ``None``).
    value_json:
        The fact's ``value_json`` column (may be ``None``).

    Returns
    -------
    bool
        ``True`` when neither column holds a verifiable value.
    """
    return _is_empty_scalar(value_md) and _json_has_no_verifiable_value(value_json)


def _weighted_score(candidate: CandidateEvidence, profile: dict[str, float]) -> float:
    """Combine a candidate's sub-scores using a named weight profile."""
    return (
        profile["provenance"] * candidate.provenance_score
        + profile["entity"] * candidate.entity_score
        + profile["keyword"] * candidate.keyword_score
        + profile["semantic"] * candidate.semantic_score
        + profile["temporal"] * candidate.temporal_score
    )


def _extract_surfaces(value_json: str | None, value_md: str | None) -> list[str]:
    """Extract candidate entity surface strings from a fact's stored value.

    Parses ``value_json`` (a JSON-encoded list/dict/scalar). For a list of
    dicts, the longest string value of each row is taken as the primary surface
    (typically the entity name); for scalars/strings the value itself. When a
    row's value is itself a list/tuple of fields (e.g. ``case_title`` stores a
    ``(case_id, title)`` pair as ``[{"case_title": ["AT.39813", "Baltic rail"]}]``),
    each element is emitted as its own surface so the deterministic entity check
    matches the real docket / title text rather than a stringified tuple. Falls
    back to ``value_md`` when JSON parsing yields nothing usable. Trivial /
    empty markers (``NA`` etc.) and very short tokens are dropped.

    Parameters
    ----------
    value_json:
        The fact's ``value_json`` column (may be ``None`` or a JSON string).
    value_md:
        The fact's ``value_md`` column, used as a fallback surface.

    Returns
    -------
    list[str]
        Deduplicated, non-trivial surface strings.
    """
    surfaces: list[str] = []

    def _primary(item: Any) -> None:
        if isinstance(item, dict):
            direct = [v for v in item.values() if isinstance(v, str) and v.strip()]
            nested = [
                element
                for value in item.values()
                if isinstance(value, (list, tuple))
                for element in value
                if isinstance(element, str) and element.strip()
            ]
            if nested:
                # Row value is a tuple/list of fields (e.g. case_title's
                # (case_id, title)); every element is a required surface.
                surfaces.extend(nested)
                if direct:
                    surfaces.append(max(direct, key=len))
            elif direct:
                surfaces.append(max(direct, key=len))
        elif isinstance(item, str):
            surfaces.append(item)
        elif item is not None:
            surfaces.append(str(item))

    parsed: Any = None
    if value_json:
        try:
            parsed = json.loads(value_json)
        except (json.JSONDecodeError, TypeError):
            parsed = None

    if isinstance(parsed, list):
        for item in parsed:
            _primary(item)
    elif isinstance(parsed, dict):
        _primary(parsed)
    elif isinstance(parsed, str):
        surfaces.append(parsed)

    if not surfaces and value_md:
        surfaces.append(value_md)

    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in surfaces:
        surface = raw.strip()
        if len(surface) < 3 or surface.lower() in _EMPTY_VALUES:
            continue
        if surface.lower() in seen:
            continue
        seen.add(surface.lower())
        cleaned.append(surface)
    return cleaned


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _date_tokens(text: str) -> tuple[list[str], list[str]]:
    """Split free text into ISO date tokens and bare year tokens.

    Both endpoints of a period (``"1949-01-21 : 1979-06-18"``) are returned.
    Years are extracted from the text with ISO dates removed first, so a year
    embedded in a full date is not double-counted; only 1900–2099 are kept to
    avoid matching unrelated four-digit numbers (amounts, ids).

    Parameters
    ----------
    text:
        Free-form fact value text.

    Returns
    -------
    tuple[list[str], list[str]]
        ``(iso_dates, years)``, each de-duplicated in first-seen order.
    """
    iso = list(dict.fromkeys(_ISO_DATE_RE.findall(text)))
    years = list(dict.fromkeys(_YEAR_RE.findall(_ISO_DATE_RE.sub(" ", text))))
    return iso, years


def _clean_question(raw: str | None) -> str | None:
    """Normalise a stored extraction question into a retrieval/judge prompt.

    Drops ``#``-prefixed comment lines (e.g. ``# Constraint: reply True/False``)
    that steer the extractor's output format but are noise for retrieval and
    judging, then collapses surrounding whitespace.

    Parameters
    ----------
    raw:
        The raw ``question`` text from the audit trail (may be ``None``).

    Returns
    -------
    str or None
        The cleaned question, or ``None`` when nothing usable remains.
    """
    if not raw:
        return None
    lines = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    cleaned = " ".join(lines).strip()
    return cleaned or None


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class EvidenceVerifierModule(dspy.Module):
    """Verify facts against their source chunks, deterministic-first.

    Provisioner pattern — all DB clients and the embedding callable are injected
    so the module is unit-testable with mocks at the system boundary.

    Parameters
    ----------
    chunk_repo:
        ``ChunkRepository`` for keyword / FTS / semantic retrieval and chunk
        text resolution. Also exposes ``.engine`` for the HeidelTime lookup.
    lineage_repo:
        ``LineageRepository`` for provenance (cited chunks / predecessor facts).
    fact_repo:
        ``FactRepository`` for loading the fact under verification.
    evidence_repo:
        ``FactEvidenceRepository`` for persisting the ledger row + verdict.
    embed_fn:
        ``(str) -> list[float]`` embedding callable. Defaults to qwen3.
    support_judge:
        Optional predictor implementing ``EvidenceSupportSignature``. Defaults
        to ``dspy.Predict(EvidenceSupportSignature)``; inject a fake in tests.
    boolean_judge:
        Optional predictor implementing ``BooleanPolaritySignature`` for the
        boolean branch. Defaults to ``dspy.Predict(BooleanPolaritySignature)``;
        inject a fake in tests.
    query_embedding_repo:
        Optional ``QueryEmbeddingRepository`` to cache statement embeddings.
    """

    def __init__(
        self,
        chunk_repo: Any,
        lineage_repo: LineageRepository,
        fact_repo: Any,
        evidence_repo: FactEvidenceRepository,
        embed_fn: Any | None = None,
        support_judge: Any | None = None,
        boolean_judge: Any | None = None,
        query_embedding_repo: Any | None = None,
    ) -> None:
        super().__init__()
        self.chunk_repo = chunk_repo
        self.lineage_repo = lineage_repo
        self.fact_repo = fact_repo
        self.evidence_repo = evidence_repo
        self.embed_fn = embed_fn or retrieve_qwen3_embedding
        self.support_judge = support_judge or dspy.Predict(EvidenceSupportSignature)
        self.boolean_judge = boolean_judge or dspy.Predict(BooleanPolaritySignature)
        self.query_embedding_repo = query_embedding_repo
        self._question_cache: dict[tuple[str, str], str | None] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def forward(self, celex_id: str, fact_name: str) -> FactVerificationResult | None:
        """Verify one fact and persist its ledger row + verdict.

        Parameters
        ----------
        celex_id:
            CELEX document identifier scoping the fact and its evidence.
        fact_name:
            The fact ``name`` to verify.

        Returns
        -------
        FactVerificationResult or None
            The verification outcome, or ``None`` when the fact name is excluded
            (no row written) or no fact row exists.
        """
        return self._verify_by_name(celex_id, fact_name, set())

    def _verify_by_name(
        self, celex_id: str, fact_name: str, seen: set[str]
    ) -> FactVerificationResult | None:
        """Route, verify and persist one fact, threading the recursion guard.

        ``seen`` holds the ids of facts already on the current verification path
        so that on-demand predecessor verification (the ``derived`` branch)
        cannot loop on cyclic lineage.
        """
        if fact_name in settings.EVIDENCE_EXCLUDED_FACT_NAMES:
            logger.debug("Skipping excluded fact name %r", fact_name)
            return None

        facts = self.fact_repo.select_fact4celex(celex_id, fact_name)
        if not facts:
            logger.debug("No fact row for %s/%s", celex_id, fact_name)
            return None
        fact = facts[0]
        seen.add(str(fact.id))

        if _has_no_verifiable_value(fact.value_md, fact.value_json):
            logger.debug(
                "%s/%s has no verifiable value (value_md/value_json empty) "
                "— recording unverified",
                celex_id, fact_name,
            )
            result = self._summary_result(
                fact,
                VerificationStrategy.NO_PROVENANCE,
                VerificationStatus.UNVERIFIED,
                support_label="empty_value",
            )
            self._persist(result)
            return result

        strategy = self._route_strategy(fact)
        logger.debug("Routing %s/%s -> %s", celex_id, fact_name, strategy)

        match strategy:
            case VerificationStrategy.TRUSTED_HUMAN:
                result = self._verify_trusted_human(fact)
            case VerificationStrategy.DERIVED:
                result = self._verify_derived(fact, seen)
            case VerificationStrategy.DETERMINISTIC_ENTITY:
                result = self._verify_entity(fact)
            case VerificationStrategy.DETERMINISTIC_TEMPORAL:
                result = self._verify_temporal(fact)
            case VerificationStrategy.BOOLEAN:
                result = self._verify_boolean(fact)
            case VerificationStrategy.LLM_SUPPORT:
                result = self._verify_llm_support(fact)
            case _:
                result = self._verify_no_provenance(fact)

        self._persist(result)
        return result

    # ------------------------------------------------------------------
    # Deterministic strategy router
    # ------------------------------------------------------------------

    def _route_strategy(self, fact: Any) -> VerificationStrategy:
        """Pick the verification strategy from the fact name, value and lineage.

        Provenance shape is consulted first and beats the name. An *aggregated*
        fact whose only lineage is predecessor facts (``source_type='fact'``)
        with no chunk/book evidence of its own — e.g. ``court_list`` derived
        from ``court_list_raw`` — has no chunks to match against: its evidence
        IS the predecessor fact, so it is verified transitively (``derived``)
        even when its name matches an entity/temporal/concept marker. Only the
        raw fact that carries chunk lineage (``court_list_raw``) is matched
        against chunks.

        For facts that do have direct chunk evidence, deterministic checks win
        as the strongest, cheapest signal: a temporal fact carrying real dates
        is matched against HeidelTime, an entity fact against chunk surfaces.
        Concept/relation facts, where word-presence does not imply support, go
        to the LLM judge when chunk evidence exists; otherwise there is nothing
        to verify (``no_provenance``).
        """
        name = (fact.name or "").lower()
        if name.startswith("human_index_") or name.startswith("human_"):
            return VerificationStrategy.TRUSTED_HUMAN

        counts = self.evidence_repo.get_source_type_counts(fact.id)
        has_chunk = counts.get("chunk", 0) + counts.get("book_case", 0) > 0
        has_fact_predecessors = counts.get("fact", 0) > 0

        # Aggregated-from-fact facts are verified against their predecessor,
        # not the document chunks — regardless of the name marker.
        if has_fact_predecessors and not has_chunk:
            return VerificationStrategy.DERIVED

        # Boolean facts are polarity claims, not surface/temporal matches.
        # Detected from the value itself (the data defines boolean-ness), and
        # routed before the name-marker branches so e.g. ``is_court_involved``
        # is not mistaken for an entity fact and matched against the surface
        # 'True'. Empty/NA values are already filtered out upstream.
        if (fact.value_md or "").strip().lower() in _BOOLEAN_LITERALS:
            return VerificationStrategy.BOOLEAN

        iso_dates, years = _date_tokens(f"{fact.value_md or ''} {fact.value_json or ''}")
        if (iso_dates or years) and any(m in name for m in _TEMPORAL_MARKERS):
            return VerificationStrategy.DETERMINISTIC_TEMPORAL
        if any(marker in name for marker in _ENTITY_MARKERS):
            return VerificationStrategy.DETERMINISTIC_ENTITY

        if any(marker in name for marker in _CONCEPT_MARKERS):
            if has_chunk:
                return VerificationStrategy.LLM_SUPPORT
            return VerificationStrategy.NO_PROVENANCE
        if has_fact_predecessors:
            return VerificationStrategy.DERIVED
        return VerificationStrategy.NO_PROVENANCE

    # ------------------------------------------------------------------
    # Strategy verifiers
    # ------------------------------------------------------------------

    def _verify_trusted_human(self, fact: Any) -> FactVerificationResult:
        """Human-authored facts are trusted as-is (no retrieval, no LLM)."""
        return self._summary_result(
            fact,
            VerificationStrategy.TRUSTED_HUMAN,
            VerificationStatus.MANUAL_APPROVED,
            support_label="human_authored",
        )

    def _verify_derived(
        self, fact: Any, seen: set[str] | None = None
    ) -> FactVerificationResult:
        """Verify transitively from predecessor facts' verdicts (no LLM).

        Aggregates the predecessors' ``verification_status`` with weakest-wins:
        all verified/approved → ``verified_supported``; any insufficient/
        contradicted → that worst state; otherwise ``weak_support``.

        A predecessor whose verdict has not been computed yet
        (``verification_status IS NULL``) is verified on demand first, so the
        derived verdict does not depend on the order facts happen to be
        processed in — e.g. ``court_list`` verified before ``court_list_raw``
        in an alphabetical backfill would otherwise wrongly flag the (present)
        raw fact as a weak element. ``seen`` guards against cyclic lineage.
        """
        seen = seen if seen is not None else {str(fact.id)}
        predecessors = self.evidence_repo.get_predecessor_facts(fact.id)

        pending = [
            p
            for p in predecessors
            if p["verification_status"] is None and str(p["fact_id"]) not in seen
        ]
        if pending:
            for predecessor in pending:
                self._verify_by_name(fact.celex_id, predecessor["name"], seen)
            predecessors = self.evidence_repo.get_predecessor_facts(fact.id)

        statuses = {p["verification_status"] for p in predecessors}

        verified = {
            VerificationStatus.VERIFIED_SUPPORTED.value,
            VerificationStatus.MANUAL_APPROVED.value,
        }
        weak = []
        status = VerificationStatus.VERIFIED_SUPPORTED
        if any(s == VerificationStatus.CONTRADICTED.value for s in statuses):
            status = VerificationStatus.CONTRADICTED
        elif any(
            s == VerificationStatus.INSUFFICIENT_EVIDENCE.value for s in statuses
        ):
            status = VerificationStatus.INSUFFICIENT_EVIDENCE
        elif not statuses or not statuses.issubset(verified):
            status = VerificationStatus.WEAK_SUPPORT

        weak = [
            p["name"] for p in predecessors if p["verification_status"] not in verified
        ]
        return self._summary_result(
            fact,
            VerificationStrategy.DERIVED,
            status,
            support_label="derived_from_facts",
            missing_elements=weak,
        )

    def _verify_entity(self, fact: Any) -> FactVerificationResult:
        """Verify entity facts by deterministic surface match in chunks (no LLM).

        Each extracted surface must appear (case-insensitive substring) in some
        chunk of the document. Full coverage in a single chunk → verified;
        partial coverage → weak; none → missing_required_entity.
        """
        surfaces = _extract_surfaces(fact.value_json, fact.value_md)
        if not surfaces:
            return self._summary_result(
                fact,
                VerificationStrategy.DETERMINISTIC_ENTITY,
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                support_label="no_extracted_values",
                missing_elements=["no_extracted_values"],
            )

        # chunk_id -> (matched surfaces, chunk text, doc_pos)
        per_chunk: dict[str, dict[str, Any]] = {}
        matched_surfaces: set[str] = set()
        for surface in surfaces:
            try:
                hits = self.chunk_repo.keyword_search(fact.celex_id, surface)
            except Exception:
                logger.warning(
                    "keyword_search failed for %s/%s surface=%r",
                    fact.celex_id, fact.name, surface, exc_info=True,
                )
                hits = []
            if hits:
                matched_surfaces.add(surface)
            for hit in hits:
                slot = per_chunk.setdefault(
                    hit.id,
                    {"surfaces": set(), "text": hit.chunk, "doc_pos": hit.doc_pos},
                )
                slot["surfaces"].add(surface)

        coverage = len(matched_surfaces) / len(surfaces)
        missing = [s for s in surfaces if s not in matched_surfaces]

        if not per_chunk:
            return self._summary_result(
                fact,
                VerificationStrategy.DETERMINISTIC_ENTITY,
                VerificationStatus.MISSING_REQUIRED_ENTITY,
                support_label="insufficient",
                missing_elements=missing,
                final_score=0.0,
            )

        best_chunk_id = max(
            per_chunk, key=lambda cid: len(per_chunk[cid]["surfaces"])
        )
        best = per_chunk[best_chunk_id]
        best_in_chunk = len(best["surfaces"]) / len(surfaces)

        if coverage >= 1.0:
            status = VerificationStatus.VERIFIED_SUPPORTED
            label = "supports"
        elif coverage > 0.0:
            status = VerificationStatus.WEAK_SUPPORT
            label = "partially_supports"
        else:  # unreachable given per_chunk non-empty, kept for completeness
            status = VerificationStatus.MISSING_REQUIRED_ENTITY
            label = "insufficient"

        candidate = CandidateEvidence(
            chunk_id=best_chunk_id,
            doc_pos=best["doc_pos"],
            chunk_text=best["text"],
            entity_score=best_in_chunk,
            final_score=best_in_chunk,
            candidate_source="sql_keyword",
        )
        return FactVerificationResult(
            fact_id=str(fact.id),
            celex_id=fact.celex_id,
            fact_name=fact.name,
            strategy=VerificationStrategy.DETERMINISTIC_ENTITY,
            status=status,
            best_chunk_id=best_chunk_id,
            final_score=coverage,
            support_label=label,
            evidence_quote=self._snippet(best["text"], best["surfaces"]),
            missing_elements=missing,
            candidates=[candidate],
        )

    def _verify_temporal(self, fact: Any) -> FactVerificationResult:
        """Verify temporal facts by HeidelTime ``timex_value`` match (no LLM).

        Individual ISO date tokens are parsed out of the fact value — including
        both endpoints of a period like ``"1949-01-21 : 1979-06-18"`` — and each
        must match a ``heideltime_extractions`` row for the same CELEX; the
        matching chunk is the evidence and ``timex_text`` the quote. A token is
        matched on the exact date, falling back to the year.
        """
        text_value = f"{fact.value_md or ''} {fact.value_json or ''}"
        iso_dates, years = _date_tokens(text_value)
        dates = iso_dates or years
        if not dates:
            return self._summary_result(
                fact,
                VerificationStrategy.DETERMINISTIC_TEMPORAL,
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                support_label="no_extracted_dates",
                missing_elements=["no_extracted_dates"],
            )

        match = self._heideltime_match(fact.celex_id, iso_dates, years)
        if match is None:
            return self._summary_result(
                fact,
                VerificationStrategy.DETERMINISTIC_TEMPORAL,
                VerificationStatus.MISSING_REQUIRED_TIME,
                support_label="insufficient",
                missing_elements=dates,
            )

        chunk_id, timex_text, matched_value = match
        chunk = self.chunk_repo.get_chunk_by_id(chunk_id)
        chunk_text = chunk["document"] if chunk else (timex_text or "")
        candidate = CandidateEvidence(
            chunk_id=chunk_id,
            doc_pos=(chunk or {}).get("doc_pos") or 0,
            chunk_text=chunk_text,
            temporal_score=1.0,
            final_score=1.0,
            candidate_source="heideltime",
        )
        return FactVerificationResult(
            fact_id=str(fact.id),
            celex_id=fact.celex_id,
            fact_name=fact.name,
            strategy=VerificationStrategy.DETERMINISTIC_TEMPORAL,
            status=VerificationStatus.VERIFIED_SUPPORTED,
            best_chunk_id=chunk_id,
            final_score=1.0,
            support_label="supports",
            evidence_quote=timex_text or matched_value,
            missing_elements=[d for d in dates if d != matched_value],
            candidates=[candidate],
        )

    def _verify_boolean(self, fact: Any) -> FactVerificationResult:
        """Verify a True/False fact by polarity, deterministic-marker-first.

        Positive facts (``True``) are verifiable by a literal marker phrase or a
        supporting chunk. Negative facts (``False``) assert absence and cannot be
        proven from positive text: they can only be ``contradicted`` (a chunk
        asserts the positive) or left ``weak_support`` (no positive evidence
        found, which is consistent with — but not proof of — ``False``).
        """
        asserted = (fact.value_md or "").strip().lower()
        if asserted not in _BOOLEAN_LITERALS:
            return self._summary_result(
                fact,
                VerificationStrategy.BOOLEAN,
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                support_label="non_boolean_value",
                missing_elements=[asserted or "empty"],
            )
        is_positive = asserted == "true"
        claim, markers = _BOOLEAN_CLAIMS.get(
            fact.name, (self._fact_statement(fact), ())
        )
        question = self._extraction_question(fact)

        # Deterministic marker fast-path (positive only) — cheaper than the LLM.
        if is_positive and markers:
            hit = self._boolean_marker_hit(fact.celex_id, markers)
            if hit is not None:
                chunk_id, text, doc_pos, phrase = hit
                candidate = CandidateEvidence(
                    chunk_id=chunk_id,
                    doc_pos=doc_pos,
                    chunk_text=text,
                    keyword_score=1.0,
                    final_score=1.0,
                    candidate_source="boolean_marker",
                )
                return FactVerificationResult(
                    fact_id=str(fact.id),
                    celex_id=fact.celex_id,
                    fact_name=fact.name,
                    strategy=VerificationStrategy.BOOLEAN,
                    status=VerificationStatus.VERIFIED_SUPPORTED,
                    best_chunk_id=chunk_id,
                    final_score=1.0,
                    support_label="supports",
                    evidence_quote=self._snippet(text, {phrase}),
                    candidates=[candidate],
                )

        # LLM polarity judge over retrieved candidates (no entity surfaces).
        candidates = self._generate_candidates(
            fact, self._retrieval_query(claim, question), []
        )
        if not candidates:
            status = (
                VerificationStatus.INSUFFICIENT_EVIDENCE
                if is_positive
                else VerificationStatus.WEAK_SUPPORT
            )
            return self._summary_result(
                fact,
                VerificationStrategy.BOOLEAN,
                status,
                support_label="no_candidates",
            )

        ranked = sorted(candidates, key=lambda c: c.final_score, reverse=True)
        best: CandidateEvidence | None = None
        best_verdict: EvidenceSupportResult | None = None
        for candidate in ranked[: settings.EVIDENCE_JUDGE_TOP_N]:
            # For False facts, invert the claim and assert True for the judge
            # so it understands we're testing the inverted (opposite) claim.
            if is_positive:
                judge_claim = claim
                judge_value = fact.value_md
            else:
                judge_claim = self._invert_boolean_claim(claim)
                judge_value = "True"  # Judge the inverted claim as True
            verdict = self._judge_boolean(
                judge_claim, judge_value, candidate.chunk_text, question
            )
            if verdict is None:
                continue
            if best is None:
                best, best_verdict = candidate, verdict
            if (
                verdict.label == "supports"
                and verdict.evidence_quote
                and verdict.evidence_quote in candidate.chunk_text
            ):
                best, best_verdict = candidate, verdict
                break
            if verdict.label == "contradicts":
                best, best_verdict = candidate, verdict
                break
        return self._boolean_result(fact, is_positive, best, best_verdict)

    def _boolean_result(
        self,
        fact: Any,
        is_positive: bool,
        best: CandidateEvidence | None,
        verdict: EvidenceSupportResult | None,
    ) -> FactVerificationResult:
        """Map a boolean polarity verdict to a result, asymmetric on ``False``.

        A ``False`` fact can never reach ``verified_supported`` from positive
        retrieval — absence is unfalsifiable that way — so an unproven verdict
        tops out at ``weak_support`` for ``False`` and ``insufficient_evidence``
        for ``True``.
        """
        unproven = (
            VerificationStatus.INSUFFICIENT_EVIDENCE
            if is_positive
            else VerificationStatus.WEAK_SUPPORT
        )
        if best is None or verdict is None:
            return self._summary_result(
                fact,
                VerificationStrategy.BOOLEAN,
                unproven,
                support_label="insufficient",
            )

        quote = (
            verdict.evidence_quote
            if verdict.evidence_quote and verdict.evidence_quote in best.chunk_text
            else None
        )
        if verdict.label == "contradicts":
            status = VerificationStatus.CONTRADICTED
        elif verdict.label == "supports" and quote:
            status = (
                VerificationStatus.VERIFIED_SUPPORTED
                if verdict.support_score >= settings.EVIDENCE_SUPPORT_MIN_SCORE
                else VerificationStatus.WEAK_SUPPORT
            )
        else:  # insufficient / partially_supports / ambiguous
            status = unproven

        return FactVerificationResult(
            fact_id=str(fact.id),
            celex_id=fact.celex_id,
            fact_name=fact.name,
            strategy=VerificationStrategy.BOOLEAN,
            status=status,
            best_chunk_id=best.chunk_id,
            final_score=verdict.support_score,
            support_label=verdict.label,
            evidence_quote=quote,
            missing_elements=verdict.missing_elements,
            candidates=[best],
        )

    def _boolean_marker_hit(
        self, celex_id: str, markers: tuple[str, ...]
    ) -> tuple[str, str, int, str] | None:
        """Return ``(chunk_id, text, doc_pos, phrase)`` for the first marker hit.

        FTS may match a stemmed or partial form, so the literal phrase presence
        is re-checked as a case-insensitive substring before it is trusted as a
        deterministic marker.
        """
        for phrase in markers:
            try:
                hits = self.chunk_repo.keyword_search(celex_id, phrase)
            except Exception:
                logger.warning(
                    "keyword_search failed for %s marker=%r",
                    celex_id, phrase, exc_info=True,
                )
                hits = []
            for hit in hits:
                if phrase.lower() in hit.chunk.lower():
                    return hit.id, hit.chunk, hit.doc_pos, phrase
        return None

    def _judge_boolean(
        self,
        claim: str,
        asserted_value: str,
        chunk_text: str,
        question: str | None = None,
    ) -> EvidenceSupportResult | None:
        """Call the boolean polarity judge; never let an LLM failure crash the run."""
        try:
            pred = self.boolean_judge(
                claim=claim,
                asserted_value=asserted_value,
                extraction_question=question or "",
                chunk_text=chunk_text,
            )
        except Exception:
            logger.warning("Boolean judge raised — skipping candidate", exc_info=True)
            return None
        verdict = getattr(pred, "output", pred)
        if not isinstance(verdict, EvidenceSupportResult):
            logger.debug("Boolean judge returned non-structured output; skipping")
            return None
        return verdict

    def _verify_llm_support(self, fact: Any) -> FactVerificationResult:
        """Verify concept/relation facts with the LLM judge (last resort).

        Generates candidates from provenance + keyword + semantic legs, applies
        a deterministic concept keyword pre-gate to cut LLM calls, ranks by the
        concept weight profile, and runs the support judge over the top-N.
        Always records the best-scored candidate even on failure.
        """
        statement = self._fact_statement(fact)
        surfaces = _extract_surfaces(fact.value_json, fact.value_md)
        concept_terms = self._concept_terms(fact.name)
        question = self._extraction_question(fact)

        retrieval_query = self._retrieval_query(statement, question)
        candidates = self._generate_candidates(fact, retrieval_query, surfaces)
        if not candidates:
            return self._summary_result(
                fact,
                VerificationStrategy.LLM_SUPPORT,
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                support_label="no_candidates",
                missing_elements=surfaces or concept_terms,
            )

        gated = self._concept_keyword_gate(candidates, surfaces, concept_terms)
        ranked = sorted(
            gated or candidates,
            key=lambda c: c.final_score,
            reverse=True,
        )

        required_elements = ", ".join(surfaces + list(concept_terms)) or fact.name
        best: CandidateEvidence | None = None
        best_verdict: EvidenceSupportResult | None = None

        for candidate in ranked[: settings.EVIDENCE_JUDGE_TOP_N]:
            verdict = self._judge(
                statement, required_elements, candidate.chunk_text, question
            )
            if verdict is None:
                continue
            if best is None:
                best, best_verdict = candidate, verdict
            if verdict.label == "supports" and verdict.evidence_quote:
                if verdict.evidence_quote in candidate.chunk_text:
                    best, best_verdict = candidate, verdict
                    break
                logger.debug(
                    "Discarding non-substring quote for %s/%s",
                    fact.celex_id, fact.name,
                )
            if verdict.label == "contradicts" and best_verdict and (
                best_verdict.label not in ("supports",)
            ):
                best, best_verdict = candidate, verdict

        return self._result_from_verdict(fact, best, best_verdict)

    def _verify_no_provenance(self, fact: Any) -> FactVerificationResult:
        """Facts with no usable evidence remain ``unverified`` (with a row)."""
        return self._summary_result(
            fact,
            VerificationStrategy.NO_PROVENANCE,
            VerificationStatus.UNVERIFIED,
            support_label="no_provenance",
        )

    # ------------------------------------------------------------------
    # Candidate generation / scoring (llm_support branch)
    # ------------------------------------------------------------------

    def _generate_candidates(
        self, fact: Any, statement: str, surfaces: list[str]
    ) -> list[CandidateEvidence]:
        """Union provenance, keyword and semantic candidates, scored + deduped."""
        limit = settings.EVIDENCE_CANDIDATE_LIMIT
        profile = _WEIGHT_PROFILES["concept"]
        by_chunk: dict[str, CandidateEvidence] = {}

        def _ensure(chunk_id: str, text: str, doc_pos: int, source: str) -> CandidateEvidence:
            cand = by_chunk.get(chunk_id)
            if cand is None:
                cand = CandidateEvidence(
                    chunk_id=chunk_id,
                    doc_pos=doc_pos,
                    chunk_text=text,
                    candidate_source=source,
                )
                by_chunk[chunk_id] = cand
            return cand

        # Provenance leg — originally cited chunks (trust_rank -> score).
        for chunk_id, trust_rank in self._provenance_chunks(fact.id):
            chunk = self.chunk_repo.get_chunk_by_id(chunk_id)
            if not chunk:
                continue
            cand = _ensure(
                chunk_id, chunk["document"], chunk.get("doc_pos") or 0, "provenance"
            )
            cand.provenance_score = max(cand.provenance_score, (trust_rank or 5) / 10.0)

        # Keyword leg — entity surfaces via FTS.
        query = " ".join(surfaces) if surfaces else statement
        try:
            for hit in self.chunk_repo.fulltext_search_in_celex(
                fact.celex_id, query, hits=limit
            ):
                cand = _ensure(hit.id, hit.chunk, hit.doc_pos, "fts")
                cand.keyword_score = max(cand.keyword_score, min(hit.similarity, 1.0))
        except Exception:
            logger.warning(
                "FTS leg failed for %s/%s", fact.celex_id, fact.name, exc_info=True
            )

        # Semantic leg — statement embedding.
        try:
            embedding = self._embed(statement)
            for hit in self.chunk_repo.semantic_search_in_celex(
                fact.celex_id, embedding, hits=limit
            ):
                cand = _ensure(hit.id, hit.chunk, hit.doc_pos, "semantic")
                cand.semantic_score = max(cand.semantic_score, hit.similarity)
        except Exception:
            logger.warning(
                "Semantic leg failed for %s/%s",
                fact.celex_id, fact.name, exc_info=True,
            )

        # Entity presence sub-score + weighted final.
        for cand in by_chunk.values():
            if surfaces:
                lowered = cand.chunk_text.lower()
                present = sum(1 for s in surfaces if s.lower() in lowered)
                cand.entity_score = present / len(surfaces)
            cand.final_score = _weighted_score(cand, profile)
        return list(by_chunk.values())

    def _concept_keyword_gate(
        self,
        candidates: list[CandidateEvidence],
        surfaces: list[str],
        concept_terms: tuple[str, ...],
    ) -> list[CandidateEvidence]:
        """Keep candidates containing an entity surface AND a concept term.

        Deterministic pre-filter so the LLM judge only sees plausibly-supporting
        chunks. Returns the subset; the caller falls back to all candidates if
        the gate empties the set (so a fact is never silently unverifiable).
        """
        kept: list[CandidateEvidence] = []
        for cand in candidates:
            lowered = cand.chunk_text.lower()
            entity_ok = (not surfaces) or any(s.lower() in lowered for s in surfaces)
            concept_ok = (not concept_terms) or any(
                term in lowered for term in concept_terms
            )
            if entity_ok and concept_ok:
                kept.append(cand)
        return kept

    def _provenance_chunks(self, fact_id: Any) -> list[tuple[str, int]]:
        """Return ``(chunk_id, trust_rank)`` for chunk/book sources of a fact."""
        out: list[tuple[str, int]] = []
        try:
            links = self.lineage_repo.get_sources_for_fact(fact_id)
        except Exception:
            logger.warning("Lineage read failed for %s", fact_id, exc_info=True)
            return out
        for link in links:
            source = self.lineage_repo.get_source(link.source_id)
            if source is None:
                continue
            if source.source_type in ("chunk", "book_case") and source.chunk_id:
                out.append((source.chunk_id, link.trust_rank))
        return out

    # ------------------------------------------------------------------
    # LLM judge wrapper
    # ------------------------------------------------------------------

    def _judge(
        self,
        statement: str,
        required_elements: str,
        chunk_text: str,
        question: str | None = None,
    ) -> EvidenceSupportResult | None:
        """Call the support judge; never let an LLM failure crash the run.

        Thin wrapper over the shared :func:`judge_support` so the support-judge
        call discipline is identical to the book cross-check module.
        """
        return judge_support(
            self.support_judge, statement, required_elements, chunk_text, question
        )

    def _result_from_verdict(
        self,
        fact: Any,
        best: CandidateEvidence | None,
        verdict: EvidenceSupportResult | None,
    ) -> FactVerificationResult:
        """Map the chosen candidate + judge verdict to a verification result."""
        if best is None or verdict is None:
            return self._summary_result(
                fact,
                VerificationStrategy.LLM_SUPPORT,
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                support_label="insufficient",
            )

        quote = (
            verdict.evidence_quote
            if verdict.evidence_quote and verdict.evidence_quote in best.chunk_text
            else None
        )
        if verdict.label == "supports" and quote:
            if verdict.support_score >= settings.EVIDENCE_SUPPORT_MIN_SCORE:
                status = VerificationStatus.VERIFIED_SUPPORTED
            else:
                status = VerificationStatus.WEAK_SUPPORT
        elif verdict.label == "contradicts":
            status = VerificationStatus.CONTRADICTED
        elif verdict.label in ("partially_supports", "ambiguous"):
            status = VerificationStatus.WEAK_SUPPORT
        else:
            status = VerificationStatus.INSUFFICIENT_EVIDENCE

        return FactVerificationResult(
            fact_id=str(fact.id),
            celex_id=fact.celex_id,
            fact_name=fact.name,
            strategy=VerificationStrategy.LLM_SUPPORT,
            status=status,
            best_chunk_id=best.chunk_id,
            final_score=verdict.support_score,
            support_label=verdict.label,
            evidence_quote=quote,
            missing_elements=verdict.missing_elements,
            candidates=[best],
        )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> list[float]:
        """Embed *text*, using the query-embedding cache when available."""
        if self.query_embedding_repo is not None:
            return self.query_embedding_repo.get_or_cache(text, self.embed_fn)
        return self.embed_fn(text)

    def _extraction_question(self, fact: Any) -> str | None:
        """Return the cleaned extraction question for a fact, or ``None``.

        Looks up the most recent question recorded in the audit trail (via
        ``fact_repo``), cleaned of output-format comment lines. Results are
        cached per ``(celex_id, name)`` so the recursive ``derived`` branch does
        not re-query. Returns ``None`` when the feature is disabled, no question
        was recorded, or the lookup fails.
        """
        if not settings.EVIDENCE_USE_EXTRACTION_QUESTION:
            return None
        key = (fact.celex_id, fact.name)
        if key in self._question_cache:
            return self._question_cache[key]
        try:
            raw = self.fact_repo.select_latest_question(fact.celex_id, fact.name)
        except Exception:
            logger.warning(
                "Audit-trail question lookup failed for %s/%s",
                fact.celex_id, fact.name, exc_info=True,
            )
            raw = None
        question = _clean_question(raw)
        self._question_cache[key] = question
        return question

    @staticmethod
    def _retrieval_query(statement: str, question: str | None) -> str:
        """Build the chunk-retrieval query, enriched with the question if any.

        The extraction question carries the semantic criteria a short fact value
        lacks (e.g. what 'high' gravity means), so it is prepended to the
        value-derived statement to steer the semantic embedding and the FTS
        fallback. Falls back to the bare statement when no question exists.
        """
        return f"{question} {statement}" if question else statement

    def _heideltime_match(
        self, celex_id: str, iso_dates: list[str], years: list[str]
    ) -> tuple[str, str | None, str] | None:
        """Return ``(chunk_id, timex_text, matched_value)`` for a date match.

        ``timex_value`` is a ``date`` column, so each full ISO date is compared
        with an explicit ``CAST(:v AS date)`` (avoiding a server-side cast of an
        arbitrary string), and bare years are matched on the extracted year.
        Tried full dates first, then years. Returns ``None`` on no match.
        """
        from sqlalchemy import text as _sql_text

        date_sql = _sql_text(
            "SELECT fk_chunk_id, timex_text "
            "FROM celex_facts.heideltime_extractions "
            "WHERE celex_id = :celex_id AND timex_value = CAST(:v AS date) "
            "LIMIT 1"
        )
        year_sql = _sql_text(
            "SELECT fk_chunk_id, timex_text "
            "FROM celex_facts.heideltime_extractions "
            "WHERE celex_id = :celex_id "
            "  AND EXTRACT(YEAR FROM timex_value) = :y "
            "ORDER BY timex_value LIMIT 1"
        )

        try:
            with self.chunk_repo.engine.connect() as conn:
                for value in iso_dates:
                    row = conn.execute(
                        date_sql, {"celex_id": celex_id, "v": value}
                    ).fetchone()
                    if row is not None:
                        return str(row.fk_chunk_id), row.timex_text, value
                for value in years:
                    row = conn.execute(
                        year_sql, {"celex_id": celex_id, "y": int(value)}
                    ).fetchone()
                    if row is not None:
                        return str(row.fk_chunk_id), row.timex_text, value
        except Exception:
            logger.warning(
                "HeidelTime lookup failed for %s", celex_id, exc_info=True
            )
            return None
        return None

    @staticmethod
    def _fact_statement(fact: Any) -> str:
        """Build the claim string passed to the judge (shared helper)."""
        return fact_statement(fact)

    @staticmethod
    def _concept_terms(fact_name: str) -> tuple[str, ...]:
        """Collect concept synonyms whose key marker appears in the fact name."""
        name = (fact_name or "").lower()
        terms: list[str] = []
        for key, synonyms in _CONCEPT_DICTIONARY.items():
            if key in name:
                terms.extend(synonyms)
        return tuple(dict.fromkeys(terms))

    @staticmethod
    def _invert_boolean_claim(claim: str) -> str:
        """Negate a boolean claim for False facts.

        When a boolean fact is False, the evidence should support the negation
        of the claim. Maps claim patterns to their explicit negations so the
        LLM judge can recognize the evidence clearly.
        """
        lower_claim = claim.lower()

        # For negative-framing claims like "need not", negate them
        if "need not take action" in lower_claim:
            return "The Commission issued a formal decision specifying remedial measures and enforcement actions."
        if "declared inapplicable" in lower_claim:
            return "The Commission found the competition rules applicable and issued an enforcement decision."
        if "no request for information" in lower_claim:
            return "The Commission issued a formal request for information."
        if "no court" in lower_claim and "involved" in lower_claim:
            return "A court (Court of Justice or General Court) was involved in this case."
        if "no leniency" in lower_claim:
            return "Leniency or immunity from fines was granted in this case."
        if "no exemption" in lower_claim:
            return "An exemption under Article 101(3) or 85(3) TFEU applies in this case."

        # Fallback: negate the claim syntactically
        return f"It is not the case that {claim}"

    @staticmethod
    def _snippet(text: str, surfaces: set[str], window: int = 60) -> str | None:
        """Return a short text window around the first matched surface (shared)."""
        return snippet(text, surfaces, window)

    def _summary_result(
        self,
        fact: Any,
        strategy: VerificationStrategy,
        status: VerificationStatus,
        support_label: str | None = None,
        missing_elements: list[str] | None = None,
        final_score: float | None = None,
    ) -> FactVerificationResult:
        """Build a chunk-less (summary-row) verification result."""
        return FactVerificationResult(
            fact_id=str(fact.id),
            celex_id=fact.celex_id,
            fact_name=fact.name,
            strategy=strategy,
            status=status,
            best_chunk_id=None,
            final_score=final_score,
            support_label=support_label,
            evidence_quote=None,
            missing_elements=missing_elements or [],
            candidates=[],
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self, result: FactVerificationResult) -> None:
        """Write the ledger row (best chunk or summary) and the fact verdict."""
        best = result.candidates[0] if result.candidates else None
        sub_scores = (
            {
                "entity": best.entity_score,
                "keyword": best.keyword_score,
                "semantic": best.semantic_score,
                "temporal": best.temporal_score,
                "provenance": best.provenance_score,
            }
            if best is not None
            else None
        )
        self.evidence_repo.upsert_evidence(
            fact_id=result.fact_id,
            chunk_id=result.best_chunk_id,
            support_label=result.support_label,
            final_score=result.final_score,
            sub_scores=sub_scores,
            evidence_quote=result.evidence_quote,
            missing_elements=result.missing_elements or None,
        )
        self.evidence_repo.set_verification_status(
            result.fact_id, result.status.value, result.final_score
        )
