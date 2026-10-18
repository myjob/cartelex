"""Integration tests for the Q&A Retrieval Pipeline.

Tests cover:
  1. Simple single-case lookup: "which company requested leniency in case 38344_5856_3"
  2. Hard macroscopic aggregation: "did leniency cases increase over time"
  3. SQL safety gate: blocks INSERT/UPDATE/DELETE
  4. Unknown tool handling

Uses unittest with MagicMock — no live DB or LLM required.
"""

import unittest
import uuid
from unittest.mock import MagicMock, patch

import dspy

from pipelines.agent_dspy.dspy_signature import (
    CelexValidationResult,
    RetrievalPlan,
    RetrievalStep,
)
from config.settings import settings
from pipelines.agent_dspy.retrieval_dispatch import (
    KB_SCHEMA_DESCRIPTION,
    _ToolDispatcher,
    _validate_read_only_sql,
    build_kb_schema,
)
from pipelines.agent_dspy.retrieval_modules import (
    CelexSelectionModule,
    QAExecutorModule,
    QARetrievalPipeline,
    QASynthesizerModule,
)
from pipelines.agent_dspy.master_planner_module import _extract_all_celex_ids
from pipelines.agent_dspy.retrieval_text import _clean_case_title
from pipelines.graph.entity_graph_models import (
    EdgeRelation,
    GraphNeighbourhood,
    GraphPeer,
    GraphSeed,
)
from tools.tests.celex_index_fixture import FakeCelexChecker


# ---------------------------------------------------------------------------
# CelexSelectionModule — unit tests with a mocked validator
# ---------------------------------------------------------------------------


_FakeCelexChecker = FakeCelexChecker


def _detector() -> CelexSelectionModule:
    """Return a gate whose detection runs against the sample registry only.

    Returns:
        A ``CelexSelectionModule`` with an empty resolution dict, used for
        candidate-extraction assertions where resolution does not matter.
    """
    return CelexSelectionModule(celex_checker=_FakeCelexChecker({}))


class TestCelexSelectionModule(unittest.TestCase):
    """Cover the three terminal states of the celex/case-ID gate."""

    def test_valid_short_form_id(self):
        """'37766_610_5' is a valid short-form id and resolves canonically."""
        checker = _FakeCelexChecker({"37766_610_5": "37766_610_5"})
        module = CelexSelectionModule(celex_checker=checker)

        pred = module(question="overview of 37766_610_5")
        result: CelexValidationResult = pred.result

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.candidate, "37766_610_5")
        self.assertEqual(result.canonical_celex_id, "37766_610_5")
        self.assertEqual(checker.calls, ["37766_610_5"])

    def test_invalid_celex_prefix_with_underscores(self):
        """'CELEX-37766_610_5' is a malformed attempt — DB rejects, gate aborts."""
        # DB knows the short form but NOT the prefixed variant.
        checker = _FakeCelexChecker({"37766_610_5": "37766_610_5"})
        module = CelexSelectionModule(celex_checker=checker)

        pred = module(question="overview of CELEX-37766_610_5")
        result: CelexValidationResult = pred.result

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.candidate, "CELEX-37766_610_5")
        self.assertEqual(result.canonical_celex_id, "")
        self.assertIn("CELEX-37766_610_5", result.message)
        # Only the malformed token was attempted; the short form never appears
        # in the question text so it must not be probed.
        self.assertEqual(checker.calls, ["CELEX-37766_610_5"])

    def test_no_celex_returns_no_celex_status(self):
        """Questions without any celex/case attempt fall through to general retrieval."""
        checker = _FakeCelexChecker({})
        module = CelexSelectionModule(celex_checker=checker)

        pred = module(question="how many leniency cases happened in 1991?")
        result: CelexValidationResult = pred.result

        self.assertEqual(result.status, "no_celex")
        self.assertEqual(checker.calls, [], "DB must not be touched when no candidate.")

    def test_case_alias_resolves_to_canonical_celex(self):
        """A 'IV/...' case number resolves to its canonical CELEX-id."""
        checker = _FakeCelexChecker({"IV/25410": "CELEX-31969D0216"})
        module = CelexSelectionModule(celex_checker=checker)

        pred = module(question="What happened in IV/25410?")
        result: CelexValidationResult = pred.result

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.candidate, "IV/25410")
        self.assertEqual(result.canonical_celex_id, "CELEX-31969D0216")

    def test_canonical_celex_prefix_resolves(self):
        """'CELEX-31991D0562' is canonical — DB returns it unchanged."""
        checker = _FakeCelexChecker({"CELEX-31991D0562": "CELEX-31991D0562"})
        module = CelexSelectionModule(celex_checker=checker)

        pred = module(question="NACE_group_code of CELEX-31991D0562?")
        result: CelexValidationResult = pred.result

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.canonical_celex_id, "CELEX-31991D0562")

    def test_validator_exception_treated_as_na(self):
        """A raising validator must not crash the module — treat as NA."""
        checker = MagicMock()
        checker.find_candidates.return_value = ["37766_610_5"]
        checker.suggest.return_value = []
        checker.check_celex_or_case_id.side_effect = RuntimeError("db down")
        module = CelexSelectionModule(celex_checker=checker)

        pred = module(question="summary of 37766_610_5")
        result: CelexValidationResult = pred.result

        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.candidate, "37766_610_5")


class TestQARetrievalPipelineCelexGate(unittest.TestCase):
    """Verify the pipeline aborts cleanly when the celex gate reports invalid."""

    def _build_pipeline(self, checker) -> QARetrievalPipeline:
        return QARetrievalPipeline(
            sql_store=MagicMock(),
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            celex_checker=checker,
        )

    def test_invalid_celex_aborts_before_planner(self):
        """An invalid celex prefix short-circuits the pipeline."""
        checker = _FakeCelexChecker({"37766_610_5": "37766_610_5"})
        pipeline = self._build_pipeline(checker)

        # Stub the planner so a regression that calls it shows up loudly.
        pipeline.planner = MagicMock(
            side_effect=AssertionError("Planner must not run on invalid celex.")
        )
        pipeline.executor = MagicMock(
            side_effect=AssertionError("Executor must not run on invalid celex.")
        )
        pipeline.synthesizer = MagicMock(
            side_effect=AssertionError("Synthesizer must not run on invalid celex.")
        )

        pred = pipeline(question="overview of CELEX-37766_610_5")

        self.assertEqual(pred.celex_validation.status, "invalid")
        self.assertIsNone(pred.plan)
        self.assertEqual(pred.evidence, "")
        self.assertIn("CELEX-37766_610_5", pred.answer)


