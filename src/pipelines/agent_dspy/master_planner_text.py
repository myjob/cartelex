"""Pure, dependency-free helpers for the Master Planner Agent.

Kept free of DSPy and DB clients so they are trivially unit-testable and can be
imported by both the router seam (to decide whether to invoke the agent) and the
agent module itself.  This mirrors the role ``retrieval_text`` plays for the
primary pipeline; it reuses that module's empty-evidence markers rather than
duplicating them.

Contents:
  - ``is_insufficient_answer`` - did the primary QA pipeline fail to answer?
  - ``looks_compositional``    - is the question worth the Master Planner's cost?
  - ``ROLE_FACT_MAP``          - concept -> candidate fact names (catalog-grounded)
  - ``ROLE_SYNONYMS``          - role words for the filter stage
  - ``map_concept_to_fact_names`` / ``filter_known_fact_names`` - catalog gating
  - ``build_fact_name_catalog`` - distinct names minus infra names
"""

import re

from pipelines.agent_dspy.retrieval_text import _EMPTY_EVIDENCE_MARKERS

# ---------------------------------------------------------------------------
# Insufficient-answer detection (router seam, step 3 of the fallback gate)
# ---------------------------------------------------------------------------

# Minimum answer length below which we treat the QA result as a non-answer.
_MIN_ANSWER_LEN = 24

# Phrases a synthesizer emits when it could not ground an answer.  Matched
# case-insensitively as substrings of the rendered answer.
_NON_ANSWER_PHRASES: tuple[str, ...] = (
    "no facts found",
    "no relevant",
    "no results",
    "not found in",
    "could not find",
    "couldn't find",
    "unable to find",
    "i could not",
    "i couldn't",
    "i don't have",
    "i do not have",
    "insufficient evidence",
    "no evidence",
    "no information",
    "no cases were found",
    "no matching",
    "cannot answer",
    "can't answer",
)


def is_insufficient_answer(answer: str, evidence: str) -> bool:
    """Return True when the primary QA pipeline failed to produce an answer.

    Two independent signals trigger insufficiency:

    1. The collected ``evidence`` is empty or carries a tool error/empty marker
       (reuses ``retrieval_text._EMPTY_EVIDENCE_MARKERS``).
    2. The rendered ``answer`` is blank, trivially short, or contains a
       known "I could not answer" phrase.

    Args:
        answer: The final answer string from the QA prediction.
        evidence: The concatenated evidence string from the QA prediction.

    Returns:
        True when the Master Planner fallback should be considered.
    """
    evidence_text = evidence or ""
    if not evidence_text.strip():
        return True
    if any(marker in evidence_text for marker in _EMPTY_EVIDENCE_MARKERS):
        return True

    answer_text = (answer or "").strip()
    if len(answer_text) < _MIN_ANSWER_LEN:
        return True
    lowered = answer_text.lower()
    return any(phrase in lowered for phrase in _NON_ANSWER_PHRASES)


# ---------------------------------------------------------------------------
# Compositional-query detection (cheap pre-gate)
# ---------------------------------------------------------------------------

# Aggregation / grouping cues.  Word-boundary regex avoids false positives
# (e.g. "count" inside "country", "per" inside "period").
_AGGREGATION_CUES: tuple[str, ...] = (
    r"how many",
    r"\bper\b",
    r"\bby (?:country|countries|company|companies|sector|year|member state)",
    r"which countries",
    r"which companies",
    r"which member states",
    r"\bcompare\b",
    r"\bversus\b",
    r"\bvs\b",
    r"\brank\b",
    r"\bmost\b",
    r"\bleast\b",
    r"\bhighest\b",
    r"\blowest\b",
    r"\ba lot of\b",
    r"\bdistribution\b",
    r"\bgroup(?:ed)? by\b",
    r"across (?:all )?cases",
)

# Role words that imply per-case filtering after retrieval.
_ROLE_CUES: tuple[str, ...] = (
    r"\bring ?leader",
    r"\bleader\b",
    r"\binstigator",
    r"\binitiator",
    r"\borganis(?:er|or)",
    r"\borganizer",
    r"\bcoordinator",
)

# Relational cues: a question about how two companies are connected. These are
# compositional without carrying an aggregation or role cue, so without them a
# graph question ("who were Bayer's co-participants?") never escalates.
_RELATIONAL_CUES: tuple[str, ...] = (
    r"\bco-?particip",
    r"\bconnected to\b",
    r"\bsame compan(?:y|ies) as\b",
    r"\bsame (?:companies|firms|undertakings)\b",
    r"\balso appear",
    r"\balongside\b",
    r"\blinked to\b",
    r"\btogether with\b",
)

# Enumeration cue paired with a relational clause ("cases where ... was").
_CASE_WHERE_RE = re.compile(
    r"\b(?:all |list )?cases?\b.*\bwhere\b", re.IGNORECASE
)

# A capitalised multi-token proper noun, e.g. "Bayer AG", "Koninklijke Philips".
_PROPER_NOUN_RE = re.compile(r"\b([A-Z][\w&.-]+(?:\s+[A-Z][\w&.-]+)+)\b")


