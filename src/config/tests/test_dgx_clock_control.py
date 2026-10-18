"""Unit tests for adaptive GPU clock capping on the DGX Spark.

The privileged command boundary (``_run_clock_guard``) is injected, so no test
opens an SSH session or runs sudo. Arguments are the guard's own verbs --
``["cap", "<min>", "<max>"]`` and ``["reset"]`` -- not raw nvidia-smi flags.
"""

import unittest
from contextlib import ExitStack
from unittest import mock

from config.dgx_clock_control import ClockControlUnavailable, GpuClockController
from config.settings import settings
from config.tests import silence_logger


def _pinned(**overrides):
    """Pin clock settings for the duration of a ``with`` block.

    Args:
        **overrides: Setting names and the values to pin them to.

    Returns:
        A context manager applying every patch.
    """
    defaults = {
        "THERMAL_CLOCK_CONTROL_ENABLED": True,
        "THERMAL_CLOCK_MIN_MHZ": 300,
        "THERMAL_CLOCK_CAP_MHZ": 2200,
    }
    defaults.update(overrides)
    stack = ExitStack()
    for name, value in defaults.items():
        stack.enter_context(mock.patch.object(settings, name, value))
    return stack


class _QuietTestCase(unittest.TestCase):
    """Base that keeps the controller's sudoers WARNING off the console."""

    def setUp(self) -> None:
        """Quieten the logger and keep test caps out of the real atexit list."""
        silence_logger(self, "config.dgx_clock_control")
        # apply_cap registers a genuine interpreter-shutdown handler. Left
        # alone, every test that caps would fire one after unittest has
        # finished -- printing restore lines that look like a real box being
        # touched. Tests asserting on registration re-patch this locally.
        patcher = mock.patch("config.dgx_clock_control.atexit.register")
        patcher.start()
        self.addCleanup(patcher.stop)


class TestApplyCap(_QuietTestCase):
    """Capping the boost clock while the box is hot."""

    def test_cap_issues_the_expected_lgc_command(self) -> None:
        """The bounds come from settings, as the guard's own cap verb."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            self.assertTrue(controller.apply_cap())

        run_fn.assert_called_once_with(["cap", "300", "2200"])
        self.assertTrue(controller.capped)

    def test_repeated_cap_is_not_reissued(self) -> None:
        """A long hot spell costs one privileged command, not one per cooldown."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            controller.apply_cap()
            self.assertFalse(controller.apply_cap())

        self.assertEqual(run_fn.call_count, 1)

    def test_disabled_control_issues_nothing(self) -> None:
        """The feature switch is honoured before the command is built."""
        run_fn = mock.Mock(return_value="")
        with _pinned(THERMAL_CLOCK_CONTROL_ENABLED=False):
            controller = GpuClockController(run_fn=run_fn)
            self.assertFalse(controller.apply_cap())

        run_fn.assert_not_called()


class TestRestore(_QuietTestCase):
    """Lifting the cap once the box has cooled."""

    def test_restore_resets_the_clock(self) -> None:
        """The reset verb returns the device to default boost behaviour."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            controller.apply_cap()
            run_fn.reset_mock()
            self.assertTrue(controller.restore())

        run_fn.assert_called_once_with(["reset"])
        self.assertFalse(controller.capped)

    def test_restore_without_a_cap_does_nothing(self) -> None:
        """Never issue a privileged command to undo something not done."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            self.assertFalse(controller.restore())

        run_fn.assert_not_called()


class TestEnsureDefault(_QuietTestCase):
    """The unconditional reset that makes a leaked cap self-healing."""

    def test_reset_is_issued_even_when_nothing_is_believed_capped(self) -> None:
        """A fresh process must still clear a cap left by a killed predecessor.

        An -lgc cap lives on the device, not in the process that set it, so
        tracked state is worthless for deciding whether one exists.
        """
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            self.assertFalse(controller.capped)
            self.assertTrue(controller.ensure_default())

        run_fn.assert_called_once_with(["reset"])

    def test_reset_clears_tracked_state(self) -> None:
        """After a reset the controller must not think a cap is still applied."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            controller.apply_cap()
            controller.ensure_default()

        self.assertFalse(controller.capped)

    def test_reset_respects_the_feature_switch(self) -> None:
        """Disabling clock control must not issue privileged commands."""
        run_fn = mock.Mock(return_value="")
        with _pinned(THERMAL_CLOCK_CONTROL_ENABLED=False):
            self.assertFalse(GpuClockController(run_fn=run_fn).ensure_default())

        run_fn.assert_not_called()


class TestRestoreOnExit(_QuietTestCase):
    """The atexit safety net that covers ordinary process termination."""

    def test_exit_hook_lifts_a_cap_that_is_still_applied(self) -> None:
        """A batch that ends while hot must not leave the box throttled."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            controller.apply_cap()
            run_fn.reset_mock()
            controller._restore_on_exit()

        run_fn.assert_called_once_with(["reset"])
        self.assertFalse(controller.capped)

    def test_exit_hook_is_silent_when_no_cap_was_applied(self) -> None:
        """A run that never got hot must not touch the clock on the way out."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            controller._restore_on_exit()

        run_fn.assert_not_called()

    def test_exit_hook_registered_only_after_a_cap_is_applied(self) -> None:
        """No cap, no atexit handler -- nothing to run at shutdown."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            self.assertFalse(controller._atexit_registered)
            with mock.patch("config.dgx_clock_control.atexit.register") as register:
                controller.apply_cap()

        register.assert_called_once()
        self.assertTrue(controller._atexit_registered)

    def test_exit_hook_registered_once_across_repeated_caps(self) -> None:
        """Cap, restore, cap again must not stack shutdown handlers."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            with mock.patch("config.dgx_clock_control.atexit.register") as register:
                controller.apply_cap()
                controller.restore()
                controller.apply_cap()

        register.assert_called_once()

    def test_exit_hook_survives_a_dead_ssh_link(self) -> None:
        """Interpreter shutdown is the worst place to raise."""
        run_fn = mock.Mock(return_value="")
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            controller.apply_cap()
            run_fn.side_effect = ClockControlUnavailable("host unreachable")
            controller._restore_on_exit()  # must not raise

        self.assertTrue(controller.capped)


class TestUnavailableControl(_QuietTestCase):
    """Behaviour when passwordless sudo is not configured on the Spark."""

    def test_failure_is_warned_once_and_never_raises(self) -> None:
        """Clock control is a mitigation; the gate is the guard.

        A missing sudoers line must not take down a batch, and must not repeat
        the same warning on every breach -- that would bury the temperature the
        operator actually needs to read.
        """
        run_fn = mock.Mock(
            side_effect=ClockControlUnavailable("sudo: a password is required")
        )
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            with self.assertLogs("config.dgx_clock_control", level="WARNING") as logs:
                self.assertFalse(controller.apply_cap())
            self.assertFalse(controller.apply_cap())

        self.assertEqual(len(logs.output), 1)
        self.assertIn("install_dgx_clock_guard.sh", logs.output[0])
        self.assertEqual(run_fn.call_count, 2)
        self.assertFalse(controller.capped)

    def test_failed_cap_is_retried_on_the_next_breach(self) -> None:
        """A cap that never applied must not be remembered as applied."""
        run_fn = mock.Mock(side_effect=ClockControlUnavailable("transient"))
        with _pinned():
            controller = GpuClockController(run_fn=run_fn)
            controller.apply_cap()
            run_fn.side_effect = None
            run_fn.return_value = ""
            self.assertTrue(controller.apply_cap())

        self.assertTrue(controller.capped)


if __name__ == "__main__":
    unittest.main()
