"""JSON persistence of run plans and the single-orchestrator lock.

One file per run (``run-<run_id>.json``) under ``settings.PIPELINE_STATE_DIR``.
Writes are atomic (temp file + ``os.replace``) so a crash or a DGX hard boot
mid-write never leaves a truncated plan behind.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from batch.main_pipeline.models import RunPlan

logger = logging.getLogger(__name__)

_LOCK_NAME = "main_pipeline.lock"
_OPEN = ("pending", "running", "failed")


class PipelineLocked(RuntimeError):
    """Another live orchestrator holds the lock.

    Attributes:
        pid: Process id of the holder.
    """

    def __init__(self, pid: int) -> None:
        """Record the holder's pid.

        Args:
            pid: Process id written in the lock file.
        """
        super().__init__(f"another Main_Main_Pipeline is running (pid {pid})")
        self.pid = pid


def _pid_alive(pid: int) -> bool:
    """Return True when a process with ``pid`` exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StateStore:
    """Save and reload run plans; guard against two orchestrators."""

    def __init__(self, state_dir: Path) -> None:
        """Bind the store to a directory, creating it when missing.

        Args:
            state_dir: Directory holding the plan files and the lock.
        """
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.state_dir / f"run-{run_id}.json"

    def save(self, plan: RunPlan) -> Path:
        """Write ``plan`` atomically.

        Args:
            plan: The plan to persist.

        Returns:
            Path of the plan file.
        """
        path = self._path(plan.run_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def _plans(self) -> list[RunPlan]:
        """All stored plans, newest first."""
        plans = []
        for path in sorted(self.state_dir.glob("run-*.json"), reverse=True):
            try:
                plans.append(RunPlan.model_validate_json(path.read_text(encoding="utf-8")))
            except ValueError as exc:
                logger.warning("Ignoring unreadable plan file %s: %s", path, exc)
        return plans

    def load_open(self) -> RunPlan | None:
        """Return the newest plan that is pending, running or failed.

        Returns:
            The plan to resume or offer, or None.
        """
        return next((p for p in self._plans() if p.status in _OPEN), None)

    def last_done(self) -> RunPlan | None:
        """Return the newest finished plan, the baseline for "new since".

        Returns:
            The plan, or None when no run has finished yet.
        """
        return next((p for p in self._plans() if p.status == "done"), None)

    def baseline(self) -> datetime | None:
        """Return the time after which outside downloads count as new.

        The finish time of the newest done plan that covered every chapter; a
        plan restricted with ``--only`` never moves the baseline, and a run's own
        downloads happen before its finish. Without such a plan, the creation
        time of the oldest stored plan, so downloads of a failed or superseded
        first run are still detected.

        Returns:
            The baseline, or None when no plan was ever stored.
        """
        plans = self._plans()
        done = next((p for p in plans if p.status == "done" and p.only is None), None)
        if done is not None:
            return done.finished_at or done.created_at
        return min((p.created_at for p in plans), default=None)

    def mark_batch_done(self, plan: RunPlan, step_index: int, case_ids: list[str]) -> None:
        """Record a finished batch and persist the plan.

        Args:
            plan: The running plan (mutated in place).
            step_index: Index of the step in ``plan.steps``.
            case_ids: Cases of the batch that exited 0.
        """
        step = plan.steps[step_index]
        step.done_case_ids.extend(c for c in case_ids if c not in step.done_case_ids)
        self.save(plan)

    def acquire_lock(self) -> None:
        """Take the orchestrator lock, replacing one left by a dead process.

        Raises:
            PipelineLocked: When a live process holds the lock.
        """
        lock = self.state_dir / _LOCK_NAME
        if lock.exists():
            try:
                holder = int(lock.read_text().strip())
            except ValueError:
                holder = -1
            if holder > 0 and holder != os.getpid() and _pid_alive(holder):
                raise PipelineLocked(holder)
            logger.warning("Taking over stale lock of pid %s", holder)
        lock.write_text(str(os.getpid()))

    def release_lock(self) -> None:
        """Remove the lock when this process holds it."""
        lock = self.state_dir / _LOCK_NAME
        if lock.exists() and lock.read_text().strip() == str(os.getpid()):
            lock.unlink()
