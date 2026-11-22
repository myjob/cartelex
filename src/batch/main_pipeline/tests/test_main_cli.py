"""Tests for the Main_Main_Pipeline CLI flow.

Planner, prerequisites, runner and the operator's keyboard are fakes; the
state store is real (temp dir) because the gate and resume behaviour is about
what gets saved.
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from batch import Main_Main_Pipeline as cli
from batch.main_pipeline.models import Chapter, RunPlan, StepPlan
from batch.main_pipeline.prerequisites import CheckResult
from batch.main_pipeline.state_store import StateStore


def _plan(estimate_s: float = 60.0, facts: list[str] | None = None, run_id: str = "20261108200000") -> RunPlan:
    return RunPlan(
        run_id=run_id,
        created_at=datetime(2026, 11, 8, 20),
        triggers=["products: input changed for 3 decisions"],
        steps=[StepPlan(
            chapter=Chapter.FACTS, name="facts", module="batch.Main_ingest_generic_facts",
            case_ids=["C1", "C2", "C3"], force_facts=facts or ["OECD_sector"], estimate_s=estimate_s, gpu="bge",
        )],
    )


class _Harness:
    """Collects what the fakes were asked to do."""

    def __init__(self, store: StateStore, plan: RunPlan | None = None, **kw: object) -> None:
        self.store = store
        self.plan = plan if plan is not None else _plan()
        self.prereq_ok = kw.get("prereq_ok", True)
        self.run_status = kw.get("run_status", "done")
        self.answers = list(kw.get("answers", []))
        self.tty = kw.get("tty", True)
        self.runs: list[RunPlan] = []
        self.built = 0
        self.prompts: list[str] = []
        self.run_raises = kw.get("run_raises")
        self.baselines: list[datetime | None] = []

    def deps(self) -> cli.CliDeps:
        def prerequisites() -> list[CheckResult]:
            return [CheckResult(name="dgx", ok=self.prereq_ok, detail="down" if not self.prereq_ok else "ok")]

        def build(args: object, last_done_at: datetime | None) -> RunPlan:
            self.built += 1
            self.baselines.append(last_done_at)
            return self.plan.model_copy(deep=True)

        def run(plan: RunPlan) -> RunPlan:
            if self.run_raises:
                raise self.run_raises
            self.runs.append(plan)
            plan.status = self.run_status
            plan.failure = "facts exit 1 at batch 1/1, log /x.log" if self.run_status == "failed" else None
            self.store.save(plan)
            return plan

        def ask(prompt: str) -> str:
            self.prompts.append(prompt)
            if not self.answers:
                raise EOFError
            return self.answers.pop(0)

        return cli.CliDeps(
            store=self.store, prerequisites=prerequisites,
            gpu_check=lambda need: CheckResult(name="gpu memory", ok=True, detail="ok"),
            build_plan=build, run_plan=run,
            known_facts=lambda: {"OECD_sector", "products", "case_summary"},
            input_fn=ask, isatty=lambda: self.tty,
        )


class TestMainCli(unittest.TestCase):
    """Gate, pending plans, resume, failures, lock."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = StateStore(Path(tmp.name))

    def _main(self, argv: list[str], h: _Harness) -> int:
        return cli.main(argv, deps=h.deps())

    def test_short_plan_runs_without_asking(self) -> None:
        h = _Harness(self.store)
        self.assertEqual(self._main([], h), 0)
        self.assertEqual((len(h.runs), h.prompts), (1, []))

    def test_long_plan_asks_and_no_saves_pending(self) -> None:
        h = _Harness(self.store, _plan(estimate_s=3 * 3600), answers=["n"])
        with self.assertLogs("batch.main_pipeline.progress", "INFO") as logs:
            self.assertEqual(self._main([], h), 0)
        self.assertEqual(h.runs, [])
        self.assertEqual(self.store.load_open().status, "pending")
        self.assertTrue(any("--resume" in line for line in logs.output))

    def test_long_plan_yes_answer_runs(self) -> None:
        h = _Harness(self.store, _plan(estimate_s=3 * 3600), answers=["y"])
        self.assertEqual(self._main([], h), 0)
        self.assertEqual(len(h.runs), 1)

    def test_eof_at_gate_saves_pending(self) -> None:
        h = _Harness(self.store, _plan(estimate_s=3 * 3600), answers=[])
        self.assertEqual(self._main([], h), 0)
        self.assertEqual((h.runs, self.store.load_open().status), ([], "pending"))

    def test_non_tty_long_plan_saves_pending_without_prompt(self) -> None:
        h = _Harness(self.store, _plan(estimate_s=3 * 3600), tty=False)
        self.assertEqual(self._main([], h), 0)
        self.assertEqual((h.prompts, h.runs), ([], []))

    def test_yes_flag_skips_gate(self) -> None:
        h = _Harness(self.store, _plan(estimate_s=3 * 3600), tty=False)
        self.assertEqual(self._main(["--yes"], h), 0)
        self.assertEqual(len(h.runs), 1)

    def test_resume_runs_saved_plan_without_replanning(self) -> None:
        saved = _plan(estimate_s=3 * 3600)
        self.store.save(saved)
        h = _Harness(self.store)
        self.assertEqual(self._main(["--resume"], h), 0)
        self.assertEqual((h.built, h.runs[0].run_id), (0, saved.run_id))

    def test_resume_out_of_date_plan_refused(self) -> None:
        self.store.save(_plan(facts=["retired_fact"]))
        h = _Harness(self.store)
        self.assertEqual(self._main(["--resume"], h), 2)
        self.assertEqual(h.runs, [])

    def test_resume_without_open_plan_is_usage_error(self) -> None:
        self.assertEqual(self._main(["--resume"], _Harness(self.store)), 2)

    def test_open_plan_offered_and_replaced(self) -> None:
        self.store.save(_plan(run_id="20261101000000"))
        h = _Harness(self.store, answers=["r"])
        self.assertEqual(self._main([], h), 0)
        self.assertEqual(h.built, 1)
        self.assertEqual(len(h.prompts), 1)
        old = RunPlan.model_validate_json((self.store.state_dir / "run-20261101000000.json").read_text())
        self.assertEqual(old.status, "superseded")

    def test_open_plan_kept_when_declined(self) -> None:
        self.store.save(_plan(run_id="20261101000000"))
        h = _Harness(self.store, answers=["n"])
        self.assertEqual(self._main([], h), 0)
        self.assertEqual((h.built, h.runs), (0, []))
        self.assertEqual(self.store.load_open().run_id, "20261101000000")

    def test_prerequisite_failure_exits_3_and_runs_nothing(self) -> None:
        h = _Harness(self.store, prereq_ok=False)
        self.assertEqual(self._main([], h), 3)
        self.assertEqual((h.built, h.runs), (0, []))

    def test_failure_exit_1_logs_resume_command(self) -> None:
        h = _Harness(self.store, run_status="failed")
        with self.assertLogs("batch.main_pipeline.progress", "INFO") as logs:
            self.assertEqual(self._main([], h), 1)
        self.assertTrue(any("--resume" in line and "Main_Main_Pipeline" in line for line in logs.output))

    def test_dry_run_runs_nothing_and_saves_nothing(self) -> None:
        h = _Harness(self.store)
        with self.assertLogs("batch.main_pipeline.progress", "INFO") as logs:
            self.assertEqual(self._main(["--dry-run"], h), 0)
        self.assertEqual(h.runs, [])
        self.assertIsNone(self.store.load_open())
        self.assertTrue(any("OECD_sector" in line for line in logs.output), "plan summary shown")

    def test_empty_plan_nothing_to_do(self) -> None:
        h = _Harness(self.store, RunPlan(run_id="20261108200000", created_at=datetime(2026, 11, 8)))
        with self.assertLogs("batch.main_pipeline.progress", "INFO") as logs:
            self.assertEqual(self._main([], h), 0)
        self.assertEqual(h.runs, [])
        self.assertTrue(any("nothing to do" in line for line in logs.output))

    def test_status_without_lock(self) -> None:
        self.store.save(_plan())
        (self.store.state_dir / "main_pipeline.lock").write_text("1")  # pid 1 is alive
        with self.assertLogs("batch.main_pipeline.progress", "INFO") as logs:
            self.assertEqual(self._main(["--status"], _Harness(self.store)), 0)
        self.assertTrue(any(" 0/3 " in line and "pending" in line for line in logs.output))

    def test_locked_exits_3(self) -> None:
        (self.store.state_dir / "main_pipeline.lock").write_text("1")
        self.assertEqual(self._main([], _Harness(self.store)), 3)

    def test_lock_released_after_exception(self) -> None:
        h = _Harness(self.store, run_raises=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self._main([], h)
        self.assertFalse((self.store.state_dir / "main_pipeline.lock").exists())

    def test_build_plan_gets_store_baseline(self) -> None:
        finished = datetime(2026, 11, 8, 23, 30)
        self.store.save(_plan(run_id="20261101000000").model_copy(update={"status": "done", "finished_at": finished}))
        h = _Harness(self.store)
        self._main([], h)
        self.assertEqual(h.baselines, [finished])

    def test_skip_case_removes_case_from_open_plan(self) -> None:
        self.store.save(_plan().model_copy(update={"status": "failed"}))
        h = _Harness(self.store)
        self.assertEqual(self._main(["--resume", "--skip-case", "C2"], h), 0)
        self.assertEqual(h.runs[0].steps[0].case_ids, ["C1", "C3"])
        self.assertTrue(any("C2" in t for t in h.runs[0].triggers))

    def test_skip_case_requires_resume(self) -> None:
        self.assertEqual(self._main(["--skip-case", "C2"], _Harness(self.store)), 2)

    def test_yes_at_offer_refuses_out_of_date_plan(self) -> None:
        self.store.save(_plan(facts=["retired_fact"]))
        h = _Harness(self.store, answers=["y"])
        self.assertEqual(self._main([], h), 2)
        self.assertEqual(h.runs, [])

    def test_unknown_refresh_fact_exits_2(self) -> None:
        h = _Harness(self.store)
        self.assertEqual(self._main(["--refresh-fact", "prodcts"], h), 2)
        self.assertEqual(h.built, 0)

    def test_refresh_case_summary_exits_2(self) -> None:
        h = _Harness(self.store)
        self.assertEqual(self._main(["--refresh-fact", "case_summary"], h), 2)
        self.assertEqual(h.built, 0)

    def test_only_and_refresh_reach_the_planner(self) -> None:
        args = cli.parse_args(["--only", "report", "--refresh-fact", "products", "--refresh-fact", "NACE_class_code"])
        self.assertEqual(args.only, Chapter.REPORT)
        self.assertEqual(args.refresh_fact, ["products", "NACE_class_code"])


if __name__ == "__main__":
    unittest.main()
