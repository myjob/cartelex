"""Conversation-thread context for the ad-hoc QA retrieval pipeline.

Open WebUI posts the whole message thread to ``/v1/chat/completions`` on every
turn, but the backend historically consumed only the last user message (plus a
sticky celex lookup).  A dependent follow-up such as *"list those cases"* then
reached the planner with no referent for *"those"*, and an unscoped query over
the whole corpus was the honest result.

This module supplies the two halves that fix it:

1. **Condense** — :class:`ConversationCondenserModule` rewrites a dependent turn
   into a standalone question, and classifies an unrelated turn as
   ``standalone`` so the history is dropped rather than dragged along.
2. **Pin** — the case set a turn resolved is encoded into that turn's answer
   (:func:`encode_pinned_context`) and read back on the next turn
   (:func:`decode_pinned_context`), so a refinement re-queries exactly the cases
   the user was just shown instead of re-deriving a different set.

Everything here is free of FastAPI and of the provisioned pipeline, so it stays
unit-testable: importing ``ui.backend_server.oi_dspy`` builds repositories, a
reranker and the whole DSPy pipeline at module scope.

Every behaviour is gated by a ``settings.CONVERSATION_*`` switch, read at the
call site so a test can flip one with ``patch.object`` and a deployment can
disable a half via the environment.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Protocol, Sequence, runtime_checkable

import dspy
from pydantic import BaseModel, Field

from config.settings import settings
from pipelines.agent_dspy.dspy_signature import (
    CondenseFollowupSignature,
    TurnDependency,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transcript shapes
# ---------------------------------------------------------------------------


@runtime_checkable
class ChatMessageLike(Protocol):
    """Minimal shape of an OpenAI-compatible chat message.

    Structurally satisfied by the UI's ``ChatMessage`` pydantic model, so the
    transport layer can hand its parsed request straight to :func:`parse_history`
    without this module importing FastAPI.
    """

    role: str
    content: str


class ConversationTurn(BaseModel):
    """One completed exchange from the chat thread.

    Attributes:
        question: The user's prompt for this turn, verbatim.
        answer: The agent's reply with progress status lines, the provenance
            block and the source-document footer removed.
        pinned_celex_ids: Case ids this turn's retrieval actually resolved,
            recovered from the hidden marker in the raw reply.
    """

    question: str = Field(description="The user prompt that opened this turn.")
    answer: str = Field(
        default="", description="The agent's reply, stripped of UI furniture."
    )
    pinned_celex_ids: list[str] = Field(
        default_factory=list,
        description="Case ids this turn's retrieval resolved, in first-seen order.",
    )


# ---------------------------------------------------------------------------
# Pinned-case marker
# ---------------------------------------------------------------------------
# The resolved case set rides inside the assistant message that Open WebUI
# already stores and replays, rather than in a server-side store: nothing to key
# on a chat id, nothing lost on restart, no races between concurrent requests.
#
# The carrier is a markdown *link-reference definition*.  A definition nothing
# links to renders as nothing at all under CommonMark, and it contains no HTML
# for the chat client's sanitizer to strip or escape into visible text -- which
# an HTML comment would risk here, since Open WebUI has been observed escaping
# raw HTML rather than rendering it (see docs/Open WebUI.md).  Sanitizing only
# affects display: the stored message is the raw stream, so the marker still
# comes back on the next turn either way.

_PIN_MARKER_PREFIX = "[celex-context]: #"
_PIN_MARKER_RE = re.compile(
    r"^\[celex-context\]:\s*#(?P<ids>[^\n]*)$", re.IGNORECASE | re.MULTILINE
)
# Case ids are conservative tokens (CELEX-31969D0216, 37766_610_5, AT_39914).
# Anything else in the marker is ignored rather than trusted.
_PINNED_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


def encode_pinned_context(celex_ids: Sequence[str], max_cases: int | None = None) -> str:
    """Render *celex_ids* as a hidden marker to append to an agent answer.

    Args:
        celex_ids: Case ids the turn resolved, in first-seen order.
        max_cases: Cap on ids written; defaults to
            ``settings.CONVERSATION_PIN_MAX_CASES``.  A broad first answer can
            resolve hundreds of cases, and an unbounded marker would produce an
            unbounded ``IN`` list on the follow-up.

    Returns:
        A markdown link-reference definition that renders as nothing, or the
        empty string when there is nothing to pin or pinning is disabled.
    """
    if not settings.CONVERSATION_PIN_CELEX_ENABLED:
        return ""
    cap = settings.CONVERSATION_PIN_MAX_CASES if max_cases is None else max_cases
    clean = [i for i in _dedupe(celex_ids) if _PINNED_ID_RE.match(i)][:cap]
    if not clean:
        return ""
    return f"\n\n{_PIN_MARKER_PREFIX}{','.join(clean)}\n"


def decode_pinned_context(text: str) -> list[str]:
    """Recover the pinned case ids from a raw agent answer.

    Reads the **last** marker in *text*, so a transcript that somehow carries
    more than one reflects the most recent retrieval.

    Args:
        text: The raw assistant message as replayed by the chat client.

    Returns:
        The pinned case ids in written order; empty when no valid marker is
        present or pinning is disabled.
    """
    if not settings.CONVERSATION_PIN_CELEX_ENABLED or not text:
        return []
    matches = _PIN_MARKER_RE.findall(text)
    if not matches:
        return []
    raw = matches[-1]
    return [
        token
        for part in raw.split(",")
        if (token := part.strip()) and _PINNED_ID_RE.match(token)
    ]


def _dedupe(values: Iterable[str]) -> list[str]:
    """Return *values* without duplicates or blanks, preserving first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
    return ordered


