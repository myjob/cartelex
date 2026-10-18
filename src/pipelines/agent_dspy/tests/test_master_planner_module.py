"""Unit tests for :class:`MasterPlannerModule` with mocks at every boundary."""

import time
import types
import unittest
from unittest.mock import MagicMock

import polars as pl

from pipelines.agent_dspy.master_planner_module import (
    MasterPlannerModule,
    SubtaskResults,
)
from pipelines.agent_dspy.master_planner_signature import (
    MasterAnswer,
    MasterPlan,
    QueryAnalysis,
    ResolvedEntity,
    SubTask,
)
from pipelines.agent_dspy.retrieval_dispatch import EvidenceResult


# ---------------------------------------------------------------------------
# Boundary fakes
# ---------------------------------------------------------------------------


class _FakePredictor:
    """Returns a fixed value under ``output_attr``; records calls."""

    def __init__(self, output_attr: str, value: object) -> None:
        self._attr = output_attr
        self._value = value
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(**{self._attr: self._value})


class _FakeDispatcher:
    """Maps a tool name to a fixed EvidenceResult (or callable); records steps."""

    def __init__(self, by_tool: dict) -> None:
        self.by_tool = by_tool
        self.calls: list = []

    def dispatch(self, step):
        self.calls.append(step)
        out = self.by_tool.get(step.tool)
        if callable(out):
            return out(step)
        return out or EvidenceResult(text="No rows returned.")


class _FakeVerifier:
    """(celex_id, fact_name) -> status string; missing keys verify as None."""

    def __init__(self, statuses: dict) -> None:
        self.statuses = statuses
        self.calls: list = []

    def forward(self, celex_id: str, fact_name: str):
        self.calls.append((celex_id, fact_name))
        status = self.statuses.get((celex_id, fact_name))
        if status is None:
            return None
        return types.SimpleNamespace(status=status)


def _sql_store(distinct_names=None, facts_by_celex=None) -> MagicMock:
    store = MagicMock()
    store.select_distinct_fact_names.return_value = distinct_names or []
    facts_by_celex = facts_by_celex or {}
    store.select_facts4celex_with_ids.side_effect = lambda celex: facts_by_celex.get(
        celex, []
    )
    return store


def _build(
    *,
    analysis=None,
    plan=None,
    answer=None,
    dispatcher=None,
    verifier=None,
    distinct_names=None,
    facts_by_celex=None,
) -> MasterPlannerModule:
    return MasterPlannerModule(
        sql_store=_sql_store(distinct_names, facts_by_celex),
        dispatcher=dispatcher or _FakeDispatcher({}),
        verifier=verifier,
        analyze_predictor=_FakePredictor("analysis", analysis)
        if analysis is not None
        else None,
        decompose_predictor=_FakePredictor("plan", plan) if plan is not None else None,
        synthesize_predictor=_FakePredictor("result", answer)
        if answer is not None
        else None,
    )


# ---------------------------------------------------------------------------
# Stage B - NA short-circuit
# ---------------------------------------------------------------------------


class TestNaShortCircuit(unittest.TestCase):
    def test_requires_statistics_returns_na(self) -> None:
        analysis = QueryAnalysis(
            entities=[],
            target_type="unknown",
            requires_statistics=True,
            rationale="trend question",
        )
        module = _build(analysis=analysis)
        pred = module.forward("did banking cartels increase after the leniency law?")
        self.assertEqual(pred.routing_decision, "master_planner_na")
        self.assertTrue(pred.answer.startswith("NA"))

    def test_unsupported_plan_returns_na(self) -> None:
        analysis = QueryAnalysis(
            entities=[], target_type="compare", requires_statistics=False, rationale="x"
        )
        plan = MasterPlan(
            supported=False,
            na_reason="cross-case regression",
            suggested_path="use offline extraction",
            target_type="compare",
            subtasks=[],
        )
        module = _build(analysis=analysis, plan=plan)
        pred = module.forward("correlate fines with cartel duration")
        self.assertEqual(pred.routing_decision, "master_planner_na")
        self.assertIn("cross-case regression", pred.answer)


