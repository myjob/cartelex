"""Unit tests for the DGX health gate.

No test opens an SSH session, makes an HTTP request or sleeps: the gate's four
collaborators (``ping_fn``, ``probe_fn``, ``sleep_fn``, ``alert_fn``) are
injected, which is the whole point of the Provisioner shape. Settings are
pinned with ``mock.patch.object`` so a value in the environment cannot change
what a test asserts.
"""

import json
import threading
import unittest
from contextlib import ExitStack
from unittest import mock

from config.ollama_health_gate import (
    EXIT_CODE_THERMAL_ABORT,
    HealthCheckResult,
    OllamaHealthGate,
    ProbeUnavailable,
    ThermalAbort,
    ThermalReading,
    build_thermal_gate,
    dgx_ssh_host,
    stop_on_thermal_abort,
)
from config.settings import settings
from config.tests import silence_logger


def _payload(gate_temp_c: float, **overrides) -> str:
    """Build a probe response as the remote script would emit it.

    Args:
        gate_temp_c: The temperature the gate will compare against its limit.
        **overrides: Fields to replace in the default payload.

    Returns:
        A JSON string.
    """
    body = {
        "schema": 1,
        "hostname": "spark-niki",
        "timestamp_utc": "2026-09-02T17:39:38+00:00",
        "gpu_name": "NVIDIA GB10",
        "gpu_temp_c": gate_temp_c - 8.0,
        "soc_temp_c": gate_temp_c,
        "gate_temp_c": gate_temp_c,
        "utilization_pct": 88,
        "throttle_reasons": "0x0000000000000000",
        "clock_mhz": 2405,
        "clock_max_mhz": 3003,
        "zones": {"acpitz-0": gate_temp_c},
    }
    body.update(overrides)
    return json.dumps(body)


class _GateHarness:
    """Records what the gate did, standing in for its four boundaries."""

    def __init__(self, payloads: list[str], ping: bool = True) -> None:
        """Queue the probe responses the gate will receive.

        Args:
            payloads: Returned in order; the last one repeats once exhausted.
            ping: What the liveness probe reports.
        """
        self.payloads = payloads
        self.ping = ping
        self.probe_calls = 0
        self.sleeps: list[float] = []
        self.alerts: list[tuple[str, int, int]] = []
        # Injected rather than left to default: a real GpuClockController would
        # open an SSH session on every breach.
        self.clock = mock.MagicMock()

    def ping_fn(self) -> bool:
        """Report Ollama liveness."""
        return self.ping

    def probe_fn(self) -> str:
        """Return the next queued probe response."""
        index = min(self.probe_calls, len(self.payloads) - 1)
        self.probe_calls += 1
        return self.payloads[index]

    def sleep_fn(self, seconds: float) -> None:
        """Record a cooldown instead of serving it."""
        self.sleeps.append(seconds)

    def alert_fn(self, result: HealthCheckResult, cycle: int, max_cycles: int) -> None:
        """Record an operator alert."""
        self.alerts.append((result.reason, cycle, max_cycles))

    def build(self) -> OllamaHealthGate:
        """Return a gate wired to this harness."""
        return OllamaHealthGate(
            ping_fn=self.ping_fn,
            probe_fn=self.probe_fn,
            sleep_fn=self.sleep_fn,
            alert_fn=self.alert_fn,
            clock_controller=self.clock,
        )


def _pinned(**overrides):
    """Pin settings for the duration of a ``with`` block.

    Args:
        **overrides: Setting names and the values to pin them to.

    Returns:
        A context manager applying every patch.
    """
    defaults = {
        "THERMAL_GATE_ENABLED": True,
        "THERMAL_MAX_TEMP_C": 80.0,
        "THERMAL_COOLDOWN_SECONDS": 150.0,
        "THERMAL_MAX_COOLDOWN_CYCLES": 3,
        "THERMAL_READING_TTL_SECONDS": 30.0,
        "THERMAL_GATE_STRICT": True,
    }
    defaults.update(overrides)
    stack = ExitStack()
    for name, value in defaults.items():
        stack.enter_context(mock.patch.object(settings, name, value))
    return stack


