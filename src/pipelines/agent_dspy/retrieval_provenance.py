"""Provenance rendering — append a source trail to the synthesized answer.

Turns the fact names that grounded an answer into a user-facing ``References:``
section: resolves the question's targeted facts, narrows full case dumps to the
facts actually used, collects lineage rows, and renders chunk terminals as inline
collapsible ``<details>`` blocks with optional term highlighting.

Depends on :mod:`retrieval_text` for fact-name normalisation and the synonym
table; on :class:`~pipelines.agent_dspy.dspy_protocols.ChunkLoader` for chunk
text; and on :mod:`pipelines.rag.pgvector.lineage_repository` for lineage rows.
"""

import html
import logging
import re

from pipelines.agent_dspy.dspy_protocols import ChunkLoader, HybridSearcher
from pipelines.agent_dspy.retrieval_text import (
    _normalise_fact_token,
    _resolve_topic_synonyms,
)
from pipelines.rag.pgvector.lineage_repository import (
    FactProvenanceRow,
    LineageRepository,
)

logger = logging.getLogger(__name__)

_REFERENCE_HEADING = "References:"

# A fact drawn from the full ``case_facts`` dump earns a provenance reference
# only when at least this share of its value's content words appears in the
# answer — so references track what the answer actually used, not the whole case.
_GROUNDING_OVERLAP_MIN = 0.6
# Content word = run of >= 4 letters; ignores digits, punctuation, and the short
# structural tokens (cell separators, "la", "of") that dominate markdown tables.
_CONTENT_WORD_RE = re.compile(r"[a-z]{4,}")


def _extract_value_terms(value_md: str) -> list[str]:
    """Extract candidate highlight terms from a fact's stored value.

    Parses markdown tables and plain strings to recover cells and values worth
    highlighting in source chunks. Filters by length (>= 5 chars) and discards
    pure markdown separators.

    Args:
        value_md: Fact value, typically a markdown table or plain string.

    Returns:
        Unique terms (case-insensitive dedupe, first-seen order), or empty list.
    """
    if not value_md or not value_md.strip():
        return []

    terms: list[str] = []
    seen_lower: set[str] = set()

    # Split by line and pipe to extract table cells and line content
    for line in value_md.split("\n"):
        for cell in line.split("|"):
            part = cell.strip()
            if not part:
                continue
            # Skip pure markdown separators (e.g. "---", ":-:")
            if re.match(r"^[-:\s]+$", part):
                continue
            # Keep only substantial terms (>= 5 chars)
            if len(part) >= 5:
                part_lower = part.lower()
                if part_lower not in seen_lower:
                    seen_lower.add(part_lower)
                    terms.append(part)

    return terms


def _highlight_terms(text: str, terms: list[str]) -> str:
    """Wrap occurrences of *terms* in Markdown bold (`**text**`) syntax.

    Matches are case-insensitive and can span across whitespace/newlines in the
    source text. Terms longer than 5 chars are sorted longest-first to prevent
    shorter substrings from shadowing longer ones. Markdown bold is used for
    visual distinction and passes through Open WebUI's Markdown renderer.

    Args:
        text: Plain text chunk (not html-escaped yet).
        terms: Candidate highlight terms from _extract_value_terms().

    Returns:
        Text with terms wrapped in `**...**` (Markdown bold), or *text* unchanged
        if *terms* is empty.
    """
    if not terms:
        return text

    # Sort longest-first to prevent shorter substrings from matching first
    terms_sorted = sorted(terms, key=len, reverse=True)

    # Build regex: case-insensitive alternation of all terms.
    # \s+ allows the term to match across line breaks in the source text
    # (e.g., "Vereeniging ter\nBevordering" matches "Vereeniging ter Bevordering").
    pattern_str = r"(?i)(" + "|".join(
        re.escape(t).replace(r"\ ", r"\s+") for t in terms_sorted
    ) + r")"
    pattern = re.compile(pattern_str)

    return pattern.sub(
        lambda m: f'**{m.group()}**',
        text,
    )