# ---------------------------------------------------------------------------
# Stage C - sub-task execution & catalog grounding
# ---------------------------------------------------------------------------


class TestSubtaskExecution(unittest.TestCase):
    def test_retrieve_subtask_maps_to_step(self) -> None:
        dispatcher = _FakeDispatcher(
            {"doc_search": EvidenceResult(text="[CELEX-31994D0601] Bayer", chunk_ids=["c1"])}
        )
        module = _build(dispatcher=dispatcher, distinct_names=["companies_investigated"])
        subtask = SubTask(
            task_id=1, kind="retrieve", tool="doc_search", query="Bayer AG", rationale="r"
        )
        results = module._execute_subtasks([subtask])

        self.assertEqual(len(dispatcher.calls), 1)
        step = dispatcher.calls[0]
        self.assertEqual(step.tool, "doc_search")
        self.assertEqual(step.query, "Bayer AG")
        self.assertEqual(results.celex_by_task[1], ["CELEX-31994D0601"])
        self.assertEqual(results.chunk_ids, ["c1"])

    def test_iter_execute_streams_per_subtask_detail(self) -> None:
        dispatcher = _FakeDispatcher(
            {"doc_search": EvidenceResult(text="[CELEX-31994D0601] Bayer", chunk_ids=["c1"])}
        )
        module = _build(dispatcher=dispatcher, distinct_names=["companies_investigated"])
        subtasks = [
            SubTask(
                task_id=1, kind="retrieve", tool="doc_search", query="Bayer AG", rationale="r"
            ),
            SubTask(task_id=2, kind="filter", filter_role="ringleader", rationale="r"),
        ]
        from pipelines.agent_dspy.master_planner_module import SubtaskResults

        results = SubtaskResults()
        events = list(
            module._iter_execute_subtasks(subtasks, results, time.monotonic())
        )
        texts = "".join(e.text for e in events)

        # The retrieve sub-task is named with its tool + query, then summarised.
        self.assertIn("sub-task 1 (doc_search): Bayer AG", texts)
        self.assertIn("1 case(s)", texts)
        # The deterministic filter sub-task is reported as deferred, not silent.
        self.assertIn("sub-task 2 (filter): deferred", texts)
        # The accumulator is still populated for the caller.
        self.assertEqual(results.celex_by_task[1], ["CELEX-31994D0601"])

    def test_kb_sql_with_unknown_fact_name_is_dropped(self) -> None:
        module = _build(distinct_names=["companies_investigated", "fines"])
        unknown = SubTask(
            task_id=1,
            kind="retrieve",
            tool="kb_sql",
            query="SELECT celex_id FROM celex_facts.facts WHERE name = 'made_up_fact'",
            rationale="r",
        )
        self.assertIsNone(module._subtask_to_step(unknown))

    def test_kb_sql_with_known_fact_name_is_kept(self) -> None:
        module = _build(distinct_names=["companies_investigated", "fines"])
        known = SubTask(
            task_id=1,
            kind="retrieve",
            tool="kb_sql",
            query="SELECT celex_id FROM celex_facts.facts WHERE name = 'fines'",
            rationale="r",
        )
        step = module._subtask_to_step(known)
        self.assertIsNotNone(step)
        self.assertEqual(step.tool, "kb_sql")

    def test_graph_view_query_survives_the_fact_name_gate(self) -> None:
        """company_name / case_name are columns, not fact names.

        The extraction regex used to match on the ``name = '...'`` suffix of
        ``company_name``, so a graph-view query looked like it referenced only
        unknown fact names and the whole step was silently dropped.
        """
        module = _build(distinct_names=["companies_investigated", "fines"])
        view_query = SubTask(
            task_id=1,
            kind="retrieve",
            tool="kb_sql",
            query=(
                "SELECT celex_id, source_fact_id "
                "FROM celex_facts.company_case_relations_vw "
                "WHERE company_name ILIKE '%bayer%' AND relation = 'fined' "
                "AND case_name = 'Sugar Cartel'"
            ),
            rationale="r",
        )
        step = module._subtask_to_step(view_query)
        self.assertIsNotNone(step)
        self.assertEqual(step.tool, "kb_sql")

    def test_source_fact_name_is_still_catalog_gated(self) -> None:
        """source_fact_name holds a real fact name, so it must be validated."""
        module = _build(distinct_names=["companies_investigated", "fines"])
        base_query = (
            "SELECT celex_id FROM celex_facts.company_case_relations_vw "
            "WHERE relation = 'leniency_applied' AND source_fact_name = '{name}'"
        )
        unknown = SubTask(
            task_id=1,
            kind="retrieve",
            tool="kb_sql",
            query=base_query.format(name="made_up_fact"),
            rationale="r",
        )
        self.assertIsNone(module._subtask_to_step(unknown))

        known = SubTask(
            task_id=2,
            kind="retrieve",
            tool="kb_sql",
            query=base_query.format(name="fines"),
            rationale="r",
        )
        self.assertIsNotNone(module._subtask_to_step(known))

    def test_qualified_fact_name_column_is_still_extracted(self) -> None:
        """A table-aliased ``p.name`` filter must keep passing through the gate."""
        module = _build(distinct_names=["companies_investigated", "fines"])
        aliased = SubTask(
            task_id=1,
            kind="retrieve",
            tool="kb_sql",
            query=(
                "SELECT p.celex_id FROM celex_facts.facts p "
                "WHERE p.name = 'made_up_fact'"
            ),
            rationale="r",
        )
        self.assertIsNone(module._subtask_to_step(aliased))