class _QuietTestCase(unittest.TestCase):
    """Base that keeps the gate's loud CRITICAL/WARNING lines off the console."""

    def setUp(self) -> None:
        """Quieten the gate logger for the duration of each test."""
        silence_logger(self, "config.ollama_health_gate")


class TestThermalReadingValidation(_QuietTestCase):
    """Boundary validation of the JSON arriving over SSH."""

    def test_valid_payload_round_trips(self) -> None:
        """A well-formed reading validates and describes itself."""
        reading = ThermalReading.model_validate(json.loads(_payload(74.0)))

        self.assertEqual(reading.gate_temp_c, 74.0)
        self.assertIn("gate=74.0C", reading.describe())

    def test_newer_probe_schema_is_accepted(self) -> None:
        """A probe newer than this client must not disarm a running batch.

        The contract is additive, so a newer probe only adds optional fields.
        Rejecting it would mean that redeploying the probe silently turned
        every already-running batch's readings into "temperature unreadable" --
        which under THERMAL_GATE_STRICT walks a healthy run to a graceful abort
        with the box sitting at 37 C. That regression is what this test locks.
        """
        reading = ThermalReading.model_validate(
            json.loads(_payload(74.0, schema=99, some_future_field="ignored"))
        )

        self.assertEqual(reading.gate_temp_c, 74.0)

    def test_pre_minimum_schema_is_rejected(self) -> None:
        """A genuinely incompatible contract still fails loudly."""
        with self.assertRaises(ValueError):
            ThermalReading.model_validate(json.loads(_payload(74.0, schema=0)))

    def test_implausible_temperature_is_rejected(self) -> None:
        """A garbled payload must never validate into a plausible 'cool'."""
        with self.assertRaises(ValueError):
            ThermalReading.model_validate(json.loads(_payload(-273.0)))
        with self.assertRaises(ValueError):
            ThermalReading.model_validate(json.loads(_payload(4000.0)))


class TestGateVerdicts(_QuietTestCase):
    """What a single measurement decides."""

    def test_cool_box_passes_without_sleeping(self) -> None:
        """Below the limit the gate is transparent."""
        harness = _GateHarness([_payload(74.0)])
        with _pinned():
            gate = harness.build()
            gate.check_or_wait()

        self.assertEqual(harness.sleeps, [])
        self.assertEqual(harness.alerts, [])

    def test_soc_alone_crossing_the_limit_trips_the_gate(self) -> None:
        """An SoC zone at the limit trips it even while the GPU is cool.

        This is the case the whole design exists for: on the GB10 the board
        zones run ~8 C above the GPU die, so at a 91 C limit the SoC reaches it
        while temperature.gpu still reads a comfortable 83.
        """
        payload = _payload(91.5, gpu_temp_c=83.0, soc_temp_c=91.5)
        with _pinned(THERMAL_MAX_TEMP_C=91.0):
            result = _GateHarness([payload]).build().read()

        self.assertFalse(result.healthy)
        self.assertIn("SoC zone", result.reason)

    def test_gpu_alone_crossing_the_limit_trips_the_gate(self) -> None:
        """A GPU at the limit trips it even while every SoC zone is cool.

        The inverse case, so the gate is genuinely "either sensor" and not
        merely an SoC guard that happens to read the GPU too.
        """
        payload = _payload(
            91.5, gpu_temp_c=91.5, soc_temp_c=60.0, zones={"acpitz-0": 60.0}
        )
        with _pinned(THERMAL_MAX_TEMP_C=91.0):
            result = _GateHarness([payload]).build().read()

        self.assertFalse(result.healthy)
        self.assertIn("GPU die", result.reason)

    def test_both_sensors_below_the_limit_admits_work(self) -> None:
        """Neither sensor at the limit means the run proceeds."""
        payload = _payload(83.3, gpu_temp_c=75.0, soc_temp_c=83.3)
        with _pinned(THERMAL_MAX_TEMP_C=91.0):
            result = _GateHarness([payload]).build().read()

        self.assertTrue(result.healthy)

    def test_limit_is_inclusive(self) -> None:
        """Exactly at the limit counts as a breach, not as headroom."""
        harness = _GateHarness([_payload(80.0)])
        with _pinned():
            result = harness.build().read()

        self.assertFalse(result.healthy)

    def test_dead_server_is_unhealthy_regardless_of_temperature(self) -> None:
        """Liveness is checked first; a cool box that cannot answer is no use."""
        harness = _GateHarness([_payload(40.0)], ping=False)
        with _pinned():
            result = harness.build().read()

        self.assertFalse(result.healthy)
        self.assertEqual(harness.probe_calls, 0)

    def test_disabled_gate_measures_nothing(self) -> None:
        """THERMAL_GATE_ENABLED off short-circuits before any probe."""
        harness = _GateHarness([_payload(95.0)])
        with _pinned(THERMAL_GATE_ENABLED=False):
            harness.build().check_or_wait()

        self.assertEqual(harness.probe_calls, 0)


