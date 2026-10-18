"""Unit tests for the thermal gate wired into BatchJobRunner.

The gate itself is a MagicMock at the module boundary (patched in
``batch.BatchJobRunner``'s namespace, where the runner looks it up), so no test
opens an SSH session or sleeps. The assertions target what the runner does with
an abort: which CELEX IDs it treats as unprocessed, what it writes, and which
exit code it hands back to the shell.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from batch import BatchJobRunner as runner_module
from batch.BatchJobRunner import EXIT_CODE_THERMAL_ABORT, BatchJobRunner
from config.ollama_health_gate import HealthCheckResult, ThermalAbort, ThermalReading
from config.settings import settings

_CELEX_IDS = ["CELEX-A", "CELEX-B", "CELEX-C", "CELEX-D", "CELEX-E"]


def _reading() -> ThermalReading:
    """Build a plausible cool reading for the gate to hand back.

    Returns:
        A validated :class:`ThermalReading`.
    """
    return ThermalReading.model_validate(
        {
            "schema": 1,
            "hostname": "spark-niki",
            "timestamp_utc": "2026-09-02T17:39:38+00:00",
            "gpu_name": "NVIDIA GB10",
            "gpu_temp_c": 66.0,
            "soc_temp_c": 74.0,
            "gate_temp_c": 74.0,
            "utilization_pct": 88,
            "clock_mhz": 2405,
            "clock_max_mhz": 3003,
            "zones": {"acpitz-0": 74.0},
        }
    )


def _healthy() -> HealthCheckResult:
    """Build the verdict a passing gate returns.

    Returns:
        A healthy :class:`HealthCheckResult` carrying a reading.
    """
    return HealthCheckResult(
        healthy=True, reason="74.0C is below the limit", reading=_reading()
    )


def _abort() -> ThermalAbort:
    """Build an abort as the gate would raise it.

    Returns:
        A :class:`ThermalAbort` carrying an unhealthy result.
    """
    return ThermalAbort(
        HealthCheckResult(healthy=False, reason="84.0C at or above the 80.0C limit")
    )


class _RunnerFixture:
    """A temp project root, a CELEX list file and a recording process_fn."""

    def __init__(self, stack: unittest.TestCase) -> None:
        """Create the temporary tree and register its cleanup.

        Args:
            stack: The test case, used for ``addCleanup``.
        """
        self.root = Path(tempfile.mkdtemp())
        stack.addCleanup(self._cleanup)
        (self.root / "logs").mkdir()

        self.list_file = self.root / "celex_list.txt"
        self.list_file.write_text("\n".join(_CELEX_IDS) + "\n")

        self.processed: list[str] = []

    def _cleanup(self) -> None:
        """Remove the temporary tree."""
        import shutil

        shutil.rmtree(self.root, ignore_errors=True)

    def process_fn(self, celex_id: str, context, dry_run: bool) -> bool:
        """Record a processed CELEX and report success."""
        self.processed.append(celex_id)
        return True

    def resume_files(self) -> list[Path]:
        """Return every resume file the runner wrote."""
        return sorted((self.root / "logs").glob("thermal_abort_*.txt"))

    def argv(self, *extra: str) -> list[str]:
        """Build a CLI invocation against the fixture's list file.

        Args:
            *extra: Additional flags to append.

        Returns:
            An ``sys.argv`` list.
        """
        return [
            "prog",
            "--celex-list-file",
            str(self.list_file),
            "--audit-file",
            "",
            *extra,
        ]


class TestThermalAbortHandling(unittest.TestCase):
    """What happens when the gate stops a run part-way through."""

    def setUp(self) -> None:
        """Build the fixture, pin the project root, and mute the alert sound."""
        self.fixture = _RunnerFixture(self)
        self.logged: list[str] = []
        for patcher in (
            mock.patch.object(settings, "PRJ_ROOT_DIR", self.fixture.root),
            # The abort path really does play five sounds; a test suite must
            # not make noise, and must not spend the seconds doing it.
            mock.patch.object(runner_module, "play_alert_sound"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, gate: mock.MagicMock, *extra: str) -> int:
        """Run the batch with a mocked gate and return its exit code.

        The runner's log records are captured rather than allowed to reach the
        console. That is not tidiness: the abort path emits a CRITICAL "RUN
        STOPPED BY THE DGX THERMAL GATE" banner naming a temperature and a
        resume command, and on a terminal that is indistinguishable from a real
        incident on the real box. Captured output is left in ``self.logged`` for
        assertions.

        Args:
            gate: The gate instance the runner should use.
            *extra: Extra CLI flags.

        Returns:
            The code passed to ``sys.exit``.
        """
        with (
            mock.patch.object(sys, "argv", self.fixture.argv(*extra)),
            mock.patch.object(runner_module, "OllamaHealthGate", return_value=gate),
        ):
            # assertLogs sets propagate=False on this logger for its duration,
            # which is what keeps the banner off the console.
            with self.assertLogs("batch.BatchJobRunner", level="INFO") as captured:
                with self.assertRaises(SystemExit) as caught:
                    BatchJobRunner("test").run(self.fixture.process_fn)
        self.logged = captured.output
        return caught.exception.code

    def test_abort_stops_the_loop_and_records_the_remainder(self) -> None:
        """A gate that trips on the third CELEX leaves three unprocessed.

        The two already extracted stay committed; the resume file names exactly
        what never started, so no work is lost and none is repeated.
        """
        gate = mock.MagicMock()
        # The pre-flight check consumes the first call, so three passes admit
        # CELEX-A and CELEX-B and the abort lands on CELEX-C.
        gate.check_or_wait.side_effect = [_healthy(), _healthy(), _healthy(), _abort()]

        code = self._run(gate)

        self.assertEqual(code, EXIT_CODE_THERMAL_ABORT)
        self.assertEqual(self.fixture.processed, ["CELEX-A", "CELEX-B"])

        resume_files = self.fixture.resume_files()
        self.assertEqual(len(resume_files), 1)
        self.assertEqual(
            resume_files[0].read_text().split(),
            ["CELEX-C", "CELEX-D", "CELEX-E"],
        )

        # The banner is the operator's only in-terminal account of the stop, so
        # it must name the cause and the exact resume path.
        banner = "\n".join(self.logged)
        self.assertIn("RUN STOPPED BY THE DGX THERMAL GATE", banner)
        self.assertIn(str(resume_files[0]), banner)

    def test_preflight_abort_records_every_celex(self) -> None:
        """A box already too hot at startup costs no partial work at all."""
        gate = mock.MagicMock()
        gate.check_or_wait.side_effect = _abort()

        code = self._run(gate)

        self.assertEqual(code, EXIT_CODE_THERMAL_ABORT)
        self.assertEqual(self.fixture.processed, [])
        self.assertEqual(self.fixture.resume_files()[0].read_text().split(), _CELEX_IDS)

    def test_healthy_run_completes_with_the_normal_exit_code(self) -> None:
        """A passing gate is invisible: every CELEX runs and nothing is written."""
        gate = mock.MagicMock()
        gate.check_or_wait.return_value = _healthy()

        code = self._run(gate)

        self.assertEqual(code, 0)
        self.assertEqual(self.fixture.processed, _CELEX_IDS)
        self.assertEqual(self.fixture.resume_files(), [])

    def test_run_starts_by_resetting_the_gpu_clock(self) -> None:
        """Every run begins at default clocks, whatever killed the last one.

        A cap set with nvidia-smi -lgc persists on the device, so a batch
        killed with SIGKILL would otherwise leave the Spark throttled for every
        later run with nothing left to undo it.
        """
        gate = mock.MagicMock()
        gate.check_or_wait.return_value = _healthy()

        self._run(gate)

        gate.clock_controller.ensure_default.assert_called_once()

    def test_thermal_abort_lifts_the_cap_before_exiting(self) -> None:
        """A run stopped by heat must not bequeath its throttle to the box."""
        gate = mock.MagicMock()
        gate.check_or_wait.side_effect = _abort()

        self._run(gate)

        gate.clock_controller.restore.assert_called_once()

    def test_thermal_measures_are_logged_at_info(self) -> None:
        """Every CELEX records how hot the box was when it started.

        Over a long run these lines are the only record of the thermal profile,
        which is what a post-mortem after a silent shutdown needs and what
        tuning THERMAL_MAX_TEMP_C needs.
        """
        gate = mock.MagicMock()
        gate.check_or_wait.return_value = _healthy()

        self._run(gate)

        thermal_lines = [line for line in self.logged if "DGX thermal" in line]
        # One pre-flight line plus one per CELEX.
        self.assertEqual(len(thermal_lines), len(_CELEX_IDS) + 1)
        self.assertIn("DGX thermal pre-flight", thermal_lines[0])
        self.assertIn("gate=74.0C", thermal_lines[1])
        self.assertIn("gpu=66.0C", thermal_lines[1])
        self.assertIn("soc=74.0C", thermal_lines[1])
        self.assertIn("clock=2405/3003MHz", thermal_lines[1])
        self.assertIn("CELEX-A", thermal_lines[1])

    def test_resume_file_feeds_back_through_celex_list_file(self) -> None:
        """The resume file is consumable by the flag that already exists."""
        gate = mock.MagicMock()
        # The pre-flight check consumes the first call, so three passes admit
        # CELEX-A and CELEX-B and the abort lands on CELEX-C.
        gate.check_or_wait.side_effect = [_healthy(), _healthy(), _healthy(), _abort()]
        self._run(gate)
        resume_path = self.fixture.resume_files()[0]

        second_gate = mock.MagicMock()
        second_gate.check_or_wait.return_value = _healthy()
        self.fixture.processed.clear()

        argv = ["prog", "--celex-list-file", str(resume_path), "--audit-file", ""]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(
                runner_module, "OllamaHealthGate", return_value=second_gate
            ),
            # Captured, not printed -- see _run.
            self.assertLogs("batch.BatchJobRunner", level="INFO"),
        ):
            with self.assertRaises(SystemExit) as caught:
                BatchJobRunner("test").run(self.fixture.process_fn)

        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(self.fixture.processed, ["CELEX-C", "CELEX-D", "CELEX-E"])


class TestGateConstruction(unittest.TestCase):
    """When the runner builds a gate at all."""

    def setUp(self) -> None:
        """Build the fixture and pin the project root to it."""
        self.fixture = _RunnerFixture(self)
        patcher = mock.patch.object(settings, "PRJ_ROOT_DIR", self.fixture.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_expecting_no_gate(self, *extra: str) -> None:
        """Run the batch and assert no gate was constructed.

        Args:
            *extra: Extra CLI flags.
        """
        with (
            mock.patch.object(sys, "argv", self.fixture.argv(*extra)),
            mock.patch.object(runner_module, "OllamaHealthGate") as gate_cls,
            # Captured, not printed -- see TestThermalAbortHandling._run.
            self.assertLogs("batch.BatchJobRunner", level="INFO"),
        ):
            with self.assertRaises(SystemExit):
                BatchJobRunner("test").run(self.fixture.process_fn)
        gate_cls.assert_not_called()

    def test_dry_run_needs_no_dgx(self) -> None:
        """A dry run issues no LLM calls, so it must run with the box off."""
        self._run_expecting_no_gate("--dry-run")

    def test_no_thermal_gate_flag_disables_it(self) -> None:
        """The operator override skips construction entirely."""
        self._run_expecting_no_gate("--no-thermal-gate")

    def test_settings_switch_disables_it(self) -> None:
        """THERMAL_GATE_ENABLED=false is honoured without a CLI flag."""
        with mock.patch.object(settings, "THERMAL_GATE_ENABLED", False):
            self._run_expecting_no_gate()


if __name__ == "__main__":
    unittest.main()