def _render_chunk_details(
    label: str,
    chunk_id: str,
    chunk_loader: ChunkLoader,
    highlight_terms: list[str] | None = None,
) -> str:
    """Render a collapsed ``<details>`` block for one chunk-terminal reference.

    The provenance path (*label*) is the always-visible ``<summary>``; the
    verbatim chunk text stays hidden until the user expands it, so the source
    is embedded in place (no new browser tab) without cluttering the answer.

    Chunk text is rendered with Markdown bold (`**...**`) applied to highlighted
    terms. Open WebUI's Markdown renderer will parse the Markdown bold syntax
    and display highlighted terms as bold text, while preserving the document's
    original formatting and line breaks.

    Args:
        label: Provenance ``ancestry_path_names`` shown as the summary.
        chunk_id: Stable chunk id (``{embedding_table}.id``) to resolve.
        chunk_loader: Source resolving a chunk id to its raw text.
        highlight_terms: Optional list of terms to highlight in chunk text
            with Markdown bold (`**...**`).

    Returns:
        A block-level ``<details>`` snippet, or the empty string when the chunk
        cannot be resolved (the caller then falls back to a plain label).
        Loader failures are logged and swallowed so a provenance display error
        never breaks the answer.
    """
    try:
        chunk = chunk_loader.get_chunk_by_id(chunk_id)
    except Exception as exc:
        logger.warning("Chunk load failed for chunk_id=%s: %s", chunk_id, exc)
        return ""
    if chunk is None:
        return ""
    summary = html.escape(label)
    celex_id = html.escape(chunk.get("celex_id") or "")
    doc_pos = chunk.get("doc_pos")
    document = chunk.get("document") or ""
    if highlight_terms:
        document = _highlight_terms(document, highlight_terms)
    position = f" · doc_pos {doc_pos}" if doc_pos is not None else ""
    return (
        "<details>\n"
        f"<summary>{summary} view</summary>\n\n"
        f"{celex_id}{position}\n\n"
        f"{html.escape(document)}\n\n"
        "</details>"
    )


def _content_words(text: str) -> set[str]:
    """Return the set of lowercased content words (runs of >= 4 letters)."""
    return set(_CONTENT_WORD_RE.findall(text.lower()))


def _filter_grounded_fact_names(
    fact_names: list[str],
    answer: str,
    case_fact_values: dict[str, str],
) -> list[str]:
    """Narrow case-level fact names to those the *answer* is grounded in.

    The ``case_facts`` tool returns **every** stored fact for a case, so tracing
    all of them attaches a provenance trail for facts the answer never used.  A
    fact drawn from that full dump is kept only when a sufficient share
    (``_GROUNDING_OVERLAP_MIN``) of its value's content words appears in
    *answer*.  Fact names absent from *case_fact_values* (e.g. RAG- or
    fallback-derived) are explicitly targeted retrievals and are always kept.

    Args:
        fact_names: Retrieved fact names accumulated by the executor from the
            dispatcher's typed :class:`EvidenceResult` results.
        answer: The synthesized (or passthrough) answer text.
        case_fact_values: ``name -> value`` map of every fact for the case (the
            ``case_facts`` dump), used to judge grounding.

    Returns:
        The subset of *fact_names* grounded in *answer*, preserving order.
    """
    if not fact_names:
        return []
    answer_words = _content_words(answer)
    grounded: list[str] = []
    for name in fact_names:
        value = case_fact_values.get(name)
        if value is None:
            # Not part of the case_facts dump → targeted retrieval; always keep.
            grounded.append(name)
            continue
        value_words = _content_words(value)
        if not value_words:
            # Empty / numeric / structural-only value carries no grounding signal.
            continue
        overlap = len(value_words & answer_words) / len(value_words)
        if overlap >= _GROUNDING_OVERLAP_MIN:
            grounded.append(name)
    return grounded


