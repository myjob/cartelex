import contextlib
import io
import sys
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from batch.Main_ingest_generic_facts import (
    FactIngestionOrchestrator,
    PipelineExecutor,
)


class TestPipelineExecutor(unittest.TestCase):
    """Tests for the PipelineExecutor per-CELEX processing class."""

    def test_process_single_celex_dry_run(self):
        """Dry-run mode returns dry_run status without calling pipe.process."""
        agent = MagicMock()
        pipe = MagicMock()
        pipe.process = AsyncMock()

        yaml_params = [
            {
                "target_fact_name": "test_fact",
                "run_state_active": True,
                "question": "What is the fact?",
                "output_schema": "fact_value",
            }
        ]

        executor = PipelineExecutor(
            agents=agent, pipelines=[pipe], yaml_params=yaml_params, overwrite=False
        )
        res = executor.process_celex("CELEX-TEST", dry_run=True)

        self.assertIsInstance(res, list)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "dry_run")
        pipe.process.assert_not_called()

    def test_process_single_celex_active(self):
        """Active mode calls pipe.process and returns its result."""
        agent = MagicMock()
        agents = {"universal": agent}
        pipe = MagicMock()
        pipe.process = AsyncMock(return_value={"status": "success", "data": "dummy"})

        yaml_params = [
            {
                "target_fact_name": "test_fact_active",
                "run_state_active": True,
                "question": "What is the active fact?",
                "output_schema": "active_value",
            }
        ]

        executor = PipelineExecutor(
            agents=agents, pipelines=[pipe], yaml_params=yaml_params, overwrite=False
        )
        res = executor.process_celex("CELEX-TEST", dry_run=False)

        self.assertIsInstance(res, list)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "success")
        pipe.process.assert_called_once()

        args, kwargs = pipe.process.call_args
        self.assertIn("dspy_agent", kwargs)
        self.assertEqual(kwargs["dspy_agent"], agent)

        param_vo = args[0]
        self.assertEqual(param_vo.celex_id, "CELEX-TEST")
        self.assertEqual(param_vo.target_fact_name, "test_fact_active")

    def test_overwrite_flag_overrules_yaml_force_override(self):
        """--overwrite forces force_override on a fact whose YAML says false."""
        agents = {"universal": MagicMock()}
        pipe = MagicMock()
        pipe.process = AsyncMock(return_value={"status": "success", "data": "dummy"})

        yaml_params = [
            {
                "target_fact_name": "test_fact_active",
                "run_state_active": True,
                "question": "What is the active fact?",
                "output_schema": "active_value",
                "force_override": False,
            }
        ]

        executor = PipelineExecutor(
            agents=agents, pipelines=[pipe], yaml_params=yaml_params, overwrite=True
        )
        executor.process_celex("CELEX-TEST", dry_run=False)

        param_vo = pipe.process.call_args[0][0]
        self.assertTrue(param_vo.force_override)

    def test_yaml_force_override_applies_per_fact_without_overwrite_flag(self):
        """Without --overwrite each fact keeps its own YAML force_override."""
        agents = {"universal": MagicMock()}
        overriding_pipe = MagicMock()
        overriding_pipe.process = AsyncMock(
            return_value={"status": "success", "data": "dummy"}
        )
        cached_pipe = MagicMock()
        cached_pipe.process = AsyncMock(
            return_value={"status": "success", "data": "dummy"}
        )

        yaml_params = [
            {
                "target_fact_name": "predecessor_fact",
                "run_state_active": True,
                "question": "predecessor",
                "output_schema": "",
                "force_override": False,
            },
            {
                "target_fact_name": "overriding_fact",
                "run_state_active": True,
                "question": "overriding",
                "output_schema": "",
                "force_override": True,
            },
        ]

        executor = PipelineExecutor(
            agents=agents,
            pipelines=[cached_pipe, overriding_pipe],
            yaml_params=yaml_params,
            overwrite=False,
        )
        executor.process_celex("CELEX-TEST", dry_run=False)

        self.assertFalse(cached_pipe.process.call_args[0][0].force_override)
        self.assertTrue(overriding_pipe.process.call_args[0][0].force_override)


