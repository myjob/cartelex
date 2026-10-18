"""Q&A Retrieval Pipeline — Plan → Execute → Synthesize.

Implements a two-stage architecture over two retrieval surfaces:

1. **Structured KB** — ``celex_facts.facts`` (~800 legal cases).
   Tools: ``kb_sql`` (aggregate SQL), ``case_facts`` (single-case lookup).
2. **Document RAG** — hybrid (BM25 + semantic) search over chunked documents.
   Tools: ``rag_search`` (all cases), ``rag_search_in_case`` (scoped to one case).

Architecture::

    question
      └─ PlannerModule  ──► RetrievalPlan (typed steps)
           └─ ExecutorModule ──► evidence strings per step
                └─ SynthesizerModule ──► grounded answer + citations

Design decisions
----------------
- The **Planner** classifies the question type and emits an ordered list of
  ``RetrievalStep`` objects, each routed to a specific tool.  Steps may be
  chained: ``input_from_step`` + a ``{celex_ids}`` placeholder let a dependent
  step consume the case set discovered by an earlier finder step.
- The **Executor** runs steps sequentially, binding upstream celex_ids into
  chained steps and retrying empty ILIKE filters with relaxed word stems; if
  every step yields zero results, deterministic fallbacks fire.
- The **Synthesizer** grounds the final answer in the collected evidence only.
- ``kb_sql`` is gated to **read-only SELECT** via ``sqlglot`` parsing.

All DB clients, searchers, and the ``kb_schema`` string are injected during
``__init__`` (Provisioner pattern, Rule 1 of GEMINI-DSPy.md).

The tool dispatcher and SQL gate live in :mod:`retrieval_dispatch`; provenance
rendering in :mod:`retrieval_provenance`; shared text/synonym helpers in
:mod:`retrieval_text`.
"""

import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import List, Literal

import dspy