# ---------------------------------------------------------------------------
# SQL Safety Gate
# ---------------------------------------------------------------------------


class TestValidateReadOnlySQL(unittest.TestCase):
    """Unit tests for the SQL safety gate."""

    def test_valid_select(self):
        sql = "SELECT COUNT(*) FROM celex_facts.facts WHERE name = 'leniency_case'"
        result = _validate_read_only_sql(sql)
        self.assertIn("SELECT", result)
        # Should append LIMIT when missing
        self.assertIn("LIMIT", result)

    def test_valid_select_with_limit(self):
        sql = "SELECT * FROM celex_facts.facts LIMIT 10"
        result = _validate_read_only_sql(sql)
        # Should NOT double-add LIMIT
        self.assertEqual(result.count("LIMIT"), 1)

    def test_with_cte_allowed(self):
        sql = "WITH x AS (SELECT 1) SELECT * FROM x"
        result = _validate_read_only_sql(sql)
        self.assertIn("WITH", result)

    def test_insert_blocked(self):
        with self.assertRaises(ValueError):
            _validate_read_only_sql("INSERT INTO celex_facts.facts VALUES ('x','y','z','{}')")

    def test_delete_blocked(self):
        with self.assertRaises(ValueError):
            _validate_read_only_sql("DELETE FROM celex_facts.facts WHERE celex_id='x'")

    def test_multi_statement_blocked(self):
        with self.assertRaises(ValueError):
            _validate_read_only_sql("SELECT 1; DROP TABLE celex_facts.facts")

    def test_update_blocked(self):
        with self.assertRaises(ValueError):
            _validate_read_only_sql("UPDATE celex_facts.facts SET value_md='hack'")

    def test_graph_view_select_allowed(self):
        """The gate admits the entity-graph views, not only the facts table."""
        sql = (
            "SELECT company_name, celex_id, source_fact_id "
            "FROM celex_facts.company_case_relations_vw "
            "WHERE relation = 'fined' AND company_country = 'DE'"
        )
        result = _validate_read_only_sql(sql)
        self.assertIn("company_case_relations_vw", result)
        self.assertIn("LIMIT", result)


class TestGraphSchemaSection(unittest.TestCase):
    """The GRAPH_QA_ENABLED switch over the planner schema prose."""

    def test_graph_disabled_matches_facts_and_knowledge_only(self):
        """With the flag off the prose is exactly the pre-graph two-section text."""
        without = build_kb_schema(False)
        self.assertIn("Table: celex_facts.facts", without)
        self.assertIn("Table: celex_facts.knowledge_items", without)
        self.assertNotIn("company_case_relations_vw", without)
        self.assertNotIn("case_provision_relations_vw", without)
        self.assertNotIn("case_timeline_vw", without)

    def test_graph_enabled_adds_views_without_disturbing_other_sections(self):
        with_graph = build_kb_schema(True)
        without = build_kb_schema(False)
        self.assertIn("celex_facts.company_case_relations_vw", with_graph)
        self.assertIn("celex_facts.case_provision_relations_vw", with_graph)
        self.assertIn("celex_facts.case_timeline_vw", with_graph)
        # The facts and knowledge blocks survive verbatim.
        facts_block, knowledge_block = without.split("\n---\n\n", 1)
        self.assertIn(facts_block, with_graph)
        self.assertIn(knowledge_block, with_graph)

    def test_graph_section_declares_every_company_case_relation(self):
        with_graph = build_kb_schema(True)
        for relation in (
            "participated_in",
            "fined",
            "leniency_applied",
            "leniency_granted",
            "leniency_rejected",
        ):
            self.assertIn(relation, with_graph)

    def test_graph_section_warns_about_the_exemption_alias(self):
        """leniency_applied covers two fact families; the prose must say so.

        Measured 20260830: exemption_applicants supplies 1,813 of that
        relation's 2,254 rows, so an unfiltered query is usually wrong.
        """
        with_graph = build_kb_schema(True)
        self.assertIn("source_fact_name = 'leniency_applicants'", with_graph)
        self.assertIn("exemption_applicants", with_graph)

    def test_graph_section_forbids_equality_on_canonical_concept(self):
        """canonical_concept is a comma-separated list; '=' silently misses rows."""
        with_graph = build_kb_schema(True)
        self.assertIn("canonical_concept ILIKE", with_graph)
        self.assertIn("comma-separated", with_graph)

    def test_graph_section_carries_the_coverage_caveats(self):
        """Partial coverage must reach the LLM, or it reports floors as totals."""
        with_graph = build_kb_schema(True)
        self.assertIn("431 of 744", with_graph)
        self.assertIn("3,680 of 9,954", with_graph)
        self.assertIn("company_country", with_graph)

    def test_timeline_section_carries_its_evidence_caveats(self):
        """The timeline is evidence, not a census; the prose must say so.

        Measured 20260905: 'redacted' is 31% of the 57,758 events and only about
        half of company-like actors resolve to a node, so a planner reading this
        view as a company list will under-report every time.
        """
        with_graph = build_kb_schema(True)
        self.assertIn("never as a company census", with_graph.casefold())
        self.assertIn("IS NOT NULL", with_graph)
        self.assertIn("redacted", with_graph)
        self.assertIn("actor_class", with_graph)

    def test_timeline_section_separates_activity_from_participation(self):
        """dated_in is absent from company_case_relations_vw by design."""
        with_graph = build_kb_schema(True)
        self.assertIn("dated_in", with_graph)
        self.assertIn("never of participation", with_graph)

    def test_live_constant_follows_the_setting(self):
        self.assertEqual(
            KB_SCHEMA_DESCRIPTION,
            build_kb_schema(settings.GRAPH_QA_ENABLED, settings.GRAPH_SEARCH_ENABLED),
        )