# ---------------------------------------------------------------------------
# Agent-answer noise stripping
# ---------------------------------------------------------------------------
# An assistant message in the thread is not just the answer.  ``stream_generator``
# streams every progress line as content, and the answer itself carries the
# provenance block (whose <details> bodies hold whole source chunks) plus the
# MD/PDF footer.  Feeding that to the condenser would blow up the prompt and
# drown the actual answer.

# Leading glyphs of the progress lines emitted as RetrievalEvent("status", ...).
_STATUS_LINE_MARKERS: tuple[str, ...] = (
    "\U0001F50D",  # magnifying glass  -- validating case reference
    "\U0001F4CB",  # clipboard         -- building retrieval plan
    "\u2699",     # gear              -- executing retrieval
    "\u2728",     # sparkles          -- synthesizing answer
    "\U0001F517",  # link              -- attaching provenance
    "\U0001F4CC",  # pin               -- active / set case context
    "\U0001F522",  # input numbers     -- enumeration intent
    "\u26A1",     # high voltage      -- direct fact passthrough
    "\U0001F9ED",  # compass           -- master planner escalation
    "\U0001F504",  # arrows            -- context cleared
    "\U0001F9F5",  # thread            -- resolving follow-up
)
# Indented continuations of a status line ("   -> 3 step(s): `kb_sql`").
_STATUS_CONTINUATION_RE = re.compile(r"^\s+(?:\u2192|->)\s")
# The Master Planner prefixes its progress with an elapsed tag: "[12.3s] ...".
_ELAPSED_STATUS_RE = re.compile(r"^\[\d+(?:\.\d+)?s\]")
# Heading that opens the provenance block appended by _format_reference_block.
_REFERENCE_HEADING = "References:"
# Source-document footer emitted by the UI's _doc_footer.
_DOC_FOOTER_RE = re.compile(
    r"\n*---\n\[\U0001F4C4 MD\]\([^)]*\)\s*\[\U0001F4D5 PDF\]\([^)]*\)\s*$"
)


