"""Tests for the batch runner of the main pipeline.

The subprocess boundary is a fake ``run_fn`` that records argv and returns
scripted exit codes, so batching, stop-on-failure, restart state, progress
lines and the report zip are checked without running any step.
"""

import os
import tempfile
import time
import unittest
import zipfile
from datetime import datetime
from pathlib import Path

from batch.main_pipeline.models import Chapter, RunPlan, StepPlan
from batch.main_pipeline.prerequisites import CheckResult
from batch.main_pipeline.runner import StepRunner, step_argvs, zip_case_summaries
from batch.main_pipeline.state_store import StateStore

_PY = "python"


def _step(**kw: object) -> StepPlan:
    values: dict = {"chapter": Chapter.FACTS, "name": "facts", "module": "batch.Main_ingest_generic_facts"}
    values.update(kw)
    return StepPlan(**values)


def _plan(*steps: StepPlan) -> RunPlan:
    return RunPlan(run_id="20261108200000", created_at=datetime(2026, 11, 8, 20), status="pending", steps=list(steps))


class FakeRun:
    """Records calls; returns codes from a script (default 0)."""

    def __init__(self, codes: list[int] | None = None) -> None:
        self.codes = list(codes or [])
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], log_path: Path) -> int:
        self.calls.append(argv)
        return self.codes.pop(0) if self.codes else 0


def _ok(_need: str) -> CheckResult:
    return CheckResult(name="gpu memory", ok=True, detail="fine")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = StateStore(self.dir / "state")
        self.report_dir = self.dir / "reports"
        (self.report_dir / "case_summary").mkdir(parents=True)

    def _runner(self, run_fn: FakeRun, gpu=_ok, batch_size: int = 25) -> StepRunner:
        return StepRunner(
            store=self.store, run_fn=run_fn, gpu_check=gpu, log_dir=self.dir / "logs",
            batch_size=batch_size, report_dir=self.report_dir, python=_PY,
        )


