"""Master Planner Agent - a fallback over the primary QA retrieval pipeline.

Activated by :class:`RetrievalRouterModule` only when the primary
Plan -> Execute -> Synthesize pipeline returns insufficient evidence on a
*compositional* query (multi-entity filtering or count / group-by aggregation).

Pipeline (static decompose-then-execute DAG; Rule 2 - all logic lives here):

    run_events(question, validation) -> Iterator[RetrievalEvent]
      A  Analyze        -> QueryAnalysis (entities, target_type, NA flag)
      B  Decompose      -> MasterPlan (typed sub-task DAG) | unsupported -> NA
      C  ExecuteSubtasks-> per-subtask evidence (reuses _ToolDispatcher)
      D  JoinAggregate  -> deterministic polars join / filter / group-count
      E  VerifyGate     -> keep only EvidenceVerifier-supported rows
      F  Synthesize     -> grounded answer | low-confidence -> NA

Out-of-scope queries (statistical / causal / trend inference) short-circuit to a
clean NA with a suggested offline extraction path, never a hallucination.

Provisioner pattern: DB clients, searchers, and the verifier are injected in
``__init__``; the fact-name catalog that grounds the planner is loaded once from
``FactRepository.select_distinct_fact_names`` and cached.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from typing import Any

import dspy
import polars as pl

from config.settings import settings
from pipelines.agent_dspy.dspy_signature import (
    CelexValidationResult,
    RetrievalStep,
)
from pipelines.agent_dspy.master_planner_signature import (
    AnalyzeSignature,
    DecomposeSignature,
    MasterPlan,
    MasterSynthesizerSignature,
    QueryAnalysis,
    SubTask,
)
from pipelines.agent_dspy.master_planner_text import (
    ROLE_SYNONYMS,
    build_fact_name_catalog,
    filter_known_fact_names,
    map_concept_to_fact_names,
)
from pipelines.agent_dspy.retrieval_dispatch import (
    KB_SCHEMA_DESCRIPTION,
    EvidenceResult,
    _ToolDispatcher,
    _VALID_TOOLS,
)
from pipelines.agent_dspy.retrieval_modules import RetrievalEvent

logger = logging.getLogger(__name__)

# Verdicts that count as "verified enough" to keep a row in the answer.
_KEEP_STATUSES = frozenset(
    {"verified_supported", "weak_support", "manual_approved"}
)

# Extract every celex_id mention from rendered evidence text.
_CELEX_ID_RE = re.compile(
    r"\b(CELEX-[A-Z0-9]+|AT_\d+(?:_\d+)+|\d+_\d+_\d+)\b", re.IGNORECASE
)

# Extract fact-name literals from a planner-written kb_sql query so unknown
# names are dropped before execution.  Two columns carry a fact name: ``name``
# on ``celex_facts.facts`` and ``source_fact_name`` on the graph Q&A views.
# The leading lookbehind is load-bearing: without it ``company_name = 'Bayer AG'``
# matches on its ``name = '...'`` suffix, the company is mistaken for a fact
# name, misses the catalog, and ``_subtask_to_step`` silently drops the step.
_SQL_NAME_EQ_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:source_fact_)?name\s*=\s*'([^']+)'", re.IGNORECASE
)
_SQL_NAME_IN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:source_fact_)?name\s+IN\s*\(([^)]*)\)", re.IGNORECASE
)


def _extract_all_celex_ids(text: str) -> list[str]:
    """Return every distinct celex_id mentioned in *text*, in first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _CELEX_ID_RE.finditer(text or ""):
        cid = match.group(1)
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    return ordered


def _fact_names_in_sql(query: str) -> list[str]:
    """Return the fact-name literals referenced by a kb_sql ``name`` filter."""
    names: list[str] = []
    names.extend(_SQL_NAME_EQ_RE.findall(query or ""))
    for group in _SQL_NAME_IN_RE.findall(query or ""):
        names.extend(re.findall(r"'([^']+)'", group))
    return names


class SubtaskResults:
    """Mutable accumulator for the execute stage (per-subtask + global)."""

    def __init__(self) -> None:
        self.celex_by_task: dict[int, list[str]] = {}
        self.evidence_blocks: list[str] = []
        self.chunk_ids: list[str] = []
        self.fact_ids: list[str] = []
        self.fact_names: list[str] = []

    def all_celex_ids(self) -> list[str]:
        """Union of celex_ids surfaced by every executed sub-task."""
        seen: set[str] = set()
        ordered: list[str] = []
        for ids in self.celex_by_task.values():
            for cid in ids:
                if cid not in seen:
                    seen.add(cid)
                    ordered.append(cid)
        return ordered


