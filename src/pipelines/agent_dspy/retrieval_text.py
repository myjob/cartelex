"""Shared text, synonym, and direct-fact-passthrough helpers.

Pure, dependency-free utilities used across the retrieval pipeline: empty-evidence
detection, order-preserving dedup, case-title cleaning, fact-name normalisation and
matching, markdown detection, the topic-synonym table, and celex-id extraction.

These helpers carry no business state and import nothing from sibling retrieval
modules, so they form the base layer of the dependency graph
(``retrieval_provenance``, ``retrieval_dispatch``, and ``retrieval_modules`` all
import from here).
"""

import ast
import re

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum non-trivial value_md length to consider for direct passthrough.
_MIN_PASSTHROUGH_LEN = 10

# Markdown indicators — if *any* of these appear in a value_md string we
# treat it as pre-formatted markdown that the synthesizer should not touch.
_MARKDOWN_INDICATORS = (
    "|---",  # table separator row
    "| ---",  # table separator (with space)
    "|-",  # compact table separator
    "\n|",  # pipe at start of line (table row)
    "\n- ",  # unordered list item
    "\n* ",  # unordered list item (asterisk)
    "\n1. ",  # ordered list item
    "\n## ",  # h2 header
    "\n### ",  # h3 header
)

# Markers emitted by ``_ToolDispatcher`` when a tool returns nothing useful.
# Used by the executor to decide whether the synonym fallback should fire.
_EMPTY_EVIDENCE_MARKERS: tuple[str, ...] = (
    "No rows returned.",
    "No facts found",
    "No results.",
    "No chunks",
    "[error]",
    "skipped — upstream",
)

# Recognise a celex_id inside free-form question text.  Covers the formats
# used in this project: ``CELEX-31991D0562``, ``38344_5856_3``, and
# ``AT_40588_6339`` (AT-prefix with underscore-separated digit groups).
_CELEX_ID_RE = re.compile(
    r"\b(CELEX-[A-Z0-9]+|AT_\d+(?:_\d+)+|\d+_\d+_\d+)\b", re.IGNORECASE
)

# Topics whose information lives exclusively in raw document chunks, not in
# structured ``celex_facts.facts``.  Used by the post-plan injection gate to
# deterministically add a ``rag_search_in_case`` / ``doc_search`` step when
# the LLM planner omits one.
#
# NOTE: ``court_list_raw`` / ``court_list`` are structured *summary* facts
# that list which courts are involved.  The actual ruling / assessment content
# still lives only in document chunks, so "court" MUST remain here.
_UNSTRUCTURED_TOPICS: tuple[str, ...] = (
    "court",
    "tribunal",
    "appeal",
    "judgment",
    "annul",
    "dawn raid",
    "statement of objections",
    "settlement",
    "oral hearing",
    "hearing officer",
    "ruling",
    "assessment",
    "order",
    "opinion",
    "review",
)

# Ordered map from question phrase (lowercase substring) to canonical fact
# ``name`` in ``celex_facts.facts``.  Longer / more-specific phrases come first
# so that "decision date" wins over "decision" alone.  Used only by the
# synonym-fallback path; the planner remains the primary router.
_TOPIC_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("geographical market", "market_geographical"),
    ("decision date", "eu_commisssion_decision_date"),
    ("decision year", "eu_commisssion_decision_date"),
    ("nace group", "NACE_group_code"),
    ("nace class", "NACE_class_code"),
    ("nace code", "NACE_group_code"),
    ("oecd sector", "OECD_sector"),
    ("cartel period", "cartel_period"),
    ("leniency applicant", "leniency_applicants"),
    ("infringement gravity", "infringement_gravity"),
    ("infringement category", "infringement_category"),
    ("infringement sub", "infringement_sub_category"),
    ("commission decision", "case_commission_decision"),
    ("commission no action", "case_commission_no_action"),
    ("info request", "case_commission_info_request"),
    # Stem "summar" matches summary / summarize / summarise / summaries so the
    # verb phrasings ("summarize this case") resolve like the noun ("summary").
    ("summar", "case_summary"),
    ("synopsis", "case_summary"),
    ("overview", "case_summary"),
    ("abstract", "case_summary"),
    ("recap", "case_summary"),
    ("tldr", "case_summary"),
    ("tl;dr", "case_summary"),
    ("title", "case_title"),
    ("commissioner", "case_commissioner"),
    ("nace", "NACE_group_code"),
    ("sector", "OECD_sector"),
    ("industry", "OECD_sector"),
    ("period", "cartel_period"),
    ("duration", "cartel_period"),
    ("leniency", "leniency_applicants"),
    ("applicants", "leniency_applicants"),
    ("fine", "fines"),
    ("penalty", "fines"),
    ("companies", "companies_investigated"),
    ("company", "companies_investigated"),
    ("parties", "companies_investigated"),
    ("country", "companies_country"),
    ("product", "products"),
    ("market", "market_geographical"),
    ("remedy", "decision_remedies"),
    ("remedies", "decision_remedies"),
    ("exemption", "exemption_case"),
    ("oligopoly", "market_oligopoly"),
    ("recidivism", "company_recidivism"),
    # -- Court-related synonyms --
    # NOTE: Only EXACT fact name matches and multi-word phrases point at
    # court_list_raw.  Bare keywords like "court" and "courts" are
    # deliberately OMITTED because they cause false passthrough matches
    # when the user asks about court *rulings* / *assessments* (which
    # require document-chunk search, not the court list table).
    ("court_list_raw", "court_list_raw"),
    ("court list raw", "court_list_raw"),
    ("court_list", "court_list"),
    ("court list", "court_list"),
    ("court involvement", "is_court_involved"),
    ("court involved", "is_court_involved"),
    ("list courts", "court_list_raw"),
    ("list of courts", "court_list_raw"),
    # -- Law / infringement synonyms --
    ("law article", "law_article_all_relevant"),
    ("article", "law_article_all_relevant"),
    ("abuse of dominance", "infringement_category_abuse_of_dominance"),
    ("horizontal restraint", "infringement_subcategory_horizontal_restraints"),
    ("vertical restraint", "infringement_subcategory_vertical_restraints"),
    ("market turbulence", "market_turbulence"),
    ("legal assessment", "legal_assessment_period"),
    ("heideltime event", "heideltime_event_agg"),
    ("timeline event", "heideltime_event_agg"),
)