# ---------------------------------------------------------------------------
# Stage D - deterministic aggregation
# ---------------------------------------------------------------------------


class TestAggregate(unittest.TestCase):
    def test_group_by_country_counts_and_sorts(self) -> None:
        module = _build(distinct_names=["companies_country"])
        rows = pl.DataFrame(
            {
                "celex_id": ["A", "B", "C"],
                "fact_id": ["1", "2", "3"],
                "fact_name": ["companies_country", "companies_country", "companies_country"],
                "value": ["Germany", "Germany", "France"],
            }
        )
        group_df = module._aggregate(rows, "country")
        self.assertEqual(group_df["group"].to_list(), ["Germany", "France"])
        self.assertEqual(group_df["n_cases"].to_list(), [2, 1])


# ---------------------------------------------------------------------------
# Stage E - verification gate
# ---------------------------------------------------------------------------


class TestVerifyGate(unittest.TestCase):
    def test_insufficient_rows_are_dropped(self) -> None:
        verifier = _FakeVerifier(
            {
                ("A", "companies_investigated"): "verified_supported",
                ("B", "companies_investigated"): "insufficient_evidence",
            }
        )
        module = _build(verifier=verifier, distinct_names=["companies_investigated"])
        rows = pl.DataFrame(
            {
                "celex_id": ["A", "B"],
                "fact_id": ["1", "2"],
                "fact_name": ["companies_investigated", "companies_investigated"],
                "value": ["Bayer", "Foo"],
            }
        )
        kept = module._verify_rows(rows)
        self.assertEqual(kept["celex_id"].to_list(), ["A"])


# ---------------------------------------------------------------------------
# End-to-end happy path (Bayer cartel-leader case list)
# ---------------------------------------------------------------------------


