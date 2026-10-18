"""Health gate that keeps batch work off an unhealthy or overheating Ollama box.

The Ollama server runs on an NVIDIA DGX Spark whose undocumented shutdown
threshold sits near 85 C. Because its Grace-Blackwell memory is unified with the
CPU's, a thermal death produces no dump, no log and no response -- the batch
simply stops and the cause is unrecoverable afterwards. This module is the guard
that prevents that outcome: before each unit of work it confirms the server
answers and that the box is below :attr:`settings.THERMAL_MAX_TEMP_C`, waits out
a breach, and finally raises :class:`ThermalAbort` so the caller can stop
gracefully with its remaining work recorded.

Two boundaries are crossed, each isolated behind a named module-level function
so tests replace the function rather than patching :mod:`subprocess` or
:mod:`httpx`:

* :func:`_ping_ollama` -- HTTP liveness against ``settings.OLLAMA_HOST``.
* :func:`_run_remote_probe` -- SSH execution of
  :mod:`config.dgx_thermal_probe` on the Spark.

The SSH host is derived from ``settings.OLLAMA_HOST`` rather than configured
separately, so the machine being measured is by construction the machine serving
inference.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.dgx_clock_control import GpuClockController
from config.settings import settings

logger = logging.getLogger(__name__)

_BOLD = "\033[1m"
_RESET = "\033[0m"

#: Exit code for a run stopped by the thermal gate. Distinct from 1 (some items
#: failed) and 0 (clean) so a shell wrapper or cron job can tell "the box got
#: too hot, resume later" apart from "the data is bad". Shared by
#: ``BatchJobRunner`` and the hand-rolled batch loops.
EXIT_CODE_THERMAL_ABORT = 2

#: Widest temperature that can be a genuine sensor reading. A value outside this
#: range means the payload is garbage, and garbage must never be read as "cool".
_PLAUSIBLE_TEMP_RANGE_C = (0.0, 150.0)

#: Oldest probe contract this client can read. The check is a floor, never an
#: equality: the contract is additive, so a probe newer than this client emits
#: fields the model simply ignores, and rejecting it would mean that deploying
#: an updated probe breaks every batch already running against the box. Raise
#: this only when a field is removed, renamed, or changes meaning.
MIN_SUPPORTED_SCHEMA = 1


class ThermalAbort(RuntimeError):
    """The server stayed unhealthy past the cooldown budget; stop the run.

    Attributes:
        result: The final health check that exhausted the budget.
    """

    def __init__(self, result: "HealthCheckResult") -> None:
        """Store the failing check alongside a human-readable message.

        Args:
            result: The last :class:`HealthCheckResult` obtained.
        """
        super().__init__(result.reason)
        self.result = result


class ProbeUnavailable(RuntimeError):
    """The remote temperature probe could not be reached or understood."""


class ThermalReading(BaseModel):
    """One validated thermal reading returned by the remote probe.

    Validated at the boundary because it arrives as text over SSH: a truncated
    or garbled payload must fail loudly here rather than deserialise into a
    plausible-looking low temperature.
    """

    model_config = ConfigDict(populate_by_name=True)

    schema_version: int = Field(
        alias="schema",
        description="Contract version emitted by config.dgx_thermal_probe.",
    )
    hostname: str = Field(description="Hostname the probe ran on, for the logs.")
    timestamp_utc: str = Field(description="ISO-8601 UTC time the probe sampled.")
    gpu_name: str | None = Field(
        default=None, description="Device name as nvidia-smi reports it."
    )
    gpu_temp_c: float | None = Field(
        default=None,
        description="nvidia-smi temperature.gpu; None when unsupported.",
    )
    soc_temp_c: float | None = Field(
        default=None,
        description="Hottest ACPI thermal zone; runs ~8 C above the GPU sensor.",
    )
    gate_temp_c: float = Field(
        description="Max of GPU and zone sensors; the value the gate compares.",
    )
    utilization_pct: int | None = Field(
        default=None, description="GPU utilisation percentage, for the logs."
    )
    throttle_reasons: str | None = Field(
        default=None,
        description="nvidia-smi clocks_throttle_reasons.active bitmask.",
    )
    clock_mhz: int | None = Field(
        default=None, description="Current graphics clock, in MHz."
    )
    clock_max_mhz: int | None = Field(
        default=None, description="Maximum graphics clock the device reports."
    )
    zones: dict[str, float] = Field(
        default_factory=dict,
        description="Every readable ACPI thermal zone, in degrees Celsius.",
    )

    @model_validator(mode="after")
    def _reject_implausible_payload(self) -> "ThermalReading":
        """Reject a pre-minimum contract or an out-of-range temperature.

        The schema check is a floor rather than an equality. A probe newer than
        this client only adds optional fields, which validate and are ignored;
        refusing them would mean a probe redeployment silently disarmed every
        batch already running against the box.

        Returns:
            The validated model.

        Raises:
            ValueError: If the schema predates
                :data:`MIN_SUPPORTED_SCHEMA`, or the gate temperature is
                outside :data:`_PLAUSIBLE_TEMP_RANGE_C`.
        """
        if self.schema_version < MIN_SUPPORTED_SCHEMA:
            raise ValueError(
                f"probe reports schema {self.schema_version}, this client "
                f"needs at least {MIN_SUPPORTED_SCHEMA} -- redeploy the probe"
            )
        low, high = _PLAUSIBLE_TEMP_RANGE_C
        if not low <= self.gate_temp_c <= high:
            raise ValueError(
                f"gate_temp_c {self.gate_temp_c} outside plausible range "
                f"{low}-{high} C"
            )
        return self

    def tripped_sensor(self) -> str:
        """Name the sensor whose reading is the gate temperature.

        The gate compares one number, ``max(GPU, every SoC zone)``, so a breach
        means "the GPU reached the limit or an SoC zone did". Which one is not
        recoverable from that number alone, and it is the first thing an
        operator wants: an SoC trip points at airflow and the EC fan curve, a
        GPU trip at the workload itself.

        Returns:
            A short label such as ``"SoC zone acpitz-0"`` or ``"GPU die"``.
        """
        if self.gpu_temp_c is not None and self.gpu_temp_c >= self.gate_temp_c:
            return "GPU die"
        hottest = max(self.zones.items(), key=lambda item: item[1], default=None)
        if hottest is not None:
            return f"SoC zone {hottest[0]}"
        return "unknown sensor"

    def describe(self) -> str:
        """Render the reading as one line for a log message.

        Returns:
            A compact summary naming both sensors and the utilisation.
        """
        gpu = "n/a" if self.gpu_temp_c is None else f"{self.gpu_temp_c:.1f}C"
        soc = "n/a" if self.soc_temp_c is None else f"{self.soc_temp_c:.1f}C"
        util = "n/a" if self.utilization_pct is None else f"{self.utilization_pct}%"
        clock = "n/a" if self.clock_mhz is None else f"{self.clock_mhz}MHz"
        if self.clock_mhz is not None and self.clock_max_mhz is not None:
            clock = f"{self.clock_mhz}/{self.clock_max_mhz}MHz"
        return (
            f"{self.hostname} gate={self.gate_temp_c:.1f}C "
            f"(gpu={gpu} soc={soc}) util={util} clock={clock}"
        )


class HealthCheckResult(BaseModel):
    """Verdict of one health check, with the reading it was based on."""

    healthy: bool = Field(description="True when work may proceed.")
    reason: str = Field(description="Why the verdict was reached, for logs.")
    reading: ThermalReading | None = Field(
        default=None,
        description="The thermal reading, absent when it could not be taken.",
    )


def _ping_ollama() -> bool:
    """Check that the Ollama server answers.

    ``/api/version`` is used rather than ``/`` so that a reverse proxy serving a
    stub page cannot pass as a live inference server.

    Returns:
        True when the endpoint returns a 2xx response.
    """
    url = f"{settings.OLLAMA_HOST.rstrip('/')}/api/version"
    try:
        response = httpx.get(url, timeout=settings.OLLAMA_HEALTH_PING_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("Ollama ping to %s failed: %s", url, exc)
        return False
    if response.status_code >= 300:
        logger.warning("Ollama ping to %s returned %s", url, response.status_code)
        return False
    return True


def dgx_ssh_host() -> str:
    """Resolve the DGX hostname from the configured Ollama endpoint.

    Deriving it keeps a single source of truth: the box that is measured is
    always the box that serves inference, even after the server moves.

    Returns:
        The hostname or IP portion of ``settings.OLLAMA_HOST``.

    Raises:
        ProbeUnavailable: If the setting carries no parsable host.
    """
    host = urlparse(settings.OLLAMA_HOST).hostname
    if not host:
        raise ProbeUnavailable(
            f"cannot derive an SSH host from OLLAMA_HOST={settings.OLLAMA_HOST!r}"
        )
    return host


def _run_remote_probe() -> str:
    """Run the thermal probe on the DGX over SSH and return its stdout.

    Returns:
        The probe's raw stdout, expected to be one JSON line.

    Raises:
        ProbeUnavailable: If SSH fails, times out, or the probe exits non-zero
            (its exit code 2 means no sensor could be read).
    """
    target = f"{settings.DGX_SSH_USER}@{dgx_ssh_host()}"
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={settings.DGX_SSH_TIMEOUT_SECONDS}",
        target,
        "python3",
        settings.DGX_SSH_PROBE_PATH,
        "--json",
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
        raise ProbeUnavailable("the ssh client is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeUnavailable(
            f"probe on {target} did not answer within "
            f"{settings.DGX_SSH_TIMEOUT_SECONDS}s"
        ) from exc

    if completed.returncode != 0:
        raise ProbeUnavailable(
            f"probe on {target} exited {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip() or '(no output)'}"
        )
    return completed.stdout


def play_alert_sound() -> None:
    """Play the configured alert sound, repeated, without ever raising.

    Each play is awaited rather than detached. A detached ``Popen`` is killed
    when the batch process exits, and a thermal abort exits immediately after
    alerting -- so the fire-and-forget version reliably produced no sound at
    exactly the moment the operator most needed one. Blocking here is
    affordable: an alert is always followed by a cooldown or a shutdown.

    A missing audio device, an empty command or a busy sink must never take
    down a batch run, so every failure is swallowed after a debug line.
    """
    command = settings.THERMAL_ALERT_SOUND_CMD.strip()
    if not command:
        return

    for _ in range(max(1, settings.THERMAL_ALERT_SOUND_REPEAT)):
        try:
            subprocess.run(
                command.split(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=settings.THERMAL_ALERT_SOUND_TIMEOUT,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            logger.debug("Alert sound %r could not be played: %s", command, exc)
            # One failure means the next four will fail the same way.
            return


def _default_alert(result: HealthCheckResult, cycle: int, max_cycles: int) -> None:
    """Raise the operator alert for one breach: log banner, bell and sound.

    Args:
        result: The failing health check.
        cycle: Which cooldown is about to be served, 1-based.
        max_cycles: Total cooldowns budgeted before the run aborts.
    """
    detail = result.reading.describe() if result.reading else "no reading"
    logger.critical(
        "%s%s\nDGX HEALTH GATE TRIPPED (cooldown %d/%d): %s\n%s\n"
        "Limit is %.1fC; pausing %.0fs before re-measuring. The run stops "
        "gracefully if the box is still unhealthy after the last cooldown.\n%s%s",
        _BOLD,
        "=" * 72,
        cycle,
        max_cycles,
        result.reason,
        detail,
        settings.THERMAL_MAX_TEMP_C,
        settings.THERMAL_COOLDOWN_SECONDS,
        "=" * 72,
        _RESET,
    )
    # The bell goes to stderr so it still sounds when stdout is redirected to a
    # log file, which is how these batches are normally run.
    try:
        sys.stderr.write("\a\a\a")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass
    play_alert_sound()


class OllamaHealthGate:
    """Gate that admits batch work only while the Ollama box is healthy.

    Collaborators are injected so the gate is fully exercisable without a
    server, an SSH session or real sleeping.

    Attributes:
        ping_fn: Returns True when the Ollama server answers.
        probe_fn: Returns the remote probe's raw stdout.
        sleep_fn: Blocks for the given number of seconds.
        alert_fn: Notifies the operator of one breach.
    """

    def __init__(
        self,
        ping_fn: Callable[[], bool] | None = None,
        probe_fn: Callable[[], str] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        alert_fn: Callable[[HealthCheckResult, int, int], None] | None = None,
        clock_controller: GpuClockController | None = None,
    ) -> None:
        """Wire the gate to its boundaries.

        Args:
            ping_fn: Ollama liveness probe; defaults to :func:`_ping_ollama`.
            probe_fn: Remote thermal probe; defaults to
                :func:`_run_remote_probe`.
            sleep_fn: Cooldown sleeper; defaults to :func:`time.sleep`.
            alert_fn: Operator alert; defaults to :func:`_default_alert`.
            clock_controller: Applies and lifts the GPU boost cap; defaults to
                a fresh :class:`GpuClockController`.
        """
        self.ping_fn = ping_fn or _ping_ollama
        self.probe_fn = probe_fn or _run_remote_probe
        self.sleep_fn = sleep_fn or time.sleep
        self.alert_fn = alert_fn or _default_alert
        self.clock_controller = clock_controller or GpuClockController()

        self._lock = threading.Lock()
        self._cached: HealthCheckResult | None = None
        self._cached_at = 0.0
        self._aborted = threading.Event()
        self._alerted: HealthCheckResult | None = None

    def invalidate(self) -> None:
        """Discard the cached reading so the next check re-measures."""
        with self._lock:
            self._cached = None
            self._cached_at = 0.0

    def read(self) -> HealthCheckResult:
        """Return the current health verdict, re-probing only when stale.

        A reading younger than ``settings.THERMAL_READING_TTL_SECONDS`` is
        reused. The lock makes that cache shared rather than per-thread, so a
        ``--max-workers`` pool costs one SSH round trip per window instead of
        one per worker per item.

        Returns:
            The verdict, carrying the reading when one could be taken.
        """
        with self._lock:
            age = time.monotonic() - self._cached_at
            if self._cached is not None and age < settings.THERMAL_READING_TTL_SECONDS:
                return self._cached

            result = self._measure()
            self._cached = result
            self._cached_at = time.monotonic()
            return result

    def _measure(self) -> HealthCheckResult:
        """Take a fresh verdict: liveness first, then temperature.

        Returns:
            The verdict. Liveness is checked first because an unreachable
            server makes its temperature both unobtainable and moot.
        """
        if not self.ping_fn():
            return HealthCheckResult(
                healthy=False,
                reason=f"Ollama at {settings.OLLAMA_HOST} did not answer",
            )

        try:
            reading = self._read_temperature()
        except (ProbeUnavailable, ValueError) as exc:
            if settings.THERMAL_GATE_STRICT:
                return HealthCheckResult(
                    healthy=False,
                    reason=f"temperature unreadable ({exc}); gate is strict",
                )
            logger.warning(
                "Temperature unreadable (%s); THERMAL_GATE_STRICT is off, "
                "so the run continues unguarded.",
                exc,
            )
            return HealthCheckResult(
                healthy=True,
                reason=f"temperature unreadable ({exc}); gate is permissive",
            )

        if reading.gate_temp_c >= settings.THERMAL_MAX_TEMP_C:
            return HealthCheckResult(
                healthy=False,
                reason=(
                    f"{reading.tripped_sensor()} reached "
                    f"{reading.gate_temp_c:.1f}C, at or above the "
                    f"{settings.THERMAL_MAX_TEMP_C:.1f}C limit"
                ),
                reading=reading,
            )
        return HealthCheckResult(
            healthy=True,
            reason=f"{reading.gate_temp_c:.1f}C is below the limit",
            reading=reading,
        )

    def _read_temperature(self) -> ThermalReading:
        """Run the remote probe and validate its response.

        Returns:
            The validated reading.

        Raises:
            ProbeUnavailable: If the probe could not run or returned no JSON.
            ValueError: If the payload fails validation.
        """
        raw = self.probe_fn()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProbeUnavailable(
                f"probe returned non-JSON output: {raw.strip()[:200]!r}"
            ) from exc

        if not isinstance(payload, dict):
            raise ProbeUnavailable(f"probe returned a {type(payload).__name__}")
        if "error" in payload:
            raise ProbeUnavailable(f"probe reported: {payload['error']}")
        return ThermalReading.model_validate(payload)

    def _claim_alert(self, result: HealthCheckResult) -> bool:
        """Decide whether this caller should raise the alert for one breach.

        A worker pool all sees the same cached result object, so without this
        an eight-worker run would emit eight identical banners and eight
        overlapping sounds per cooldown. The first claimant wins; the rest wait
        silently and are still released by the same cooldown.

        Args:
            result: The failing check the caller is about to alert on.

        Returns:
            True for exactly one caller per distinct reading.
        """
        with self._lock:
            if self._alerted is result:
                return False
            self._alerted = result
            return True

    def _adjust_clock(self, result: HealthCheckResult) -> None:
        """Cap or lift the GPU boost clock to match the current reading.

        Hysteresis is deliberate: the cap goes on at
        ``settings.THERMAL_MAX_TEMP_C`` and comes off only at the lower
        ``settings.THERMAL_CLOCK_RESTORE_TEMP_C``, so a reading hovering at the
        limit does not flap the clock, and each flap would be a privileged SSH
        command.

        Args:
            result: The verdict just measured.
        """
        if not result.healthy:
            self.clock_controller.apply_cap()
            return
        reading = result.reading
        if (
            reading is not None
            and reading.gate_temp_c <= settings.THERMAL_CLOCK_RESTORE_TEMP_C
        ):
            self.clock_controller.restore()

    def check_or_wait(self) -> HealthCheckResult:
        """Admit work now, or wait out a breach, or abort the run.

        Returns immediately when the gate is disabled or the box is healthy.
        On a breach it caps the GPU boost clock, alerts, sleeps
        ``settings.THERMAL_COOLDOWN_SECONDS`` and re-measures, up to
        ``settings.THERMAL_MAX_COOLDOWN_CYCLES`` times. Once the box is cool
        enough the cap is lifted again.

        Returns:
            The passing health check, so the caller can log the measured
            temperatures alongside the work it is about to do.

        Raises:
            ThermalAbort: If the budget is exhausted, or if another thread has
                already aborted -- so a worker pool drains at once instead of
                each thread serving its own full cooldown.
        """
        if not settings.THERMAL_GATE_ENABLED:
            return HealthCheckResult(healthy=True, reason="thermal gate disabled")
        if self._aborted.is_set():
            raise ThermalAbort(
                HealthCheckResult(
                    healthy=False,
                    reason="the run was already aborted by the thermal gate",
                )
            )

        max_cycles = settings.THERMAL_MAX_COOLDOWN_CYCLES
        cycle = 0
        while True:
            result = self.read()
            self._adjust_clock(result)
            if result.healthy:
                if cycle:
                    logger.info(
                        "DGX recovered after %d cooldown(s): %s", cycle, result.reason
                    )
                return result

            if cycle >= max_cycles:
                self._aborted.set()
                raise ThermalAbort(result)

            cycle += 1
            if self._claim_alert(result):
                self.alert_fn(result, cycle, max_cycles)
            # Sleep outside the lock: a pool that all sees the same breach must
            # cool down concurrently, not one worker's cooldown after another's.
            self.sleep_fn(settings.THERMAL_COOLDOWN_SECONDS)
            self.invalidate()


def build_thermal_gate(
    dry_run: bool,
    disabled: bool,
    gate_factory: Callable[[], OllamaHealthGate] | None = None,
) -> OllamaHealthGate | None:
    """Construct the thermal gate for one batch run, or None when it must not run.

    One gate is shared by every unit of work in a run: its reading cache is
    what keeps a concurrent run to a single SSH probe per window. The same
    three switches apply to every batch, so the decision lives here rather
    than in each driver.

    Args:
        dry_run: A dry run issues no LLM calls, so it stays runnable with the
            DGX switched off entirely.
        disabled: The operator passed ``--no-thermal-gate``.
        gate_factory: Constructor to use; defaults to
            :class:`OllamaHealthGate`. Injected so a caller can keep the
            construction on its own module-level name for patching.

    Returns:
        The gate, or None when the run is a dry run, the operator disabled it,
        or ``settings.THERMAL_GATE_ENABLED`` is off.
    """
    if dry_run:
        return None
    if disabled:
        logger.warning(
            "Thermal gate disabled by --no-thermal-gate: this run can drive "
            "the Ollama box into a silent thermal shutdown."
        )
        return None
    if not settings.THERMAL_GATE_ENABLED:
        logger.warning("Thermal gate disabled by settings.THERMAL_GATE_ENABLED.")
        return None
    return (gate_factory or OllamaHealthGate)()


def stop_on_thermal_abort(
    log: logging.Logger,
    gate: OllamaHealthGate | None,
    abort: ThermalAbort,
    remaining: int,
    resume_hint: str,
) -> None:
    """Log the abort banner, lift the clock cap, sound the alarm and exit.

    For batch loops that do not go through ``BatchJobRunner`` (which writes a
    resume file of CELEX ids as well). Everything already written by the loop
    stays committed; ``resume_hint`` tells the operator how to continue.

    Args:
        log: The batch's logger.
        gate: The gate that raised, whose clock cap is restored.
        abort: The abort carrying the final health check.
        remaining: Units of work that were not processed.
        resume_hint: Command line that resumes the run.

    Raises:
        SystemExit: Always, with :data:`EXIT_CODE_THERMAL_ABORT`.
    """
    reading = abort.result.reading
    log.critical(
        "%s%s\nRUN STOPPED BY THE DGX THERMAL GATE: %s\n%s\n"
        "%d unit(s) of work were not processed; everything already written "
        "is committed.\nResume with:\n  %s\n%s%s",
        _BOLD,
        "=" * 72,
        abort.result.reason,
        reading.describe() if reading else "no reading available",
        remaining,
        resume_hint,
        "=" * 72,
        _RESET,
    )
    if gate is not None:
        gate.clock_controller.restore()
    play_alert_sound()
    sys.exit(EXIT_CODE_THERMAL_ABORT)