# Suffixes stripped by ``_stem_search_terms``, longest first so the most
# specific stem is produced before broader ones ("breweries" yields
# "brew" via 'eries' before "brewer" via 'ies').
_STEM_SUFFIXES: tuple[str, ...] = (
    "eries",
    "ery",
    "iers",
    "ies",
    "ing",
    "ers",
    "er",
    "s",
)

# Stems shorter than this are too ambiguous for an ILIKE '%stem%' retry.
_MIN_STEM_LEN = 4


# ---------------------------------------------------------------------------
# Evidence / dedup helpers
# ---------------------------------------------------------------------------


def _stem_search_terms(term: str) -> list[str]:
    """Return progressively relaxed stem candidates for an ILIKE search term.

    Dependency-free suffix stripper used when an ``ILIKE '%term%'`` filter
    returned zero rows — typically a vocabulary mismatch such as the user's
    'brewery' versus the stored 'brewing'.  Stripping common English suffixes
    yields broader substrings ('brewery' -> 'brew') that match all inflections.

    Args:
        term: The original search term (without the surrounding ``%`` wildcards).

    Returns:
        Unique stem candidates ordered longest (least aggressive) first,
        excluding the original term.  Stems shorter than ``_MIN_STEM_LEN``
        are dropped; an empty list means no viable relaxation exists.
    """
    base = (term or "").strip().lower()
    stems: list[str] = []
    for suffix in _STEM_SUFFIXES:
        if base.endswith(suffix):
            stem = base[: -len(suffix)]
            if len(stem) >= _MIN_STEM_LEN and stem != base and stem not in stems:
                stems.append(stem)
    stems.sort(key=len, reverse=True)
    return stems


def _is_empty_evidence(evidence: str) -> bool:
    """Return True when *evidence* is an empty / error marker from a tool."""
    return any(marker in evidence for marker in _EMPTY_EVIDENCE_MARKERS)


def _extend_unique(target: list[str], incoming: list[str]) -> None:
    """Append items from *incoming* to *target*, skipping values already present.

    Preserves first-seen order so accumulated ids/names across retrieval steps
    stay stable and free of duplicates.
    """
    seen = set(target)
    for item in incoming:
        if item not in seen:
            target.append(item)
            seen.add(item)


def _clean_case_title(raw: str) -> str:
    """Extract the human-readable title from a stored ``case_title`` value.

    The ``case_title`` fact is persisted as a stringified ``(case_id, title)``
    tuple, e.g. ``"('COMP/B-2/37.766', 'Dutch beer market')"``.  Returning the
    raw string confuses the synthesizer, which then drops it as a malformed
    title.  This helper returns just the title element.

    Args:
        raw: The raw ``value_md`` string from the ``case_title`` fact.

    Returns:
        The title element when *raw* parses as a 2-item ``(case_id, title)``
        tuple/list; otherwise the stripped raw string (best-effort fallback).
        Returns the empty string for missing values or a literal ``"NA"``
        placeholder, so callers omit the title rather than printing ``NA``.
    """
    stripped = (raw or "").strip()
    if not stripped:
        return ""
    try:
        parsed = ast.literal_eval(stripped)
    except (ValueError, SyntaxError):
        parsed = stripped
    if isinstance(parsed, (tuple, list)) and len(parsed) == 2:
        title = str(parsed[1]).strip()
    else:
        title = stripped
    return "" if title.upper() == "NA" else title