class TestBayerHappyPath(unittest.TestCase):
    def test_filtered_case_list_end_to_end(self) -> None:
        analysis = QueryAnalysis(
            entities=[
                ResolvedEntity(
                    surface="Bayer AG",
                    entity_type="company",
                    available_in=["chunk", "fact"],
                )
            ],
            target_type="case_list",
            requires_statistics=False,
            rationale="entity + role filter",
        )
        plan = MasterPlan(
            supported=True,
            target_type="case_list",
            subtasks=[
                SubTask(
                    task_id=1,
                    kind="retrieve",
                    tool="doc_search",
                    query="Bayer AG ringleader instigator cartel leader",
                    rationale="find candidate cases",
                ),
                SubTask(
                    task_id=2,
                    kind="filter",
                    filter_role="ringleader / instigator / cartel leader",
                    depends_on=[1],
                    rationale="keep cases where Bayer led",
                ),
            ],
        )
        answer = MasterAnswer(
            answer_md="CELEX-31994D0601 - Bayer AG acted as ringleader.",
            confident=True,
            cited_celex_ids=["CELEX-31994D0601"],
        )
        doc_text = (
            "[doc pos=1 CELEX-31994D0601] Bayer AG acted as ringleader and instigator.\n"
            "[doc pos=2 CELEX-31994D0602] Another company, no leadership role."
        )
        dispatcher = _FakeDispatcher(
            {"doc_search": EvidenceResult(text=doc_text, chunk_ids=["c1", "c2"])}
        )
        verifier = _FakeVerifier(
            {
                ("CELEX-31994D0601", "companies_investigated"): "verified_supported",
                ("CELEX-31994D0601", "company_recidivism"): "verified_supported",
            }
        )
        facts_by_celex = {
            "CELEX-31994D0601": [
                ("f1", "companies_investigated", "Bayer AG, BASF"),
                ("f2", "company_recidivism", "Bayer AG flagged as ringleader"),
            ],
            "CELEX-31994D0602": [("f3", "companies_investigated", "Foo Corp")],
        }
        module = _build(
            analysis=analysis,
            plan=plan,
            answer=answer,
            dispatcher=dispatcher,
            verifier=verifier,
            distinct_names=["companies_investigated", "company_recidivism", "companies_country"],
            facts_by_celex=facts_by_celex,
        )

        pred = module.forward("List all cases where Bayer AG was the cartel leader")

        self.assertEqual(pred.routing_decision, "master_planner")
        self.assertIn("CELEX-31994D0601", pred.answer)
        # The non-leader case must not have reached the verifier.
        self.assertNotIn("CELEX-31994D0602", {c for c, _ in verifier.calls})


# ---------------------------------------------------------------------------
# Evidence value normalization (deterministic, no LLM)
# ---------------------------------------------------------------------------


class TestNormalizeValueForEvidence(unittest.TestCase):
    """The synthesizer must see clean condensed values, not table fragments."""

    def test_markdown_table_collapses_to_comma_separated(self) -> None:
        value = (
            "| Company | Role |\n"
            "| --- | --- |\n"
            "| Anheuser-Busch InBev NV/SA | member |\n"
            "| InBev Belgium | member |"
        )
        result = MasterPlannerModule._normalize_value_for_evidence(value)
        self.assertNotIn("|", result)
        self.assertNotIn("---", result)
        self.assertNotIn("\n", result)
        self.assertIn("Anheuser-Busch InBev NV/SA", result)
        self.assertIn("InBev Belgium", result)

    def test_empty_or_none_value_is_empty_string(self) -> None:
        self.assertEqual(MasterPlannerModule._normalize_value_for_evidence(None), "")
        self.assertEqual(MasterPlannerModule._normalize_value_for_evidence(""), "")

    def test_plain_value_is_preserved(self) -> None:
        result = MasterPlannerModule._normalize_value_for_evidence(
            "Anheuser-Busch Incorporated, Scottish & Newcastle"
        )
        self.assertEqual(result, "Anheuser-Busch Incorporated, Scottish & Newcastle")

    def test_snippet_budget_is_respected(self) -> None:
        from config.settings import settings

        long_value = ", ".join(f"Company {index}" for index in range(200))
        result = MasterPlannerModule._normalize_value_for_evidence(long_value)
        self.assertLessEqual(len(result), settings.MASTER_PLANNER_EVIDENCE_SNIPPET)