class TestUnreadableTemperature(_QuietTestCase):
    """Strict and permissive handling of a probe that cannot be read."""

    def test_strict_treats_an_unreadable_probe_as_a_breach(self) -> None:
        """The default refuses to run blind."""
        harness = _GateHarness([])
        harness.probe_fn = mock.Mock(side_effect=ProbeUnavailable("ssh refused"))
        with _pinned(THERMAL_GATE_STRICT=True):
            result = harness.build().read()

        self.assertFalse(result.healthy)
        self.assertIn("strict", result.reason)

    def test_permissive_allows_work_when_the_probe_is_missing(self) -> None:
        """The escape hatch for a box where the probe is not deployed yet."""
        harness = _GateHarness([])
        harness.probe_fn = mock.Mock(side_effect=ProbeUnavailable("ssh refused"))
        with _pinned(THERMAL_GATE_STRICT=False):
            result = harness.build().read()

        self.assertTrue(result.healthy)

    def test_non_json_output_is_a_probe_failure(self) -> None:
        """SSH banners and shell errors must not parse as a reading."""
        harness = _GateHarness(["bash: python3: command not found"])
        with _pinned(THERMAL_GATE_STRICT=True):
            result = harness.build().read()

        self.assertFalse(result.healthy)

    def test_probe_error_object_is_a_probe_failure(self) -> None:
        """The probe's own {'error': ...} response is honoured."""
        harness = _GateHarness([json.dumps({"schema": 1, "error": "no sensor"})])
        with _pinned(THERMAL_GATE_STRICT=True):
            result = harness.build().read()

        self.assertFalse(result.healthy)
        self.assertIn("no sensor", result.reason)


class TestCooldownAndAbort(_QuietTestCase):
    """The throttle-then-stop behaviour that protects the box."""

    def test_hot_then_cool_serves_one_cooldown_and_proceeds(self) -> None:
        """A transient excursion costs one pause, not the run."""
        harness = _GateHarness([_payload(84.0), _payload(72.0)])
        with _pinned():
            harness.build().check_or_wait()

        self.assertEqual(harness.sleeps, [150.0])
        self.assertEqual(len(harness.alerts), 1)
        self.assertEqual(harness.alerts[0][1:], (1, 3))

    def test_persistently_hot_aborts_after_the_budget(self) -> None:
        """The run stops gracefully rather than cooking the server."""
        harness = _GateHarness([_payload(84.0)])
        with _pinned(THERMAL_MAX_COOLDOWN_CYCLES=3):
            with self.assertRaises(ThermalAbort) as caught:
                harness.build().check_or_wait()

        self.assertEqual(len(harness.sleeps), 3)
        self.assertEqual(len(harness.alerts), 3)
        self.assertFalse(caught.exception.result.healthy)
        self.assertEqual(caught.exception.result.reading.gate_temp_c, 84.0)

    def test_abort_latches_so_later_callers_fail_fast(self) -> None:
        """A worker pool drains at once instead of each serving a cooldown."""
        harness = _GateHarness([_payload(84.0)])
        with _pinned(THERMAL_MAX_COOLDOWN_CYCLES=1):
            gate = harness.build()
            with self.assertRaises(ThermalAbort):
                gate.check_or_wait()
            sleeps_after_first = len(harness.sleeps)

            with self.assertRaises(ThermalAbort):
                gate.check_or_wait()

        self.assertEqual(len(harness.sleeps), sleeps_after_first)

    def test_one_alert_per_breach_across_concurrent_workers(self) -> None:
        """Eight workers on one breach must not emit eight banners and sounds."""
        harness = _GateHarness([_payload(84.0)])
        with _pinned(THERMAL_MAX_COOLDOWN_CYCLES=1, THERMAL_READING_TTL_SECONDS=300.0):
            gate = harness.build()
            barrier = threading.Barrier(8)

            def worker() -> None:
                barrier.wait()
                try:
                    gate.check_or_wait()
                except ThermalAbort:
                    pass

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(harness.alerts), 1)

    def test_zero_budget_aborts_on_the_first_breach(self) -> None:
        """A budget of zero means stop immediately, with no waiting."""
        harness = _GateHarness([_payload(84.0)])
        with _pinned(THERMAL_MAX_COOLDOWN_CYCLES=0):
            with self.assertRaises(ThermalAbort):
                harness.build().check_or_wait()

        self.assertEqual(harness.sleeps, [])