class TestStepRunner(_Base):
    """Batching, failure and restart."""

    def test_batches_and_marks_done(self) -> None:
        cases = [f"C{i:02d}" for i in range(60)]
        run = FakeRun()
        plan = self._runner(run).run(_plan(_step(case_ids=cases)))
        self.assertEqual(len(run.calls), 3)
        self.assertEqual(plan.steps[0].done_case_ids, cases)
        self.assertEqual((plan.status, plan.steps[0].status), ("done", "done"))
        self.assertEqual(self.store.last_done().run_id, "20261108200000")

    def test_failure_stops_and_records(self) -> None:
        cases = [f"C{i:02d}" for i in range(60)]
        run = FakeRun([0, 1])
        later = _step(chapter=Chapter.ENTITY_GRAPH, name="entity_graph", module="batch.Main_materialize_entity_graph", case_ids=["C1"])
        plan = self._runner(run).run(_plan(_step(case_ids=cases), later))
        self.assertEqual(len(run.calls), 2)
        self.assertEqual(plan.status, "failed")
        self.assertEqual(plan.steps[0].status, "failed")
        self.assertEqual(plan.steps[1].status, "pending")
        self.assertIn("exit 1", plan.failure)
        self.assertIn("20261108200000-facts.log", plan.failure)
        self.assertEqual(self.store.load_open().status, "failed")

    def test_llm_timeout_exit_is_tolerated(self) -> None:
        from batch.BatchJobRunner import EXIT_CODE_LLM_TIMEOUTS

        cases = [f"C{i:02d}" for i in range(60)]
        run = FakeRun([0, EXIT_CODE_LLM_TIMEOUTS, 0])
        later = _step(chapter=Chapter.ENTITY_GRAPH, name="entity_graph", module="batch.Main_materialize_entity_graph", case_ids=["C1"])
        plan = self._runner(run).run(_plan(_step(case_ids=cases), later))
        self.assertEqual(plan.status, "done")
        self.assertEqual(plan.steps[0].done_case_ids, cases)
        self.assertEqual(plan.steps[1].status, "done")
        self.assertEqual(len(plan.tolerated), 1)
        self.assertIn("facts batch 2/3", plan.tolerated[0])
        self.assertEqual(self.store.load_open(), None)

    def test_progress_bar_follows_batches_and_closes(self) -> None:
        events: list[tuple] = []

        class FakeBar:
            def __init__(self, step: StepPlan, later: float) -> None:
                events.append(("open", step.name, later))

            def batch_started(self, done: int) -> None:
                events.append(("start", done))

            def cases_started(self, started: int) -> None:
                events.append(("cases", started))

            def batch_done(self, done: int) -> None:
                events.append(("done", done))

            def close(self) -> None:
                events.append(("close",))

        later = _step(chapter=Chapter.ENTITY_GRAPH, name="entity_graph", module="batch.Main_materialize_entity_graph",
                      case_ids=["A", "B"], estimate_s=120)
        runner = self._runner(FakeRun())
        runner.progress_factory = FakeBar
        runner.run(_plan(_step(case_ids=[f"C{i:02d}" for i in range(30)]), later))
        self.assertEqual(events[:6], [
            ("open", "facts", 120), ("start", 0), ("done", 25), ("start", 25), ("done", 30), ("close",),
        ])
        self.assertEqual(events[6:], [
            ("open", "entity_graph", 0), ("start", 0), ("done", 1), ("done", 2), ("done", 2), ("close",),
        ])

    def test_killed_batch_not_marked_done(self) -> None:
        cases = [f"C{i:02d}" for i in range(30)]
        plan = self._runner(FakeRun([0, -9])).run(_plan(_step(case_ids=cases)))
        self.assertEqual(plan.steps[0].done_case_ids, cases[:25])
        self.assertIn("exit -9", plan.failure)

    def test_resume_skips_done_cases(self) -> None:
        step = _step(case_ids=["A", "B", "C"], done_case_ids=["A", "B"], status="failed")
        run = FakeRun()
        self._runner(run).run(_plan(step))
        listed = Path(run.calls[0][run.calls[0].index("--celex-list-file") + 1]).read_text().split()
        self.assertEqual(listed, ["C"])

    def test_all_cases_done_but_running_step_runs_nothing(self) -> None:
        run = FakeRun()
        steps = [
            _step(case_ids=["A"], done_case_ids=["A"], status="running"),
            _step(chapter=Chapter.DOWNLOAD, name="download", module="batch.Main_download_documents",
                  case_ids=["A"], done_case_ids=["A"], status="running", extra_args=["--force"]),
        ]
        plan = self._runner(run).run(_plan(*steps))
        self.assertEqual(run.calls, [])
        self.assertEqual([s.status for s in plan.steps], ["done", "done"])

    def test_done_plan_records_finish_time(self) -> None:
        plan = self._runner(FakeRun()).run(_plan(_step(case_ids=["A"])))
        self.assertIsNotNone(plan.finished_at)
        self.assertGreaterEqual(plan.finished_at, plan.created_at)

    def test_done_steps_are_skipped(self) -> None:
        run = FakeRun()
        self._runner(run).run(_plan(_step(case_ids=["A"], done_case_ids=["A"], status="done")))
        self.assertEqual(run.calls, [])

    def test_gpu_check_failure_stops_before_step(self) -> None:
        def no_gpu(need: str) -> CheckResult:
            return CheckResult(name="gpu memory", ok=False, detail=f"2000 MiB free, {need} needs 3000 MiB")

        run = FakeRun()
        plan = self._runner(run, gpu=no_gpu).run(_plan(_step(case_ids=["A"], gpu="bge")))
        self.assertEqual(run.calls, [])
        self.assertIn("3000 MiB", plan.failure)

    def test_per_case_step_runs_one_call_per_case(self) -> None:
        run = FakeRun()
        step = _step(chapter=Chapter.ENTITY_GRAPH, name="entity_graph", module="batch.Main_materialize_entity_graph", case_ids=["A", "B"])
        self._runner(run).run(_plan(step))
        self.assertEqual(run.calls, [
            [_PY, "-m", "batch.Main_materialize_entity_graph", "--celex-id", "A"],
            [_PY, "-m", "batch.Main_materialize_entity_graph", "--celex-id", "B"],
        ])

    def test_progress_line_format(self) -> None:
        cases = [f"C{i:02d}" for i in range(60)]
        with self.assertLogs("batch.main_pipeline.progress", "INFO") as logs:
            self._runner(FakeRun()).run(_plan(_step(case_ids=cases)))
        text = "\n".join(logs.output)
        self.assertIn("chapter 3 facts | facts | 25/60 cases |", text)
        self.assertIn("ETA", text)
        self.assertIn("chapter 3 facts finished", text)

    def test_log_path_announced_at_step_start(self) -> None:
        with self.assertLogs("batch.main_pipeline.progress", "INFO") as logs:
            self._runner(FakeRun()).run(_plan(_step(case_ids=["A"])))
        self.assertTrue(any("20261108200000-facts.log" in line for line in logs.output))


