"""Intent-based router over the QA retrieval pipeline and enumeration strategy.

Mirrors the architecture of :mod:`query_router` but operates at the
**retrieval-pipeline** level. The existing ``QueryRouterModule`` dispatches
per-case fact-extraction calls (YAML-driven, single ``celex_id``). This
module dispatches **ad-hoc user questions** arriving at the QA retrieval
pipeline.

Routing logic (two-axis decision):

1. **Intent axis** — does the question have enumeration intent?
   Tested via the same regex patterns used by ``query_router.classify_intent``.

2. **Scope axis** — does the question reference a specific ``celex_id``?
   ``EnumerationFactModule`` requires a single case to enumerate within.
   Cross-case enumeration queries (e.g. "list all cases with leniency")
   have no single case, so they must stay on the QA pipeline which uses
   ``kb_sql`` / ``doc_search`` across the entire knowledge base.

Only when **both** axes hold does the router dispatch to
``EnumerationFactModule``. Everything else goes through the existing
Plan → Execute → Synthesize QA pipeline.

The classifier is heuristic for v1. If real queries later prove too
varied for patterns, swap ``classify_retrieval_intent`` for an LLM-backed
classifier — the rest of the module stays the same.

The decided intent is attached to the returned prediction as
``routing_decision`` so misroutes show up in logs and audit trails.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Literal

import dspy

from config.settings import settings
from pipelines.agent_dspy.conversation_context import (
    ConversationCondenserModule,
    ConversationTurn,
    latest_pinned_celex_ids,
)
from pipelines.agent_dspy.dspy_protocols import CelexValidator
from pipelines.agent_dspy.dspy_signature import CelexValidationResult
from pipelines.agent_dspy.enumeration_fact_module import EnumerationFactModule
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.master_planner_text import (
    is_insufficient_answer,
    looks_compositional,
)
from pipelines.agent_dspy.query_router import classify_intent
from pipelines.agent_dspy.retrieval_modules import (
    CelexSelectionModule,
    QARetrievalPipeline,
    RetrievalEvent,
)

if TYPE_CHECKING:
    from pipelines.agent_dspy.master_planner_module import MasterPlannerModule

logger = logging.getLogger(__name__)


RetrievalIntent = Literal["enumeration", "qa"]


# ---------------------------------------------------------------------------
# Known-entity schema table (high-quality mappings for common domain types)
# ---------------------------------------------------------------------------
# Each entry maps a substring (matched case-insensitively against the
# question) to a ``(target_fact_name, output_schema)`` pair that feeds
# ``FactExtractorParamVO``.  Longer / more-specific phrases come first.

_KNOWN_ENTITY_SCHEMAS: tuple[tuple[str, str, str], ...] = (
    # (substring, target_fact_name, output_schema)
    ("court", "court_list_raw", "court, country, jurisdiction"),
    ("fine", "fines", "company, amount"),
    ("penalt", "fines", "company, amount"),
    ("partie", "companies_investigated", "companies"),
    ("party", "companies_investigated", "companies"),
    ("compan", "companies_investigated", "companies"),
    ("undertaking", "companies_investigated", "companies"),
    ("applicant", "leniency_applicants", "applicant"),
    ("leniency", "leniency_applicants", "applicant"),
    ("remed", "decision_remedies", "remedy"),
)


# Regex to extract the entity noun from enumeration phrasings.
# Captures the first noun/noun-phrase after "list all/every/each",
# "enumerate (all)", "which", "identify all/every", etc.
_ENTITY_NOUN_RE = re.compile(
    r"(?:list\s+(?:all|every|each)\s+"
    r"|enumerate\s+(?:all\s+|the\s+)?"
    r"|all\s+"
    r"|every\s+"
    r"|which\s+"
    r"|identify\s+(?:all|every)\s+)"
    r"([a-zA-Z]+(?:\s+[a-zA-Z]+)?)",
    re.IGNORECASE,
)


def _singularise(word: str) -> str:
    """Naive English singular form — good enough for schema labels.

    Rules (order of specificity):
      - -ies  → -y   (companies → company, parties → party)
      - -ses  → -se  (cases → case)
      - -s    → drop (courts → court)

    Guards: skip words ≤ 4 chars, skip double-s endings.
    """
    if len(word) <= 4:
        return word
    low = word.lower()
    if low.endswith("ies"):
        return word[:-3] + "y"
    if low.endswith("ses"):
        return word[:-1]
    if low[-1] == "s" and low[-2] != "s":
        return word[:-1]
    return word


# ---------------------------------------------------------------------------
# Intent Classifier
# ---------------------------------------------------------------------------


def classify_retrieval_intent(
    question: str, *, has_case_reference: bool
) -> RetrievalIntent:
    """Classify a QA-pipeline question as ``"enumeration"`` or ``"qa"``.

    Pure function — no I/O, no LLM call. The enumeration path only triggers
    when **both** conditions hold:

    1. The question has enumeration intent (reuses ``query_router.classify_intent``).
    2. The question references a specific case.

    Without a case, cross-case enumeration queries (e.g. "list all cases
    with leniency") are better served by the QA pipeline's Plan→Execute→Synthesize
    which uses ``kb_sql`` / ``doc_search`` across the full knowledge base.

    Args:
        question: The user's free-form question.
        has_case_reference: Whether a case was actually resolved for this turn.
            The caller passes the gate's verdict rather than re-detecting, so a
            mistyped id can never look like a single-case enumeration.

    Returns:
        ``"enumeration"`` when both conditions hold, ``"qa"`` otherwise.
    """
    if not question:
        return "qa"

    # Axis 1: enumeration intent
    if classify_intent(question) != "enumeration":
        return "qa"

    # Axis 2: a case was resolved for this turn
    if not has_case_reference:
        return "qa"

    return "enumeration"


# ---------------------------------------------------------------------------
# Schema Inference
# ---------------------------------------------------------------------------


def infer_enumeration_schema(question: str) -> tuple[str, str]:
    """Infer ``(target_fact_name, output_schema)`` from a free-form question.

    Two-tier approach:

    1. **Known entity table** — substring match against domain-specific
       entities (courts, fines, companies, …). Returns a high-quality
       schema with the canonical fact name and structured output keys.

    2. **Generic fallback** — extracts the entity noun from the question
       phrasing itself (e.g. "list all subsidiaries" → ``"subsidiary"``).
       The schema becomes ``(noun, noun)`` so the ``EnumerationFactModule``
       still receives a meaningful schema for any entity type.

    Args:
        question: The user's natural-language question.

    Returns:
        A ``(target_fact_name, output_schema)`` tuple suitable for
        ``FactExtractorParamVO``.
    """
    lowered = (question or "").lower()

    # Tier 1: known entity table (longer substrings checked first via tuple ordering)
    for substring, fact_name, schema in _KNOWN_ENTITY_SCHEMAS:
        if substring in lowered:
            return fact_name, schema

    # Tier 2: generic noun extraction
    match = _ENTITY_NOUN_RE.search(question or "")
    if match:
        raw_noun = match.group(1).strip().split()[0]  # first word only
        singular = _singularise(raw_noun).lower()
        return singular, singular

    # Ultimate fallback — should rarely trigger given the enumeration
    # patterns that gate classify_retrieval_intent().
    return "entities", "entity"


# ---------------------------------------------------------------------------
# Enumeration → answer formatter
# ---------------------------------------------------------------------------


def format_enumeration_answer(
    items: list[dict], target_fact_name: str, celex_id: str
) -> str:
    """Convert ``FactOutput.items`` into a human-readable markdown answer.

    Produces a markdown table when items have multiple keys, or a bullet
    list for single-key items. This bridges the ``EnumerationFactModule``
    output format to the ``QARetrievalPipeline`` output contract (a plain
    ``answer`` string).
    """
    if not items:
        return (
            f"No {target_fact_name} entities were found in case `{celex_id}`."
        )

    # Determine column keys from the first item.
    keys = list(items[0].keys())

    if len(keys) == 1:
        # Single-key → bullet list
        key = keys[0]
        lines = [f"**{target_fact_name}** for `{celex_id}`:\n"]
        for item in items:
            lines.append(f"- {item.get(key, '')}")
        return "\n".join(lines)

    # Multi-key → markdown table
    header = "| " + " | ".join(keys) + " |"
    separator = "| " + " | ".join("---" for _ in keys) + " |"
    rows = []
    for item in items:
        row = "| " + " | ".join(str(item.get(k, "")) for k in keys) + " |"
        rows.append(row)

    lines = [f"**{target_fact_name}** for `{celex_id}`:\n", header, separator]
    lines.extend(rows)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Case-context-only prompt detection
# ---------------------------------------------------------------------------

# Framing words that wrap a "set the case context" command without adding a
# question (e.g. "set celex to <id>", "case <id>"). When the prompt contains
# only these plus a resolved case ID, the user is selecting context, not asking.
_CELEX_SET_FILLERS = frozenset(
    {
        "set", "celex", "celex_id", "celexid", "case", "to", "use", "for",
        "switch", "select", "context", "the", "id", "this", "please", "on",
    }
)


def _is_celex_only_prompt(question: str, candidate: str | None) -> bool:
    """Return True when the prompt only sets a case context, asking nothing.

    A prompt like ``"CELEX-31969D0216"`` or ``"set celex to 37766_610_5"`` names
    a case but poses no question. After the resolved case-ID candidate and the
    framing filler words are removed, no question content remains — so the
    orchestrator should acknowledge the context switch instead of planning,
    synthesizing, or attaching provenance.

    Args:
        question: The raw user prompt.
        candidate: The case/celex token that resolved
            (``CelexValidationResult.candidate``).

    Returns:
        ``True`` when only the case ID and framing words are present.
    """
    if not candidate:
        return False
    remainder = question.lower().replace(candidate.lower(), " ")
    meaningful = [
        token
        for raw in remainder.split()
        if (token := raw.strip(".,;:!?=\"'`()-")) and token not in _CELEX_SET_FILLERS
    ]
    return not meaningful


# ---------------------------------------------------------------------------
# Router Module
# ---------------------------------------------------------------------------


class RetrievalRouterModule(dspy.Module):
    """Dispatch each QA retrieval call to the full pipeline or enumeration.

    Parameters
    ----------
    qa_pipeline:
        ``QARetrievalPipeline`` — the existing Plan → Execute → Synthesize
        strategy for analytical, cross-case, and general questions.
    enumeration:
        ``EnumerationFactModule`` — the recall-tuned map-then-aggregate
        strategy for single-case entity enumeration.
    celex_checker:
        ``CelexValidator`` used to resolve celex_ids from the question.
        If ``None``, a default ``CelexCheckerToolkit`` is lazily created.
    master_planner:
        Optional ``MasterPlannerModule`` fallback.  When injected, a
        compositional question that the QA pipeline answers with insufficient
        evidence is escalated to the Master Planner (decompose -> retrieve ->
        join/aggregate -> verify).  ``None`` disables the fallback entirely.
    condenser:
        ``ConversationCondenserModule`` resolving a dependent follow-up
        ("list those cases") against the chat thread.  Defaults to a fresh
        instance; injectable so tests can stub the LM boundary.
    """

    def __init__(
        self,
        qa_pipeline: QARetrievalPipeline,
        enumeration: EnumerationFactModule,
        celex_checker: CelexValidator | None = None,
        master_planner: "MasterPlannerModule | None" = None,
        condenser: ConversationCondenserModule | None = None,
    ):
        super().__init__()
        self.qa_pipeline = qa_pipeline
        self.enumeration = enumeration
        self.condenser = condenser or ConversationCondenserModule()
        # Optional fallback for compositional queries the QA pipeline cannot
        # answer.  Left ``None`` in lightweight tests / when disabled.
        self.master_planner = master_planner

        if celex_checker is None:
            from tools.celex_checker import CelexCheckerToolkit

            celex_checker = CelexCheckerToolkit()
        self.celex_selector = CelexSelectionModule(celex_checker=celex_checker)

    def forward(self, question: str) -> dspy.Prediction:
        """Classify, dispatch, and tag the prediction (non-streaming contract).

        Drains :meth:`run_events` and returns the terminal prediction.

        Output contract (same shape as ``QARetrievalPipeline.forward``):
          - ``answer``:           str — the final grounded answer.
          - ``plan``:             RetrievalPlan | None.
          - ``evidence``:         str.
          - ``celex_validation``: CelexValidationResult.
          - ``routing_decision``: str — ``"enumeration"``, ``"qa"``,
            ``"qa_fallback"`` (enumeration dispatch failed), ``"case_context"``
            (prompt only set a case), or ``"invalid"`` (bad celex/case ID).
        """
        result: dspy.Prediction | None = None
        for event in self.run_events(question):
            if event.prediction is not None:
                result = event.prediction
        if result is None:  # defensive — run_events always emits a terminal event
            raise RuntimeError("run_events produced no terminal result")
        return result

    def run_events(
        self,
        question: str,
        fallback_celex_id: str | None = None,
        history: Sequence[ConversationTurn] | None = None,
    ) -> Iterator[RetrievalEvent]:
        """Stream the full retrieval flow as :class:`RetrievalEvent`s.

        This is the single orchestration core shared by the non-streaming
        :meth:`forward` and the streaming UI: gate → sticky-case promotion →
        case-context-only acknowledgement → enumeration-vs-QA dispatch.  The QA
        path is delegated to :meth:`QARetrievalPipeline.run_events` so the
        Plan → Execute → Synthesize stages and provenance have exactly one
        implementation.

        Args:
            question: The user's natural-language question.
            fallback_celex_id: A previously-resolved "active case" id (e.g. from
                conversation history).  When *question* names no case, this is
                promoted to the resolved case so follow-up questions behave like
                an inline id — fixing the class of sticky-case bugs.
            history: Completed prior turns of the chat thread, oldest first.
                A turn that depends on them ("list those cases") is rewritten
                into a standalone question and scoped to the cases the previous
                turn resolved; an unrelated turn drops them entirely.

        Yields:
            ``status`` events before each stage, and exactly one terminal
            ``result`` (or ``case_context_set``) event carrying the prediction.
        """
        # -1. Conversation stage — resolve a follow-up against the thread before
        # anything reads the question.  A self-contained turn drops the history
        # here (and costs no LLM call), so an unrelated new question is never
        # contaminated by what came before.
        pinned_celex_ids: list[str] = []
        if history and settings.CONVERSATION_HISTORY_ENABLED:
            verdict = self.condenser(question=question, turns=history).result
            if verdict.dependency == "refine_previous":
                pinned_celex_ids = latest_pinned_celex_ids(history)
                if verdict.standalone_question != question:
                    # Show the resolved reading: this is an audit-grade system,
                    # so the assumption the answer rests on must be visible.
                    yield RetrievalEvent(
                        "status",
                        "🧵 Follow-up resolved against the thread:\n"
                        f"   → {verdict.standalone_question}\n",
                    )
                    question = verdict.standalone_question
                if pinned_celex_ids:
                    yield RetrievalEvent(
                        "status",
                        f"   → scoped to {len(pinned_celex_ids)} case(s) from "
                        "the previous answer\n",
                    )

        # 0. Celex/case-ID gate — run once for every path.
        yield RetrievalEvent("status", "🔍 Validating case reference …\n")
        validation: CelexValidationResult = self.celex_selector(
            question=question
        ).result

        if validation.status == "invalid":
            logger.warning(
                "RetrievalRouter aborted: invalid celex/case-ID '%s'.",
                validation.candidate,
            )
            yield RetrievalEvent(
                "result",
                prediction=dspy.Prediction(
                    celex_validation=validation,
                    plan=None,
                    evidence="",
                    answer=validation.message,
                    references=[],
                    fact_ids=[],
                    chunk_ids=[],
                    fact_names=[],
                    celex_ids=[],
                    routing_decision="invalid",
                ),
            )
            return

        # 0a. Promote a sticky / active case so a follow-up question ("summary")
        # behaves exactly as if the id had been typed inline this turn.
        if validation.status != "valid" and fallback_celex_id:
            promoted = self.celex_selector(question=fallback_celex_id).result
            if promoted.status == "valid":
                validation = promoted
                question = f"{question}  [context: celex_id={fallback_celex_id}]"
                yield RetrievalEvent(
                    "status",
                    f"📌 Using active case `{fallback_celex_id}` "
                    "(say a new ID to switch)\n",
                )

        # 0b. Case-context-only prompt → acknowledge, do not plan or answer.
        if validation.status == "valid" and _is_celex_only_prompt(
            question, validation.candidate
        ):
            message = (
                f"📌 Case context set to `{validation.canonical_celex_id}`. "
                "Ask a question about this case to continue.\n"
            )
            yield RetrievalEvent(
                "case_context_set",
                text=message,
                prediction=dspy.Prediction(
                    celex_validation=validation,
                    plan=None,
                    evidence="",
                    answer=message,
                    references=[],
                    fact_ids=[],
                    chunk_ids=[],
                    fact_names=[],
                    celex_ids=(
                        [validation.canonical_celex_id]
                        if validation.canonical_celex_id
                        else []
                    ),
                    routing_decision="case_context",
                ),
            )
            return

        # Canonical-alias rewrite (shared with QARetrievalPipeline.run_events).
        question_for_planner = question
        if (
            validation.status == "valid"
            and validation.candidate
            and validation.candidate != validation.canonical_celex_id
            and validation.candidate in question
        ):
            question_for_planner = question.replace(
                validation.candidate, validation.canonical_celex_id
            )

        # 1. Enumeration path (single-case entity recall).
        qa_routing = "qa"
        intent = classify_retrieval_intent(
            question_for_planner,
            has_case_reference=validation.status == "valid",
        )
        if intent == "enumeration" and validation.status == "valid":
            celex_id = validation.canonical_celex_id
            target_fact_name, output_schema = infer_enumeration_schema(
                question_for_planner
            )
            logger.info(
                "RetrievalRouter enumeration: celex=%s fact=%s schema='%s'",
                celex_id,
                target_fact_name,
                output_schema,
            )
            yield RetrievalEvent(
                "status", "🔢 Enumeration intent — mapping over case chunks …\n"
            )
            yield RetrievalEvent(
                "status",
                f"   → fact=`{target_fact_name}` schema=`{output_schema}`\n",
            )
            try:
                enum_prediction = self.enumeration(
                    params=FactExtractorParamVO(
                        target_fact_name=target_fact_name,
                        question=question_for_planner,
                        celex_id=celex_id,
                        output_schema=output_schema,
                        force_override=True,  # QA is always live, never cached
                    )
                )
                items = (
                    enum_prediction.output.items if enum_prediction.output else []
                )
                answer = format_enumeration_answer(items, target_fact_name, celex_id)
                answer, references = self.qa_pipeline.attach_references(
                    answer=answer,
                    validation=validation,
                    fact_names=[target_fact_name],
                    question=question_for_planner,
                )
                if references:
                    yield RetrievalEvent(
                        "status", "🔗 Attaching source provenance …\n\n"
                    )
                yield RetrievalEvent(
                    "result",
                    prediction=dspy.Prediction(
                        celex_validation=validation,
                        plan=None,
                        evidence=f"[enumeration] {len(items)} entities extracted",
                        answer=answer,
                        references=references,
                        fact_ids=[],
                        chunk_ids=[],
                        fact_names=[target_fact_name],
                        celex_ids=[celex_id],
                        routing_decision="enumeration",
                    ),
                )
                return
            except Exception:
                logger.exception(
                    "Enumeration dispatch failed; falling through to QA pipeline."
                )
                qa_routing = "qa_fallback"

        # 2. QA path — delegate to the single QA sequencer, tag the routing.
        logger.info(
            "RetrievalRouter routed question to qa strategy: '%.80s'",
            question_for_planner,
        )
        qa_result_event: RetrievalEvent | None = None
        for event in self.qa_pipeline.run_events(
            question,
            validation=validation,
            question_for_planner=question_for_planner,
            pinned_celex_ids=pinned_celex_ids,
        ):
            if event.kind == "result" and event.prediction is not None:
                event.prediction.routing_decision = qa_routing
                # Hold the terminal result back until the fallback gate decides.
                qa_result_event = event
                continue
            yield event

        # 3. Master Planner fallback — only when the QA pipeline produced an
        # insufficient answer AND the question is compositional enough to be
        # worth the extra cost.  Simple misses pass through unchanged.
        if self._should_escalate(qa_result_event, question_for_planner):
            yield RetrievalEvent(
                "status",
                "🧭 Primary retrieval thin — escalating to the Master Planner …\n",
            )
            master_terminal: RetrievalEvent | None = None
            for m_event in self.master_planner.run_events(
                question_for_planner, validation=validation
            ):
                if m_event.kind == "result" and m_event.prediction is not None:
                    master_terminal = m_event
                    continue
                yield m_event
            if master_terminal is not None:
                yield master_terminal
                return

        # No escalation (or fallback unavailable) — emit the held QA result.
        if qa_result_event is not None:
            yield qa_result_event

    def _should_escalate(
        self, qa_result_event: RetrievalEvent | None, question_for_planner: str
    ) -> bool:
        """Return True when the Master Planner fallback should run.

        Gated on three conditions: a Master Planner is wired in, the QA
        pipeline produced a terminal answer that ``is_insufficient_answer``
        flags, and the question ``looks_compositional``.
        """
        if self.master_planner is None or qa_result_event is None:
            return False
        prediction = qa_result_event.prediction
        if prediction is None:
            return False
        return is_insufficient_answer(
            getattr(prediction, "answer", "") or "",
            getattr(prediction, "evidence", "") or "",
        ) and looks_compositional(question_for_planner)
