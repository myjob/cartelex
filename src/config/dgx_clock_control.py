"""Adaptive GPU clock capping for the DGX Spark.

The Spark does not die of GPU die temperature. It dies of SoC temperature: under
sustained inference the ARM cores and the ACPI thermal zones spike toward
93-96 C and the board firmware executes a hard power-off, while the GPU die
trails safely at 78-84 C, far below its own ~105 C throttle. The chassis intake
is restrictive and the embedded-controller fan curve is conservative, so the
firmware reaches its cutout before the GPU ever asks for help.

Capping the maximum boost frequency attacks that directly: it removes the
transient power spikes that heat the shared superchip package faster than the
fans respond. This module applies the cap when the box runs hot and lifts it
again once the box has cooled well below the gate limit, so a run is slowed only
while it needs to be.

The cap is applied over SSH through a small root-owned wrapper on the Spark,
``scripts/dgx_gpu_clock_guard.sh`` installed as
``settings.DGX_CLOCK_GUARD_PATH``, which accepts only ``reset`` or
``cap <min> <max>`` and validates both numbers before calling ``nvidia-smi``.
Run ``scripts/install_dgx_clock_guard.sh`` to set it up.

The wrapper exists because sudo matches command arguments as one concatenated
string, and its manual warns that a wildcard there spans multiple words: a rule
of ``nvidia-smi -lgc *`` would also authorise ``-lgc 300,2200 -i 0 -r``, a GPU
reset. Granting one fixed script path instead leaves no argument to smuggle.

Without that install the controller degrades to a single actionable warning and
the thermal gate still protects the box by throttling and stopping the batch --
clock control is a mitigation, never the guard.
"""

from __future__ import annotations

import argparse
import atexit
import logging
import subprocess
import sys
import threading

from config.settings import settings

logger = logging.getLogger(__name__)


class ClockControlUnavailable(RuntimeError):
    """The clock command could not be run (usually missing passwordless sudo)."""