from config.settings import settings
from pipelines.agent_dspy.dspy_protocols import (
    CelexValidator,
    ChunkLoader,
    CrossCaseAggregator,
    DocChunkSearcher,
    GraphSearcher,
    HybridSearcher,
    KBFactSearcher,
    KnowledgeSearcher,
    SQLFactStore,
)
from pipelines.agent_dspy.dspy_signature import (
    CelexValidationResult,
    QAPlannerSignature,
    QASynthesizerSignature,
    RetrievalPlan,
    RetrievalStep,
)
from pipelines.agent_dspy.knowledge_probe import KnowledgeProbe
from pipelines.agent_dspy.retrieval_dispatch import (
    KB_SCHEMA_DESCRIPTION,
    EvidenceResult,
    _ToolDispatcher,
)
from pipelines.agent_dspy.retrieval_provenance import (
    _collect_fallback_references,
    _collect_references,
    _compute_highlight_terms,
    _filter_grounded_fact_names,
    _format_reference_block,
    _resolve_question_fact_names,
)
from pipelines.agent_dspy.retrieval_text import (
    _MIN_PASSTHROUGH_LEN,
    _UNSTRUCTURED_TOPICS,
    _extend_unique,
    _extract_celex_id,
    _is_direct_fact_request,
    _is_empty_evidence,
    _looks_like_markdown,
    _match_fact_name,
    _resolve_topic_synonyms,
    _stem_search_terms,
)
from pipelines.rag.pgvector.lineage_repository import (
    FactProvenanceRow,
    LineageRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class RetrievalEvent:
    """A single step emitted by an event-yielding retrieval run.

    Lets callers stream per-stage progress (``status``) and consume the final
    answer (``result``) from one generator, so a streaming UI and a one-shot
    ``forward()`` share the exact same orchestration.

    Attributes:
        kind: ``"status"`` (human-readable progress line), ``"case_context_set"``
            (the prompt only selected a case — acknowledge, do not answer), or
            ``"result"`` (terminal; ``prediction`` holds the full output).
        text: Display text for ``status`` / ``case_context_set`` events.
        prediction: The terminal :class:`dspy.Prediction` for ``result`` /
            ``case_context_set`` events; ``None`` for ``status`` events.
    """

    kind: Literal["status", "case_context_set", "result"]
    text: str = ""
    prediction: dspy.Prediction | None = None


# ---------------------------------------------------------------------------
# Post-plan injection — deterministic case_facts step for the SINGLE-CASE RULE
# ---------------------------------------------------------------------------


def _needs_case_facts_step(question: str, plan: RetrievalPlan) -> str:
    """Return the celex_id named in *question* when *plan* lacks a matching
    ``case_facts`` step for it, or ``''`` otherwise.

    Backs the SINGLE-CASE RULE (:class:`QAPlannerSignature` rule 2): whenever
    the question names a specific celex_id, the plan must include a
    ``case_facts`` lookup for it, regardless of which tool the LLM planner
    chose on a given run.
    """
    celex_id = _extract_celex_id(question)
    if not celex_id:
        return ""
    has_case_facts = any(
        s.tool == "case_facts" and s.case_id == celex_id for s in plan.steps
    )
    return "" if has_case_facts else celex_id


def _inject_case_facts_step(question: str, plan: RetrievalPlan) -> RetrievalPlan:
    """Deterministically prepend a ``case_facts`` step for a named celex_id.

    When :func:`_needs_case_facts_step` fires, this function inserts a
    ``case_facts`` step at the front of the plan (renumbering the remaining
    steps and their ``input_from_step`` references) so the full fact bag for
    the named case is always fetched, independent of the LLM planner's tool
    choice on a given run.

    The plan is **mutated in-place** and returned for convenience.
    """
    celex_id = _needs_case_facts_step(question, plan)
    if not celex_id:
        return plan

    for step in plan.steps:
        step.step_id += 1
        if step.input_from_step is not None:
            step.input_from_step += 1

    new_step = RetrievalStep(
        step_id=1,
        tool="case_facts",
        query="",
        case_id=celex_id,
        rationale=(
            "Post-plan injection: SINGLE-CASE RULE — question names a "
            "celex_id, so the full fact bag is fetched first."
        ),
    )
    plan.steps.insert(0, new_step)
    logger.info(
        "Post-plan injection: added case_facts step for celex_id=%s", celex_id
    )
    return plan


# ---------------------------------------------------------------------------
# Post-plan injection — deterministic doc-chunk step for unstructured topics
# ---------------------------------------------------------------------------


def _needs_rag_followup(question: str, plan: RetrievalPlan) -> bool:
    """Return True when *question* mentions unstructured topics but *plan*
    lacks a document-chunk retrieval step.

    The structured facts schema does not cover court proceedings, tribunal
    rulings, appeals, etc. — those live only in document chunks.  When the
    LLM planner fails to include a ``rag_search_in_case`` or ``doc_search``
    step, the post-plan injection gate uses this predicate to decide whether
    to inject one.
    """
    q_lower = (question or "").lower()
    has_unstructured_topic = any(t in q_lower for t in _UNSTRUCTURED_TOPICS)
    if not has_unstructured_topic:
        return False
    has_doc_step = any(
        s.tool in ("rag_search_in_case", "doc_search") for s in plan.steps
    )
    return not has_doc_step


def _inject_rag_step(question: str, plan: RetrievalPlan) -> RetrievalPlan:
    """Deterministically inject a doc-chunk retrieval step for unstructured topics.

    When :func:`_needs_rag_followup` fires, this function appends a
    ``rag_search_in_case`` step (if a celex_id is available from an earlier
    step) or a ``doc_search`` step (cross-case fallback) to the plan.

    The plan is **mutated in-place** and returned for convenience.
    """
    if not _needs_rag_followup(question, plan):
        return plan

    q_lower = (question or "").lower()
    matched = [t for t in _UNSTRUCTURED_TOPICS if t in q_lower]
    # Use the full original question as the search query — not just the
    # topic keywords.  A multi-word query avoids the HybridSearch single-word
    # gate that skips ILIKE keyword search for terms like "court".
    search_query = question

    # Recover a celex_id from existing plan steps.
    case_id = next((s.case_id for s in plan.steps if s.case_id), "")

    if case_id:
        new_step = RetrievalStep(
            step_id=len(plan.steps) + 1,
            tool="rag_search_in_case",
            query=search_query,
            case_id=case_id,
            rationale=(
                "Post-plan injection: court/tribunal/appeal info is not in "
                "structured facts — searching document chunks."
            ),
        )
    else:
        new_step = RetrievalStep(
            step_id=len(plan.steps) + 1,
            tool="doc_search",
            query=search_query,
            rationale=(
                "Post-plan injection: court/tribunal/appeal info is not in "
                "structured facts — cross-case document search."
            ),
        )

    plan.steps.append(new_step)
    logger.info(
        "Post-plan injection: added %s step for unstructured topic(s): %s",
        new_step.tool,
        matched,
    )
    return plan


# ---------------------------------------------------------------------------
# Celex scoping — shared by intra-turn chaining and cross-turn refinement
# ---------------------------------------------------------------------------


def _scope_sql_to_celex_ids(sql: str, celex_ids: list[str]) -> str:
    """Restrict a ``kb_sql`` query to *celex_ids*.

    Substitutes the ``{celex_ids}`` placeholder with a quoted, escaped comma
    list.  When the query carries no placeholder, the whole statement is
    wrapped in an outer SELECT filtered on ``celex_id`` — robust for any inner
    query that projects ``celex_id``.

    Used for both chained plan steps (a step consuming the case ids an earlier
    step surfaced) and conversation refinement (a follow-up re-querying the
    case set the previous turn resolved).

    Args:
        sql: The planner-generated SQL.
        celex_ids: Case ids to scope the query to.

    Returns:
        The scoped SQL string, or *sql* unchanged when *celex_ids* is empty.
    """
    if not celex_ids:
        return sql
    quoted = ", ".join("'" + i.replace("'", "''") + "'" for i in celex_ids)
    if _CELEX_IDS_PLACEHOLDER in sql:
        return sql.replace(_CELEX_IDS_PLACEHOLDER, quoted)
    inner = sql.strip().rstrip(";")
    return (
        f"SELECT * FROM ({inner}) AS chained "
        f"WHERE chained.celex_id IN ({quoted})"
    )


def _scope_plan_to_celex_ids(
    plan: RetrievalPlan, celex_ids: list[str]
) -> RetrievalPlan:
    """Pin every unscoped step of *plan* to the cases a previous turn resolved.

    Used when the conversation condenser classifies a turn as
    ``refine_previous``: re-deriving the case set from the rewritten question
    would be lossy and non-deterministic, and would silently answer about
    *different* cases than the ones the user was just shown.  Scoping the plan
    instead keeps the follow-up on the same provenance chain.

    Per tool:
      - ``kb_sql`` / ``cross_case_aggregate`` — the query is wrapped by
        :func:`_scope_sql_to_celex_ids`.
      - ``case_facts`` / ``rag_search_in_case`` — already case-scoped; a step
        with no ``case_id`` takes the first pinned id, the rest are left alone.
      - other tools — untouched; they carry no celex predicate to bind.

    A step that already chains off an upstream step (``input_from_step``) is
    skipped, so intra-turn chaining always wins over the carried-over set.

    The plan is **mutated in-place** and returned for convenience.

    Args:
        plan: The plan produced for the rewritten question.
        celex_ids: Case ids pinned from the previous turn.

    Returns:
        The scoped plan.
    """
    if not celex_ids or not plan.steps:
        return plan
    capped = celex_ids[: settings.CONVERSATION_PIN_MAX_CASES]
    scoped = 0
    for step in plan.steps:
        if step.input_from_step is not None:
            continue
        if step.tool in ("kb_sql", "cross_case_aggregate"):
            step.query = _scope_sql_to_celex_ids(step.query, capped)
            scoped += 1
        elif step.tool in ("case_facts", "rag_search_in_case") and not step.case_id:
            step.case_id = capped[0]
            scoped += 1
    if scoped:
        logger.info(
            "Conversation refinement: scoped %d step(s) to %d pinned case(s).",
            scoped,
            len(capped),
        )
    return plan


# ---------------------------------------------------------------------------
# DSPy Modules (Rule 2 — logic in forward())
# ---------------------------------------------------------------------------


class CelexSelectionModule(dspy.Module):
    """Gate the pipeline on a valid celex/case-ID reference in the question.

    Runs as **step 0** of the QA retrieval pipeline and replaces the legacy
    Agno-based ``CelexSelectionAgent``.  The logic is deterministic — the
    injected ``CelexValidator`` both *detects* case-reference attempts and
    *resolves* them, working off the cached identifier registry rather than a
    hand-maintained pattern list — so no LLM call is needed.

    The three terminal states (see :class:`CelexValidationResult`) drive the
    pipeline:

    * ``no_celex`` — proceed with general retrieval over the facts KB.
    * ``valid``   — proceed; the canonical celex_id is surfaced so the planner
      can plug it into ``case_facts`` / ``rag_search_in_case`` steps.
    * ``invalid`` — abort and return the validator's message to the user.
      In particular ``'37766_610_5'`` resolves (valid), but the malformed
      ``'CELEX-37766_610_5'`` is rejected by the DB and trips this branch.

    Args:
        celex_checker: Any object satisfying the ``CelexValidator`` protocol;
            ``tools.celex_checker.CelexCheckerToolkit`` is the production
            implementation.  Injected via the Provisioner pattern.
    """

    def __init__(self, celex_checker: CelexValidator) -> None:
        super().__init__()
        self.celex_checker = celex_checker

    def forward(self, question: str) -> dspy.Prediction:
        """Validate the question's celex/case-ID against the database.

        Args:
            question: The user's natural-language question.

        Returns:
            ``dspy.Prediction`` with a single ``result`` attribute of type
            :class:`CelexValidationResult`.
        """
        candidates = self.extract_candidates(question or "")
        if not candidates:
            logger.info("CelexSelection: no celex/case-ID token in question.")
            return dspy.Prediction(result=CelexValidationResult(status="no_celex"))

        for cand in candidates:
            try:
                resolved = self.celex_checker.check_celex_or_case_id(cand)
            except Exception as exc:
                logger.warning("CelexValidator failed for '%s': %s", cand, exc)
                resolved = "NA"
            if resolved and resolved != "NA":
                logger.info(
                    "CelexSelection: '%s' resolved to canonical '%s'.",
                    cand,
                    resolved,
                )
                return dspy.Prediction(
                    result=CelexValidationResult(
                        status="valid",
                        candidate=cand,
                        canonical_celex_id=resolved,
                    )
                )

        first = candidates[0]
        logger.info(
            "CelexSelection: %d candidate(s) %s — none resolved in the registry.",
            len(candidates),
            candidates,
        )
        return dspy.Prediction(
            result=CelexValidationResult(
                status="invalid",
                candidate=first,
                message=self._not_found_message(first),
            )
        )

    def extract_candidates(self, question: str) -> list[str]:
        """Return ordered, deduplicated celex/case-ID candidates from *question*.

        Delegates to the validator's registry-backed detector, so the set of
        recognised identifier shapes follows the data instead of a regex that
        has to be edited whenever a new id family is ingested.

        Args:
            question: The user's natural-language question.

        Returns:
            Candidate tokens in order of appearance, without duplicates.
        """
        try:
            return self.celex_checker.find_candidates(question)
        except Exception as exc:
            # A registry read failure must not silently turn every prompt into
            # a "no case mentioned" one — log loudly and let the caller proceed
            # with general retrieval.
            logger.error("Identifier detection failed: %s", exc)
            return []

    def _not_found_message(self, candidate: str) -> str:
        """Build the user-facing rejection text for an unresolved *candidate*.

        Args:
            candidate: The token that failed to resolve.

        Returns:
            A message naming the token, plus near-miss suggestions when the
            registry holds anything close to it.
        """
        base = f"The case identifier '{candidate}' was not found in the database."
        try:
            suggestions = list(self.celex_checker.suggest(candidate))
        except Exception as exc:
            logger.warning("Suggestion lookup failed for '%s': %s", candidate, exc)
            suggestions = []

        if suggestions:
            joined = ", ".join(f"'{item}'" for item in suggestions)
            return f"{base} Did you mean {joined}?"
        return (
            f"{base} Please check the format — valid examples are "
            "'37766_610_5' (short form) or 'CELEX-31991D0562' (CELEX form)."
        )


class QAPlannerModule(dspy.Module):
    """Decompose a user question into a structured retrieval plan.

    Args:
        kb_schema: Schema description string injected via Provisioner pattern.
    """

    def __init__(self, kb_schema: str = KB_SCHEMA_DESCRIPTION) -> None:
        super().__init__()
        self.kb_schema = kb_schema
        self.plan = dspy.ChainOfThought(QAPlannerSignature)

    def forward(self, question: str) -> dspy.Prediction:
        prediction = self.plan(question=question, kb_schema=self.kb_schema)

        # Post-plan injection: enforce the SINGLE-CASE RULE — a question that
        # names a celex_id always gets a case_facts step, even when the LLM
        # planner routed it to kb_sql alone.
        prediction.plan = _inject_case_facts_step(question, prediction.plan)

        # Post-plan injection: deterministically add a doc-chunk retrieval
        # step when the question touches topics (court, tribunal, appeal …)
        # that are absent from the structured facts schema.
        prediction.plan = _inject_rag_step(question, prediction.plan)

        logger.info(
            "Plan for '%s': type=%s, %d steps",
            question[:60],
            prediction.plan.question_type,
            len(prediction.plan.steps),
        )
        return prediction


# Upper bound on per-case fan-out when a chained case_facts /
# rag_search_in_case step consumes a discovered case set.
_MAX_CHAINED_CASES = 15

# Maximum deterministic ILIKE stem-retry attempts per kb_sql step.
_MAX_STEM_RETRIES = 3

# Placeholder the planner embeds in chained kb_sql queries; the executor
# replaces it with the quoted celex_ids retrieved by the upstream step.
_CELEX_IDS_PLACEHOLDER = "{celex_ids}"

# Matches ILIKE '%term%' filters for the deterministic stem retry.
_ILIKE_PATTERN_RE = re.compile(r"ILIKE\s+'%([^%']+)%'", re.IGNORECASE)


class QAExecutorModule(dspy.Module):
    """Execute a retrieval plan step-by-step and collect evidence.

    Args:
        sql_store:          FactRepository satisfying SQLFactStore protocol.
        kb_searcher:        CelexDatabaseToolkit satisfying KBFactSearcher protocol.
        aggregator:         Object satisfying CrossCaseAggregator protocol.
        hybrid_searcher:    HybridSearch satisfying HybridSearcher protocol.
        doc_searcher:       CelexDatabaseToolkit satisfying DocChunkSearcher protocol.
        knowledge_searcher: KnowledgeRepository satisfying KnowledgeSearcher protocol.
        graph_searcher:     EntityGraphRepository satisfying GraphSearcher protocol.
        knowledge_probe:    Pre-plan question probe; when injected, knowledge
                            from a previously answered question is prepended
                            as step 0 whatever tools the plan uses.
    """

    def __init__(
        self,
        sql_store: SQLFactStore,
        kb_searcher: KBFactSearcher,
        aggregator: CrossCaseAggregator,
        hybrid_searcher: HybridSearcher,
        doc_searcher: DocChunkSearcher,
        knowledge_searcher: KnowledgeSearcher | None = None,
        graph_searcher: GraphSearcher | None = None,
        knowledge_probe: KnowledgeProbe | None = None,
    ) -> None:
        super().__init__()
        self.dispatcher = _ToolDispatcher(
            sql_store=sql_store,
            kb_searcher=kb_searcher,
            aggregator=aggregator,
            hybrid_searcher=hybrid_searcher,
            doc_searcher=doc_searcher,
            knowledge_searcher=knowledge_searcher,
            graph_searcher=graph_searcher,
        )
        self.knowledge_probe = knowledge_probe

    def forward(self, plan: RetrievalPlan, question: str = "") -> dspy.Prediction:
        """Run each plan step; if every step is empty, attempt fallbacks.

        Steps may be **chained**: when a step carries ``input_from_step`` (or a
        ``{celex_ids}`` placeholder in its query), the celex_ids retrieved by
        the referenced upstream step are bound into it before dispatch — SQL
        placeholder substitution for ``kb_sql``, per-case fan-out for
        ``case_facts`` / ``rag_search_in_case``.  A ``kb_sql`` step whose
        ILIKE filter returns no rows is retried with progressively relaxed
        word stems (vocabulary-mismatch recovery, e.g. 'brewery' vs the
        stored 'brewing').

        Fallback chain (triggered only when every plan step yields nothing):
          1. **Synonym fallback** — when a celex_id is available, fetch the
             full fact bag and filter by synonym-resolved topic.
          2. **Fact-name fallback** — when the question explicitly mentions a
             fact ``name`` that exists in the DB (e.g. ``court_list_raw``),
             run a direct ``kb_sql`` query for that fact across all cases.

        Args:
            plan: Ordered retrieval plan produced by the planner.
            question: Original user question — required for the synonym
                fallback to recover a celex_id and topic words when the
                planner's exact-match SQL returns no rows.

        Returns:
            ``dspy.Prediction`` with an ``evidence`` string composed of one
            block per executed step (optionally followed by fallback blocks),
            plus the typed ``fact_ids`` / ``chunk_ids`` / ``fact_names`` /
            ``celex_ids`` accumulated across every step and fallback
            (order-preserving dedup).  ``celex_ids`` names the cases this run
            actually touched, so the caller can pin them to the conversation
            turn and scope a follow-up to the same set.
        """
        evidence_parts: List[str] = []
        fact_ids: list[str] = []
        chunk_ids: list[str] = []
        fact_names: list[str] = []
        celex_ids: list[str] = []
        any_hit = False
        step_results: dict[int, EvidenceResult] = {}
        execution_order: list[int] = []

        def _accumulate(result: EvidenceResult) -> None:
            _extend_unique(fact_ids, result.fact_ids)
            _extend_unique(chunk_ids, result.chunk_ids)
            _extend_unique(fact_names, result.fact_names)
            _extend_unique(celex_ids, result.celex_ids)

        # Step 0 — knowledge from a previously answered question.  It does not
        # count as a plan hit, so the empty-plan fallbacks still run.
        if self.knowledge_probe is not None and question:
            probe_result = self.knowledge_probe.probe(question)
            if probe_result is not None:
                _accumulate(probe_result)
                evidence_parts.append(
                    f"--- Step 0 (knowledge_match) ---\n{probe_result.text}"
                )

        for step in plan.steps:
            logger.debug(
                "Executing step %d: tool=%s query='%s'",
                step.step_id,
                step.tool,
                step.query[:80],
            )
            result = self._dispatch_step(step, step_results, execution_order)
            step_results[step.step_id] = result
            execution_order.append(step.step_id)
            if not _is_empty_evidence(result.text):
                any_hit = True
            _accumulate(result)
            evidence_parts.append(
                f"--- Step {step.step_id} ({step.tool}) ---\n{result.text}"
            )

        if not any_hit:
            fallback = self._synonym_fallback(plan=plan, question=question)
            if fallback.text:
                evidence_parts.append(fallback.text)
                _accumulate(fallback)
                any_hit = not _is_empty_evidence(fallback.text)

        # Fact-name fallback — when there's still no hit and the question
        # mentions a known fact name, query the facts table directly.
        if not any_hit:
            fact_name_fallback = self._fact_name_fallback(question=question)
            if fact_name_fallback.text:
                evidence_parts.append(fact_name_fallback.text)
                _accumulate(fact_name_fallback)

        evidence = "\n\n".join(evidence_parts)
        return dspy.Prediction(
            evidence=evidence,
            fact_ids=fact_ids,
            chunk_ids=chunk_ids,
            fact_names=fact_names,
            celex_ids=celex_ids,
        )

    def _dispatch_step(
        self,
        step: RetrievalStep,
        step_results: dict[int, EvidenceResult],
        execution_order: list[int],
    ) -> EvidenceResult:
        """Dispatch one plan step, resolving chaining and stem retries.

        Chaining triggers when ``step.input_from_step`` is set or the query
        contains the ``{celex_ids}`` placeholder.  The upstream celex_ids are
        bound into the step before dispatch; a chained step whose upstream
        produced no case ids is skipped with an explanatory evidence block
        (recognised as empty so the global fallbacks can still fire).

        Args:
            step: The plan step to execute.
            step_results: Typed results of previously executed steps, keyed
                by ``step_id``.
            execution_order: ``step_id`` values in execution order, used to
                find the nearest earlier step with case ids when the
                referenced step has none.

        Returns:
            The typed :class:`EvidenceResult` for this step.
        """
        is_chained = (
            step.input_from_step is not None
            or _CELEX_IDS_PLACEHOLDER in step.query
        )
        if is_chained:
            upstream_ids, source_id = self._resolve_upstream_celex_ids(
                step, step_results, execution_order
            )
            if not upstream_ids:
                source_label = source_id if source_id is not None else "(none)"
                logger.info(
                    "Chained step %d skipped: no upstream celex_ids.", step.step_id
                )
                return EvidenceResult(
                    text=(
                        f"[chained] Step {step.step_id} skipped — upstream "
                        f"step {source_label} returned no case ids."
                    )
                )
            logger.info(
                "Chained step %d: bound %d celex_id(s) from step %s.",
                step.step_id,
                len(upstream_ids),
                source_id,
            )
            if step.tool in ("case_facts", "rag_search_in_case"):
                return self._fan_out_chained_step(step, upstream_ids)
            if step.tool == "kb_sql":
                step = step.model_copy(
                    update={"query": _scope_sql_to_celex_ids(step.query, upstream_ids)}
                )

        result = self.dispatcher.dispatch(step)
        if step.tool == "kb_sql" and _is_empty_evidence(result.text):
            retried = self._retry_with_stemmed_ilike(step)
            if retried is not None:
                return retried
        return result

    @staticmethod
    def _resolve_upstream_celex_ids(
        step: RetrievalStep,
        step_results: dict[int, EvidenceResult],
        execution_order: list[int],
    ) -> tuple[list[str], int | None]:
        """Return the celex_ids feeding a chained step and their source step_id.

        Prefers the explicitly referenced ``input_from_step``; when that step
        produced no celex_ids (or the reference is missing), falls back to the
        nearest earlier step that surfaced any.

        Returns:
            ``(celex_ids, source_step_id)`` — an empty list with the
            referenced step_id (or ``None``) when no upstream ids exist.
        """
        ref = step.input_from_step
        if ref is not None:
            upstream = step_results.get(ref)
            if upstream is not None and upstream.celex_ids:
                return upstream.celex_ids, ref
        for step_id in reversed(execution_order):
            result = step_results[step_id]
            if result.celex_ids:
                return result.celex_ids, step_id
        return [], ref

    def _fan_out_chained_step(
        self, step: RetrievalStep, upstream_ids: list[str]
    ) -> EvidenceResult:
        """Run a case-scoped tool once per upstream celex_id (bounded fan-out).

        Args:
            step: The chained ``case_facts`` / ``rag_search_in_case`` step.
            upstream_ids: celex_ids discovered by the upstream step; only the
                first ``_MAX_CHAINED_CASES`` are dispatched.

        Returns:
            One :class:`EvidenceResult` concatenating the per-case blocks,
            with ids/names merged across all dispatched cases.
        """
        capped = upstream_ids[:_MAX_CHAINED_CASES]
        blocks: list[str] = []
        fan_fact_ids: list[str] = []
        fan_chunk_ids: list[str] = []
        fan_fact_names: list[str] = []
        for celex_id in capped:
            sub_step = step.model_copy(update={"case_id": celex_id})
            result = self.dispatcher.dispatch(sub_step)
            blocks.append(result.text)
            _extend_unique(fan_fact_ids, result.fact_ids)
            _extend_unique(fan_chunk_ids, result.chunk_ids)
            _extend_unique(fan_fact_names, result.fact_names)
        omitted = len(upstream_ids) - len(capped)
        if omitted > 0:
            blocks.append(
                f"[chained] {omitted} additional case(s) omitted "
                f"(fan-out cap {_MAX_CHAINED_CASES})."
            )
        return EvidenceResult(
            text="\n".join(blocks),
            fact_ids=fan_fact_ids,
            chunk_ids=fan_chunk_ids,
            fact_names=fan_fact_names,
            celex_ids=capped,
        )

    def _retry_with_stemmed_ilike(self, step: RetrievalStep) -> EvidenceResult | None:
        """Retry an empty kb_sql step with progressively relaxed ILIKE stems.

        Deterministic vocabulary-mismatch recovery: an ``ILIKE '%brewery%'``
        filter returns zero rows when the stored value is 'brewing'; retrying
        with the stem ``'%brew%'`` matches all inflections.  At most
        ``_MAX_STEM_RETRIES`` dispatches are attempted; the first non-empty
        result wins.

        Args:
            step: The kb_sql step whose original query returned no rows.

        Returns:
            The first non-empty :class:`EvidenceResult` (its ``text`` prefixed
            with a stem-retry note), or ``None`` when the query has no ILIKE
            pattern or every retry stayed empty.
        """
        terms = _ILIKE_PATTERN_RE.findall(step.query)
        if not terms:
            return None
        attempts = 0
        for term in dict.fromkeys(terms):
            for stem in _stem_search_terms(term):
                if attempts >= _MAX_STEM_RETRIES:
                    return None
                attempts += 1
                retried_sql = step.query.replace(f"'%{term}%'", f"'%{stem}%'")
                logger.info(
                    "kb_sql stem retry %d for step %d: '%%%s%%' -> '%%%s%%'",
                    attempts,
                    step.step_id,
                    term,
                    stem,
                )
                result = self.dispatcher.dispatch(
                    step.model_copy(update={"query": retried_sql})
                )
                if not _is_empty_evidence(result.text):
                    result.text = (
                        f"[stem-retry '%{term}%' -> '%{stem}%']\n{result.text}"
                    )
                    return result
        return None

    def _synonym_fallback(self, plan: RetrievalPlan, question: str) -> EvidenceResult:
        """Last-resort retrieval triggered when every plan step yields nothing.

        Strategy:
          1. Resolve a celex_id from the plan steps or the question text.
             Abort if neither path produces one.
          2. Issue a synthetic ``case_facts`` step to fetch the full fact
             bag for that case.
          3. Map question words to canonical fact names via the synonym
             table.  If matches are found, filter the bag down to those
             facts; otherwise return the whole bag (the synthesizer can
             still pick the relevant slice).

        Args:
            plan: The (failed) retrieval plan.
            question: The original user question.

        Returns:
            An :class:`EvidenceResult` whose ``text`` is the formatted
            ``--- Fallback (synonym ...) ---`` block and whose ids/names cover
            the surviving facts; an empty-``text`` result when no celex_id can
            be recovered.
        """
        case_id = next(
            (s.case_id for s in plan.steps if s.case_id), ""
        ) or _extract_celex_id(question)
        if not case_id:
            logger.debug("Synonym fallback skipped: no celex_id available.")
            return EvidenceResult(text="")

        fallback_step = RetrievalStep(
            step_id=0,
            tool="case_facts",
            query="synonym-fallback",
            case_id=case_id,
            rationale="All planner steps returned no evidence; fetch full fact bag.",
        )
        bag = self.dispatcher.dispatch(fallback_step)
        if _is_empty_evidence(bag.text):
            return EvidenceResult(
                text=f"--- Fallback (synonym, no facts) ---\n{bag.text}"
            )

        resolved_names = _resolve_topic_synonyms(question)
        if not resolved_names:
            logger.info(
                "Synonym fallback for celex_id=%s: no topic match, returning full bag.",
                case_id,
            )
            return EvidenceResult(
                text=f"--- Fallback (synonym, full bag) ---\n{bag.text}",
                fact_ids=bag.fact_ids,
                fact_names=bag.fact_names,
            )

        header, _, body = bag.text.partition("\n")
        kept = [
            line
            for line in body.split("\n")
            if any(line.lstrip().startswith(f"{name}:") for name in resolved_names)
        ]
        if not kept:
            logger.info(
                "Synonym fallback for celex_id=%s: resolved=%s but no matching "
                "fact lines; returning full bag.",
                case_id,
                resolved_names,
            )
            return EvidenceResult(
                text=f"--- Fallback (synonym, full bag) ---\n{bag.text}",
                fact_ids=bag.fact_ids,
                fact_names=bag.fact_names,
            )

        logger.info(
            "Synonym fallback for celex_id=%s: resolved=%s, %d matching lines.",
            case_id,
            resolved_names,
            len(kept),
        )
        # Scope the edges to the kept facts: names whose line survived the
        # filter, and the matching fact ids from the structured fact bag.
        kept_names = [
            name
            for name in resolved_names
            if any(line.lstrip().startswith(f"{name}:") for line in kept)
        ]
        kept_name_set = set(kept_names)
        kept_fact_ids = [
            fact_id
            for fact_id, name, _ in self.dispatcher.sql_store.select_facts4celex_with_ids(
                case_id
            )
            if name in kept_name_set
        ]
        text = (
            f"--- Fallback (synonym -> {', '.join(resolved_names)}) ---\n"
            f"{header}\n" + "\n".join(kept)
        )
        return EvidenceResult(
            text=text, fact_ids=kept_fact_ids, fact_names=kept_names
        )

    def _fact_name_fallback(self, question: str) -> EvidenceResult:
        """Last-resort fallback for cross-case queries mentioning a specific fact name.

        When all plan steps and the synonym fallback return nothing (typically
        because the planner generated wrong SQL), this method:
          1. Scans the question for a token that looks like a fact ``name``
             (e.g. ``court_list_raw``, ``infringement_gravity``).
          2. Runs a direct, safe ``kb_sql`` query:
             ``SELECT id, celex_id, value_md FROM celex_facts.facts
               WHERE name = '<matched>' LIMIT 500``

        Projecting ``id`` lets ``_run_kb_sql`` capture the matched rows' fact
        UUIDs as edges, so the cross-case fallback contributes ``fact_ids`` too.

        This guarantees that a query like "list from fact table court_list_raw"
        always returns data when the fact exists in the DB.

        Args:
            question: The original user question.

        Returns:
            An :class:`EvidenceResult` whose ``text`` is the formatted
            ``--- Fallback (fact_name ...) ---`` block (carrying the matched
            rows' ``fact_ids`` and the queried ``fact_name``), or an
            empty-``text`` result when no fact name is detected or no row hits.
        """
        # Extract a fact-name token from the question.  We normalise the
        # question to underscored form and match against the canonical names
        # listed in the synonym table, plus try exact matches with
        # underscore-separated tokens found in the question.
        resolved = _resolve_topic_synonyms(question)
        # Additionally, find underscore-separated tokens in the raw question
        # that look like fact names (e.g. 'court_list_raw').
        _FACT_TOKEN_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9]*(?:_[a-zA-Z0-9]+)+)\b")
        explicit_tokens = _FACT_TOKEN_RE.findall(question)
        # Merge: explicit tokens first (they are the user's literal intent),
        # then synonym-resolved names as a softer match.
        candidates = list(dict.fromkeys(explicit_tokens + resolved))
        if not candidates:
            logger.debug("Fact-name fallback skipped: no fact name candidates.")
            return EvidenceResult(text="")

        for fact_name in candidates:
            sql = (
                f"SELECT id, celex_id, value_md FROM celex_facts.facts "
                f"WHERE name = '{fact_name}' LIMIT 500"
            )
            try:
                result = self.dispatcher._run_kb_sql(sql)
            except Exception as exc:
                logger.warning("Fact-name fallback SQL failed for '%s': %s", fact_name, exc)
                continue
            if not _is_empty_evidence(result.text):
                logger.info(
                    "Fact-name fallback hit: name='%s', evidence_len=%d",
                    fact_name,
                    len(result.text),
                )
                return EvidenceResult(
                    text=f"--- Fallback (fact_name -> {fact_name}) ---\n{result.text}",
                    fact_ids=result.fact_ids,
                    fact_names=[fact_name],
                )

        logger.debug(
            "Fact-name fallback: tried %d candidates, none returned rows.",
            len(candidates),
        )
        return EvidenceResult(text="")


