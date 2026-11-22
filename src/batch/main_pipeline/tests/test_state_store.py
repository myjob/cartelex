"""Tests for the run plan models and the JSON state store.

The store is the restart memory of the main pipeline: a plan saved as pending
or failed must come back exactly, and a crash must never leave a half-written
file or a lock that blocks the next run forever.
"""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from batch.main_pipeline.models import Chapter, RunPlan, StepPlan
from batch.main_pipeline.state_store import PipelineLocked, StateStore


def _plan(run_id: str = "20261108200000", status: str = "pending") -> RunPlan:
    return RunPlan(
        run_id=run_id,
        created_at=datetime(2026, 11, 8, 20, 0, 0),
        status=status,
        triggers=["products refreshed"],
        steps=[
            StepPlan(
                chapter=Chapter.FACTS,
                name="facts",
                module="batch.Main_ingest_generic_facts",
                case_ids=["C1", "C2", "C3"],
                force_facts=["NACE_class_code"],
                estimate_s=90.0,
                gpu="bge",
            )
        ],
    )


class TestModels(unittest.TestCase):
    """Derived plan properties."""

    def test_estimate_sums_steps(self) -> None:
        plan = _plan()
        plan.steps.append(plan.steps[0].model_copy(update={"estimate_s": 10.0}))
        self.assertEqual(plan.estimate_s, 100.0)

    def test_empty_plan(self) -> None:
        self.assertTrue(_plan().model_copy(update={"steps": []}).is_empty)
        self.assertFalse(_plan().is_empty)

    def test_remaining_cases(self) -> None:
        step = _plan().steps[0].model_copy(update={"done_case_ids": ["C2"]})
        self.assertEqual(step.remaining_case_ids, ["C1", "C3"])


class TestStateStore(unittest.TestCase):
    """Save, reload and lock."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = StateStore(self.dir)

    def test_round_trip(self) -> None:
        plan = _plan()
        self.store.save(plan)
        self.assertEqual(self.store.load_open(), plan)

    def test_load_open_ignores_done_and_takes_newest(self) -> None:
        self.store.save(_plan("20261101000000", "failed"))
        self.store.save(_plan("20261105000000", "pending"))
        self.store.save(_plan("20261107000000", "done"))
        self.assertEqual(self.store.load_open().run_id, "20261105000000")

    def test_load_open_none_when_nothing_open(self) -> None:
        self.assertIsNone(self.store.load_open())

    def test_last_done(self) -> None:
        self.store.save(_plan("20261101000000", "done"))
        self.store.save(_plan("20261105000000", "done"))
        self.store.save(_plan("20261107000000", "failed"))
        self.assertEqual(self.store.last_done().run_id, "20261105000000")

    def test_baseline_is_finish_of_last_full_done_plan(self) -> None:
        finished = datetime(2026, 11, 8, 23, 0)
        self.store.save(_plan("20261108200000", "done").model_copy(update={"finished_at": finished}))
        only = _plan("20261109000000", "done").model_copy(
            update={"finished_at": datetime(2026, 11, 9, 1), "only": Chapter.REPORT}
        )
        self.store.save(only)
        self.assertEqual(self.store.baseline(), finished)

    def test_baseline_falls_back_to_oldest_plan(self) -> None:
        self.store.save(_plan("20261105000000", "superseded"))
        self.store.save(_plan("20261107000000", "failed").model_copy(update={"created_at": datetime(2026, 11, 7)}))
        self.assertEqual(self.store.baseline(), datetime(2026, 11, 7))

    def test_baseline_none_without_plans(self) -> None:
        self.assertIsNone(self.store.baseline())

    def test_mark_batch_done_persists(self) -> None:
        plan = _plan(status="running")
        self.store.mark_batch_done(plan, 0, ["C1", "C2"])
        reloaded = self.store.load_open()
        self.assertEqual(reloaded.steps[0].done_case_ids, ["C1", "C2"])

    def test_atomic_write_leaves_no_temp_file(self) -> None:
        self.store.save(_plan())
        self.assertEqual([p.name for p in self.dir.glob("*.tmp")], [])

    def test_save_writes_json_named_by_run_id(self) -> None:
        path = self.store.save(_plan())
        self.assertEqual(path.name, "run-20261108200000.json")
        self.assertEqual(json.loads(path.read_text())["status"], "pending")

    def test_live_lock_blocks(self) -> None:
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.kill)
        (self.dir / "main_pipeline.lock").write_text(str(child.pid))
        with self.assertRaises(PipelineLocked) as ctx:
            self.store.acquire_lock()
        self.assertEqual(ctx.exception.pid, child.pid)

    def test_stale_lock_is_taken_over(self) -> None:
        child = subprocess.Popen(["true"])
        child.wait()
        (self.dir / "main_pipeline.lock").write_text(str(child.pid))
        self.store.acquire_lock()
        self.assertEqual((self.dir / "main_pipeline.lock").read_text(), str(os.getpid()))

    def test_release_lock_removes_file(self) -> None:
        self.store.acquire_lock()
        self.store.release_lock()
        self.assertFalse((self.dir / "main_pipeline.lock").exists())


if __name__ == "__main__":
    unittest.main()