class TestGraphTraversalSchemaSection(unittest.TestCase):
    """The GRAPH_SEARCH_ENABLED switch over the graph_search prose block."""

    def test_traversal_block_absent_by_default(self):
        """The views flag alone must not pull in the traversal block.

        The two surfaces carry separate settings precisely so they stay
        independently A/B-able.
        """
        views_only = build_kb_schema(True, False)
        self.assertIn("celex_facts.company_case_relations_vw", views_only)
        self.assertNotIn("graph_search", views_only)

    def test_traversal_block_leaves_other_sections_verbatim(self):
        with_traversal = build_kb_schema(True, True)
        views_only = build_kb_schema(True, False)
        self.assertIn("Tool: graph_search", with_traversal)
        facts_block = views_only.split("\n---\n\n", 1)[0]
        self.assertIn(facts_block, with_traversal)
        self.assertIn("Table: celex_facts.knowledge_items", with_traversal)

    def test_traversal_block_states_what_the_views_cannot_do(self):
        """The whole justification for the tool must reach the planner."""
        prose = build_kb_schema(True, True)
        self.assertIn("no edge between two COMPANIES", prose)

    def test_traversal_block_forbids_full_legal_names(self):
        """Aliases split across nodes, so a full name under-reports."""
        prose = build_kb_schema(True, True)
        self.assertIn("never a full legal name", prose.casefold())
        self.assertIn("graph_seed", prose)

    def test_traversal_block_carries_the_coverage_floor_caveat(self):
        """A peer list is a floor, not a census; the LLM must be told."""
        prose = build_kb_schema(True, True)
        self.assertIn("FLOOR", prose)
        self.assertIn("'EC'", prose)

    def test_traversal_block_routes_counting_away(self):
        prose = build_kb_schema(True, True)
        self.assertIn("Using graph_search to COUNT", prose)

    def test_traversal_block_caps_hops(self):
        prose = build_kb_schema(True, True)
        self.assertIn("graph_hops above 2", prose)


def _neighbourhood(peers=None, seeds=None, truncated=False):
    """Build a GraphNeighbourhood fixture with realistic id shapes."""
    if seeds is None:
        seeds = [
            GraphSeed(
                entity_id=uuid.UUID(int=1),
                canonical_name="Bayer FRG",
                lei_id=None,
                edge_count=13,
            )
        ]
    if peers is None:
        peers = [
            GraphPeer(
                entity_id=uuid.UUID(int=2),
                canonical_name="BASF",
                lei_id="529900PM64WH8AF1E917",
                hops=1,
                # Both live id shapes, so the regex round-trip is exercised.
                shared_celex_ids=("CELEX-31989D0191", "38354_527_4"),
                source_fact_ids=(str(uuid.UUID(int=9)),),
            )
        ]
    return GraphNeighbourhood(
        seed_stem="bayer",
        relation=EdgeRelation.PARTICIPATED_IN,
        max_hops=1,
        seeds=tuple(seeds),
        peers=tuple(peers),
        truncated=truncated,
    )


class TestGraphSearchDispatch(unittest.TestCase):
    """The graph_search tool: degradation, clamping, lineage and rendering."""

    def setUp(self):
        self.graph_searcher = MagicMock()
        self.graph_searcher.co_participants_by_name.return_value = _neighbourhood()
        self.dispatcher = _ToolDispatcher(
            sql_store=MagicMock(),
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            graph_searcher=self.graph_searcher,
        )

    @staticmethod
    def _step(**kwargs):
        params = {
            "step_id": 1,
            "tool": "graph_search",
            "query": "",
            "graph_seed": "bayer",
            "rationale": "peers of Bayer",
        }
        params.update(kwargs)
        return RetrievalStep(**params)

    def test_graph_search_is_a_valid_tool(self):
        result = self.dispatcher.dispatch(self._step())
        self.assertNotIn("Unknown tool", result.text)

    def test_missing_searcher_degrades_instead_of_raising(self):
        dispatcher = _ToolDispatcher(
            sql_store=MagicMock(),
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
        )
        self.assertIn("Not available", dispatcher.dispatch(self._step()).text)

    def test_disabled_setting_short_circuits(self):
        with patch.object(settings, "GRAPH_SEARCH_ENABLED", False):
            result = self.dispatcher.dispatch(self._step())
        self.assertIn("Disabled", result.text)
        self.graph_searcher.co_participants_by_name.assert_not_called()

    def test_missing_seed_is_reported_not_dispatched(self):
        result = self.dispatcher.dispatch(self._step(graph_seed=""))
        self.assertIn("No graph_seed", result.text)
        self.graph_searcher.co_participants_by_name.assert_not_called()

    def test_hops_are_clamped_to_the_ceiling(self):
        """Depth 3 costs 20x depth 2 and returns a tenth of the corpus."""
        with patch.object(settings, "GRAPH_SEARCH_MAX_HOPS", 2):
            self.dispatcher.dispatch(self._step(graph_hops=9))
        kwargs = self.graph_searcher.co_participants_by_name.call_args.kwargs
        self.assertEqual(kwargs["max_hops"], 2)

    def test_hops_below_one_are_raised_to_one(self):
        self.dispatcher.dispatch(self._step(graph_hops=0))
        kwargs = self.graph_searcher.co_participants_by_name.call_args.kwargs
        self.assertEqual(kwargs["max_hops"], 1)

    def test_relation_is_passed_through_as_an_enum(self):
        self.dispatcher.dispatch(self._step(graph_relation="fined"))
        kwargs = self.graph_searcher.co_participants_by_name.call_args.kwargs
        self.assertEqual(kwargs["relation"], EdgeRelation.FINED)

    def test_celex_ids_survive_the_text_round_trip(self):
        """The contract that silently breaks if the renderer drops a raw id.

        The Master Planner recovers case ids by regexing the rendered evidence,
        so an id present only in the typed field never reaches the join stage.
        """
        result = self.dispatcher.dispatch(self._step())
        recovered = set(_extract_all_celex_ids(result.text))
        self.assertEqual(set(result.celex_ids), {"CELEX-31989D0191", "38354_527_4"})
        self.assertTrue(set(result.celex_ids) <= recovered)

    def test_fact_ids_carry_the_edge_lineage_anchors(self):
        result = self.dispatcher.dispatch(self._step())
        self.assertEqual(result.fact_ids, [str(uuid.UUID(int=9))])

    def test_ids_are_deduplicated_across_peers(self):
        """One fact produces many edges, so the same anchor repeats."""
        peer_a = GraphPeer(
            entity_id=uuid.UUID(int=2), canonical_name="BASF", hops=1,
            shared_celex_ids=("CELEX-31989D0191",),
            source_fact_ids=(str(uuid.UUID(int=9)),),
        )
        peer_b = GraphPeer(
            entity_id=uuid.UUID(int=3), canonical_name="Hoechst", hops=1,
            shared_celex_ids=("CELEX-31989D0191",),
            source_fact_ids=(str(uuid.UUID(int=9)),),
        )
        self.graph_searcher.co_participants_by_name.return_value = _neighbourhood(
            peers=[peer_a, peer_b]
        )
        result = self.dispatcher.dispatch(self._step())
        self.assertEqual(result.celex_ids, ["CELEX-31989D0191"])
        self.assertEqual(result.fact_ids, [str(uuid.UUID(int=9))])

    def test_no_seed_match_is_reported_plainly(self):
        self.graph_searcher.co_participants_by_name.return_value = _neighbourhood(
            seeds=[], peers=[]
        )
        result = self.dispatcher.dispatch(self._step())
        self.assertIn("No company node matches", result.text)
        self.assertEqual(result.celex_ids, [])

    def test_seeds_without_peers_still_report_the_seeds(self):
        self.graph_searcher.co_participants_by_name.return_value = _neighbourhood(
            peers=[]
        )
        result = self.dispatcher.dispatch(self._step())
        self.assertIn("Bayer FRG", result.text)
        self.assertIn("No co-participating companies", result.text)

    def test_truncation_is_declared_in_the_text(self):
        """A capped list must not read as a complete one."""
        self.graph_searcher.co_participants_by_name.return_value = _neighbourhood(
            truncated=True
        )
        result = self.dispatcher.dispatch(self._step())
        self.assertIn("incomplete", result.text)

    def test_coverage_floor_note_is_always_rendered(self):
        result = self.dispatcher.dispatch(self._step())
        self.assertIn("floor, not a census", result.text)

    def test_repository_failure_becomes_an_error_result(self):
        self.graph_searcher.co_participants_by_name.side_effect = RuntimeError("boom")
        result = self.dispatcher.dispatch(self._step())
        self.assertIn("[error]", result.text)


