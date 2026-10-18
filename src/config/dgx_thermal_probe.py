#!/usr/bin/env python3
"""Read the thermal state of an NVIDIA DGX Spark and print it as one JSON line.

This module is executed **on the DGX Spark itself**, not on the client. It is
copied to the box by ``scripts/deploy_dgx_thermal_probe.sh`` and invoked over
SSH by :mod:`config.ollama_health_gate`.

Because the DGX carries no copy of this repository and no virtualenv, the module
must stay importable by a bare ``python3``:

* no third-party imports,
* no import of :mod:`config.settings` or any other project module.

``test_dgx_thermal_probe.TestStdlibOnly`` parses this file's own import
statements and fails the suite if that rule is ever broken.

Why two sensors are read rather than only ``nvidia-smi``: on the GB10
(Grace-Blackwell) part the ACPI thermal zones run roughly 8 C hotter than the
GPU die sensor. Measured on ``spark-niki`` at 88 percent utilisation,
``temperature.gpu`` reported 75 C while ``thermal_zone0`` reported 83.3 C, with
the box's undocumented shutdown threshold near 85 C. A guard reading only
``temperature.gpu`` against an 80 C limit would not have fired at that moment,
so ``gate_temp_c`` is the maximum over both sources.

Usage::

    python3 dgx_thermal_probe.py            # one JSON line on stdout
    python3 dgx_thermal_probe.py --pretty   # indented, for human eyes

Exit codes:
    0   a reading was produced.
    2   no sensor could be read at all -- the caller must treat this as
        "temperature unknown", never as "cool".
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone

#: Version of the JSON contract below.
#:
#: The contract is **additive**: new optional fields do not bump it. This is not
#: cosmetic. The probe is redeployed to the Spark while batches are already
#: running against it, and those processes hold whatever client code they
#: imported hours earlier. A bump that an older client rejects turns a routine
#: deployment into a fleet-wide outage -- which is exactly what happened when
#: the clock fields were briefly shipped as schema 2: a running heideltime
#: cascade began reading every response as "temperature unreadable" and, under
#: THERMAL_GATE_STRICT, started cooling down toward a graceful abort while the
#: box sat at 37 C.
#:
#: Bump this only for a breaking change -- a field removed, renamed, or given a
#: new meaning or unit. Adding a field is not one.
SCHEMA_VERSION = 1

#: Fields requested from nvidia-smi, in order. ``memory.used``,
#: ``memory.total`` and ``temperature.memory`` are deliberately absent: the GB10
#: reports all three as ``N/A`` (its memory is unified with the CPU's), so
#: asking for them would only add columns that never carry a value.
NVIDIA_SMI_QUERY_FIELDS = (
    "index",
    "name",
    "temperature.gpu",
    "utilization.gpu",
    "clocks_throttle_reasons.active",
    "clocks.gr",
    "clocks.max.gr",
)

#: Where the kernel exposes the ACPI thermal zones.
THERMAL_ZONE_GLOB = "/sys/class/thermal/thermal_zone*"

#: Seconds allowed for the nvidia-smi call. It is a local read of driver state
#: and returns in milliseconds; anything slower means the driver is wedged,
#: which is itself a reason to stop rather than to wait.
NVIDIA_SMI_TIMEOUT_SECONDS = 10


class ProbeError(RuntimeError):
    """No temperature could be established from any sensor."""


def _run_nvidia_smi() -> str:
    """Invoke ``nvidia-smi`` and return its raw stdout.

    Isolated as a named function so tests can replace the process boundary
    without patching :mod:`subprocess` itself, matching the
    ``_run_heideltime_jar`` convention used elsewhere in this project.

    Returns:
        The command's stdout, decoded and stripped.

    Raises:
        ProbeError: If the binary is missing, times out, or exits non-zero.
    """
    command = [
        "nvidia-smi",
        "--query-gpu=" + ",".join(NVIDIA_SMI_QUERY_FIELDS),
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=NVIDIA_SMI_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProbeError("nvidia-smi is not installed on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(
            f"nvidia-smi did not return within {NVIDIA_SMI_TIMEOUT_SECONDS}s"
        ) from exc

    if completed.returncode != 0:
        raise ProbeError(
            f"nvidia-smi exited {completed.returncode}: "
            f"{completed.stderr.strip() or '(no stderr)'}"
        )
    return completed.stdout.strip()


def _parse_float(raw: str) -> float | None:
    """Convert one nvidia-smi CSV cell to a float, tolerating ``N/A``.

    Args:
        raw: A single cell of ``--format=csv,noheader,nounits`` output.

    Returns:
        The parsed value, or ``None`` when the field is unsupported on this
        device (rendered by nvidia-smi as ``N/A`` or ``[N/A]``).
    """
    cleaned = raw.strip().strip("[]")
    if not cleaned or cleaned.upper() == "N/A":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_nvidia_smi(stdout: str) -> dict[str, object]:
    """Parse the first GPU row of an nvidia-smi CSV response.

    Only the first row is used: the Spark is a single-GPU box, and a
    hypothetical second device would need its own gate semantics rather than
    being silently averaged in.

    Args:
        stdout: Raw ``--format=csv,noheader,nounits`` output.

    Returns:
        Mapping with ``gpu_name``, ``gpu_temp_c``, ``utilization_pct`` and
        ``throttle_reasons``. Individual values are ``None`` when the device
        does not report them.

    Raises:
        ProbeError: If no parsable row is present.
    """
    rows = [line for line in stdout.splitlines() if line.strip()]
    if not rows:
        raise ProbeError("nvidia-smi returned no GPU rows")

    cells = [cell.strip() for cell in rows[0].split(",")]
    if len(cells) < len(NVIDIA_SMI_QUERY_FIELDS):
        raise ProbeError(
            f"nvidia-smi row has {len(cells)} fields, expected "
            f"{len(NVIDIA_SMI_QUERY_FIELDS)}: {rows[0]!r}"
        )

    utilization = _parse_float(cells[3])
    clock_mhz = _parse_float(cells[5])
    clock_max_mhz = _parse_float(cells[6])
    return {
        "gpu_name": cells[1] or None,
        "gpu_temp_c": _parse_float(cells[2]),
        "utilization_pct": None if utilization is None else int(utilization),
        "throttle_reasons": cells[4] or None,
        "clock_mhz": None if clock_mhz is None else int(clock_mhz),
        "clock_max_mhz": None if clock_max_mhz is None else int(clock_max_mhz),
    }


def _read_thermal_zones() -> dict[str, float]:
    """Read every readable ACPI thermal zone, in degrees Celsius.

    The kernel exposes millidegrees in ``temp`` and a label in ``type``. Zones
    are keyed ``"<type>-<n>"`` because the Spark presents seven zones that all
    report the type ``acpitz``, which alone would collide.

    A zone that cannot be read is skipped rather than raised on: sensors come
    and go across driver versions, and one missing zone must not blind the
    guard to the six that still work.

    Returns:
        Mapping of zone label to temperature in Celsius; empty when the
        sysfs interface is absent (a non-Linux or containerised host).
    """
    zones: dict[str, float] = {}
    for index, zone_dir in enumerate(sorted(glob.glob(THERMAL_ZONE_GLOB))):
        try:
            with open(os.path.join(zone_dir, "temp"), "rt") as handle:
                millidegrees = int(handle.read().strip())
        except (OSError, ValueError):
            continue

        try:
            with open(os.path.join(zone_dir, "type"), "rt") as handle:
                zone_type = handle.read().strip() or "zone"
        except OSError:
            zone_type = "zone"

        zones[f"{zone_type}-{index}"] = round(millidegrees / 1000.0, 1)
    return zones


def collect_reading() -> dict[str, object]:
    """Gather both sensor sources into the JSON contract sent to the client.

    Returns:
        The full reading, including ``gate_temp_c`` -- the maximum of the GPU
        die sensor and every thermal zone, which is the value the client
        compares against its limit.

    Raises:
        ProbeError: If neither the GPU sensor nor any thermal zone yields a
            temperature, so the caller learns "unknown" rather than a number
            that happens to be low.
    """
    zones = _read_thermal_zones()

    try:
        gpu = parse_nvidia_smi(_run_nvidia_smi())
    except ProbeError:
        # A missing driver is not fatal while the board sensors still answer:
        # they are the hotter of the two sources and the ones the gate acts on.
        if not zones:
            raise
        gpu = {
            "gpu_name": None,
            "gpu_temp_c": None,
            "utilization_pct": None,
            "throttle_reasons": None,
            "clock_mhz": None,
            "clock_max_mhz": None,
        }

    candidates = [value for value in (gpu["gpu_temp_c"],) if value is not None]
    candidates.extend(zones.values())
    if not candidates:
        raise ProbeError(
            "no temperature available: nvidia-smi reported N/A and no thermal "
            "zone could be read"
        )

    soc_temp_c = max(zones.values()) if zones else None
    return {
        "schema": SCHEMA_VERSION,
        "hostname": socket.gethostname(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gpu_name": gpu["gpu_name"],
        "gpu_temp_c": gpu["gpu_temp_c"],
        "soc_temp_c": soc_temp_c,
        "gate_temp_c": max(candidates),
        "utilization_pct": gpu["utilization_pct"],
        "throttle_reasons": gpu["throttle_reasons"],
        "clock_mhz": gpu["clock_mhz"],
        "clock_max_mhz": gpu["clock_max_mhz"],
        "zones": zones,
    }


def main(argv: list[str] | None = None) -> int:
    """Print one thermal reading as JSON.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code: 0 when a reading was printed, 2 when no sensor
        could be read.
    """
    parser = argparse.ArgumentParser(
        description="Report DGX Spark GPU and SoC temperatures as JSON."
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Indent the JSON for human reading instead of emitting one line.",
    )
    # Accepted and ignored: the client passes it for readability at the call
    # site, and JSON is the only output this probe has ever produced.
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (the default; accepted for explicitness).",
    )
    args = parser.parse_args(argv)

    try:
        reading = collect_reading()
    except ProbeError as exc:
        print(json.dumps({"schema": SCHEMA_VERSION, "error": str(exc)}))
        return 2

    print(json.dumps(reading, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
