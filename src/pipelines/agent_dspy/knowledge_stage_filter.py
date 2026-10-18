"""
Stage-1 structural rejects for the knowledge-extraction pipeline.

Implements the cheap, rules-only gate described in
``docs/knowledge_extraction_queue rubbish prevention.md``.

The vast majority of rows that historically ended up in
``knowledge_extraction_queue`` (and from there in ``knowledge_edges``) are
not knowledge at all — they are UI meta-tasks (follow-up suggestions, title
generation, tag generation), control commands (``set case all``, ``reset``, ``set IV/26.918``),
error templates, empty answers, null-result answers ("No occurrences found"),
or volatile corpus aggregates ("4025 chunks across 497 cases").

This module provides a single, pure-Python predicate
:func:`should_enqueue_for_knowledge` so the producer (OpenAI-compatible API)
can avoid enqueuing rubbish in the first place, and the batch consumer can
re-apply the same filter as a safety net against the existing backlog.

Exports:
    StageFilterVerdict        — pydantic verdict (kept, reason).
    should_enqueue_for_knowledge — main predicate used by callers.
    apply_stage_filter        — verbose variant returning a StageFilterVerdict.
    normalize_prompt          — dedup key for re-asked questions.
    is_meta_task_prompt       — UI-scaffolding prompt test, also used by the
        chat endpoint to reject a background call before it reaches the LLM.
"""

import logging
import re
from typing import Final

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rejection categories
# ---------------------------------------------------------------------------


_META_TASK_PROMPT_MARKERS: Final[tuple[str, ...]] = (
    "### task:",
    "follow-up question",
    "follow up question",
    "followup question",
    "generate a concise",
    "generate 1-3 broad",
    "generate broad tag",
    "generate a title",
    "generate tags",
    "suggest 3-5",
    "suggest 3 to 5",
    "suggest follow",
)

_CONTROL_COMMAND_PHRASES: Final[frozenset[str]] = frozenset(
    {
        "celex all",
        "set celex all",
        "celex reset",
        "celex null",
        "reset celex",
        "reset celex_id",
        "reset case",
        "celex_id null",
        "celex_id reset",
        "set case all",
        "set case",
        "set all",
        "reset",
        "unset",
        "clear",
    }
)

# Verbs that open a case-context command ("set IV/26.918", "unset celex").
_CONTROL_COMMAND_VERBS: Final[frozenset[str]] = frozenset(
    {"set", "unset", "reset", "clear"}
)

# Words that may follow a control verb without turning it into a question.
_CONTROL_COMMAND_FILLERS: Final[frozenset[str]] = frozenset(
    {"all", "null", "none", "celex", "celex_id", "case", "case_id", "to", "the", "id"}
)

# The retrieval router appends the sticky case as a suffix before the prompt
# reaches the enqueue sink ("reset  [context: celex_id=CELEX-31998D0273]").
_CONTEXT_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*\[context:[^\]]*\]\s*$", re.IGNORECASE
)

_NON_ANSWER_MARKERS: Final[tuple[str, ...]] = (
    "the input does not contain a question",
    "no question was provided",
    "i don't understand",
    "please provide a question",
    "case not found",
    "case identifier was not found",
    "the case identifier",
    "pipeline error",
)

_NULL_RESULT_MARKERS: Final[tuple[str, ...]] = (
    "no occurrences of",
    "no occurrences were found",
    "no results found",
    "no court references were found",
    "no references were found",
    "no chunks were found",
    "no facts were found",
)

# Aggregate-counting answers are correct but volatile: they reflect the
# current size of the corpus and will be wrong as soon as a new case is
# ingested. They are query results, not knowledge.
_CORPUS_AGGREGATE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(\d{1,3}(?:[,\s]?\d{3})*)\s+(chunks?|cases?|documents?|rows?|hits?)\b",
    re.IGNORECASE,
)

# Empty list answers (e.g. ``celex_id —`` with nothing after the dash) and
# stub bullet lists ("* — " repeated) are common error templates.
_EMPTY_LIST_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^(\s*[-*]\s*|\s*\d+\.\s*)?[^\s-]+\s*[—\-]\s*$", re.MULTILINE
)