def _run_clock_guard(arguments: list[str]) -> str:
    """Run the root-owned clock guard on the Spark over SSH.

    Isolated as a named function so tests replace this boundary rather than
    patching :mod:`subprocess`, matching the convention used by
    :mod:`config.ollama_health_gate` and the HeidelTime JAR wrapper.

    Args:
        arguments: Guard verb and operands, e.g. ``["cap", "300", "2200"]`` or
            ``["reset"]``.

    Returns:
        Combined stdout of the command.

    Raises:
        ClockControlUnavailable: If SSH fails, sudo demands a password, the
            guard rejects the arguments (exit 64), or the device does not
            support locked clocks (exit 70).
    """
    # Imported here rather than at module scope to keep the dependency one-way:
    # the health gate owns the SSH target, and this module is optional.
    from config.ollama_health_gate import dgx_ssh_host

    target = f"{settings.DGX_SSH_USER}@{dgx_ssh_host()}"
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={settings.DGX_SSH_TIMEOUT_SECONDS}",
        target,
        "sudo",
        "-n",
        settings.DGX_CLOCK_GUARD_PATH,
        *arguments,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.DGX_SSH_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ClockControlUnavailable("the ssh client is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClockControlUnavailable(
            f"clock command on {target} did not answer within "
            f"{settings.DGX_SSH_TIMEOUT_SECONDS}s"
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or "(no output)"
        raise ClockControlUnavailable(
            f"clock guard {' '.join(arguments)} on {target} exited "
            f"{completed.returncode}: {detail}"
        )
    return completed.stdout


class GpuClockController:
    """Caps and restores the Spark's GPU boost clock as temperature moves.

    Applying the same cap twice is skipped, so a long hot spell issues one
    privileged command rather than one per cooldown. All state is guarded by a
    lock because a worker pool shares one controller.

    Attributes:
        run_fn: Executes one privileged clock-guard invocation.
    """

    def __init__(self, run_fn=None) -> None:
        """Wire the controller to its command boundary.

        Args:
            run_fn: Callable taking clock-guard arguments; defaults to
                :func:`_run_clock_guard`.
        """
        self.run_fn = run_fn or _run_clock_guard
        self._lock = threading.Lock()
        self._capped = False
        self._atexit_registered = False
        # Sudo either works or it does not; complaining once per breach would
        # bury the temperature the operator actually needs to see.
        self._unavailable_logged = False

    @property
    def capped(self) -> bool:
        """Whether a cap is currently believed to be applied."""
        with self._lock:
            return self._capped

    def _attempt(self, arguments: list[str], description: str) -> bool:
        """Run one privileged command, converting failure into a warning.

        Args:
            arguments: Guard verb and operands.
            description: Human-readable action, for the log line.

        Returns:
            True when the command succeeded.
        """
        try:
            self.run_fn(arguments)
        except ClockControlUnavailable as exc:
            if not self._unavailable_logged:
                self._unavailable_logged = True
                logger.warning(
                    "GPU clock control is unavailable (%s). The thermal gate "
                    "still throttles and stops the run, but boost spikes are "
                    "not being capped. To enable it, add on the Spark: "
                    "run scripts/install_dgx_clock_guard.sh (install runbook "
                    "section 7.5.3).",
                    exc,
                )
            else:
                logger.debug("Clock control still unavailable: %s", exc)
            return False
        logger.info("GPU clock %s", description)
        return True

    def apply_cap(self) -> bool:
        """Cap the boost clock because the box is running hot.

        Returns:
            True when a cap was newly applied; False when one was already in
            place or the command could not be run.
        """
        if not settings.THERMAL_CLOCK_CONTROL_ENABLED:
            return False
        with self._lock:
            if self._capped:
                return False

        low = settings.THERMAL_CLOCK_MIN_MHZ
        high = settings.THERMAL_CLOCK_CAP_MHZ
        applied = self._attempt(
            ["cap", str(low), str(high)], f"capped to {low}-{high} MHz"
        )
        if applied:
            with self._lock:
                self._capped = True
                if not self._atexit_registered:
                    # Registered only once a cap really exists on the device, so
                    # a run that never got hot never touches the clock on exit.
                    # This covers every ordinary way a batch ends -- normal
                    # completion, sys.exit from the thermal abort, an unhandled
                    # exception, Ctrl-C -- because all of them unwind through
                    # interpreter shutdown. SIGKILL does not, which is why
                    # ensure_default() also runs at the start of every run.
                    atexit.register(self._restore_on_exit)
                    self._atexit_registered = True
        return applied

    def _restore_on_exit(self) -> None:
        """Lift any cap still applied as the interpreter shuts down.

        Never raises: an exit path is the worst possible place to fail, and
        :meth:`_attempt` already converts a dead SSH link into a warning.
        """
        with self._lock:
            if not self._capped:
                return
        logger.info("Restoring the DGX GPU clock before exit.")
        self.restore()

    def restore(self) -> bool:
        """Lift the cap this controller applied.

        Returns:
            True when a cap was lifted; False when none was applied or the
            command could not be run.
        """
        if not settings.THERMAL_CLOCK_CONTROL_ENABLED:
            return False
        with self._lock:
            if not self._capped:
                return False

        restored = self._attempt(["reset"], "cap lifted (default boost restored)")
        if restored:
            with self._lock:
                self._capped = False
        return restored

    def ensure_default(self) -> bool:
        """Reset the clock to default regardless of what this process believes.

        Unlike :meth:`restore` this ignores the tracked state and always issues
        ``-rgc``. That is deliberate, and it is what makes a leaked cap
        self-healing.

        A cap set by ``nvidia-smi -lgc`` persists on the device until it is
        cleared, the driver reloads, or the box reboots -- it does not belong to
        the process that set it. So a batch killed with SIGKILL, or a machine
        that loses power mid-run, leaves the Spark throttled with nothing in
        this process left to undo it. Calling this at the start of every run
        guarantees each batch begins at full clocks whatever happened to the
        last one.

        Detection is not used because an ``-lgc`` range is not reliably visible
        through ``--query-gpu``: ``clocks.applications.gr`` tracks application
        clocks, a different mechanism. An unconditional reset is idempotent and
        costs one SSH round trip, so it is the cheaper certainty.

        Returns:
            True when the reset command succeeded.
        """
        if not settings.THERMAL_CLOCK_CONTROL_ENABLED:
            return False

        reset = self._attempt(["reset"], "reset to default boost")
        if reset:
            with self._lock:
                self._capped = False
        return reset


def main(argv: list[str] | None = None) -> int:
    """Reset or cap the Spark's GPU clock from the command line.

    The manual counterpart to the automatic restore paths, for the one case
    they cannot cover: a batch killed with SIGKILL (or a machine that lost
    power) leaves the cap on the device with no process left to lift it.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 on success, 1 when the command could not be run.
    """
    parser = argparse.ArgumentParser(
        description="Reset or cap the DGX Spark GPU boost clock."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--reset",
        action="store_true",
        help="Return the GPU to its default boost behaviour.",
    )
    group.add_argument(
        "--cap",
        action="store_true",
        help=(
            "Apply the configured cap now "
            "(THERMAL_CLOCK_MIN_MHZ..THERMAL_CLOCK_CAP_MHZ)."
        ),
    )
    args = parser.parse_args(argv)

    controller = GpuClockController()
    if args.cap:
        # Bypass the once-only guard: an operator asking for a cap explicitly
        # wants the command issued, whatever this fresh process believes.
        succeeded = controller.ensure_default() and controller.apply_cap()
    else:
        succeeded = controller.ensure_default()

    if not succeeded:
        logger.error(
            "GPU clock command failed. Install the guard with "
            "scripts/install_dgx_clock_guard.sh (install runbook section "
            "7.5.3), or run 'ssh %s@<spark> sudo nvidia-smi -rgc' by hand.",
            settings.DGX_SSH_USER,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