class TestStepArgvs(_Base):
    """CLI shape per step kind."""

    def test_facts_uses_list_file_and_force_facts(self) -> None:
        (argv,) = step_argvs(_step(force_facts=["NACE_class_code", "OECD_sector"]), ["A", "B"], self.dir, None, _PY)
        self.assertEqual(argv[:3], [_PY, "-m", "batch.Main_ingest_generic_facts"])
        self.assertEqual(argv[argv.index("--force-facts") + 1], "NACE_class_code,OECD_sector")
        self.assertEqual(Path(argv[argv.index("--celex-list-file") + 1]).read_text().split(), ["A", "B"])

    def test_select_facts_passed(self) -> None:
        (argv,) = step_argvs(_step(select_facts=["OECD_sector", "products"]), ["A"], self.dir, None, _PY)
        self.assertEqual(argv[argv.index("--select-facts") + 1], "OECD_sector,products")

    def test_plain_facts_run_has_no_force(self) -> None:
        (argv,) = step_argvs(_step(), ["A"], self.dir, None, _PY)
        self.assertNotIn("--force-facts", argv)

    def test_ids_steps_repeat_ids(self) -> None:
        step = _step(chapter=Chapter.DOCUMENT_PREPARATION, name="convert", module="batch.Main_convert_pdf_markdown", extra_args=["--mode", "force"])
        (argv,) = step_argvs(step, ["A", "B"], self.dir, None, _PY)
        self.assertEqual(argv[3:], ["--mode", "force", "--ids", "A", "--ids", "B"])

    def test_eddy_gets_this_runs_workbook(self) -> None:
        step = _step(chapter=Chapter.REPORT, name="report_eddy_celex_facts", module="reporting.report_eddy_celex_facts")
        workbook = self.report_dir / "celex_fact_report_20261108201000.xlsx"
        (argv,) = step_argvs(step, [], self.dir, workbook, _PY)
        self.assertEqual(argv[-2:], ["--facts-report", str(workbook)])

    def test_corpus_step_has_no_case_args(self) -> None:
        step = _step(chapter=Chapter.REPORT, name="report_celex_facts", module="reporting.report_celex_facts")
        self.assertEqual(step_argvs(step, [], self.dir, None, _PY), [[_PY, "-m", "reporting.report_celex_facts"]])


class TestReportChapter(_Base):
    """The eddy report gets the workbook of this run; the zip is built in-process."""

    def _report_plan(self) -> RunPlan:
        return _plan(*(
            _step(chapter=Chapter.REPORT, name=name, module=module)
            for name, module in (
                ("report_celex_facts", "reporting.report_celex_facts"),
                ("report_eddy_celex_facts", "reporting.report_eddy_celex_facts"),
                ("zip_case_summary", "builtin.zip_case_summary"),
            )
        ))

    def test_eddy_report_missing_workbook_fails(self) -> None:
        plan = self._runner(FakeRun()).run(self._report_plan())
        self.assertEqual(plan.status, "failed")
        self.assertIn("celex_fact_report", plan.failure)

    def test_report_chain_passes_workbook_and_zips(self) -> None:
        old = self.report_dir / "celex_fact_report_20260101000000.xlsx"
        old.write_text("old")
        os.utime(old, (time.time() - 10 * 86400,) * 2)
        new = self.report_dir / "celex_fact_report_20261108201000.xlsx"
        new.write_text("new")
        (self.report_dir / "case_summary" / "C1_summary.md").write_text("# C1")
        run = FakeRun()
        plan_created = datetime.fromtimestamp(time.time() - 60)  # plan exists before the workbook is written
        plan = self._runner(run).run(self._report_plan().model_copy(update={"created_at": plan_created}))
        self.assertEqual(plan.status, "done", plan.failure)
        self.assertEqual(run.calls[1][-1], str(new))
        self.assertEqual(len(run.calls), 2, "zip runs in-process")
        self.assertEqual(len(list(self.report_dir.glob("case_summary-*.zip"))), 1)