# ---------------------------------------------------------------------------
# Direct Fact Passthrough helpers
# ---------------------------------------------------------------------------


def _normalise_fact_token(token: str) -> str:
    """Lowercase, strip whitespace, collapse underscores/hyphens/spaces.

    Used so that the user can write ``'heideltime event agg'`` or
    ``'heideltime-event-agg'`` and still hit ``'heideltime_event_agg'``.
    """
    return re.sub(r"[\s_-]+", "_", token.strip().lower())


def _match_fact_name(question: str, available_names: list[str]) -> str | None:
    """Try to find a single fact name that the user's question is asking about.

    Strategy (ordered by specificity):
      1. Exact token match — a normalised fact name appears literally in the
         normalised question.
      2. Synonym table — fall back to ``_resolve_topic_synonyms``; if it
         resolves to exactly one name that exists in *available_names*, use it.

    Returns the canonical fact name (as stored in the DB) or ``None``.
    """
    q_norm = _normalise_fact_token(question)

    # 1. Longest-match-first exact token scan.
    #    Sort descending by length so 'heideltime_event_agg' wins over
    #    'heideltime_event' when both exist.
    for name in sorted(available_names, key=len, reverse=True):
        if _normalise_fact_token(name) in q_norm:
            return name

    # 2. Synonym fallback — resolve question words to canonical names.
    resolved = _resolve_topic_synonyms(question)
    hits = [n for n in resolved if n in available_names]
    if len(hits) == 1:
        return hits[0]

    return None


def _looks_like_markdown(value: str) -> bool:
    """Return True when *value* contains markdown formatting.

    We look for tables, bullet lists, and section headers — the kind of
    content that a synthesizer LLM would likely corrupt or flatten.
    """
    return any(indicator in value for indicator in _MARKDOWN_INDICATORS)


def _is_direct_fact_request(question: str) -> bool:
    """Heuristic: the user is simply requesting a named fact, not asking
    a complex analytical question or a content-seeking question.

    Triggers on patterns like:
      - "heideltime_event_agg of CELEX-31969D0216"
      - "show me the fines for 38344_5856_3"
      - "case_summary of CELEX-31991D0562"

    Returns False for:
      - Analytical / aggregation questions ("did leniency increase over time?")
      - Content-seeking questions about rulings, assessments, opinions, or
        decisions from courts/tribunals — these need document-chunk search,
        not a direct fact return.
    """
    lowered = (question or "").lower()

    # Aggregation / comparison keywords — block passthrough.
    # Uses word-boundary regex for single-word entries to avoid false
    # positives (e.g. "count" matching inside "country").
    _COMPLEX_KEYWORDS = (
        "how many",
        "compare",
        "trend",
        "increase",
        "decrease",
        "which cases",
        "all cases",
        "across",
        "aggregate",
        r"\bcount\b",
        "average",
        "total",
    )

    # Content-seeking keywords — the user wants substantive content from
    # document chunks, not a structured fact table.  E.g. "what was the
    # ruling of courts" should NOT return court_list_raw.
    _CONTENT_SEEKING_KEYWORDS = (
        r"\bruling\b",
        r"\bassessment\b",
        r"\bopinion\b",
        r"\border\b",
        "decision of",
        r"\bdecided\b",
        r"\bjudgment\b",
        r"\bannul",
        r"\breview\b",
        "what did",
        "what was",
        "what were",
        "how did",
        "why did",
        r"\bexplain\b",
        r"\bdescribe\b",
        "details of",
        "details about",
    )

    for kw in _COMPLEX_KEYWORDS:
        if kw.startswith(r"\b"):
            if re.search(kw, lowered):
                return False
        elif kw in lowered:
            return False

    for kw in _CONTENT_SEEKING_KEYWORDS:
        if kw.startswith(r"\b"):
            if re.search(kw, lowered):
                return False
        elif kw in lowered:
            return False

    return True


def _extract_celex_id(text: str) -> str:
    """Return the first celex_id found in *text*, or '' if none."""
    match = _CELEX_ID_RE.search(text or "")
    return match.group(1) if match else ""


def _resolve_topic_synonyms(question: str) -> list[str]:
    """Map free-form question words to canonical fact ``name`` values.

    Args:
        question: The user's natural-language question.

    Returns:
        Deduplicated list of canonical fact names whose synonym phrases
        appear (case-insensitively) in *question*.  Returns an empty list
        when no synonym matches — callers should treat that as "fetch the
        full fact bag and let the synthesizer choose".
    """
    lowered = (question or "").lower()
    resolved: list[str] = []
    for phrase, fact_name in _TOPIC_SYNONYMS:
        if phrase in lowered and fact_name not in resolved:
            resolved.append(fact_name)
    return resolved