class TestFactIngestionOrchestratorDependencies(unittest.TestCase):
    """Tests for YAML predecessor preparation before pipeline construction."""

    def setUp(self):
        self.orchestrator = FactIngestionOrchestrator(description="test")

    def test_filter_fact_includes_transitive_predecessors_and_sorts(self):
        structs = [
            {
                "target_fact_name": "heideltime_raw",
                "run_state_active": True,
                "question": "raw",
                "output_schema": "",
            },
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": True,
                "question": "event",
                "output_schema": "",
                "predecessors": "heideltime_raw",
            },
            {
                "target_fact_name": "heideltime_event_agg",
                "run_state_active": True,
                "question": "agg",
                "output_schema": "",
                "predecessors": ["heideltime_event"],
            },
            {
                "target_fact_name": "unrelated",
                "run_state_active": True,
                "question": "other",
                "output_schema": "",
            },
        ]

        active = self.orchestrator._filter_active_structs(
            structs, "heideltime_event_agg"
        )

        self.assertIsNotNone(active)
        self.assertEqual(
            {s["target_fact_name"] for s in active},
            {"heideltime_raw", "heideltime_event", "heideltime_event_agg"},
        )
        event_struct = next(
            s for s in active if s["target_fact_name"] == "heideltime_event"
        )
        self.assertEqual(event_struct["predecessors"], ["heideltime_raw"])

        execution_order = self.orchestrator._topological_order(active)
        self.assertLess(
            execution_order.index("heideltime_raw"),
            execution_order.index("heideltime_event"),
        )
        self.assertLess(
            execution_order.index("heideltime_event"),
            execution_order.index("heideltime_event_agg"),
        )

    def test_filter_fact_keeps_target_when_predecessor_config_missing(self):
        structs = [
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": True,
                "question": "event",
                "output_schema": "",
                "predecessors": ["heideltime_raw"],
            }
        ]

        active = self.orchestrator._filter_active_structs(
            structs, "heideltime_event"
        )

        self.assertIsNotNone(active)
        self.assertEqual([s["target_fact_name"] for s in active], ["heideltime_event"])

    def test_filter_fact_ignores_unrelated_missing_predecessor(self):
        structs = [
            {
                "target_fact_name": "heideltime_raw",
                "run_state_active": True,
                "question": "raw",
                "output_schema": "",
            },
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": True,
                "question": "event",
                "output_schema": "",
                "predecessors": ["heideltime_raw"],
            },
            {
                "target_fact_name": "unrelated",
                "run_state_active": True,
                "question": "other",
                "output_schema": "",
                "predecessors": ["missing_unrelated"],
            },
        ]

        active = self.orchestrator._filter_active_structs(
            structs, "heideltime_event"
        )

        self.assertIsNotNone(active)
        self.assertEqual(
            {s["target_fact_name"] for s in active},
            {"heideltime_raw", "heideltime_event"},
        )

    def test_filter_fact_activates_inactive_target(self):
        """--filter-fact overrules run_state_active: false on the named fact."""
        structs = [
            {
                "target_fact_name": "companies_investigated",
                "run_state_active": False,
                "question": "who",
                "output_schema": "",
            }
        ]

        active = self.orchestrator._filter_active_structs(
            structs, "companies_investigated"
        )

        self.assertIsNotNone(active)
        self.assertEqual(
            [s["target_fact_name"] for s in active], ["companies_investigated"]
        )

    def test_filter_fact_activates_inactive_predecessors(self):
        """The predecessor closure is selected regardless of its run state."""
        structs = [
            {
                "target_fact_name": "heideltime_raw",
                "run_state_active": False,
                "question": "raw",
                "output_schema": "",
            },
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": True,
                "question": "event",
                "output_schema": "",
                "predecessors": ["heideltime_raw"],
            },
        ]

        active = self.orchestrator._filter_active_structs(structs, "heideltime_event")

        self.assertIsNotNone(active)
        self.assertEqual(
            {s["target_fact_name"] for s in active},
            {"heideltime_raw", "heideltime_event"},
        )

        execution_order = self.orchestrator._topological_order(active)
        self.assertLess(
            execution_order.index("heideltime_raw"),
            execution_order.index("heideltime_event"),
        )

    def test_filter_fact_excludes_unrelated_inactive_facts(self):
        """Inactive facts outside the closure stay excluded."""
        structs = [
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": False,
                "question": "event",
                "output_schema": "",
            },
            {
                "target_fact_name": "unrelated",
                "run_state_active": False,
                "question": "other",
                "output_schema": "",
            },
        ]

        active = self.orchestrator._filter_active_structs(structs, "heideltime_event")

        self.assertIsNotNone(active)
        self.assertEqual([s["target_fact_name"] for s in active], ["heideltime_event"])

    def test_no_filter_fact_still_drops_inactive_facts(self):
        """Without --filter-fact the YAML run state governs the selection."""
        structs = [
            {
                "target_fact_name": "active_fact",
                "run_state_active": True,
                "question": "active",
                "output_schema": "",
            },
            {
                "target_fact_name": "inactive_fact",
                "run_state_active": False,
                "question": "inactive",
                "output_schema": "",
            },
        ]

        active = self.orchestrator._filter_active_structs(structs, None)

        self.assertIsNotNone(active)
        self.assertEqual([s["target_fact_name"] for s in active], ["active_fact"])

    def test_all_facts_selects_inactive_facts(self):
        """--all-facts overrules run_state_active for the whole catalogue."""
        structs = [
            {
                "target_fact_name": "active_fact",
                "run_state_active": True,
                "question": "active",
                "output_schema": "",
            },
            {
                "target_fact_name": "inactive_fact",
                "run_state_active": False,
                "question": "inactive",
                "output_schema": "",
            },
            {
                "target_fact_name": "unrelated_inactive_fact",
                "run_state_active": False,
                "question": "other",
                "output_schema": "",
            },
        ]

        active = self.orchestrator._filter_active_structs(structs, None, True)

        self.assertIsNotNone(active)
        self.assertEqual(
            [s["target_fact_name"] for s in active],
            ["active_fact", "inactive_fact", "unrelated_inactive_fact"],
        )

    def test_all_facts_keeps_dangling_predecessor_facts_selected(self):
        """A fact whose predecessor has no config is still selected.

        The runtime, not the selection step, decides whether it can run; see
        the phantom ``products`` predecessor in FACT_EXTRACTOR.yaml.
        """
        structs = [
            {
                "target_fact_name": "NACE_class_code",
                "run_state_active": False,
                "question": "nace",
                "output_schema": "",
                "predecessors": ["products"],
            },
        ]

        active = self.orchestrator._filter_active_structs(structs, None, True)

        self.assertIsNotNone(active)
        self.assertEqual([s["target_fact_name"] for s in active], ["NACE_class_code"])
        self.assertEqual(active[0]["predecessors"], ["products"])

    def test_all_facts_rejects_duplicate_config(self):
        """Duplicate detection applies to the catalogue-wide selection too."""
        structs = [
            {
                "target_fact_name": "duplicated_fact",
                "run_state_active": False,
                "question": "first",
                "output_schema": "",
            },
            {
                "target_fact_name": "duplicated_fact",
                "run_state_active": False,
                "question": "second",
                "output_schema": "",
            },
        ]

        self.assertIsNone(
            self.orchestrator._filter_active_structs(structs, None, True)
        )

    def test_filter_fact_rejects_duplicate_inactive_config(self):
        """Duplicate detection still applies across the widened candidate set."""
        structs = [
            {
                "target_fact_name": "duplicated_fact",
                "run_state_active": True,
                "question": "first",
                "output_schema": "",
            },
            {
                "target_fact_name": "duplicated_fact",
                "run_state_active": False,
                "question": "second",
                "output_schema": "",
            },
        ]

        self.assertIsNone(
            self.orchestrator._filter_active_structs(structs, "duplicated_fact")
        )

    def test_topological_order_keeps_missing_predecessor_before_dependent(self):
        active = [
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": True,
                "question": "event",
                "output_schema": "",
                "predecessors": ["heideltime_raw"],
            }
        ]

        execution_order = self.orchestrator._topological_order(active)
        self.assertLess(
            execution_order.index("heideltime_raw"),
            execution_order.index("heideltime_event"),
        )

    def test_expanded_sorted_structs_execute_predecessor_first(self):
        structs = [
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": True,
                "question": "event",
                "output_schema": "",
                "predecessors": ["heideltime_raw"],
            },
            {
                "target_fact_name": "heideltime_raw",
                "run_state_active": True,
                "question": "raw",
                "output_schema": "",
            },
        ]

        active = self.orchestrator._filter_active_structs(
            structs, "heideltime_event"
        )
        execution_order = self.orchestrator._topological_order(active)
        active.sort(key=lambda s: execution_order.index(s["target_fact_name"]))

        called_facts = []

        async def record_process(param_vo, **_kwargs):
            called_facts.append(param_vo.target_fact_name)
            return {
                "status": "success",
                "data": [{"fact": param_vo.target_fact_name}],
            }

        pipes = []
        for _struct in active:
            pipe = MagicMock()
            pipe.process = AsyncMock(side_effect=record_process)
            pipes.append(pipe)

        executor = PipelineExecutor(
            agents={"universal": MagicMock()},
            pipelines=pipes,
            yaml_params=active,
            overwrite=False,
        )

        res = executor.process_celex("CELEX-TEST", dry_run=False)

        self.assertEqual(
            [s["target_fact_name"] for s in active],
            ["heideltime_raw", "heideltime_event"],
        )
        self.assertEqual(called_facts, ["heideltime_raw", "heideltime_event"])
        self.assertEqual(len(res), 2)

    def test_executor_skips_dependent_fact_without_running_or_inserting(self):
        pipe = MagicMock()
        pipe.process = AsyncMock()
        pipe.ingestor.fact_table.select_fact4celex.return_value = []
        yaml_params = [
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": True,
                "question": "event",
                "output_schema": "",
                "predecessors": ["heideltime_raw"],
            }
        ]
        executor = PipelineExecutor(
            agents={"universal": MagicMock()},
            pipelines=[pipe],
            yaml_params=yaml_params,
            overwrite=False,
        )

        res = executor.process_celex("CELEX-TEST", dry_run=False)

        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "dependency_missing")
        self.assertEqual(res[0]["missing_predecessors"], ["heideltime_raw"])
        pipe.process.assert_not_called()

    def test_executor_continues_after_missing_predecessor(self):
        """A missing predecessor skips only its own fact, not the sequence.

        The transitively dependent fact still self-skips because it re-checks
        its own predecessors, so nothing runs on missing data.
        """
        raw_pipe = MagicMock()
        raw_pipe.process = AsyncMock(
            return_value={"status": "success", "data": []}
        )
        event_pipe = MagicMock()
        event_pipe.process = AsyncMock()
        event_pipe.ingestor.fact_table.select_fact4celex.return_value = []
        agg_pipe = MagicMock()
        agg_pipe.process = AsyncMock()
        agg_pipe.ingestor.fact_table.select_fact4celex.return_value = []
        yaml_params = [
            {
                "target_fact_name": "heideltime_raw",
                "run_state_active": True,
                "question": "raw",
                "output_schema": "",
            },
            {
                "target_fact_name": "heideltime_event",
                "run_state_active": True,
                "question": "event",
                "output_schema": "",
                "predecessors": ["heideltime_raw"],
            },
            {
                "target_fact_name": "heideltime_event_agg",
                "run_state_active": True,
                "question": "agg",
                "output_schema": "",
                "predecessors": ["heideltime_event"],
            },
        ]
        executor = PipelineExecutor(
            agents={"universal": MagicMock()},
            pipelines=[raw_pipe, event_pipe, agg_pipe],
            yaml_params=yaml_params,
            overwrite=False,
        )

        res = executor.process_celex("CELEX-TEST", dry_run=False)

        self.assertEqual(
            [r["status"] for r in res],
            ["success", "dependency_missing", "dependency_missing"],
        )
        event_pipe.process.assert_not_called()
        agg_pipe.process.assert_not_called()

    def test_executor_runs_independent_fact_after_missing_predecessor(self):
        """Facts further down the order still run once one fact is skipped."""
        blocked_pipe = MagicMock()
        blocked_pipe.process = AsyncMock()
        blocked_pipe.ingestor.fact_table.select_fact4celex.return_value = []
        independent_pipe = MagicMock()
        independent_pipe.process = AsyncMock(
            return_value={"status": "success", "data": "value"}
        )
        yaml_params = [
            {
                "target_fact_name": "NACE_class_code",
                "run_state_active": False,
                "question": "nace",
                "output_schema": "",
                "predecessors": ["products"],
            },
            {
                "target_fact_name": "case_commissioner",
                "run_state_active": True,
                "question": "commissioner",
                "output_schema": "",
            },
        ]

        executor = PipelineExecutor(
            agents={"universal": MagicMock()},
            pipelines=[blocked_pipe, independent_pipe],
            yaml_params=yaml_params,
            overwrite=True,
        )

        res = executor.process_celex("CELEX-TEST", dry_run=False)

        self.assertEqual(
            [r["status"] for r in res], ["dependency_missing", "success"]
        )
        independent_pipe.process.assert_called_once()