def _resolve_question_fact_names(
    question: str,
    available_names: list[str],
) -> list[str]:
    """Return the fact names the *question* explicitly targets.

    Scopes the provenance trail to the fact(s) the user actually asked about
    (e.g. ``fines``), so incidental facts whose values merely appear in the
    synthesized answer (``companies_investigated``, ``companies_country``) do not attach their
    own references.  Combines an exact normalised-token scan with the synonym
    table and keeps only names that exist for the resolved case.

    Args:
        question: The user's natural-language question.
        available_names: Fact names that exist for the resolved case (the
            ``case_facts`` dump keys); the result is limited to these.

    Returns:
        Targeted fact names in first-seen order, or an empty list when the
        question names no specific fact (a broad / analytical question) — the
        caller then falls back to answer-grounding.
    """
    if not question or not available_names:
        return []
    available = set(available_names)
    targeted: list[str] = []
    q_norm = _normalise_fact_token(question)
    # 1. Exact normalised token match (longest-first so 'court_list_raw' wins
    #    over 'court_list').
    for name in sorted(available_names, key=len, reverse=True):
        if _normalise_fact_token(name) in q_norm and name not in targeted:
            targeted.append(name)
    # 2. Synonym phrases -> canonical names that exist for the case.
    for name in _resolve_topic_synonyms(question):
        if name in available and name not in targeted:
            targeted.append(name)
    return targeted


def _collect_references(
    lineage_repo: LineageRepository,
    celex_id: str,
    fact_names: list[str],
) -> list[FactProvenanceRow]:
    """Collect deduplicated provenance rows for *fact_names* within *celex_id*.

    A provenance lookup failure must never break the answer, so DB errors are
    logged and swallowed.

    Args:
        lineage_repo: Repository exposing ``get_fact_provenance``.
        celex_id: Canonical CELEX identifier of the resolved case.
        fact_names: Root fact names to trace, typically the executor's typed
            :class:`EvidenceResult` fact names.

    Returns:
        Provenance rows, deduplicated on ``ancestry_path_names`` and preserving
        traversal order.  Empty when *fact_names* is empty or no lineage exists.
    """
    if not fact_names:
        return []
    rows: list[FactProvenanceRow] = []
    seen_paths: set[str | None] = set()
    for fact_name in fact_names:
        try:
            provenance = lineage_repo.get_fact_provenance(celex_id, fact_name)

        except Exception as exc:
            logger.warning(
                "Provenance lookup failed for celex_id=%s fact=%s: %s",
                celex_id,
                fact_name,
                exc,
            )
            continue
        for row in provenance:
            if row.ancestry_path_names in seen_paths:
                continue
            seen_paths.add(row.ancestry_path_names)
            rows.append(row)
             # !! TODO: for pathes with differnt facts, append will work but multi chunks should go in a list with clickable digits
    return rows


def _format_reference_block(
    rows: list[FactProvenanceRow],
    chunk_loader: ChunkLoader | None = None,
    highlight_terms: list[str] | None = None,
) -> str:
    """Render the user-facing provenance trail for *rows*.

    ``ancestry_path_names`` is the visible label; ``depth`` is omitted.  Rows
    that terminate on a document chunk render as a collapsed ``<details>``
    section (built by :func:`_render_chunk_details`) whose summary is the
    provenance path and whose body — hidden until the user expands it — holds
    the verbatim source text.  This keeps the source in place (no new browser
    tab) without showing every chunk's full text by default.  Non-chunk
    terminals (heideltime / lei / csv) have no ``chunk_id`` and render as plain
    ``- label`` bullets.

    Args:
        rows: Provenance rows from :func:`_collect_references`.
        chunk_loader: Optional source resolving chunk ids to their raw text.
            When omitted (e.g. lightweight tests), chunk terminals render as
            plain labels with no expandable section.
        highlight_terms: Optional list of terms to highlight in chunk text
            with ``<mark>`` tags.

    Returns:
        A markdown ``References:`` section, or the empty string when *rows* is
        empty (so nothing is appended).
    """
    entries: list[str] = []
    for row in rows:
        if not row.ancestry_path_names:
            continue
        if row.chunk_id and chunk_loader is not None:
            details = _render_chunk_details(
                row.ancestry_path_names,
                row.chunk_id,
                chunk_loader,
                highlight_terms=highlight_terms,
            )
            if details:
                entries.append(details)
                continue
        entries.append(f"- {row.ancestry_path_names}")
    if not entries:
        return ""
    # Blank-line separation keeps each standalone <details> block parseable as
    # raw HTML by the chat markdown renderer.
    return _REFERENCE_HEADING + "\n\n" + "\n\n".join(entries)