_MIN_PROMPT_LEN: Final[int] = 6
_MIN_ANSWER_LEN: Final[int] = 20

# Typographic quotes the chat UI substitutes for ASCII ones; the same question
# typed twice must normalise to one key ("NSK" vs “NSK”).
_TYPOGRAPHIC_QUOTES: Final[dict[int, str]] = str.maketrans(
    {"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"}
)
_WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class StageFilterVerdict(BaseModel):
    """Outcome of the structural-rejects gate.

    Attributes
    ----------
    kept:
        ``True`` when the (prompt, answer) pair survives all rules and is
        eligible to enter the knowledge-extraction queue.
    reason:
        Empty when ``kept`` is ``True``; otherwise a short human-readable
        category, e.g. ``"meta_task"`` or ``"null_result"``.
    """

    kept: bool = Field(description="True when the candidate survives all rules.")
    reason: str = Field(
        default="", description="Rejection category (empty when kept=True)."
    )


# ---------------------------------------------------------------------------
# Individual rule checks
# ---------------------------------------------------------------------------


def is_meta_task_prompt(prompt: str) -> bool:
    """Return True when *prompt* is a UI scaffolding LLM call.

    These are the bulk of historical queue garbage -- title / tag / follow-up
    generation requests that should never have entered it.  The chat endpoint
    also uses this to reject such a call *before* it runs the retrieval
    pipeline: Open WebUI fires them in the background on every conversation,
    and each one otherwise costs a full plan-execute-synthesize on the DGX.

    Args:
        prompt: The user-role prompt as received.

    Returns:
        True when the prompt is UI scaffolding rather than a real question.
    """
    lowered = (prompt or "").lower()
    return any(marker in lowered for marker in _META_TASK_PROMPT_MARKERS)


def _is_control_command(prompt: str) -> bool:
    """Return True for set/reset/clear commands and single-token chit-chat.

    Anything below ``_MIN_PROMPT_LEN`` characters is treated as chit-chat:
    "ok", "nice", "yes" cannot generate reusable knowledge.  The router's
    ``[context: celex_id=...]`` suffix is removed first, so ``reset`` with a
    sticky case attached is still recognised.  A control verb followed only
    by filler words or case-ID-like tokens (containing a digit) is a command:
    "set IV/26.918", "set 32002D0180", "unset celex".  "set out the fines"
    keeps its question word and survives.
    """
    stripped = _CONTEXT_SUFFIX_RE.sub("", prompt).strip().lower()
    if not stripped:
        return True
    if len(stripped) < _MIN_PROMPT_LEN:
        return True
    if stripped in _CONTROL_COMMAND_PHRASES:
        return True
    verb, *rest = stripped.split()
    return verb in _CONTROL_COMMAND_VERBS and all(
        token in _CONTROL_COMMAND_FILLERS or any(ch.isdigit() for ch in token)
        for token in rest
    )


def _is_non_answer(answer: str) -> bool:
    """Return True when the answer is an error template or refusal.

    Captures the case-not-found banner, pipeline error template, and
    refusal text emitted when the question is malformed.
    """
    lowered = answer.lower()
    return any(marker in lowered for marker in _NON_ANSWER_MARKERS)


def _is_null_result(answer: str) -> bool:
    """Return True when the answer reports zero hits.

    "No occurrences of 'court' were found" is a correct answer to a count
    question but carries zero positive knowledge.
    """
    lowered = answer.lower()
    return any(marker in lowered for marker in _NULL_RESULT_MARKERS)


def _is_corpus_aggregate(answer: str) -> bool:
    """Return True when the answer is dominated by volatile count statistics.

    Aggregates over the corpus ("4025 chunks across 497 cases") are valid
    query results but should never be persisted as ``knowledge_items``
    because they change with every new document ingested.
    """
    matches = _CORPUS_AGGREGATE_RE.findall(answer)
    if not matches:
        return False
    # Short answer + at least one count phrase => aggregate-only response.
    return len(answer.strip()) < 400 and len(matches) >= 1


