"""Unit tests for the DGX Spark thermal probe.

Mocks live only at the system boundaries: ``_run_nvidia_smi`` (the named
subprocess wrapper) and ``_read_thermal_zones`` (the sysfs reader). No test
spawns a process or touches ``/sys``.
"""

import ast
import sys
import unittest
from pathlib import Path
from unittest import mock

from config import dgx_thermal_probe
from config.dgx_thermal_probe import (
    ProbeError,
    collect_reading,
    main,
    parse_nvidia_smi,
)

#: The exact row observed from spark-niki at 88 percent utilisation, where the
#: GPU sensor read 75 C while the hottest board zone read 83.3 C. The trailing
#: pair is the measured clock state: 2405 MHz current against a 3003 MHz max.
_OBSERVED_ROW = "0, NVIDIA GB10, 75, 88, 0x0000000000000000, 2405, 3003"

#: The seven zones that box exposes, at the same moment.
_OBSERVED_ZONES = {
    "acpitz-0": 83.3,
    "acpitz-1": 76.6,
    "acpitz-2": 77.3,
    "acpitz-3": 76.4,
    "acpitz-4": 83.0,
    "acpitz-5": 83.3,
    "acpitz-6": 76.0,
}


class TestParseNvidiaSmi(unittest.TestCase):
    """Parsing of the nvidia-smi CSV row."""

    def test_parses_the_observed_row(self) -> None:
        """Every field of a real GB10 row is extracted."""
        parsed = parse_nvidia_smi(_OBSERVED_ROW)

        self.assertEqual(parsed["gpu_name"], "NVIDIA GB10")
        self.assertEqual(parsed["gpu_temp_c"], 75.0)
        self.assertEqual(parsed["utilization_pct"], 88)
        self.assertEqual(parsed["throttle_reasons"], "0x0000000000000000")
        self.assertEqual(parsed["clock_mhz"], 2405)
        self.assertEqual(parsed["clock_max_mhz"], 3003)

    def test_na_temperature_becomes_none(self) -> None:
        """An unsupported sensor yields None rather than a bogus number."""
        parsed = parse_nvidia_smi("0, NVIDIA GB10, N/A, [N/A], 0x0, N/A, N/A")

        self.assertIsNone(parsed["gpu_temp_c"])
        self.assertIsNone(parsed["utilization_pct"])

    def test_empty_output_raises(self) -> None:
        """No row at all is an error, not an empty reading."""
        with self.assertRaises(ProbeError):
            parse_nvidia_smi("   \n  ")

    def test_short_row_raises(self) -> None:
        """A truncated row must not be parsed into partial values."""
        with self.assertRaises(ProbeError):
            parse_nvidia_smi("0, NVIDIA GB10, 75")