def _compute_highlight_terms(
    target_names: list[str],
    case_fact_values: dict[str, str],
    answer: str,
) -> list[str]:
    """Collect highlight terms drawn from the grounded facts and present in *answer*.

    For each targeted fact, extracts candidate terms from its stored value
    (:func:`_extract_value_terms`) and keeps only those that actually appear in
    *answer* — so highlighting marks the parts of a source chunk the answer used.

    Args:
        target_names: Fact names scoped as the answer's provenance roots.
        case_fact_values: ``name -> value_md`` map for the resolved case.
        answer: The synthesized (or passthrough) answer text.

    Returns:
        Highlight terms in first-seen order (may contain duplicates across facts;
        the renderer dedupes by longest-first alternation).
    """
    answer_lower = answer.lower()
    terms: list[str] = []
    for name in target_names:
        value = case_fact_values.get(name)
        if not value:
            continue
        for term in _extract_value_terms(value):
            if term.lower() in answer_lower:
                terms.append(term)
    return terms


def _collect_fallback_references(
    celex_id: str,
    grounded_names: list[str],
    case_fact_values: dict[str, str],
    searcher: HybridSearcher,
) -> list[FactProvenanceRow]:
    """Search for source chunks when no lineage rows exist (e.g. direct facts).

    For each grounded fact, runs an in-case hybrid search using the fact's stored
    value as the query and wraps each distinct hit in a synthetic
    :class:`FactProvenanceRow` (``ancestry_path_names`` suffixed
    ``" (fallback search)"``) so the reference block can still show source text.

    Args:
        celex_id: Canonical CELEX identifier of the resolved case.
        grounded_names: Root fact names to search for.
        case_fact_values: ``name -> value_md`` map for the case.
        searcher: In-case hybrid searcher (``HybridSearcher`` protocol).

    Returns:
        Synthetic provenance rows with ``chunk_id`` set, deduplicated on chunk id;
        empty when nothing is found.  DB / search errors are logged and swallowed.
    """
    # Sentinel values that make poor search queries — booleans, short labels,
    # and date strings never appear verbatim in document chunks.
    _SKIP_VALUES = frozenset({"True", "False", "NA", "true", "false"})
    _MIN_QUERY_LEN = 10

    rows: list[FactProvenanceRow] = []
    seen_chunk_ids: set[str | None] = set()
    for fact_name in grounded_names:
        value = case_fact_values.get(fact_name)
        if not value:
            continue
        if value in _SKIP_VALUES or len(value) < _MIN_QUERY_LEN:
            logger.debug(
                "Fallback search skipped for fact=%s: value too short or boolean (%r)",
                fact_name,
                value[:20],
            )
            continue
        try:
            chunks = searcher.search(
                celex_id=celex_id, query=value, keyword="", top_k=3
            )
        except Exception as exc:
            logger.warning(
                "Fallback search failed for celex_id=%s fact=%s: %s",
                celex_id,
                fact_name,
                exc,
            )
            continue
        for chunk in chunks:
            if chunk.id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk.id)
            rows.append(
                FactProvenanceRow(
                    ancestry_path_names=f"{fact_name} (fallback search)",
                    chunk_id=chunk.id,
                    depth=1,
                )
            )
    return rows