class TestFactIngestionOrchestratorSelectionFlags(unittest.TestCase):
    """Tests for the mutually exclusive fact-selection CLI flags."""

    def test_all_facts_and_filter_fact_are_mutually_exclusive(self):
        """Passing both selection flags aborts with the argparse exit code."""
        orchestrator = FactIngestionOrchestrator(description="test")
        argv = ["Main_ingest_generic_facts.py", "--all-facts", "--filter-fact", "fines"]

        with (
            patch.object(sys, "argv", argv),
            patch.object(orchestrator, "_configure_llm"),
            patch.object(orchestrator, "_build_agents", return_value={}),
            patch("batch.Main_ingest_generic_facts.FactRepository"),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            orchestrator.run()

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("mutually exclusive", stderr.getvalue())


class TestOverwriteCascadeConfirmation(unittest.TestCase):
    """Tests for the [y/N] gate on --filter-fact combined with --overwrite."""

    STRUCTS = [
        {"target_fact_name": "heideltime_raw"},
        {"target_fact_name": "heideltime_event", "predecessors": ["heideltime_raw"]},
        {"target_fact_name": "heideltime_event_agg", "predecessors": "heideltime_event"},
        {"target_fact_name": "heideltime_cartel_period", "predecessors": ["heideltime_event"]},
        {"target_fact_name": "decision_date", "predecessors": ["heideltime_event_agg"]},
        {"target_fact_name": "fines"},
    ]
    AGG_CHAIN = ["heideltime_raw", "heideltime_event", "heideltime_event_agg"]

    def setUp(self):
        self.orchestrator = FactIngestionOrchestrator(description="test")

    def _args(self, *flags: str):
        return self.orchestrator.runner.parser.parse_args(list(flags))

    def _selected(self, names: list[str]) -> list[dict]:
        return [s for s in self.STRUCTS if s["target_fact_name"] in names]

    def _confirm(self, args, names: list[str], answer: str | None = "") -> tuple[bool, MagicMock]:
        side_effect = EOFError if answer is None else [answer]
        with patch("builtins.input", side_effect=side_effect) as mock_input:
            confirmed = self.orchestrator._confirm_overwrite_cascade(
                args, self.STRUCTS, self._selected(names)
            )
        return confirmed, mock_input

    def test_enter_declines(self):
        """A bare Enter is treated as no."""
        args = self._args("--filter-fact", "heideltime_event_agg", "--overwrite")
        with self.assertLogs("batch.Main_ingest_generic_facts", level="WARNING"):
            confirmed, mock_input = self._confirm(args, self.AGG_CHAIN, "")
        self.assertFalse(confirmed)
        self.assertIn("[y/N]", mock_input.call_args.args[0])

    def test_yes_answer_confirms(self):
        """'y' and 'YES' (case-insensitive, trimmed) confirm the run."""
        args = self._args("--filter-fact", "heideltime_event_agg", "--overwrite")
        for answer in ("y", " YES "):
            with self.subTest(answer=answer), self.assertLogs(level="WARNING"):
                self.assertTrue(self._confirm(args, self.AGG_CHAIN, answer)[0])

    def test_other_answer_and_eof_decline(self):
        """Any other answer, or EOF without a terminal, counts as no."""
        args = self._args("--filter-fact", "heideltime_event_agg", "--overwrite")
        for answer in ("n", "jein", None):
            with self.subTest(answer=answer), self.assertLogs(level="WARNING"):
                self.assertFalse(self._confirm(args, self.AGG_CHAIN, answer)[0])

    def test_warning_lists_predecessors_stale_dependents_and_scope(self):
        """The warning names the cascade, the untouched dependents and the scope."""
        args = self._args("--filter-fact", "heideltime_event_agg", "--overwrite")
        with self.assertLogs("batch.Main_ingest_generic_facts", level="WARNING") as logs:
            self._confirm(args, self.AGG_CHAIN, "")
        output = "\n".join(logs.output)
        self.assertIn("heideltime_event, heideltime_raw", output)
        self.assertIn("decision_date, heideltime_cartel_period", output)
        self.assertNotIn("fines", output)
        self.assertIn("the whole corpus", output)

    def test_yes_flag_skips_prompt(self):
        """--yes still warns but never reads stdin."""
        args = self._args("--filter-fact", "heideltime_event_agg", "--overwrite", "--yes")
        with self.assertLogs(level="WARNING"):
            confirmed, mock_input = self._confirm(args, self.AGG_CHAIN)
        self.assertTrue(confirmed)
        mock_input.assert_not_called()

    def test_no_prompt_without_cascade(self):
        """No prompt without --overwrite, without predecessors, or on --dry-run."""
        cases = [
            (self._args("--filter-fact", "heideltime_event_agg"), self.AGG_CHAIN),
            (self._args("--filter-fact", "fines", "--overwrite"), ["fines"]),
            (
                self._args("--filter-fact", "heideltime_event_agg", "--overwrite", "--dry-run"),
                self.AGG_CHAIN,
            ),
            (self._args("--all-facts", "--overwrite"), [s["target_fact_name"] for s in self.STRUCTS]),
        ]
        for args, names in cases:
            with self.subTest(args=args):
                confirmed, mock_input = self._confirm(args, names)
                self.assertTrue(confirmed)
                mock_input.assert_not_called()

    def test_describe_celex_scope(self):
        """Scope text follows BatchJobRunner's CELEX source precedence."""
        describe = FactIngestionOrchestrator._describe_celex_scope
        self.assertEqual(
            describe(self._args("--filter-celex-id", "CELEX-1", "--celex-list-file", "l.txt")),
            "single document CELEX-1",
        )
        self.assertEqual(
            describe(self._args("--celex-list-file", "l.txt", "--limit", "5")),
            "first 5 of list file l.txt",
        )

    def test_declined_run_aborts_before_llm_setup(self):
        """Declining returns from run() before the LLM or agents are built."""
        argv = ["Main_ingest_generic_facts.py", "--filter-fact", "heideltime_event_agg", "--overwrite"]
        with (
            patch.object(sys, "argv", argv),
            patch.object(self.orchestrator, "_load_yaml_configs", return_value=self.STRUCTS),
            patch.object(self.orchestrator, "_configure_llm") as configure_llm,
            patch.object(self.orchestrator, "_build_agents") as build_agents,
            patch.object(self.orchestrator.runner, "run") as runner_run,
            patch("builtins.input", return_value=""),
            self.assertLogs("batch.Main_ingest_generic_facts", level="WARNING") as logs,
        ):
            self.orchestrator.run()

        configure_llm.assert_not_called()
        build_agents.assert_not_called()
        runner_run.assert_not_called()
        self.assertIn("Run aborted", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