def looks_compositional(question: str) -> bool:
    """Return True when *question* is worth the Master Planner's extra cost.

    Pure heuristic - no I/O.  A question is "compositional" when it shows an
    aggregation / grouping cue, a relational cue (how two companies are
    connected), a role-filter cue, an enumerated relational clause ("cases
    where X was the ..."), or names a proper-noun entity in a multi-entity /
    filtering context.  Simple single-fact lookups return False so the fallback
    never fires for them.

    Args:
        question: The user's natural-language question.

    Returns:
        True when the question looks like a multi-step compositional query.
    """
    text = question or ""
    lowered = text.lower()

    for pattern in _AGGREGATION_CUES:
        if re.search(pattern, lowered):
            return True

    for pattern in _RELATIONAL_CUES:
        if re.search(pattern, lowered):
            return True

    if _CASE_WHERE_RE.search(text):
        return True

    has_role = any(re.search(p, lowered) for p in _ROLE_CUES)
    has_proper_noun = bool(_PROPER_NOUN_RE.search(text))
    # A role cue is only compositional when tied to an entity to filter by.
    if has_role and has_proper_noun:
        return True

    return False


# ---------------------------------------------------------------------------
# Concept -> fact-name mapping (catalog-grounded; the fix for narrow Literals)
# ---------------------------------------------------------------------------

# Ordered map from a query concept (lowercase substring) to candidate fact
# names in ``celex_facts.facts``.  Longer / more-specific phrases come first.
# These are the *candidate* names; the live catalog is the authority, so
# ``map_concept_to_fact_names`` always intersects the result with it.
#
# Deliberate gaps exist (e.g. "cartel leader" has no structured fact), which is
# precisely why those queries fall back to doc_search / rag_search_in_case.
ROLE_FACT_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("undertaking", ("companies_investigated",)),
    ("party", ("companies_investigated",)),
    ("parties", ("companies_investigated",)),
    ("company", ("companies_investigated", "companies_country", "companies_address")),
    ("companies", ("companies_investigated",)),
    (
        "leniency",
        (
            "leniency_applicants",
            "leniency_granted",
            "leniency_rejected",
            "leniency_case",
            "leniency_case_match",
        ),
    ),
    ("recidivism", ("company_recidivism",)),
    (
        "country",
        ("companies_country", "market_geographical", "infringement_category_member_states"),
    ),
    ("member state", ("infringement_category_member_states", "companies_country")),
    ("geograph", ("market_geographical",)),
    (
        "sector",
        ("OECD_sector", "NACE_group_code", "NACE_class_code", "companies_industry"),
    ),
    (
        "industry",
        ("OECD_sector", "companies_industry", "NACE_group_code", "NACE_class_code"),
    ),
    ("nace", ("NACE_group_code", "NACE_class_code")),
    ("product", ("products", "products_name")),
    ("court", ("court_list_raw", "court_list", "is_court_involved")),
    ("tribunal", ("court_list_raw", "court_list")),
    ("fine", ("fines",)),
    ("penalt", ("fines",)),
    ("remed", ("decision_remedies",)),
    ("commitment", ("decision_remedies",)),
    (
        "gravity",
        ("infringement_gravity",),
    ),
    ("exemption", ("exemption_applicants", "exemption_case")),
    ("commissioner", ("case_commissioner",)),
    (
        "decision date",
        ("eu_commisssion_decision_date", "human_index_decision_date"),
    ),
    ("decision year", ("eu_commisssion_decision_date", "human_index_decision_date")),
    ("cartel period", ("cartel_period", "heideltime_cartel_period")),
)

# Role words used by the deterministic filter stage to keep matching rows.
# Maps a canonical role label to the surface words that signal it in text.
ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ringleader": (
        "ringleader",
        "ring leader",
        "leader",
        "instigator",
        "initiator",
        "organiser",
        "organizer",
        "coordinator",
        "driving force",
        "instrumental role",
    ),
    "leniency": ("leniency", "immunity", "cooperation", "reduction of fine"),
}


def map_concept_to_fact_names(text: str, catalog: frozenset[str]) -> list[str]:
    """Map free-text concepts in *text* to catalog-validated fact names.

    Args:
        text: A question, entity surface, or concept phrase.
        catalog: The live set of distinct fact names (authority).

    Returns:
        Catalog-validated candidate fact names in first-seen order; empty when
        no concept maps to a known fact (the caller then uses chunk search).
    """
    lowered = (text or "").lower()
    resolved: list[str] = []
    for phrase, fact_names in ROLE_FACT_MAP:
        if phrase in lowered:
            for name in fact_names:
                if name in catalog and name not in resolved:
                    resolved.append(name)
    return resolved


def filter_known_fact_names(
    names: list[str], catalog: frozenset[str]
) -> tuple[list[str], list[str]]:
    """Split *names* into (known, unknown) against the live catalog.

    Order-preserving and deduplicating.  Unknown names are returned so the
    caller can log and drop them rather than executing them blindly.

    Args:
        names: Candidate fact names emitted by the planner / LLM.
        catalog: The live set of distinct fact names.

    Returns:
        ``(known, unknown)`` lists, each first-seen-ordered and deduplicated.
    """
    known: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        (known if name in catalog else unknown).append(name)
    return known, unknown


def build_fact_name_catalog(
    distinct_names: list[str], excluded: frozenset[str]
) -> frozenset[str]:
    """Build the live fact-name catalog from distinct names minus infra names.

    Drops names in *excluded* (e.g. ``settings.EVIDENCE_EXCLUDED_FACT_NAMES``).
    Exclusion is config-driven only: integration tests delete their own
    scaffolding rows, so no name-pattern heuristic is applied here.

    Args:
        distinct_names: Output of ``FactRepository.select_distinct_fact_names``.
        excluded: Infrastructure fact names to drop from the catalog.

    Returns:
        A frozenset of catalog fact names suitable for grounding the planner.
    """
    return frozenset(
        name for name in distinct_names if name and name not in excluded
    )