class TestClockAdjustment(_QuietTestCase):
    """Capping and lifting the GPU boost clock as temperature moves."""

    def test_breach_caps_the_clock(self) -> None:
        """A hot box has its boost ceiling cut before the cooldown begins."""
        harness = _GateHarness([_payload(84.0)])
        with _pinned(THERMAL_MAX_COOLDOWN_CYCLES=1):
            with self.assertRaises(ThermalAbort):
                harness.build().check_or_wait()

        harness.clock.apply_cap.assert_called()
        harness.clock.restore.assert_not_called()

    def test_cool_box_below_the_restore_point_lifts_the_cap(self) -> None:
        """Once genuinely cool the run gets its full clock back."""
        harness = _GateHarness([_payload(65.0)])
        with _pinned(THERMAL_CLOCK_RESTORE_TEMP_C=70.0):
            harness.build().check_or_wait()

        harness.clock.restore.assert_called_once()
        harness.clock.apply_cap.assert_not_called()

    def test_hysteresis_band_touches_neither_control(self) -> None:
        """Between the restore point and the limit the clock is left alone.

        Capping at 80 C and restoring at 80 C would flap the clock on every
        reading that straddles the limit, and each flap is a privileged SSH
        command.
        """
        harness = _GateHarness([_payload(75.0)])
        with _pinned(THERMAL_CLOCK_RESTORE_TEMP_C=70.0, THERMAL_MAX_TEMP_C=80.0):
            harness.build().check_or_wait()

        harness.clock.apply_cap.assert_not_called()
        harness.clock.restore.assert_not_called()

    def test_recovery_caps_then_restores(self) -> None:
        """A breach that cools past the restore point does both, in order."""
        harness = _GateHarness([_payload(84.0), _payload(60.0)])
        with _pinned(THERMAL_CLOCK_RESTORE_TEMP_C=70.0):
            harness.build().check_or_wait()

        harness.clock.apply_cap.assert_called_once()
        harness.clock.restore.assert_called_once()


class TestReadingCache(_QuietTestCase):
    """The TTL cache that keeps a concurrent run to one SSH probe per window."""

    def test_second_read_inside_the_window_reuses_the_reading(self) -> None:
        """Probing per CELEX would add minutes of SSH latency to a run."""
        harness = _GateHarness([_payload(74.0)])
        with _pinned(THERMAL_READING_TTL_SECONDS=300.0):
            gate = harness.build()
            gate.read()
            gate.read()

        self.assertEqual(harness.probe_calls, 1)

    def test_invalidate_forces_a_fresh_probe(self) -> None:
        """A cooldown must be followed by a real re-measurement."""
        harness = _GateHarness([_payload(74.0)])
        with _pinned(THERMAL_READING_TTL_SECONDS=300.0):
            gate = harness.build()
            gate.read()
            gate.invalidate()
            gate.read()

        self.assertEqual(harness.probe_calls, 2)

    def test_zero_ttl_probes_every_time(self) -> None:
        """The cache can be turned off without disabling the gate."""
        harness = _GateHarness([_payload(74.0)])
        with _pinned(THERMAL_READING_TTL_SECONDS=0.0):
            gate = harness.build()
            gate.read()
            gate.read()

        self.assertEqual(harness.probe_calls, 2)

    def test_concurrent_readers_share_one_probe(self) -> None:
        """Eight workers must not open eight SSH sessions for one window."""
        harness = _GateHarness([_payload(74.0)])
        with _pinned(THERMAL_READING_TTL_SECONDS=300.0):
            gate = harness.build()
            barrier = threading.Barrier(8)

            def worker() -> None:
                barrier.wait()
                gate.read()

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(harness.probe_calls, 1)