class TestGraphSearchSubtask(unittest.TestCase):
    """graph_search sub-tasks reach the dispatcher with their typed fields."""

    @staticmethod
    def _subtask(**kwargs):
        params = {
            "task_id": 1,
            "kind": "retrieve",
            "tool": "graph_search",
            "graph_seed": "bayer",
            "rationale": "peers of Bayer",
        }
        params.update(kwargs)
        return SubTask(**params)

    def test_graph_fields_are_forwarded_to_the_step(self):
        planner = _build()
        step = planner._subtask_to_step(
            self._subtask(graph_relation="fined", graph_hops=2)
        )
        self.assertIsNotNone(step)
        self.assertEqual(step.tool, "graph_search")
        self.assertEqual(step.graph_seed, "bayer")
        self.assertEqual(step.graph_relation, "fined")
        self.assertEqual(step.graph_hops, 2)

    def test_seedless_graph_step_is_dropped(self):
        """Without a seed the traversal has nothing to start from."""
        planner = _build()
        self.assertIsNone(planner._subtask_to_step(self._subtask(graph_seed="")))
        self.assertIsNone(planner._subtask_to_step(self._subtask(graph_seed="   ")))

    def test_graph_celex_ids_reach_the_case_table(self):
        """Cases a traversal surfaced must be loaded like any other tool's."""
        evidence = EvidenceResult(
            text=(
                "[graph_search] relation=participated_in hops<=1\n"
                "  BASF hops=1 shared_cases: CELEX-31989D0191, 38354_527_4"
            ),
            fact_ids=["f-1"],
            celex_ids=["CELEX-31989D0191", "38354_527_4"],
        )
        planner = _build(
            facts_by_celex={
                "CELEX-31989D0191": [("f-1", "companies_investigated", "BASF")],
                "38354_527_4": [("f-2", "companies_investigated", "Aspla")],
            },
            dispatcher=_FakeDispatcher({"graph_search": evidence}),
        )
        results = planner._execute_subtasks([self._subtask()])
        self.assertEqual(
            results.celex_by_task[1], ["CELEX-31989D0191", "38354_527_4"]
        )
        cases_df = planner._build_case_table(results.all_celex_ids())
        self.assertEqual(
            sorted(cases_df["celex_id"].unique().to_list()),
            ["38354_527_4", "CELEX-31989D0191"],
        )


class TestGraphEvidenceSurvivesRendering(unittest.TestCase):
    """The peer list is the answer, so it must reach the synthesizer."""

    @staticmethod
    def _results():
        results = SubtaskResults()
        results.evidence_blocks.append(
            "--- Sub-task 1 (graph_search) ---\n"
            "  BASF hops=1 shared_cases: CELEX-31989D0191"
        )
        return results

    def test_graph_block_is_kept_alongside_verified_rows(self):
        """Regression: a non-empty rows_df used to discard the graph block.

        The peer company names live only in the traversal block; the verified
        rows carry the facts of the shared cases, not the companies. Dropping
        the block turns a "which companies" answer into a case list.
        """
        rows_df = pl.DataFrame(
            {
                "celex_id": ["CELEX-31989D0191"],
                "fact_id": ["f-1"],
                "fact_name": ["companies_investigated"],
                "value": ["BASF"],
            }
        )
        rendered = MasterPlannerModule._render_evidence(rows_df, None, self._results())
        self.assertIn("=== Verified case rows ===", rendered)
        self.assertIn("=== Graph relations ===", rendered)
        self.assertIn("BASF hops=1", rendered)

    def test_non_graph_blocks_are_not_promoted(self):
        results = SubtaskResults()
        results.evidence_blocks.append(
            "--- Sub-task 1 (doc_search) ---\nsome chunk text"
        )
        rows_df = pl.DataFrame(
            {
                "celex_id": ["CELEX-31989D0191"],
                "fact_id": ["f-1"],
                "fact_name": ["companies_investigated"],
                "value": ["BASF"],
            }
        )
        rendered = MasterPlannerModule._render_evidence(rows_df, None, results)
        self.assertNotIn("=== Graph relations ===", rendered)
        self.assertNotIn("some chunk text", rendered)

    def test_graph_block_still_renders_when_no_rows_survive(self):
        empty = pl.DataFrame(
            schema={
                "celex_id": pl.Utf8,
                "fact_id": pl.Utf8,
                "fact_name": pl.Utf8,
                "value": pl.Utf8,
            }
        )
        rendered = MasterPlannerModule._render_evidence(empty, None, self._results())
        self.assertIn("BASF hops=1", rendered)



if __name__ == "__main__":
    unittest.main()