class TestZip(unittest.TestCase):
    """Zip naming and content."""

    def test_zip_name_has_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp)
            (report_dir / "case_summary").mkdir()
            (report_dir / "case_summary" / "C1_summary.md").write_text("# C1")
            path = zip_case_summaries(report_dir, datetime(2026, 11, 8, 18, 30, 5))
            self.assertEqual(path.name, "case_summary-20261108183005.zip")
            with zipfile.ZipFile(path) as archive:
                self.assertIn("case_summary/C1_summary.md", archive.namelist())


if __name__ == "__main__":
    unittest.main()


class TestPlanToIngestSelection(_Base):
    """A planned fact step, resolved by the real ingest CLI against the real catalogue.

    Guards the run_state_active trap: the catalogue switches most facts off for
    plain batch runs, so a step that names no facts would extract only the few
    active ones.
    """

    def _selected(self, step: StepPlan) -> tuple[set[str], frozenset[str]]:
        import sys
        from unittest.mock import MagicMock, patch

        from batch.Main_ingest_generic_facts import FactIngestionOrchestrator

        (argv,) = step_argvs(step, step.case_ids, self.dir, None, _PY)
        orchestrator = FactIngestionOrchestrator(description="test")
        executor_cls = MagicMock()
        with (
            patch.object(sys, "argv", ["Main_ingest_generic_facts.py", *argv[3:]]),
            patch.object(orchestrator, "_configure_llm"),
            patch.object(orchestrator, "_build_agents", return_value={}),
            patch.object(orchestrator, "_build_pipelines", return_value=[]),
            patch.object(orchestrator.runner, "run"),
            patch("batch.Main_ingest_generic_facts.FactRepository"),
            patch("batch.Main_ingest_generic_facts.PipelineExecutor", executor_cls),
        ):
            orchestrator.run()
        kwargs = executor_cls.call_args.kwargs
        return {s["target_fact_name"] for s in kwargs["yaml_params"]}, kwargs["force_facts"]

    def _plan_steps(self, **kw: object) -> list[StepPlan]:
        from batch.fact_dependency_manager import load_structs
        from batch.main_pipeline.planner import build_plan
        from batch.main_pipeline.tests.test_planner import FakeRepo
        from config.settings import settings

        structs = load_structs(settings.FACT_EXTRACTOR_CONFIG_DIR)
        args = dict(refresh_facts=set(), completeness_gaps={}, loader={}, loader_file="l.xlsx",
                    last_done_at=None, only=None, now=datetime(2026, 11, 8, 20), chunk_age_trigger=False)
        args.update(kw)
        repo = kw.pop("repo", FakeRepo())
        args.pop("repo", None)
        return [s for s in build_plan(repo, structs, **args).steps if s.chapter == Chapter.FACTS], structs

    def test_new_decision_gets_every_fact_but_summary(self) -> None:
        from batch.main_pipeline.tests.test_planner import FakeRepo

        steps, structs = self._plan_steps(repo=FakeRepo(changes=({"D9"}, set())))
        (step,) = steps
        selected, forced = self._selected(step)
        expected = {s["target_fact_name"] for s in structs} - {"case_summary"}
        self.assertEqual(selected, expected)
        self.assertEqual(forced, frozenset())

    def test_gap_fill_selects_the_gap_fact(self) -> None:
        steps, _ = self._plan_steps(completeness_gaps={"OECD_sector": {"C1"}})
        (step,) = steps
        selected, forced = self._selected(step)
        self.assertIn("OECD_sector", selected)
        self.assertIn("products", selected, "predecessor is selected for ordering")
        self.assertEqual(forced, frozenset())