class QASynthesizerModule(dspy.Module):
    """Synthesize a grounded answer from collected evidence."""

    def __init__(self) -> None:
        super().__init__()
        self.synthesize = dspy.ChainOfThought(QASynthesizerSignature)

    def forward(self, question: str, evidence: str) -> dspy.Prediction:
        prediction = self.synthesize(question=question, evidence=evidence)
        return prediction


# ---------------------------------------------------------------------------
# Orchestrator — top-level module composing Plan → Execute → Synthesize
# ---------------------------------------------------------------------------


class QARetrievalPipeline(dspy.Module):
    """End-to-end Q&A pipeline over the CELEX knowledge base.

    Composes four sub-modules:
      0. **CelexSelector** — gates on a valid celex/case-ID reference; aborts
         on invalid IDs so the pipeline never reasons over the wrong case.
      1. **Planner**       — decomposes the user question into retrieval steps.
      2. **Executor**      — runs each step against the appropriate tool.
      3. **Synthesizer**   — produces a grounded answer from the evidence.

    Args:
        sql_store:          FactRepository (SQLFactStore protocol).
        kb_searcher:        CelexDatabaseToolkit (KBFactSearcher protocol).
        aggregator:         CrossCaseAggregator implementation.
        hybrid_searcher:    HybridSearch (HybridSearcher protocol).
        doc_searcher:       CelexDatabaseToolkit (DocChunkSearcher protocol).
        celex_checker:      CelexValidator implementation; defaults to
                            ``CelexCheckerToolkit()`` when omitted.
        kb_schema:          Optional schema description override.
        knowledge_searcher: KnowledgeRepository (KnowledgeSearcher protocol);
                            when injected, activates the ``knowledge_search`` tool.
        graph_searcher:     EntityGraphRepository (GraphSearcher protocol);
                            when injected, activates the ``graph_search`` tool.
        knowledge_probe:    Pre-plan question probe forwarded to the executor;
                            ``None`` keeps evidence plan-only.
    """

    def __init__(
        self,
        sql_store: SQLFactStore,
        kb_searcher: KBFactSearcher,
        aggregator: CrossCaseAggregator,
        hybrid_searcher: HybridSearcher,
        doc_searcher: DocChunkSearcher,
        celex_checker: CelexValidator | None = None,
        kb_schema: str = KB_SCHEMA_DESCRIPTION,
        lineage_repo: LineageRepository | None = None,
        chunk_loader: ChunkLoader | None = None,
        enqueue_fn: Callable[..., None] | None = None,
        knowledge_searcher: KnowledgeSearcher | None = None,
        graph_searcher: GraphSearcher | None = None,
        knowledge_probe: KnowledgeProbe | None = None,
    ) -> None:
        super().__init__()
        if celex_checker is None:
            # Lazy import keeps ``retrieval_modules`` importable in environments
            
            from tools.celex_checker import CelexCheckerToolkit

            celex_checker = CelexCheckerToolkit()
        self.celex_selector = CelexSelectionModule(celex_checker=celex_checker)
        # Retained to fetch the full case-fact set when narrowing references to
        # the facts that actually grounded an answer (see ``attach_references``).
        self.sql_store = sql_store
        self.planner = QAPlannerModule(kb_schema=kb_schema)
        self.executor = QAExecutorModule(
            sql_store=sql_store,
            kb_searcher=kb_searcher,
            aggregator=aggregator,
            hybrid_searcher=hybrid_searcher,
            doc_searcher=doc_searcher,
            knowledge_searcher=knowledge_searcher,
            graph_searcher=graph_searcher,
            knowledge_probe=knowledge_probe,
        )
        self.synthesizer = QASynthesizerModule()
        # Optional provenance source — when injected, answers are annotated with
        # a lineage trail.  Left ``None`` in lightweight tests, which simply
        # skips reference attachment.
        self.lineage_repo = lineage_repo
        # Optional chunk-text source — when injected, chunk terminals in the
        # provenance trail embed an inline collapsible ``view`` section.
        self.chunk_loader = chunk_loader
        # Optional fire-and-forget sink for the knowledge-extraction queue.
        # When injected, the Plan->Execute->Synthesize branch enqueues each
        # answer for later batch extraction.  Left ``None`` outside the live UI
        # so tests / batch callers never touch the queue.
        self.enqueue_fn = enqueue_fn

    def forward(self, question: str) -> dspy.Prediction:
        """Run the full Select → Plan → Execute → Synthesize pipeline.

        Contains a **direct fact passthrough** optimisation: when the user
        asks for a specific fact by name (e.g. ``"heideltime_event_agg of
        CELEX-31969D0216"``) and the stored ``value_md`` already contains
        markdown formatting, the pipeline returns it verbatim — bypassing
        the planner, executor, and synthesizer entirely.  This preserves
        markdown tables and lists that the synthesizer would otherwise
        flatten or corrupt.

        Passthrough conditions (all must hold):
          1. Exactly **one** celex_id was resolved by the celex gate.
          2. The question text contains a fact ``name`` token that closely
             matches one of the available fact names for that case.
          3. The retrieved ``value_md`` looks like pre-formatted markdown
             (tables, bullet lists, or section headers).
          4. The question is a simple fact request, not an analytical /
             aggregation question.

        Args:
            question: User's natural-language question.

        Returns:
            dspy.Prediction with attributes:
              - ``celex_validation``: :class:`CelexValidationResult` produced
                by the celex-selection gate (always present).
              - ``plan``:     the RetrievalPlan, or ``None`` when the gate
                aborted on an invalid celex/case ID, or when the direct
                fact passthrough fired.
              - ``evidence``: concatenated evidence from the executor, or the
                empty string on abort / passthrough.
              - ``answer``:   final grounded answer, or the gate's rejection
                message on abort, or the raw ``value_md`` on passthrough.  For a
                single resolved case, a **Reference (provenance)** trail is
                appended when lineage exists.
              - ``references``: structured :class:`FactProvenanceRow` rows
                (carrying ``chunk_id`` for later chunk display); ``[]`` when no
                lineage applies.
        """
        result: dspy.Prediction | None = None
        for event in self.run_events(question):
            if event.prediction is not None:
                result = event.prediction
        if result is None:  # defensive — run_events always emits a terminal result
            raise RuntimeError("run_events produced no terminal result")
        return result

    def run_events(
        self,
        question: str,
        *,
        validation: CelexValidationResult | None = None,
        question_for_planner: str | None = None,
        pinned_celex_ids: list[str] | None = None,
    ) -> Iterator[RetrievalEvent]:
        """Stream the Select → Plan → Execute → Synthesize pipeline as events.

        This is the single QA sequencer; :meth:`forward` drains it into a
        :class:`dspy.Prediction`, and the orchestrator delegates its QA path
        here so per-stage progress streaming and one-shot calls share one
        implementation.

        Args:
            question: The user's question; used verbatim for synthesis so the
                answer mirrors the user's wording.
            validation: Pre-computed gate result.  When ``None`` the gate is run
                here (and a "validating" status is emitted); the orchestrator
                passes its already-resolved validation to avoid re-gating.
            question_for_planner: Canonicalised question for planning / passthrough
                / provenance.  When ``None`` it is derived from *question* by
                rewriting a non-canonical alias to the canonical celex_id.
            pinned_celex_ids: Cases resolved by the previous conversation turn.
                When the turn was classified as a refinement, the plan is
                scoped to them so the follow-up answers about the same cases
                the user was just shown rather than a freshly-derived set.

        Yields:
            ``status`` events before each stage and exactly one terminal
            ``result`` event carrying the full prediction (the same field
            contract as :meth:`forward`'s previous return value).
        """
        t0 = time.perf_counter()

        # 0. Celex/case-ID gate — abort early on an unknown identifier so the
        # downstream planner is never asked to reason about the wrong case.
        if validation is None:
            yield RetrievalEvent("status", "🔍 Validating case reference …\n")
            validation = self.celex_selector(question=question).result
        if validation.status == "invalid":
            logger.warning(
                "QARetrievalPipeline aborted: invalid celex/case-ID '%s'.",
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
                ),
            )
            return

        # If the user typed a non-canonical alias (e.g. 'IV/35814' resolving to
        # 'CELEX-31969D0216'), rewrite the question so the planner sees the
        # canonical id its rules are tuned for.
        if question_for_planner is None:
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

        # 0b. Direct fact passthrough — skip Plan → Execute → Synthesize when
        # the user is simply requesting a named fact whose value_md is already
        # well-formatted markdown.
        passthrough = self.try_direct_fact_passthrough(
            question=question_for_planner,
            validation=validation,
        )
        if passthrough is not None:
            value_md, matched_name = passthrough
            yield RetrievalEvent(
                "status", "⚡ Direct fact — returning stored value …\n\n"
            )
            answer, references = self.attach_references(
                answer=value_md,
                validation=validation,
                fact_names=[matched_name],
                question=question_for_planner,
            )
            logger.info(
                "QARetrievalPipeline direct-passthrough in %.2fs",
                time.perf_counter() - t0,
            )
            yield RetrievalEvent(
                "result",
                prediction=dspy.Prediction(
                    celex_validation=validation,
                    plan=None,
                    evidence="",
                    answer=answer,
                    references=references,
                    fact_ids=[],
                    chunk_ids=[],
                    fact_names=[matched_name],
                    celex_ids=(
                        [validation.canonical_celex_id]
                        if validation.canonical_celex_id
                        else []
                    ),
                ),
            )
            return

        # 1. Plan
        yield RetrievalEvent("status", "📋 Building retrieval plan …\n")
        plan_pred = self.planner(question=question_for_planner)
        plan: RetrievalPlan = plan_pred.plan

        # Conversation refinement — pin the plan to the cases the previous turn
        # resolved before any step runs (the caller only passes ids when the
        # condenser classified this turn as a refinement).
        if pinned_celex_ids:
            plan = _scope_plan_to_celex_ids(plan, pinned_celex_ids)
        tools_used = ", ".join(f"`{s.tool}`" for s in plan.steps)
        yield RetrievalEvent(
            "status", f"   → {len(plan.steps)} step(s): {tools_used}\n"
        )

        # 2. Execute — pass the (rewritten) question so the synonym fallback
        # can recover a celex_id and topic words if every step is empty.
        yield RetrievalEvent("status", "⚙️  Executing retrieval …\n")
        exec_pred = self.executor(plan=plan, question=question_for_planner)
        evidence: str = exec_pred.evidence

        # 3. Synthesize against the ORIGINAL question so the answer mirrors
        # the user's wording.
        yield RetrievalEvent("status", "✨ Synthesizing answer …\n\n")
        synth_pred = self.synthesizer(question=question, evidence=evidence)
        answer = synth_pred.answer

        # 4. Enqueue for knowledge extraction (fire-and-forget) when a sink is
        # injected.  Failures never break the answer.
        if self.enqueue_fn is not None:
            try:
                self.enqueue_fn(
                    user_prompt=question,
                    agent_answer=answer,
                    evidence=evidence,
                    supporting_fact_ids=exec_pred.fact_ids,
                    supporting_chunk_ids=exec_pred.chunk_ids,
                )
            except Exception:
                logger.warning(
                    "Failed to enqueue knowledge extraction", exc_info=True
                )

        # 5. Annotate with a provenance trail for the facts that grounded the
        # answer (single-case scope only).  The grounding fact names come
        # straight from the executor's typed accumulation — no regex re-parse
        # of the rendered evidence.
        answer, references = self.attach_references(
            answer=answer,
            validation=validation,
            fact_names=exec_pred.fact_names,
            question=question_for_planner,
        )

        # 5b. For cross-case queries attach_references produces nothing (no
        # single celex_id to trace).  When knowledge_search populated
        # chunk_ids via its edge lookup, render those directly so the user
        # still sees expandable source chunks.
        if not references and exec_pred.chunk_ids and self.chunk_loader is not None:
            from pipelines.rag.pgvector.lineage_repository import FactProvenanceRow

            extra_rows: list[FactProvenanceRow] = []
            seen: set[str] = set()
            for cid in exec_pred.chunk_ids:
                if cid in seen:
                    continue
                seen.add(cid)
                try:
                    chunk = self.chunk_loader.get_chunk_by_id(cid)
                except Exception:
                    chunk = None
                if chunk is None:
                    continue
                celex = chunk.get("celex_id") or ""
                pos = chunk.get("doc_pos")
                pos_part = f", pos={pos}" if pos is not None else ""
                label = f"knowledge source [{celex}{pos_part}]"
                extra_rows.append(
                    FactProvenanceRow(ancestry_path_names=label, chunk_id=cid, depth=1)
                )
            block = _format_reference_block(
                extra_rows, chunk_loader=self.chunk_loader
            )
            if block:
                answer = f"{answer}\n\n{block}"
            references = extra_rows

        logger.info(
            "QARetrievalPipeline completed in %.2fs", time.perf_counter() - t0
        )
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                celex_validation=validation,
                plan=plan,
                evidence=evidence,
                answer=answer,
                references=references,
                fact_ids=exec_pred.fact_ids,
                chunk_ids=exec_pred.chunk_ids,
                fact_names=exec_pred.fact_names,
                celex_ids=exec_pred.celex_ids,
            ),
        )

    # ------------------------------------------------------------------
    # Provenance references
    # ------------------------------------------------------------------

    def attach_references(
        self,
        answer: str,
        validation: CelexValidationResult,
        fact_names: list[str],
        question: str = "",
        *,
        highlight: bool = True,
        fallback: bool = True,
    ) -> tuple[str, list[FactProvenanceRow]]:
        """Append a provenance trail to *answer* for a single-case result.

        References are attached only when a lineage repository is injected and
        the celex gate resolved exactly one canonical ``celex_id``.  The
        rendered block shows only ``ancestry_path_names``; the structured rows
        (carrying ``chunk_id``) are returned for downstream chunk display.

        The traced fact names are scoped to the fact(s) the *question* explicitly
        targets (via :func:`_resolve_question_fact_names`), so an answer that
        merely mentions company / country values does not attach references for
        those incidental facts.  When the question names no specific fact (a
        broad / analytical question), the trail falls back to the facts actually
        grounding *answer* (via :func:`_filter_grounded_fact_names`), so a
        ``case_facts`` full-dump step still does not reference every case fact.

        Args:
            answer: The synthesized or passthrough answer text.
            validation: Outcome of the celex-selection gate.
            fact_names: Root fact names retrieved while answering.
            question: The user's question, used to scope references to its
                primary fact(s).
            highlight: When True, terms drawn from the grounded facts and present
                in *answer* are bolded inside the embedded source chunks.
            fallback: When True and no lineage rows exist for the targeted facts
                (e.g. direct facts with no recorded provenance), an in-case
                hybrid chunk search supplies synthetic source references.

        Returns:
            ``(answer, references)`` — *answer* with the reference block appended
            when rows were found (otherwise unchanged), and the structured rows
            (empty when nothing applies).
        """
        if (
            self.lineage_repo is None
            or validation.status != "valid"
            or not validation.canonical_celex_id
        ):
            return answer, []
        celex_id = validation.canonical_celex_id
        try:
            case_fact_values = self.sql_store.select_facts4celex(celex_id) or {}
        except Exception as exc:
            logger.warning(
                "Case-fact fetch for grounding failed (celex_id=%s): %s",
                celex_id,
                exc,
            )
            case_fact_values = {}
        primary_names = _resolve_question_fact_names(
            question, list(case_fact_values.keys())
        )
        if primary_names:
            target_names = primary_names
        else:
            target_names = _filter_grounded_fact_names(
                fact_names, answer, case_fact_values
            )
        highlight_terms = (
            _compute_highlight_terms(target_names, case_fact_values, answer)
            if highlight
            else []
        )
        references = _collect_references(self.lineage_repo, celex_id, target_names)
        # Direct facts (and other no-lineage roots) carry no recorded provenance;
        # fall back to an in-case chunk search so the answer still cites a source.
        if fallback and not references and target_names:
            references.extend(
                _collect_fallback_references(
                    celex_id,
                    target_names,
                    case_fact_values,
                    self.executor.dispatcher.hybrid_searcher,
                )
            )
        block = _format_reference_block(
            references,
            chunk_loader=self.chunk_loader,
            highlight_terms=highlight_terms,
        )
        if block:
            answer = f"{answer}\n\n{block}"
        return answer, references

    # ------------------------------------------------------------------
    # Direct fact passthrough
    # ------------------------------------------------------------------

    def try_direct_fact_passthrough(
        self,
        question: str,
        validation: CelexValidationResult,
    ) -> tuple[str, str] | None:
        """Return the raw ``value_md`` when the question is a simple named-fact
        request and the stored value is already markdown-formatted.

        Returns ``(value_markdown, matched_fact_name)`` on a hit so the caller
        can attach a provenance reference for the matched fact, or ``None`` when
        the passthrough conditions are not met — in which case the caller should
        proceed with the full Plan → Execute → Synthesize pipeline.
        """
        # Condition 1 — a single celex_id was resolved.
        if validation.status != "valid" or not validation.canonical_celex_id:
            return None

        # Condition 4 — not a complex / analytical question.
        if not _is_direct_fact_request(question):
            return None

        celex_id = validation.canonical_celex_id

        # Fetch the full fact bag (cheap — single DB round-trip).
        try:
            facts = self.executor.dispatcher.sql_store.select_facts4celex(celex_id)
        except Exception as exc:
            logger.warning("Direct-passthrough DB read failed: %s", exc)
            return None

        if not facts:
            return None

        # Condition 2 — the question text contains a token that closely
        # matches one of the available fact names.
        matched_name = _match_fact_name(question, list(facts.keys()))
        if matched_name is None:
            return None

        value = facts.get(matched_name, "")
        if not value or len(value) < _MIN_PASSTHROUGH_LEN:
            return None

        # Condition 3 — the value looks like pre-formatted markdown.
        if not _looks_like_markdown(value):
            # Even if it's not markdown, a short scalar fact (e.g. a date or
            # boolean) can still be returned directly — no point running the
            # full pipeline for "True" or "2003-12-09".
            #
            # But longer free-text values without markdown structure should
            # go through the synthesizer so the answer reads naturally.
            if len(value) > 200:
                return None

        logger.info(
            "Direct-passthrough: celex_id=%s, fact=%s, len=%d",
            celex_id,
            matched_name,
            len(value),
        )
        return f"**{matched_name}** for `{celex_id}`:\n\n{value}", matched_name