def _is_status_line(line: str) -> bool:
    """Return True when *line* is pipeline progress rather than answer content."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(_STATUS_LINE_MARKERS):
        return True
    if _ELAPSED_STATUS_RE.match(stripped):
        return True
    return bool(_STATUS_CONTINUATION_RE.match(line))


def strip_agent_noise(text: str) -> str:
    """Reduce a raw agent message to the answer prose the user actually read.

    Removes, in order: the pinned-case marker, the source-document footer, the
    ``References:`` provenance block (including the ``<details>`` bodies holding
    full source chunks), and every streamed progress line.

    Args:
        text: The raw assistant message as replayed by the chat client.

    Returns:
        The answer text alone, with surrounding blank lines collapsed.
    """
    if not text:
        return ""
    cleaned = _PIN_MARKER_RE.sub("", text)
    cleaned = _DOC_FOOTER_RE.sub("", cleaned)
    # The provenance block always terminates the answer, so cut from its heading.
    heading_at = cleaned.find(f"\n{_REFERENCE_HEADING}")
    if heading_at != -1:
        cleaned = cleaned[:heading_at]
    elif cleaned.startswith(_REFERENCE_HEADING):
        cleaned = ""
    kept = [line for line in cleaned.splitlines() if not _is_status_line(line)]
    return "\n".join(kept).strip()


# ---------------------------------------------------------------------------
# History parsing
# ---------------------------------------------------------------------------


def parse_history(
    messages: Sequence[ChatMessageLike], max_turns: int | None = None
) -> list[ConversationTurn]:
    """Pair the prior messages of a thread into completed turns.

    The final message is the turn being answered now and is therefore excluded.
    Each user message is paired with the assistant reply that followed it;
    an unanswered user message (e.g. a regeneration in flight) is skipped.

    Args:
        messages: The full OpenAI-compatible ``messages`` array for this request.
        max_turns: Keep only this many of the most recent turns; defaults to
            ``settings.CONVERSATION_HISTORY_MAX_TURNS``.

    Returns:
        Completed turns, oldest first, with answers stripped of UI furniture and
        pinned case ids recovered.  Empty when history is disabled.
    """
    if not settings.CONVERSATION_HISTORY_ENABLED or len(messages) < 2:
        return []
    limit = (
        settings.CONVERSATION_HISTORY_MAX_TURNS if max_turns is None else max_turns
    )
    if limit <= 0:
        return []

    turns: list[ConversationTurn] = []
    prior = list(messages[:-1])
    index = 0
    while index < len(prior):
        message = prior[index]
        if message.role != "user" or not (message.content or "").strip():
            index += 1
            continue
        answer_raw = ""
        cursor = index + 1
        while cursor < len(prior):
            if prior[cursor].role == "assistant":
                answer_raw = prior[cursor].content or ""
                break
            if prior[cursor].role == "user":
                break
            cursor += 1
        turns.append(
            ConversationTurn(
                question=message.content.strip(),
                answer=strip_agent_noise(answer_raw),
                pinned_celex_ids=decode_pinned_context(answer_raw),
            )
        )
        index = max(cursor, index + 1)
    return turns[-limit:]


def latest_pinned_celex_ids(turns: Sequence[ConversationTurn]) -> list[str]:
    """Return the pinned case ids of the most recent turn that resolved any.

    Scanning backwards rather than reading only the last turn means an
    intervening turn that resolved nothing (a clarification, a refusal) does not
    erase the case set the user is still working with.

    Args:
        turns: Completed turns, oldest first.

    Returns:
        The newest non-empty pinned id list, or ``[]``.
    """
    for turn in reversed(turns):
        if turn.pinned_celex_ids:
            return list(turn.pinned_celex_ids)
    return []


def render_transcript(turns: Sequence[ConversationTurn], max_answer_chars: int = 1200) -> str:
    """Render *turns* as the plain transcript the condenser reads.

    Answers are truncated because the condenser only needs enough of the prior
    reply to resolve a reference — never the full case listing.

    Args:
        turns: Completed turns, oldest first.
        max_answer_chars: Per-answer character budget.

    Returns:
        A ``User:`` / ``Agent:`` transcript, or the empty string for no turns.
    """
    blocks: list[str] = []
    for turn in turns:
        blocks.append(f"User: {turn.question}")
        if turn.answer:
            answer = turn.answer
            if len(answer) > max_answer_chars:
                answer = answer[:max_answer_chars].rstrip() + " […]"
            blocks.append(f"Agent: {answer}")
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Dependency pre-gate (pure, no LLM)
# ---------------------------------------------------------------------------
# Deciding "does this turn even need the history?" with a rule keeps the common
# case — a fresh, self-contained question — at zero extra inference cost.  It is
# deliberately biased towards saying yes: a false positive costs one LLM call
# that can still answer "standalone", while a false negative loses the feature.

_ANAPHORA_RE = re.compile(
    r"\b(?:those|these|them|they|their|its|such|same|above|former|latter|"
    r"previous|prior|earlier|aforementioned|thereof)\b",
    re.IGNORECASE,
)
# "that case", "this decision", "it" as a bare subject — demonstratives that
# only point somewhere when a noun or the conversation supplies the referent.
_DEMONSTRATIVE_RE = re.compile(
    r"\b(?:that|this)\s+(?:case|one|decision|company|cartel|fine|period|"
    r"question|answer|list|set|result)s?\b",
    re.IGNORECASE,
)
# Openers that continue an existing thought rather than starting a topic.
_CONTINUATION_RE = re.compile(
    r"^\s*(?:and|also|plus|but|what about|how about|why|why not|and then|"
    r"more detail|more details|tell me more|go on|continue|expand|elaborate|"
    r"in addition)\b",
    re.IGNORECASE,
)
# Below this length a prompt is almost never a self-contained legal question.
_TERSE_WORD_COUNT = 4


def looks_dependent(question: str) -> bool:
    """Return True when *question* plausibly refers back to the conversation.

    Pure predicate — no I/O, no LLM.  Used as a cheap pre-gate so a fresh
    question never pays for a condense call.

    Args:
        question: The latest user prompt.

    Returns:
        ``True`` when the prompt carries anaphora, a bare demonstrative, a
        continuation opener, or is too terse to stand alone.
    """
    text = (question or "").strip()
    if not text:
        return False
    if _ANAPHORA_RE.search(text) or _DEMONSTRATIVE_RE.search(text):
        return True
    if _CONTINUATION_RE.match(text):
        return True
    return len(text.split()) <= _TERSE_WORD_COUNT


# ---------------------------------------------------------------------------
# Condenser module
# ---------------------------------------------------------------------------


class ConversationCondenserModule(dspy.Module):
    """Resolve a dependent chat turn into a standalone question.

    Follows the Provisioner pattern: the predictor is injected rather than
    built inside ``forward``, so tests can supply a stub and assert that the
    cheap paths make no LLM call at all.

    Args:
        predictor: DSPy predictor over :class:`CondenseFollowupSignature`.
            Defaults to ``dspy.Predict(CondenseFollowupSignature)``.
    """

    def __init__(self, predictor: dspy.Module | None = None) -> None:
        super().__init__()
        self.condense = predictor or dspy.Predict(CondenseFollowupSignature)

    def forward(
        self, question: str, turns: Sequence[ConversationTurn] | None = None
    ) -> dspy.Prediction:
        """Classify the turn and rewrite it when it depends on the thread.

        Short-circuits — without touching the LM — when there is no history,
        when condensing is disabled, or when :func:`looks_dependent` says the
        prompt already stands on its own.  A failure in the LM path degrades to
        ``standalone`` with the original wording, so a condenser problem can
        never cost the user their answer.

        Args:
            question: The latest user prompt, verbatim.
            turns: Completed prior turns, oldest first.

        Returns:
            ``dspy.Prediction`` with a ``result`` :class:`TurnDependency`.
        """
        turns = list(turns or [])
        if not turns or not settings.CONVERSATION_CONDENSE_ENABLED:
            return self._standalone(question, reason="no usable conversation history")
        if not settings.CONVERSATION_CONDENSE_ALWAYS and not looks_dependent(question):
            return self._standalone(
                question, reason="prompt is self-contained (pre-gate)"
            )

        try:
            prediction = self.condense(
                conversation=render_transcript(turns), question=question
            )
            result: TurnDependency = prediction.result
        except Exception:
            logger.warning(
                "Follow-up condensation failed; answering the turn as-is.",
                exc_info=True,
            )
            return self._standalone(question, reason="condensation error")

        rewritten = (result.standalone_question or "").strip()
        if not rewritten:
            rewritten = question
        logger.info(
            "Condenser verdict=%s: '%.60s' -> '%.60s'",
            result.dependency,
            question,
            rewritten,
        )
        return dspy.Prediction(
            result=TurnDependency(
                dependency=result.dependency,
                standalone_question=rewritten,
                reason=result.reason,
            )
        )

    @staticmethod
    def _standalone(question: str, reason: str) -> dspy.Prediction:
        """Build a ``standalone`` verdict that leaves *question* untouched."""
        return dspy.Prediction(
            result=TurnDependency(
                dependency="standalone",
                standalone_question=question,
                reason=reason,
            )
        )