def _is_empty_or_stub(answer: str) -> bool:
    """Return True when the answer is empty or a dashed-stub list.

    Catches answers like::

        celex_id —
        38344_5856_3 —

    which arise when a downstream tool returned no value and the formatter
    still emitted the row.
    """
    stripped = answer.strip()
    if len(stripped) < _MIN_ANSWER_LEN:
        return True
    # All non-empty lines look like dashed stubs?  Probably an empty result.
    lines = [line for line in stripped.splitlines() if line.strip()]
    if not lines:
        return True
    stub_lines = sum(bool(_EMPTY_LIST_LINE_RE.match(line)) for line in lines)
    return stub_lines == len(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_prompt(prompt: str) -> str:
    """Return the dedup key of a prompt.

    Removes the router's ``[context: ...]`` suffix, maps typographic quotes to
    ASCII, lowercases, and collapses whitespace, so re-asking the same question
    maps to the same key.

    Parameters
    ----------
    prompt:
        The user prompt as received (``None`` is treated as empty).

    Returns
    -------
    str
        The normalised prompt.
    """
    stripped = _CONTEXT_SUFFIX_RE.sub("", prompt or "")
    unquoted = stripped.translate(_TYPOGRAPHIC_QUOTES)
    return _WHITESPACE_RE.sub(" ", unquoted).strip().lower()


def apply_stage_filter(
    user_prompt: str,
    agent_answer: str,
    fact_ids: list[str] | None = None,
) -> StageFilterVerdict:
    """Run all structural-reject rules and return a verdict.

    Order is intentional: cheapest checks first, prompt-side rules before
    answer-side rules so meta-tasks short-circuit immediately.  The grounding
    rule runs last: it only means something once the pair is otherwise a
    plausible knowledge candidate.

    Parameters
    ----------
    user_prompt:
        The user's original question / command.
    agent_answer:
        The agent's synthesised reply.
    fact_ids:
        Fact UUIDs the answer was retrieved from.  ``None`` skips the
        grounding rule; an empty list rejects with ``no_fact_ids``, because
        ``KnowledgeIngestModule`` grounds nuggets on facts only and would
        spend an extraction call to drop every nugget as ungrounded.

    Returns
    -------
    StageFilterVerdict
        ``kept=True`` when the candidate is eligible for knowledge ingestion;
        ``kept=False`` with a populated ``reason`` otherwise.
    """
    if user_prompt is None or agent_answer is None:
        return StageFilterVerdict(kept=False, reason="empty_input")

    if is_meta_task_prompt(user_prompt):
        return StageFilterVerdict(kept=False, reason="meta_task")

    if _is_control_command(user_prompt):
        return StageFilterVerdict(kept=False, reason="control_command")

    if _is_non_answer(agent_answer):
        return StageFilterVerdict(kept=False, reason="non_answer")

    if _is_empty_or_stub(agent_answer):
        return StageFilterVerdict(kept=False, reason="empty_or_stub")

    if _is_null_result(agent_answer):
        return StageFilterVerdict(kept=False, reason="null_result")

    if _is_corpus_aggregate(agent_answer):
        return StageFilterVerdict(kept=False, reason="corpus_aggregate")

    if fact_ids is not None and not fact_ids:
        return StageFilterVerdict(kept=False, reason="no_fact_ids")

    return StageFilterVerdict(kept=True)


def should_enqueue_for_knowledge(user_prompt: str, agent_answer: str) -> bool:
    """Boolean wrapper around :func:`apply_stage_filter`.

    Convenience for call-sites that only need the keep/drop decision and
    log the reason at WARNING level on a drop.

    Parameters
    ----------
    user_prompt:
        The user's original question / command.
    agent_answer:
        The agent's synthesised reply.

    Returns
    -------
    bool
        ``True`` when the candidate should be enqueued / processed.
    """
    verdict = apply_stage_filter(user_prompt, agent_answer)
    if not verdict.kept:
        logger.info(
            "Stage filter rejected (reason=%s, prompt=%r)",
            verdict.reason,
            (user_prompt or "")[:80],
        )
    return verdict.kept