class TestCleanCaseTitle(unittest.TestCase):
    """Unit tests for the case_title unwrapper."""

    def test_unwraps_tuple_string(self):
        raw = "('COMP/B-2/37.766', 'Dutch beer market')"
        self.assertEqual(_clean_case_title(raw), "Dutch beer market")

    def test_unwraps_list_string(self):
        raw = '["IV/32.736", "Moosehead/Whitbread"]'
        self.assertEqual(_clean_case_title(raw), "Moosehead/Whitbread")

    def test_plain_string_passthrough(self):
        self.assertEqual(_clean_case_title("Dutch beer market"), "Dutch beer market")

    def test_empty_and_none(self):
        self.assertEqual(_clean_case_title(""), "")
        self.assertEqual(_clean_case_title(None), "")

    def test_non_pair_tuple_falls_back_to_raw(self):
        raw = "('a', 'b', 'c')"
        self.assertEqual(_clean_case_title(raw), raw)

    def test_na_placeholder_treated_as_empty(self):
        self.assertEqual(_clean_case_title("('AT.40023', 'NA')"), "")
        self.assertEqual(_clean_case_title("NA"), "")


# ---------------------------------------------------------------------------
# Tool Dispatcher
# ---------------------------------------------------------------------------


class TestToolDispatcher(unittest.TestCase):
    """Tests for the _ToolDispatcher routing and error handling."""

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_kb_searcher = MagicMock()
        self.mock_aggregator = MagicMock()
        self.mock_hybrid_searcher = MagicMock()
        self.mock_doc_searcher = MagicMock()

        # Default: no case_title rows resolve unless a test wires them up.
        self.mock_sql_store.select_fact4celex.return_value = []

        self.dispatcher = _ToolDispatcher(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )

    def test_unknown_tool_returns_error(self):
        step = RetrievalStep(
            step_id=1, tool="bad_tool", query="hello", rationale="test"
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn("[error]", result)

    def test_case_facts_no_case_id(self):
        step = RetrievalStep(
            step_id=1, tool="case_facts", query="", case_id="", rationale="test"
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn("No case_id", result)

    def test_case_facts_returns_facts(self):
        self.mock_sql_store.select_facts4celex_with_ids.return_value = [
            (
                "11111111-1111-1111-1111-111111111111",
                "leniency_applicants",
                "Company A, Company B",
            ),
            (
                "22222222-2222-2222-2222-222222222222",
                "fines",
                "| company | amount |\n|---|---|\n| A | 1M |",
            ),
        ]
        step = RetrievalStep(
            step_id=1, tool="case_facts", query="", case_id="38344_5856_3", rationale="test"
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertIn("[case_facts]", evidence.text)
        self.assertIn("38344_5856_3", evidence.text)
        self.assertIn("leniency_applicants", evidence.text)
        # Fact ids and names are carried as typed edges.
        self.assertEqual(
            evidence.fact_ids,
            [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
        )
        self.assertEqual(evidence.fact_names, ["leniency_applicants", "fines"])

    def test_kb_sql_delegates_to_aggregator(self):
        self.mock_aggregator.aggregate_facts.return_value = [
            {"cnt": 42}
        ]
        step = RetrievalStep(
            step_id=1,
            tool="kb_sql",
            query="SELECT COUNT(*) AS cnt FROM celex_facts.facts WHERE name = 'leniency_case'",
            rationale="count leniency cases",
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn("[kb_sql]", result)
        self.assertIn("42", result)

    def test_kb_sql_blocks_write(self):
        step = RetrievalStep(
            step_id=1,
            tool="kb_sql",
            query="DELETE FROM celex_facts.facts",
            rationale="bad",
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn("[error]", result)

    def test_kb_sql_harvests_source_fact_id_as_provenance(self):
        """A graph-view answer keeps its lineage: source_fact_id is a fact edge.

        The views project the lineage anchor as source_fact_id rather than id,
        so without this fallback a graph-backed answer would cite no evidence.
        Repeated anchors are deduplicated - one fact produces many edges.
        """
        self.mock_aggregator.aggregate_facts.return_value = [
            {
                "company_name": "Bayer AG",
                "celex_id": "CELEX-31991D0562",
                "source_fact_id": "33333333-3333-3333-3333-333333333333",
            },
            {
                "company_name": "BASF AG",
                "celex_id": "CELEX-31991D0562",
                "source_fact_id": "33333333-3333-3333-3333-333333333333",
            },
        ]
        step = RetrievalStep(
            step_id=1,
            tool="kb_sql",
            query=(
                "SELECT company_name, celex_id, source_fact_id "
                "FROM celex_facts.company_case_relations_vw WHERE relation = 'fined'"
            ),
            rationale="fined companies",
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertEqual(
            evidence.fact_ids, ["33333333-3333-3333-3333-333333333333"]
        )
        self.assertEqual(evidence.celex_ids, ["CELEX-31991D0562"])

    def test_kb_sql_still_prefers_the_id_column(self):
        """The facts-table path is unchanged: a projected id is the anchor."""
        self.mock_aggregator.aggregate_facts.return_value = [
            {"id": "44444444-4444-4444-4444-444444444444", "celex_id": "TEST-001"}
        ]
        step = RetrievalStep(
            step_id=1,
            tool="kb_sql",
            query="SELECT id, celex_id FROM celex_facts.facts WHERE name = 'fines'",
            rationale="fines",
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertEqual(
            evidence.fact_ids, ["44444444-4444-4444-4444-444444444444"]
        )

    def test_rag_search_returns_results(self):
        mock_result = MagicMock()
        mock_result.id = "fact-uuid-001"
        mock_result.celex_id = "TEST-001"
        mock_result.fact_name = "leniency_applicants"
        mock_result.similarity = 0.92
        mock_result.value_txt = "Acme Corp applied for leniency"
        self.mock_kb_searcher.semantic_search_facts.return_value = [mock_result]

        step = RetrievalStep(
            step_id=1, tool="rag_search", query="leniency applicants", rationale="test"
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertIn("[rag_search]", evidence.text)
        self.assertIn("TEST-001", evidence.text)
        # The fact UUID and name ride along as typed edges.
        self.assertEqual(evidence.fact_ids, ["fact-uuid-001"])
        self.assertEqual(evidence.fact_names, ["leniency_applicants"])

    def test_rag_search_enriches_celex_ids_with_case_title(self):
        """rag_search results list each celex_id with its case_title."""
        result_1 = MagicMock()
        result_1.id = "fact-uuid-2"
        result_1.celex_id = "37766_610_5"
        result_1.fact_name = "leniency_applicants"
        result_1.similarity = 0.91
        result_1.value_txt = "Heineken applied for leniency"
        self.mock_kb_searcher.semantic_search_facts.return_value = [result_1]

        title_fact = MagicMock()
        title_fact.value_md = "('COMP/B-2/37.766', 'Dutch beer market')"
        self.mock_sql_store.select_fact4celex.return_value = [title_fact]

        step = RetrievalStep(
            step_id=1, tool="rag_search", query="leniency", rationale="test"
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn('celex_id=37766_610_5 case_title="Dutch beer market"', result)

    def test_rag_search_in_case(self):
        mock_chunk = MagicMock()
        mock_chunk.id = "chunk-pk-5"
        mock_chunk.doc_pos = 5
        mock_chunk.chunk = "The Commission granted immunity to Company X."
        self.mock_hybrid_searcher.search.return_value = [mock_chunk]

        step = RetrievalStep(
            step_id=1, tool="rag_search_in_case", query="leniency",
            case_id="38344_5856_3", rationale="test"
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertIn("[rag_search_in_case]", evidence.text)
        self.assertIn("38344_5856_3", evidence.text)
        self.assertEqual(evidence.chunk_ids, ["chunk-pk-5"])

    def test_doc_search_returns_chunks(self):
        mock_chunk = MagicMock()
        mock_chunk.id = "chunk-pk-2"
        mock_chunk.celex_id = "CELEX-31998D0247"
        mock_chunk.doc_pos = 2
        mock_chunk.origin = "pgvector_semantic"
        mock_chunk.chunk = "Commissioner Karel Van Miert decided the case in Brussels."
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = []
        self.mock_doc_searcher.semantic_search_chunks.return_value = [mock_chunk]

        step = RetrievalStep(
            step_id=1, tool="doc_search", query="commissioner Karel Van Miert",
            rationale="Find cases handled by this commissioner."
        )
        evidence = self.dispatcher.dispatch(step)
        self.assertIn("[doc_search]", evidence.text)
        self.assertIn("CELEX-31998D0247", evidence.text)
        self.assertIn("Karel Van Miert", evidence.text)
        self.assertEqual(evidence.chunk_ids, ["chunk-pk-2"])

    def test_doc_search_no_results(self):
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = []
        self.mock_doc_searcher.semantic_search_chunks.return_value = []

        step = RetrievalStep(
            step_id=1, tool="doc_search", query="nonexistent town",
            rationale="test"
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn("No chunks found", result)

    def test_doc_search_keyword_first_then_semantic_merge(self):
        """Keyword hits lead; semantic only adds cases the keyword pass missed."""
        kw_chunk = MagicMock()
        kw_chunk.id = "chunk-kw-1"
        kw_chunk.celex_id = "38456_792_2"
        kw_chunk.doc_pos = 15
        kw_chunk.origin = "sql_keyword"
        kw_chunk.chunk = "Koninklijke Volker Wessels Stevin NV is a Dutch construction group."

        # Semantic returns one duplicate (same celex_id) and one new case.
        sem_dup = MagicMock()
        sem_dup.id = "chunk-sem-dup"
        sem_dup.celex_id = "38456_792_2"
        sem_dup.doc_pos = 99
        sem_dup.origin = "pgvector_semantic"
        sem_dup.chunk = "duplicate case, must be deduplicated"

        sem_new = MagicMock()
        sem_new.id = "chunk-sem-new"
        sem_new.celex_id = "39437_7332_3"
        sem_new.doc_pos = 4
        sem_new.origin = "pgvector_semantic"
        sem_new.chunk = "A related concept chunk the keyword pass missed."

        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = [kw_chunk]
        self.mock_doc_searcher.semantic_search_chunks.return_value = [sem_dup, sem_new]

        step = RetrievalStep(
            step_id=1, tool="doc_search", query="Koninklijke", rationale="entity lookup"
        )
        result = self.dispatcher.dispatch(step).text

        self.assertIn("38456_792_2", result)
        self.assertIn("39437_7332_3", result)
        # The semantic duplicate of 38456_792_2 must not appear.
        self.assertNotIn("duplicate case, must be deduplicated", result)
        # Keyword hit precedes the semantic-only case in the merged output.
        self.assertLess(
            result.index("38456_792_2"), result.index("39437_7332_3")
        )

    def test_doc_search_enriches_celex_ids_with_case_title(self):
        """A general doc_search lists each celex_id with its case_title."""
        kw_chunk = MagicMock()
        kw_chunk.id = "chunk-kw-2"
        kw_chunk.celex_id = "38456_792_2"
        kw_chunk.doc_pos = 15
        kw_chunk.origin = "sql_keyword"
        kw_chunk.chunk = "Koninklijke Volker Wessels Stevin NV is a Dutch group."

        sem_new = MagicMock()
        sem_new.id = "chunk-sem-2"
        sem_new.celex_id = "CELEX-31991D0562"
        sem_new.doc_pos = 4
        sem_new.origin = "pgvector_semantic"
        sem_new.chunk = "A related concept chunk."

        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = [kw_chunk]
        self.mock_doc_searcher.semantic_search_chunks.return_value = [sem_new]

        title_1 = MagicMock()
        title_1.value_md = "Dutch construction cartel"
        title_2 = MagicMock()
        title_2.value_md = "Soda-ash/Solvay"

        def _fake_select_fact4celex(cid, name):
            if name != "case_title":
                return []
            return {
                "38456_792_2": [title_1],
                "CELEX-31991D0562": [title_2],
            }.get(cid, [])

        self.mock_sql_store.select_fact4celex.side_effect = _fake_select_fact4celex

        step = RetrievalStep(
            step_id=1, tool="doc_search", query="Koninklijke", rationale="entity lookup"
        )
        result = self.dispatcher.dispatch(step).text

        self.assertIn('celex_id=38456_792_2 case_title="Dutch construction cartel"', result)
        self.assertIn('celex_id=CELEX-31991D0562 case_title="Soda-ash/Solvay"', result)

    def test_doc_search_unwraps_tuple_form_case_title(self):
        """case_title stored as a stringified (case_id, title) tuple is unwrapped."""
        chunk = MagicMock()
        chunk.id = "chunk-beer"
        chunk.celex_id = "37766_610_5"
        chunk.doc_pos = 1
        chunk.origin = "sql_keyword"
        chunk.chunk = "The Dutch beer market chunk."
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = [chunk]
        self.mock_doc_searcher.semantic_search_chunks.return_value = []

        tuple_fact = MagicMock()
        tuple_fact.value_md = "('COMP/B-2/37.766', 'Dutch beer market')"
        self.mock_sql_store.select_fact4celex.return_value = [tuple_fact]

        step = RetrievalStep(
            step_id=1, tool="doc_search", query="beer", rationale="test"
        )
        result = self.dispatcher.dispatch(step).text

        self.assertIn('case_title="Dutch beer market"', result)
        # The raw tuple wrapper must not leak into the evidence.
        self.assertNotIn("COMP/B-2/37.766", result)

    def test_doc_search_omits_case_title_when_unresolved(self):
        """A celex_id with no case_title fact still appears, without a title field."""
        chunk = MagicMock()
        chunk.id = "chunk-no-title"
        chunk.celex_id = "NO-TITLE-ID"
        chunk.doc_pos = 1
        chunk.origin = "pgvector_semantic"
        chunk.chunk = "Some chunk text."
        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = []
        self.mock_doc_searcher.semantic_search_chunks.return_value = [chunk]
        # select_fact4celex returns [] by default (setUp) — no title resolves.

        step = RetrievalStep(
            step_id=1, tool="doc_search", query="something", rationale="test"
        )
        result = self.dispatcher.dispatch(step).text

        self.assertIn("celex_id=NO-TITLE-ID", result)
        self.assertNotIn("case_title=", result)

    # -- case_title enrichment tests --

    def test_kb_sql_enriches_celex_id_rows_with_case_title(self):
        """When kb_sql returns rows with a 'celex_id' key, each row gets a 'case_title'."""
        self.mock_aggregator.aggregate_facts.return_value = [
            {"celex_id": "38344_5856_3"},
            {"celex_id": "CELEX-31991D0562"},
        ]
        # Wire up the sql_store to return case_title facts.
        mock_fact_1 = MagicMock()
        mock_fact_1.value_md = "Dutch beer market"
        mock_fact_2 = MagicMock()
        mock_fact_2.value_md = "Soda-ash/Solvay"

        def _fake_select_fact4celex(cid, name):
            if name != "case_title":
                return []
            return {
                "38344_5856_3": [mock_fact_1],
                "CELEX-31991D0562": [mock_fact_2],
            }.get(cid, [])

        self.mock_sql_store.select_fact4celex.side_effect = _fake_select_fact4celex

        step = RetrievalStep(
            step_id=1,
            tool="kb_sql",
            query="SELECT DISTINCT celex_id FROM celex_facts.facts WHERE name = 'leniency_case'",
            rationale="list leniency cases",
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn("Dutch beer market", result)
        self.assertIn("Soda-ash/Solvay", result)
        self.assertIn("38344_5856_3", result)

    def test_kb_sql_no_enrichment_without_celex_id_key(self):
        """Rows that lack a 'celex_id' column are returned as-is (no enrichment)."""
        self.mock_aggregator.aggregate_facts.return_value = [
            {"yr": 2003, "cnt": 12},
        ]
        step = RetrievalStep(
            step_id=1,
            tool="kb_sql",
            query="SELECT EXTRACT(YEAR FROM value_md::date) AS yr, COUNT(*) AS cnt FROM celex_facts.facts WHERE name = 'eu_commisssion_decision_date' AND LENGTH(value_md) = 10 GROUP BY yr",
            rationale="count by year",
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn("12", result)
        # sql_store.select_fact4celex must not be called at all.
        self.mock_sql_store.select_fact4celex.assert_not_called()

    def test_kb_sql_enrichment_handles_db_error_gracefully(self):
        """A failing title lookup must not crash the dispatcher — the row is returned without a title."""
        self.mock_aggregator.aggregate_facts.return_value = [
            {"celex_id": "BROKEN-ID"},
        ]
        self.mock_sql_store.select_fact4celex.side_effect = RuntimeError("db down")

        step = RetrievalStep(
            step_id=1,
            tool="kb_sql",
            query="SELECT DISTINCT celex_id FROM celex_facts.facts LIMIT 1",
            rationale="test",
        )
        result = self.dispatcher.dispatch(step).text
        self.assertIn("BROKEN-ID", result)
        self.assertNotIn("case_title", result)


# ---------------------------------------------------------------------------
# Integration Test 1 — Simple single-case lookup
# ---------------------------------------------------------------------------


class TestQASimpleCaseLookup(unittest.TestCase):
    """Test case: 'Which company requested leniency in case 38344_5856_3?'

    Verifies the full pipeline with mocked LLM and tools.
    The planner should produce a case_facts or rag_search_in_case step,
    the executor should call the SQL store, and the synthesizer should
    produce a grounded answer.
    """

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_kb_searcher = MagicMock()
        self.mock_aggregator = MagicMock()
        self.mock_hybrid_searcher = MagicMock()
        self.mock_doc_searcher = MagicMock()

        # Wire up case_facts to return leniency data (id-carrying read).
        self.mock_sql_store.select_facts4celex_with_ids.return_value = [
            ("id-leniency", "leniency_applicants", "Acme Corp, Beta Industries"),
            ("id-case", "leniency_case", "True"),
            (
                "id-fines",
                "fines",
                "| company | amount |\n| Acme Corp | 0 |\n| Beta Industries | 5M |",
            ),
        ]

    def test_executor_with_case_facts_plan(self):
        """Given a pre-built plan targeting case_facts, the executor retrieves data."""
        plan = RetrievalPlan(
            question_type="single_case",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="case_facts",
                    query="leniency applicants",
                    case_id="38344_5856_3",
                    rationale="Look up all facts for the specific case.",
                )
            ],
        )

        executor = QAExecutorModule(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )
        pred = executor(plan=plan)

        self.assertIn("leniency_applicants", pred.evidence)
        self.assertIn("Acme Corp", pred.evidence)
        self.mock_sql_store.select_facts4celex_with_ids.assert_called_once_with(
            "38344_5856_3"
        )
        # The executor surfaces the typed fact edges from the case_facts step.
        self.assertEqual(
            pred.fact_ids, ["id-leniency", "id-case", "id-fines"]
        )
        self.assertEqual(
            pred.fact_names, ["leniency_applicants", "leniency_case", "fines"]
        )

    def test_executor_surfaces_the_cases_it_touched(self):
        """The executor reports its celex_ids so a turn's case set can be pinned.

        ``EvidenceResult.celex_ids`` already drove intra-turn step chaining;
        surfacing it on the prediction is what lets a conversation follow-up be
        scoped to the same cases the user was just shown.
        """
        plan = RetrievalPlan(
            question_type="single_case",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="case_facts",
                    query="leniency applicants",
                    case_id="38344_5856_3",
                    rationale="Look up all facts for the specific case.",
                )
            ],
        )

        executor = QAExecutorModule(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )
        pred = executor(plan=plan)

        self.assertEqual(pred.celex_ids, ["38344_5856_3"])


# ---------------------------------------------------------------------------
# Integration Test 2 — Macroscopic aggregation
# ---------------------------------------------------------------------------


class TestQAMacroscopicAggregation(unittest.TestCase):
    """Test case: 'Did leniency cases accepted by the Commission increase over time?'

    This is a hard aggregation question that should route through kb_sql
    to aggregate across all ~800 cases.
    """

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_kb_searcher = MagicMock()
        self.mock_aggregator = MagicMock()
        self.mock_hybrid_searcher = MagicMock()
        self.mock_doc_searcher = MagicMock()

        # Simulate aggregation result: counts of leniency cases by year-bucket
        self.mock_aggregator.aggregate_facts.return_value = [
            {"period": "1990-2000", "cnt": 5},
            {"period": "2000-2010", "cnt": 18},
            {"period": "2010-2020", "cnt": 32},
        ]

    def test_executor_with_kb_sql_plan(self):
        """Given a plan with kb_sql step, the aggregator is called correctly."""
        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="kb_sql",
                    query=(
                        "SELECT COUNT(DISTINCT celex_id) AS cnt "
                        "FROM celex_facts.facts "
                        "WHERE name = 'leniency_case' AND value_md = 'True'"
                    ),
                    rationale="Count cases where leniency was applied.",
                )
            ],
        )

        executor = QAExecutorModule(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )
        pred = executor(plan=plan)

        self.assertIn("[kb_sql]", pred.evidence)
        self.mock_aggregator.aggregate_facts.assert_called_once()

    def test_multi_step_plan(self):
        """A multi-step plan executes all steps and collects evidence."""
        plan = RetrievalPlan(
            question_type="multi_case_aggregation",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="kb_sql",
                    query="SELECT DISTINCT celex_id FROM celex_facts.facts WHERE name = 'leniency_case' AND value_md = 'True'",
                    rationale="Get all leniency celex_ids.",
                ),
                RetrievalStep(
                    step_id=2,
                    tool="rag_search",
                    query="leniency cases trend over time Commission decisions",
                    rationale="Find textual evidence about trends.",
                ),
            ],
        )

        self.mock_kb_searcher.semantic_search_facts.return_value = []

        executor = QAExecutorModule(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )
        pred = executor(plan=plan)

        self.assertIn("Step 1", pred.evidence)
        self.assertIn("Step 2", pred.evidence)


# ---------------------------------------------------------------------------
# Synthesizer (unit-level with mocked DSPy)
# ---------------------------------------------------------------------------


class TestQASynthesizerModule(unittest.TestCase):
    """Verify the synthesizer properly delegates to ChainOfThought."""

    @patch("pipelines.agent_dspy.retrieval_modules.dspy.ChainOfThought")
    def test_forward_calls_chain_of_thought(self, MockCoT):
        mock_instance = MagicMock()
        mock_instance.return_value = dspy.Prediction(answer="Leniency increased.")
        MockCoT.return_value = mock_instance

        synth = QASynthesizerModule()
        synth.synthesize = mock_instance

        pred = synth(question="Did leniency increase?", evidence="[kb_sql] cnt=42")
        self.assertEqual(pred.answer, "Leniency increased.")


# ---------------------------------------------------------------------------
# Regression Test: Entity lookups with doc_search (e.g. "Koninklijke")
# ---------------------------------------------------------------------------


class TestEntityLookupWithDocSearch(unittest.TestCase):
    """Regression test for Koninklijke issue.

    Test case: "list all cases related to Koninklijke"

    The planner should recognize this as an entity lookup and route to doc_search.
    doc_search successfully finds celex_id=38456_792_2 with "Koninklijke Volker
    Wessels Stevin" and "Koninklijke BAM Groep" mentions.

    The executor should collect this evidence and the synthesizer should cite it
    in the final answer, not return "No cases found".
    """

    def setUp(self):
        self.mock_sql_store = MagicMock()
        self.mock_kb_searcher = MagicMock()
        self.mock_aggregator = MagicMock()
        self.mock_hybrid_searcher = MagicMock()
        self.mock_doc_searcher = MagicMock()

    def test_doc_search_finds_koninklijke_cases(self):
        """The keyword pass finds Koninklijke cases that pure vector search misses."""
        # Keyword search (ILIKE) reliably finds the entity across cases —
        # one representative chunk per distinct celex_id.
        kw_chunk_1 = MagicMock()
        kw_chunk_1.id = "chunk-kon-1"
        kw_chunk_1.celex_id = "38456_792_2"
        kw_chunk_1.doc_pos = 193
        kw_chunk_1.origin = "sql_keyword"
        kw_chunk_1.chunk = (
            "(j) KWS: Koninklijke Volker Wessels Stevin NV and "
            "Koninklijke Wegenbouw Stevin BV from 1 April 1994 to 15 April 2002;"
        )

        kw_chunk_2 = MagicMock()
        kw_chunk_2.id = "chunk-kon-2"
        kw_chunk_2.celex_id = "39437_7332_3"
        kw_chunk_2.doc_pos = 12
        kw_chunk_2.origin = "sql_keyword"
        kw_chunk_2.chunk = "Koninklijke BAM Groep NV is jointly and severally liable."

        self.mock_doc_searcher.keyword_search_chunks_all_cases.return_value = [
            kw_chunk_1, kw_chunk_2
        ]
        # Pure semantic search returns nothing useful for the proper noun.
        self.mock_doc_searcher.semantic_search_chunks.return_value = []

        # The executor runs a doc_search step
        plan = RetrievalPlan(
            question_type="exploratory",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="doc_search",
                    query="Koninklijke",
                    rationale="Find cases mentioning company Koninklijke.",
                )
            ],
        )

        executor = QAExecutorModule(
            sql_store=self.mock_sql_store,
            kb_searcher=self.mock_kb_searcher,
            aggregator=self.mock_aggregator,
            hybrid_searcher=self.mock_hybrid_searcher,
            doc_searcher=self.mock_doc_searcher,
        )
        pred = executor(plan=plan)

        # Verify the evidence includes both celex_ids and Koninklijke mentions
        self.assertIn("38456_792_2", pred.evidence)
        self.assertIn("39437_7332_3", pred.evidence)
        self.assertIn("Koninklijke", pred.evidence)
        self.assertIn("[doc_search]", pred.evidence)


# ---------------------------------------------------------------------------
# Regression: AT_\d+_\d+ celex_id format (e.g. AT_40588_6339)
# ---------------------------------------------------------------------------


class TestATUnderscoreCelexIdFormat(unittest.TestCase):
    """Regression tests for AT-prefix IDs with underscore-separated digits.

    The bug: ``AT_40588_6339`` was not matched by ``CELEX_CANDIDATE_RE``
    because ``\\d+_\\d+_\\d+`` requires the first segment to be all-digits
    and ``AT[.\\-]\\d+`` only accepts dot/hyphen after AT.  This caused
    ``CelexSelectionModule`` to return ``status='no_celex'``, which in turn
    triggered the history-based context lookup in ``oi_dspy.py`` to inject
    the stale celex_id from a prior conversation turn.
    """

    def test_candidate_extraction_at_underscore_2_groups(self):
        """AT_40588_6339 (AT + 2 digit groups) is extracted as a candidate."""
        candidates = _detector().extract_candidates(
            "list courts of AT_40588_6339"
        )
        self.assertIn("AT_40588_6339", candidates)

    def test_candidate_extraction_at_underscore_4_groups(self):
        """AT_40760_9770404_1218_4 (AT + 4 digit groups) is extracted."""
        candidates = _detector().extract_candidates(
            "summary of AT_40760_9770404_1218_4"
        )
        self.assertIn("AT_40760_9770404_1218_4", candidates)

    def test_celex_selection_valid_at_underscore(self):
        """CelexSelectionModule resolves an AT_\\d+_\\d+ id as 'valid'."""
        checker = _FakeCelexChecker({"AT_40588_6339": "AT_40588_6339"})
        module = CelexSelectionModule(celex_checker=checker)

        pred = module(question="list courts of AT_40588_6339")
        result: CelexValidationResult = pred.result

        self.assertEqual(result.status, "valid")
        self.assertEqual(result.candidate, "AT_40588_6339")
        self.assertEqual(result.canonical_celex_id, "AT_40588_6339")

    def test_at_underscore_not_shadowed_by_context_tag(self):
        """When both AT_40588_6339 and a [context: celex_id=...] tag appear,
        the AT_ id is the first candidate — not the context tag's id."""
        candidates = _detector().extract_candidates(
            "list courts of AT_40588_6339  [context: celex_id=39226_8310_11]"
        )
        # AT_40588_6339 must appear before 39226_8310_11
        self.assertIn("AT_40588_6339", candidates)
        idx_at = candidates.index("AT_40588_6339")
        idx_old = candidates.index("39226_8310_11")
        self.assertLess(idx_at, idx_old,
                        "AT_ id must be extracted before the context-tag id")

    def test_celex_id_re_matches_at_underscore(self):
        """_CELEX_ID_RE also recognises AT_\\d+_\\d+ format."""
        from pipelines.agent_dspy.retrieval_text import _CELEX_ID_RE

        match = _CELEX_ID_RE.search("courts of AT_40588_6339")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "AT_40588_6339")

    def test_numeric_only_id_still_works(self):
        """Ensure the existing \\d+_\\d+_\\d+ pattern is not broken."""
        candidates = _detector().extract_candidates(
            "overview of 39226_8310_11"
        )
        self.assertIn("39226_8310_11", candidates)


if __name__ == "__main__":
    unittest.main()