class TestSshHostDerivation(_QuietTestCase):
    """The single source of truth for which box gets measured."""

    def test_host_comes_from_the_ollama_endpoint(self) -> None:
        """No separate DGX_SSH_HOST setting can drift from OLLAMA_HOST."""
        with mock.patch.object(settings, "OLLAMA_HOST", "http:/localhost:11434"):
            self.assertEqual(dgx_ssh_host(), "localhost")

    def test_unparsable_endpoint_raises(self) -> None:
        """A malformed endpoint fails here, not as a confusing SSH error."""
        with mock.patch.object(settings, "OLLAMA_HOST", "not-a-url"):
            with self.assertRaises(ProbeUnavailable):
                dgx_ssh_host()


class TestBuildThermalGate(_QuietTestCase):
    """The shared decision every batch driver delegates to."""

    def test_dry_run_builds_no_gate(self) -> None:
        """A dry run must stay runnable with the DGX switched off."""
        factory = mock.MagicMock()
        with _pinned():
            self.assertIsNone(build_thermal_gate(True, False, gate_factory=factory))
        factory.assert_not_called()

    def test_operator_flag_disables_the_gate(self) -> None:
        factory = mock.MagicMock()
        with _pinned():
            self.assertIsNone(build_thermal_gate(False, True, gate_factory=factory))
        factory.assert_not_called()

    def test_setting_disables_the_gate(self) -> None:
        factory = mock.MagicMock()
        with _pinned(THERMAL_GATE_ENABLED=False):
            self.assertIsNone(build_thermal_gate(False, False, gate_factory=factory))
        factory.assert_not_called()

    def test_live_run_builds_one_gate_from_the_factory(self) -> None:
        """The caller's constructor is used, so its module name stays patchable."""
        gate = object()
        factory = mock.MagicMock(return_value=gate)
        with _pinned():
            self.assertIs(build_thermal_gate(False, False, gate_factory=factory), gate)
        factory.assert_called_once_with()


class TestStopOnThermalAbort(_QuietTestCase):
    """The hand-rolled loops stop the way the runner does."""

    def test_restores_the_clock_and_exits_with_the_abort_code(self) -> None:
        harness = _GateHarness([_payload(84.0)])
        with _pinned(THERMAL_MAX_COOLDOWN_CYCLES=1):
            gate = harness.build()
            with self.assertRaises(ThermalAbort) as caught:
                gate.check_or_wait()
            with mock.patch("config.ollama_health_gate.play_alert_sound") as sound:
                with self.assertRaises(SystemExit) as exited:
                    stop_on_thermal_abort(
                        mock.MagicMock(), gate, caught.exception, 7, "re-run"
                    )

        self.assertEqual(exited.exception.code, EXIT_CODE_THERMAL_ABORT)
        harness.clock.restore.assert_called_once_with()
        sound.assert_called_once_with()

    def test_banner_names_the_remaining_work_and_the_resume_hint(self) -> None:
        harness = _GateHarness([_payload(84.0)])
        log = mock.MagicMock()
        with _pinned(THERMAL_MAX_COOLDOWN_CYCLES=1):
            gate = harness.build()
            with self.assertRaises(ThermalAbort) as caught:
                gate.check_or_wait()
            with mock.patch("config.ollama_health_gate.play_alert_sound"):
                with self.assertRaises(SystemExit):
                    stop_on_thermal_abort(log, gate, caught.exception, 7, "re-run me")

        banner = log.critical.call_args.args[0] % log.critical.call_args.args[1:]
        self.assertIn("RUN STOPPED BY THE DGX THERMAL GATE", banner)
        self.assertIn("7 unit(s) of work", banner)
        self.assertIn("re-run me", banner)


if __name__ == "__main__":
    unittest.main()