class MasterPlannerModule(dspy.Module):
    """Decompose -> execute -> join -> verify -> synthesize fallback agent.

    Parameters
    ----------
    sql_store:
        ``FactRepository`` - supplies ``select_distinct_fact_names`` (catalog)
        and ``select_facts4celex_with_ids`` (structured per-case rows).
    kb_searcher, aggregator, hybrid_searcher, doc_searcher, knowledge_searcher,
    graph_searcher:
        The same retrieval dependencies the primary executor uses; assembled
        into a private ``_ToolDispatcher`` so the seven tools are reused
        verbatim. ``graph_searcher`` backs ``graph_search``, the only tool that
        traverses company-to-company adjacency in the entity graph.
    verifier:
        ``EvidenceVerifierModule`` - per-row evidence gate. When ``None`` the
        verify stage passes rows through unchanged.
    dispatcher:
        Optional pre-built ``_ToolDispatcher`` (used by tests to inject a fake);
        when provided it overrides the searcher-built dispatcher.
    analyze_predictor / decompose_predictor / synthesize_predictor:
        Optional DSPy predictors (injected as fakes in tests). Default to
        ``dspy.ChainOfThought`` over the corresponding signatures.
    """

    def __init__(
        self,
        sql_store: Any,
        kb_searcher: Any = None,
        aggregator: Any = None,
        hybrid_searcher: Any = None,
        doc_searcher: Any = None,
        knowledge_searcher: Any = None,
        graph_searcher: Any = None,
        verifier: Any | None = None,
        kb_schema: str = KB_SCHEMA_DESCRIPTION,
        dispatcher: _ToolDispatcher | None = None,
        analyze_predictor: Any | None = None,
        decompose_predictor: Any | None = None,
        synthesize_predictor: Any | None = None,
    ) -> None:
        super().__init__()
        self.sql_store = sql_store
        self.verifier = verifier
        self.kb_schema = kb_schema
        self.dispatcher = dispatcher or _ToolDispatcher(
            sql_store=sql_store,
            kb_searcher=kb_searcher,
            aggregator=aggregator,
            hybrid_searcher=hybrid_searcher,
            doc_searcher=doc_searcher,
            knowledge_searcher=knowledge_searcher,
            graph_searcher=graph_searcher,
        )
        self.analyze = analyze_predictor or dspy.ChainOfThought(AnalyzeSignature)
        self.decompose = decompose_predictor or dspy.ChainOfThought(
            DecomposeSignature
        )
        self.synthesize = synthesize_predictor or dspy.ChainOfThought(
            MasterSynthesizerSignature
        )
        self._catalog: frozenset[str] | None = None

    # ------------------------------------------------------------------
    # Catalog grounding (lazy, cached)
    # ------------------------------------------------------------------

    @property
    def catalog(self) -> frozenset[str]:
        """The live distinct fact-name catalog, minus infra / scaffolding names."""
        if self._catalog is None:
            try:
                distinct = self.sql_store.select_distinct_fact_names()
            except Exception:
                logger.exception("Failed to load fact-name catalog; using empty set.")
                distinct = []
            self._catalog = build_fact_name_catalog(
                distinct, settings.EVIDENCE_EXCLUDED_FACT_NAMES
            )
        return self._catalog

    def _catalog_text(self) -> str:
        """Newline-joined catalog for the planner signatures."""
        return "\n".join(sorted(self.catalog))

    @staticmethod
    def _elapsed_prefix(start: float) -> str:
        """Return a ``[<seconds>s]`` tag for the time elapsed since *start*."""
        return f"[{time.monotonic() - start:.1f}s]"

    # ------------------------------------------------------------------
    # Public contracts
    # ------------------------------------------------------------------

    def forward(
        self, question: str, validation: CelexValidationResult | None = None
    ) -> dspy.Prediction:
        """Drain :meth:`run_events` into the terminal prediction."""
        result: dspy.Prediction | None = None
        for event in self.run_events(question, validation=validation):
            if event.prediction is not None:
                result = event.prediction
        if result is None:  # defensive - run_events always emits a terminal event
            raise RuntimeError("Master Planner produced no terminal result")
        return result

    def run_events(
        self,
        question: str,
        validation: CelexValidationResult | None = None,
    ) -> Iterator[RetrievalEvent]:
        """Stream the Master Planner stages as :class:`RetrievalEvent`s."""
        start = time.monotonic()
        validation = validation or CelexValidationResult(status="no_celex")
        catalog_text = self._catalog_text()

        # A. Analyze
        yield RetrievalEvent(
            "status",
            f"{self._elapsed_prefix(start)} Analyzing query & entities ...\n",
        )
        analysis: QueryAnalysis = self.analyze(
            question=question,
            kb_schema=self.kb_schema,
            fact_name_catalog=catalog_text,
        ).analysis

        if analysis.requires_statistics:
            yield self._na_event(
                validation,
                reason="statistical or causal inference over time",
                suggested=(
                    "Run the offline sub-agent extraction over decision-date and "
                    "leniency facts to build the time series this question needs."
                ),
            )
            return

        # B. Decompose
        yield RetrievalEvent(
            "status",
            f"{self._elapsed_prefix(start)} Decomposing into sub-tasks ...\n",
        )
        plan: MasterPlan = self.decompose(
            question=question,
            analysis=analysis,
            kb_schema=self.kb_schema,
            fact_name_catalog=catalog_text,
        ).plan

        if not plan.supported or not plan.subtasks:
            yield self._na_event(
                validation,
                reason=plan.na_reason or "a capability this agent does not support",
                suggested=plan.suggested_path,
            )
            return

        # C. Execute sub-tasks (reuse the six retrieval tools)
        subtasks = self._ordered_subtasks(plan.subtasks)
        yield RetrievalEvent(
            "status",
            f"{self._elapsed_prefix(start)} Executing {len(subtasks)} "
            "sub-task(s) over the tools ...\n",
        )
        results = SubtaskResults()
        yield from self._iter_execute_subtasks(subtasks, results, start)

        # D. Join / filter / aggregate (deterministic polars)
        yield RetrievalEvent(
            "status",
            f"{self._elapsed_prefix(start)} Joining & aggregating results ...\n",
        )
        cases_df = self._build_case_table(results.all_celex_ids())
        rows_df, group_df = self._join_aggregate(
            plan, subtasks, results, cases_df, analysis
        )

        if rows_df.is_empty() and group_df is None:
            yield self._na_event(
                validation,
                reason="no cases matched after joining the sub-task results",
                suggested="Try a narrower entity or an explicit case identifier.",
            )
            return

        # E. Verify the contributing rows
        yield RetrievalEvent(
            "status",
            f"{self._elapsed_prefix(start)} Verifying contributing facts ...\n",
        )
        rows_df = self._verify_rows(rows_df)

        # F. Synthesize
        yield RetrievalEvent(
            "status",
            f"{self._elapsed_prefix(start)} Synthesizing grounded answer ...\n\n",
        )
        joined_evidence = self._render_evidence(rows_df, group_df, results)
        synth = self.synthesize(
            question=question,
            joined_evidence=joined_evidence,
            target_type=plan.target_type,
        ).result

        answer_md = (synth.answer_md or "").strip()
        if synth.confident and answer_md:
            answer = synth.answer_md
        else:
            # The deterministic join + verify stages already confirmed supported
            # case rows (an empty result returned NA above). Do not let a hedging
            # synthesizer discard a grounded answer: fall back to a deterministic
            # case list / grouped counts built straight from the verified rows.
            answer = self._fallback_answer(plan, rows_df, group_df)
            if not answer:
                yield self._na_event(
                    validation,
                    reason="the retrieved evidence did not confidently support an answer",
                    suggested="Consider rephrasing or scoping the query to a specific case.",
                )
                return
            logger.info(
                "Master synthesizer hedged (confident=%s); using deterministic "
                "fallback over %d verified row(s).",
                synth.confident, rows_df.height,
            )

        chunk_ids = list(dict.fromkeys(results.chunk_ids))
        fact_ids = list(dict.fromkeys(results.fact_ids))
        fact_names = list(dict.fromkeys(results.fact_names))
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                celex_validation=validation,
                plan=None,
                evidence=joined_evidence,
                answer=answer,
                references=[],
                fact_ids=fact_ids,
                chunk_ids=chunk_ids,
                fact_names=fact_names,
                routing_decision="master_planner",
            ),
        )

    # ------------------------------------------------------------------
    # Stage C - execution
    # ------------------------------------------------------------------

    @staticmethod
    def _ordered_subtasks(subtasks: list[SubTask]) -> list[SubTask]:
        """Stable topological order honouring ``depends_on`` (best-effort).

        Falls back to declared order on cycles so execution always terminates.
        """
        by_id = {st.task_id: st for st in subtasks}
        ordered: list[SubTask] = []
        placed: set[int] = set()

        def _visit(task: SubTask, stack: set[int]) -> None:
            if task.task_id in placed or task.task_id in stack:
                return
            stack.add(task.task_id)
            for dep in task.depends_on:
                if dep in by_id:
                    _visit(by_id[dep], stack)
            stack.discard(task.task_id)
            if task.task_id not in placed:
                placed.add(task.task_id)
                ordered.append(task)

        for st in subtasks:
            _visit(st, set())
        return ordered

    def _execute_subtasks(self, subtasks: list[SubTask]) -> SubtaskResults:
        """Run retrieval / resolution sub-tasks via the shared dispatcher.

        Non-streaming wrapper over :meth:`_iter_execute_subtasks`; drains the
        per-sub-task status events and returns the accumulated results.
        """
        results = SubtaskResults()
        for _ in self._iter_execute_subtasks(subtasks, results, time.monotonic()):
            pass
        return results

    def _iter_execute_subtasks(
        self, subtasks: list[SubTask], results: SubtaskResults, start: float
    ) -> Iterator[RetrievalEvent]:
        """Dispatch retrieve / entity_resolve sub-tasks, streaming per-task detail.

        Yields one ``status`` event naming each sub-task's tool and query before
        dispatch, plus a follow-up line summarising what it surfaced (cases /
        chunks / facts).  Non-retrieval sub-tasks (join / filter / aggregate) and
        sub-tasks that map to no executable step are reported as deferred /
        skipped rather than silently passed over.  ``results`` is mutated in
        place so callers can both stream and read the accumulator.  ``start`` is
        the ``time.monotonic()`` reference used to tag each event with the
        seconds elapsed since the run began.
        """
        budget = settings.MASTER_PLANNER_MAX_SUBTASKS
        for st in subtasks[:budget]:
            if st.kind not in ("retrieve", "entity_resolve"):
                # join / filter / aggregate are handled deterministically later.
                yield RetrievalEvent(
                    "status",
                    f"{self._elapsed_prefix(start)}   - sub-task {st.task_id} "
                    f"({st.kind}): deferred to join/aggregate stage\n",
                )
                continue
            step = self._subtask_to_step(st)
            if step is None:
                yield RetrievalEvent(
                    "status",
                    f"{self._elapsed_prefix(start)}   - sub-task {st.task_id} "
                    f"({st.kind}): skipped (no executable step)\n",
                )
                continue
            target = (
                step.query or step.graph_seed or step.case_id or st.rationale or ""
            ).strip()
            yield RetrievalEvent(
                "status",
                f"{self._elapsed_prefix(start)}   - sub-task {st.task_id} "
                f"({step.tool}): {target[:80]}\n",
            )
            try:
                evidence: EvidenceResult = self.dispatcher.dispatch(step)
            except Exception:
                logger.exception("Master sub-task %d dispatch failed.", st.task_id)
                yield RetrievalEvent(
                    "status",
                    f"{self._elapsed_prefix(start)}     -> sub-task {st.task_id} "
                    "failed; skipping\n",
                )
                continue
            celex_ids = _extract_all_celex_ids(evidence.text)
            results.celex_by_task[st.task_id] = celex_ids
            results.evidence_blocks.append(
                f"--- Sub-task {st.task_id} ({step.tool}) ---\n{evidence.text}"
            )
            for cid in evidence.chunk_ids:
                if cid not in results.chunk_ids:
                    results.chunk_ids.append(cid)
            for fid in evidence.fact_ids:
                if fid not in results.fact_ids:
                    results.fact_ids.append(fid)
            for name in evidence.fact_names:
                if name not in results.fact_names:
                    results.fact_names.append(name)
            yield RetrievalEvent(
                "status",
                f"{self._elapsed_prefix(start)}     -> {len(celex_ids)} case(s), "
                f"{len(evidence.chunk_ids)} chunk(s), "
                f"{len(evidence.fact_ids)} fact(s)\n",
            )

    def _subtask_to_step(self, st: SubTask) -> RetrievalStep | None:
        """Map a retrieve / entity_resolve sub-task to a RetrievalStep.

        Drops kb_sql steps that reference only fact names outside the live
        catalog (logged) so unknown names are never executed blindly.
        """
        tool = st.tool or ("doc_search" if st.kind == "entity_resolve" else "")
        if tool not in _VALID_TOOLS:
            logger.warning(
                "Master sub-task %d has invalid tool '%s'; skipping.",
                st.task_id,
                st.tool,
            )
            return None

        if tool == "kb_sql":
            referenced = _fact_names_in_sql(st.query)
            if referenced:
                known, unknown = filter_known_fact_names(referenced, self.catalog)
                if unknown:
                    logger.warning(
                        "Master sub-task %d kb_sql references unknown fact names %s; "
                        "dropping them.",
                        st.task_id,
                        unknown,
                    )
                if not known:
                    logger.warning(
                        "Master sub-task %d kb_sql references no known fact names; "
                        "skipping step.",
                        st.task_id,
                    )
                    return None

        if tool == "graph_search" and not (st.graph_seed or "").strip():
            logger.warning(
                "Master sub-task %d graph_search carries no graph_seed; "
                "skipping step.",
                st.task_id,
            )
            return None

        return RetrievalStep(
            step_id=st.task_id,
            tool=tool,
            query=st.query,
            case_id=st.case_id,
            graph_seed=st.graph_seed,
            graph_relation=st.graph_relation,
            graph_hops=st.graph_hops,
            rationale=st.rationale,
        )

    # ------------------------------------------------------------------
    # Stage D - deterministic join / filter / aggregate (polars)
    # ------------------------------------------------------------------

    def _build_case_table(self, celex_ids: list[str]) -> pl.DataFrame:
        """Fetch structured facts for the candidate cases as a polars frame."""
        schema = {"celex_id": pl.Utf8, "fact_id": pl.Utf8, "fact_name": pl.Utf8, "value": pl.Utf8}
        rows: list[dict[str, str]] = []
        for celex in celex_ids[: settings.MASTER_PLANNER_MAX_CASES]:
            try:
                facts = self.sql_store.select_facts4celex_with_ids(celex)
            except Exception:
                logger.exception("Failed to load facts for %s.", celex)
                continue
            for fact_id, name, value in facts:
                rows.append(
                    {
                        "celex_id": celex,
                        "fact_id": str(fact_id),
                        "fact_name": name,
                        "value": value or "",
                    }
                )
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema)

    def _join_aggregate(
        self,
        plan: MasterPlan,
        subtasks: list[SubTask],
        results: SubtaskResults,
        cases_df: pl.DataFrame,
        analysis: QueryAnalysis | None = None,
    ) -> tuple[pl.DataFrame, pl.DataFrame | None]:
        """Apply join, role filter, and group-by aggregation deterministically.

        Returns ``(rows_df, group_df)`` where ``rows_df`` is the per-case
        surviving rows (celex_id, fact_name, value) and ``group_df`` is the
        grouped counts for a count_group answer (or ``None``).
        """
        # 1. Candidate celex set - join (intersection) when join sub-tasks exist.
        candidate_ids = self._joined_celex_ids(subtasks, results)
        rows_df = cases_df
        if candidate_ids is not None:
            rows_df = cases_df.filter(pl.col("celex_id").is_in(list(candidate_ids)))

        # 2. Role filter sub-tasks.
        evidence_by_celex = self._evidence_by_celex(
            results.evidence_blocks, rows_df["celex_id"].unique().to_list()
        )
        for st in subtasks:
            if st.kind == "filter" and st.filter_role:
                keep_ids = self._role_filter(
                    rows_df, st.filter_role, evidence_by_celex
                )
                rows_df = rows_df.filter(pl.col("celex_id").is_in(list(keep_ids)))

        # 3. Aggregation (count_group) or fact-name scoping (case_list / compare).
        group_df: pl.DataFrame | None = None
        if plan.target_type == "count_group":
            group_st = next(
                (st for st in subtasks if st.kind == "aggregate" and st.group_by),
                None,
            )
            group_by = group_st.group_by if group_st else ""
            group_df = self._aggregate(rows_df, group_by)
            # Keep the rows backing the group values for verification.
            group_fact_names = map_concept_to_fact_names(group_by, self.catalog) or [
                group_by
            ]
            rows_df = rows_df.filter(pl.col("fact_name").is_in(group_fact_names))
        else:
            # A case_list / compare answer only needs the entity / role facts
            # that establish membership - not every one of a case's ~60 facts.
            # Scope the rows so the verify + synthesize stages do not pay to
            # LLM-judge hundreds of irrelevant facts across dozens of cases.
            rows_df = self._scope_to_relevant_facts(rows_df, analysis, subtasks)

        return rows_df, group_df

    def _scope_to_relevant_facts(
        self,
        rows_df: pl.DataFrame,
        analysis: QueryAnalysis | None,
        subtasks: list[SubTask],
    ) -> pl.DataFrame:
        """Keep only fact rows relevant to the query's entities and role filter.

        Mirrors the fact-name scoping ``count_group`` already applies, so a
        ``case_list`` / ``compare`` answer verifies the handful of membership /
        role facts per case rather than all of them. Falls back to the full
        frame when no relevant fact name resolves (catalog miss), so a case is
        never silently dropped for lack of a mapped fact name.
        """
        relevant = self._relevant_fact_names(analysis, subtasks)
        if not relevant:
            return rows_df
        scoped = rows_df.filter(pl.col("fact_name").is_in(relevant))
        return scoped if not scoped.is_empty() else rows_df

    def _relevant_fact_names(
        self, analysis: QueryAnalysis | None, subtasks: list[SubTask]
    ) -> list[str]:
        """Catalog-validated fact names that carry the query's entities / role.

        Draws on the planner's per-entity ``candidate_fact_names`` first, then
        deterministically backfills from each entity surface / type and the
        filter-role text via :func:`map_concept_to_fact_names`. First-seen
        ordered and deduplicated; empty when nothing maps.
        """
        names: list[str] = []

        def _add(candidates: list[str]) -> None:
            for name in candidates:
                if name in self.catalog and name not in names:
                    names.append(name)

        if analysis is not None:
            for entity in analysis.entities:
                _add(entity.candidate_fact_names)
                _add(map_concept_to_fact_names(entity.surface, self.catalog))
                _add(map_concept_to_fact_names(entity.entity_type, self.catalog))
        for st in subtasks:
            if st.kind == "filter" and st.filter_role:
                _add(map_concept_to_fact_names(st.filter_role, self.catalog))
        return names

    @staticmethod
    def _joined_celex_ids(
        subtasks: list[SubTask], results: SubtaskResults
    ) -> set[str] | None:
        """Intersect celex sets across join inputs; ``None`` when no join."""
        join_tasks = [st for st in subtasks if st.kind == "join"]
        if not join_tasks:
            return None
        intersection: set[str] | None = None
        for st in join_tasks:
            inputs = st.depends_on or list(results.celex_by_task.keys())
            for dep in inputs:
                dep_ids = set(results.celex_by_task.get(dep, []))
                intersection = dep_ids if intersection is None else (intersection & dep_ids)
        return intersection if intersection is not None else set()

    @staticmethod
    def _evidence_by_celex(
        evidence_blocks: list[str], celex_ids: list[str]
    ) -> dict[str, str]:
        """Collect rendered lines mentioning each celex_id (for role filtering)."""
        by_celex: dict[str, list[str]] = {cid: [] for cid in celex_ids}
        for block in evidence_blocks:
            for line in block.splitlines():
                for cid in celex_ids:
                    if cid in line:
                        by_celex[cid].append(line)
        return {cid: "\n".join(lines) for cid, lines in by_celex.items()}

    @staticmethod
    def _role_filter(
        rows_df: pl.DataFrame, filter_role: str, evidence_by_celex: dict[str, str]
    ) -> set[str]:
        """Return celex_ids whose facts or evidence carry a role surface."""
        role_lower = (filter_role or "").lower()
        surfaces: tuple[str, ...] = ()
        for key, words in ROLE_SYNONYMS.items():
            if key in role_lower or any(w in role_lower for w in words):
                surfaces = surfaces + words
        if not surfaces:
            surfaces = tuple(w for w in role_lower.split() if len(w) > 3)
        if not surfaces:
            return set(rows_df["celex_id"].unique().to_list()) if not rows_df.is_empty() else set()

        # Word-boundary patterns so "leader" does not match inside "leadership".
        patterns = [
            re.compile(r"\b" + re.escape(surface) + r"\b") for surface in surfaces
        ]
        keep: set[str] = set()
        if not rows_df.is_empty():
            for celex, value in zip(
                rows_df["celex_id"].to_list(), rows_df["value"].to_list()
            ):
                haystack = f"{value}\n{evidence_by_celex.get(celex, '')}".lower()
                if any(pattern.search(haystack) for pattern in patterns):
                    keep.add(celex)
        return keep

    def _aggregate(self, rows_df: pl.DataFrame, group_by: str) -> pl.DataFrame:
        """Group candidate cases by a resolved fact value and count them."""
        empty = pl.DataFrame(schema={"group": pl.Utf8, "n_cases": pl.UInt32})
        if rows_df.is_empty():
            return empty
        group_fact_names = map_concept_to_fact_names(group_by, self.catalog) or [
            group_by
        ]
        sub = rows_df.filter(pl.col("fact_name").is_in(group_fact_names))
        if sub.is_empty():
            return empty
        return (
            sub.group_by(pl.col("value").alias("group"))
            .agg(pl.col("celex_id").n_unique().alias("n_cases"))
            .sort("n_cases", descending=True)
        )

    # ------------------------------------------------------------------
    # Stage E - verification
    # ------------------------------------------------------------------

    def _verify_rows(self, rows_df: pl.DataFrame) -> pl.DataFrame:
        """Keep only rows whose (celex_id, fact_name) passes the verifier.

        Bounded by ``MASTER_PLANNER_MAX_VERIFY_ROWS``: rows beyond the cap are
        passed through unverified (logged) rather than dropped, so a pathological
        fan-out cannot stall the verify stage on hundreds of LLM-judge calls.
        """
        if (
            self.verifier is None
            or not settings.MASTER_PLANNER_VERIFY_ROWS
            or rows_df.is_empty()
        ):
            return rows_df

        cap = settings.MASTER_PLANNER_MAX_VERIFY_ROWS
        passthrough: pl.DataFrame | None = None
        if cap and rows_df.height > cap:
            logger.warning(
                "Master Planner verify set (%d rows) exceeds cap %d; verifying "
                "the first %d and passing the rest through unverified.",
                rows_df.height, cap, cap,
            )
            passthrough = rows_df.tail(rows_df.height - cap)
            rows_df = rows_df.head(cap)

        verdict_cache: dict[tuple[str, str], bool] = {}

        def _keep(celex_id: str, fact_name: str) -> bool:
            key = (celex_id, fact_name)
            if key in verdict_cache:
                return verdict_cache[key]
            try:
                verdict = self.verifier.forward(celex_id=celex_id, fact_name=fact_name)
            except Exception:
                logger.exception("Verification failed for %s/%s.", celex_id, fact_name)
                verdict = None
            kept = verdict is not None and str(verdict.status) in _KEEP_STATUSES
            verdict_cache[key] = kept
            return kept

        mask = [
            _keep(celex, name)
            for celex, name in zip(
                rows_df["celex_id"].to_list(), rows_df["fact_name"].to_list()
            )
        ]
        verified = rows_df.filter(pl.Series(mask))
        if passthrough is not None:
            return pl.concat([verified, passthrough])
        return verified

    # ------------------------------------------------------------------
    # Rendering & NA terminal
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_value_for_evidence(value: str | None) -> str:
        """Condense a raw markdown fact value into a clean evidence snippet.

        Many entity facts (``companies_investigated``, ``fines``) are stored as
        markdown tables. A blind slice of the raw ``value_md`` hands the
        synthesizer a fragment chopped mid-table - loose ``|`` pipes and half a
        header row - which invites the model to hedge. This collapses table
        syntax to a flat comma-separated value before truncation so the model
        sees ``A, B, C`` rather than ``| A | B |\\n| --- |...``.

        Args:
            value: The raw fact value (may be ``None`` or contain markdown).

        Returns:
            A whitespace-normalized, comma-joined snippet bounded by
            ``settings.MASTER_PLANNER_EVIDENCE_SNIPPET`` characters.
        """
        if not value:
            return ""
        cleaned_lines: list[str] = []
        for line in value.splitlines():
            stripped = line.strip().strip("|").strip()
            # Drop markdown table separator rows, e.g. ``| --- | :--: |``.
            if stripped and not re.fullmatch(r"[\s|:-]+", stripped):
                cleaned_lines.append(stripped)
        flattened = ", ".join(cleaned_lines).replace("|", ", ")
        # Collapse runs of whitespace and redundant comma sequences.
        flattened = re.sub(r"\s+", " ", flattened)
        flattened = re.sub(r"(?:\s*,\s*){2,}", ", ", flattened).strip(" ,")
        return flattened[: settings.MASTER_PLANNER_EVIDENCE_SNIPPET]

    @staticmethod
    def _render_evidence(
        rows_df: pl.DataFrame,
        group_df: pl.DataFrame | None,
        results: SubtaskResults,
    ) -> str:
        """Render verified rows + group counts for the synthesizer prompt."""
        parts: list[str] = []
        if group_df is not None and not group_df.is_empty():
            parts.append("=== Grouped counts ===")
            for group, n_cases in zip(
                group_df["group"].to_list(), group_df["n_cases"].to_list()
            ):
                parts.append(f"- {group}: {n_cases} case(s)")
        if not rows_df.is_empty():
            parts.append("\n=== Verified case rows ===")
            for celex, name, value in zip(
                rows_df["celex_id"].to_list(),
                rows_df["fact_name"].to_list(),
                rows_df["value"].to_list(),
            ):
                snippet = MasterPlannerModule._normalize_value_for_evidence(value)
                parts.append(f"[{celex}] {name} = {snippet}")
        # A graph traversal's answer IS its rendered block - the peer company
        # names live nowhere else. The verified case rows below carry the facts
        # of the shared cases, not the companies, so without this the block
        # would be dropped whenever any case row survived and the answer would
        # degenerate into a case list.
        graph_blocks = [
            block
            for block in results.evidence_blocks
            if "(graph_search)" in block.partition("\n")[0]
        ]
        if graph_blocks:
            parts.append("\n=== Graph relations ===")
            parts.extend(graph_blocks)
        if not parts:
            # Fall back to the raw sub-task evidence so the synthesizer can still
            # enumerate celex_ids it surfaced.
            parts.append("\n".join(results.evidence_blocks))
        return "\n".join(parts)

    @staticmethod
    def _fallback_answer(
        plan: MasterPlan,
        rows_df: pl.DataFrame,
        group_df: pl.DataFrame | None,
    ) -> str:
        """Deterministic answer from the verified rows when the LLM hedges.

        Used only when the synthesizer returns ``confident=False`` or an empty
        answer despite the join + verify stages producing supported rows. Emits
        grouped counts for a ``count_group`` answer, otherwise a markdown list of
        the distinct matching ``celex_id``s. Returns ``""`` when there is nothing
        to render (the caller then falls back to NA).
        """
        if (
            plan.target_type == "count_group"
            and group_df is not None
            and not group_df.is_empty()
        ):
            lines = ["The grouped counts are:"]
            for group, n_cases in zip(
                group_df["group"].to_list(), group_df["n_cases"].to_list()
            ):
                lines.append(f"- {group}: {n_cases} case(s)")
            return "\n".join(lines)
        if not rows_df.is_empty():
            celex_ids = list(dict.fromkeys(rows_df["celex_id"].to_list()))
            header = f"Found {len(celex_ids)} matching case(s):"
            return "\n".join([header, *(f"- {cid}" for cid in celex_ids)])
        return ""

    def _na_event(
        self,
        validation: CelexValidationResult,
        reason: str,
        suggested: str = "",
    ) -> RetrievalEvent:
        """Build the terminal NA result event."""
        message = f"NA - this query needs {reason}."
        if suggested:
            message += f" {suggested}"
        logger.info("Master Planner returned NA: %s", reason)
        return RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                celex_validation=validation,
                plan=None,
                evidence="",
                answer=message,
                references=[],
                fact_ids=[],
                chunk_ids=[],
                fact_names=[],
                routing_decision="master_planner_na",
            ),
        )