class TestCollectReading(unittest.TestCase):
    """Assembly of the JSON contract from both sensor sources."""

    def test_soc_zone_beats_the_gpu_sensor(self) -> None:
        """The gate temperature is the hottest sensor, not the GPU one.

        This is the reason the probe reads sysfs at all: on the GB10 the board
        zones run about 8 C above ``temperature.gpu``, and a guard reading only
        the GPU sensor would have passed an 80 C limit at 83.3 C.
        """
        with (
            mock.patch.object(
                dgx_thermal_probe, "_run_nvidia_smi", return_value=_OBSERVED_ROW
            ),
            mock.patch.object(
                dgx_thermal_probe,
                "_read_thermal_zones",
                return_value=dict(_OBSERVED_ZONES),
            ),
        ):
            reading = collect_reading()

        self.assertEqual(reading["gpu_temp_c"], 75.0)
        self.assertEqual(reading["soc_temp_c"], 83.3)
        self.assertEqual(reading["gate_temp_c"], 83.3)
        self.assertEqual(reading["schema"], dgx_thermal_probe.SCHEMA_VERSION)

    def test_gpu_sensor_wins_when_it_is_hotter(self) -> None:
        """The maximum is genuinely a maximum, not a preference for sysfs."""
        with (
            mock.patch.object(
                dgx_thermal_probe,
                "_run_nvidia_smi",
                return_value="0, GeForce, 91, 10, 0x0, 1900, 1900",
            ),
            mock.patch.object(
                dgx_thermal_probe, "_read_thermal_zones", return_value={"pkg-0": 37.0}
            ),
        ):
            reading = collect_reading()

        self.assertEqual(reading["gate_temp_c"], 91.0)

    def test_zones_alone_still_produce_a_reading(self) -> None:
        """A dead nvidia-smi does not blind the guard to the board sensors."""
        with (
            mock.patch.object(
                dgx_thermal_probe,
                "_run_nvidia_smi",
                side_effect=ProbeError("nvidia-smi is not installed on this host"),
            ),
            mock.patch.object(
                dgx_thermal_probe,
                "_read_thermal_zones",
                return_value=dict(_OBSERVED_ZONES),
            ),
        ):
            reading = collect_reading()

        self.assertIsNone(reading["gpu_temp_c"])
        self.assertEqual(reading["gate_temp_c"], 83.3)

    def test_no_sensor_at_all_raises(self) -> None:
        """With nothing readable the probe must say so, not report cool."""
        with (
            mock.patch.object(
                dgx_thermal_probe, "_run_nvidia_smi", side_effect=ProbeError("gone")
            ),
            mock.patch.object(
                dgx_thermal_probe, "_read_thermal_zones", return_value={}
            ),
        ):
            with self.assertRaises(ProbeError):
                collect_reading()

    def test_na_gpu_temperature_with_no_zones_raises(self) -> None:
        """nvidia-smi answering 'N/A' is no more a reading than a crash is."""
        with (
            mock.patch.object(
                dgx_thermal_probe,
                "_run_nvidia_smi",
                return_value="0, NVIDIA GB10, N/A, N/A, 0x0, 1000, 3003",
            ),
            mock.patch.object(
                dgx_thermal_probe, "_read_thermal_zones", return_value={}
            ),
        ):
            with self.assertRaises(ProbeError):
                collect_reading()


class TestMain(unittest.TestCase):
    """Exit codes and stdout of the command-line entry point."""

    def test_success_prints_json_and_exits_zero(self) -> None:
        """A good reading is one JSON line on stdout with exit code 0."""
        with (
            mock.patch.object(
                dgx_thermal_probe, "_run_nvidia_smi", return_value=_OBSERVED_ROW
            ),
            mock.patch.object(
                dgx_thermal_probe,
                "_read_thermal_zones",
                return_value=dict(_OBSERVED_ZONES),
            ),
            mock.patch("builtins.print") as printed,
        ):
            code = main(["--json"])

        self.assertEqual(code, 0)
        self.assertIn('"gate_temp_c": 83.3', printed.call_args[0][0])

    def test_unreadable_sensors_exit_two(self) -> None:
        """Exit 2 lets the client distinguish 'unknown' from 'cool'."""
        with (
            mock.patch.object(
                dgx_thermal_probe, "_run_nvidia_smi", side_effect=ProbeError("gone")
            ),
            mock.patch.object(
                dgx_thermal_probe, "_read_thermal_zones", return_value={}
            ),
            mock.patch("builtins.print") as printed,
        ):
            code = main([])

        self.assertEqual(code, 2)
        self.assertIn('"error"', printed.call_args[0][0])


class TestStdlibOnly(unittest.TestCase):
    """Guard on the constraint that makes the probe deployable to the DGX."""

    def test_probe_imports_only_the_standard_library(self) -> None:
        """The probe must not grow a third-party or project import.

        It runs on the Spark under a bare ``python3``, with no virtualenv and
        no copy of this repository. An import of ``httpx`` or
        ``config.settings`` would not fail here -- it would fail silently at
        3am on the box the gate is supposed to protect.
        """
        source = Path(dgx_thermal_probe.__file__).read_text()
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

        # __future__ is not listed in stdlib_module_names but is always present.
        offenders = sorted(
            name
            for name in imported
            if name not in sys.stdlib_module_names and name != "__future__"
        )
        self.assertEqual(
            offenders,
            [],
            f"dgx_thermal_probe must stay stdlib-only; found {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
