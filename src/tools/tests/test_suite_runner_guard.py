"""Tests for the live-DGX tripwire applied by the unittest suite runners."""

import unittest
from unittest import mock

from config import ollama_health_gate
from config.ollama_health_gate import OllamaHealthGate
from config.settings import settings
from config.tests.test_ollama_health_gate import _payload
from tools.tests.run_unittest_suite_wo_usr import no_live_dgx


class TestNoLiveDgx(unittest.TestCase):
    """``no_live_dgx`` must turn an unmocked gate into an immediate failure."""

    def test_default_probe_fails_fast_instead_of_ssh(self) -> None:
        """The default SSH probe raises instead of reaching the network."""
        with no_live_dgx(), mock.patch("subprocess.run") as ssh:
            with self.assertRaises(AssertionError):
                ollama_health_gate._run_remote_probe()
        ssh.assert_not_called()

    def test_default_ping_fails_fast(self) -> None:
        """The default liveness ping raises instead of opening a socket."""
        with no_live_dgx():
            with self.assertRaises(AssertionError):
                ollama_health_gate._ping_ollama()

    def test_unstubbed_gate_does_not_cool_down(self) -> None:
        """A gate built without injection errors out rather than sleeping."""
        sleeper = mock.Mock()
        with no_live_dgx(), mock.patch.object(ollama_health_gate.time, "sleep", sleeper):
            with self.assertRaises(AssertionError):
                OllamaHealthGate().check_or_wait()
        sleeper.assert_not_called()

    def test_cooldown_zeroed_then_restored(self) -> None:
        """The cooldown is zero inside the guard and restored on exit."""
        before = settings.THERMAL_COOLDOWN_SECONDS
        with no_live_dgx():
            self.assertEqual(settings.THERMAL_COOLDOWN_SECONDS, 0.0)
        self.assertEqual(settings.THERMAL_COOLDOWN_SECONDS, before)

    def test_injected_collaborators_still_work(self) -> None:
        """A gate with injected probe and ping is untouched by the guard."""
        gate = OllamaHealthGate(
            ping_fn=lambda: True,
            probe_fn=lambda: _payload(50.0),
            sleep_fn=lambda _s: None,
            clock_controller=mock.Mock(),
        )
        with no_live_dgx(), mock.patch.object(settings, "THERMAL_GATE_ENABLED", True):
            result = gate.check_or_wait()
        self.assertTrue(result.healthy)
