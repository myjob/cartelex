"""
Unit tests for the book cross-check batch driver.

Every collaborator is patched at the module boundary (case source, LM
configuration, reranker, cross-check module, ledger repository, thermal gate),
so the tests pin the driver's own behaviour: a dry run builds nothing
expensive, counters are reported honestly, the case-run ledger is written per
case, ``--resume`` skips finished keys, and a thermal abort stops the run.
"""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from batch import Main_cross_check_facts_vs_book as driver
from pipelines.agent_dspy.book_cross_check_module import (
    BookAbsence,
    BookReferenceType,
)

_MODULE = "batch.Main_cross_check_facts_vs_book"


def _result(
    celex_id: str,
    case_id: str,
    *,
    in_book: bool = True,
    absence: BookAbsence = BookAbsence.NONE,
    corroborated: int = 0,
    mismatch: int = 0,
    skipped: int = 0,
    already_checked: int = 0,
):
    """Build a ``BookCrossCheckResult`` stand-in."""
    return SimpleNamespace(
        celex_id=celex_id,
        case_id=case_id,
        in_book=in_book,
        absence_reason=absence,
        reference_type=BookReferenceType.LANDMARK,
        corroborated_count=corroborated,
        mismatch_count=mismatch,
        skipped_count=skipped,
        already_checked_count=already_checked,
    )


class _DriverHarness(unittest.TestCase):
    """Patches every boundary of the driver module."""

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.identity_repo = MagicMock()
        self.stack.enter_context(
            patch(f"{_MODULE}.CelexIdentityRepository", return_value=self.identity_repo)
        )
        self.configure = self.stack.enter_context(patch(f"{_MODULE}._configure_dspy"))
        self.ranker = self.stack.enter_context(patch(f"{_MODULE}.create_ranker"))
        for name in (
            "FactRepository",
            "CelexDatabaseToolkit",
            "HybridBookSearchToolkit",
            "BookChunkRepository",
            "LineageRepository",
        ):
            self.stack.enter_context(patch(f"{_MODULE}.{name}"))
        self.check_repo = MagicMock()
        self.check_repo.select_finished_case_keys.return_value = set()
        self.stack.enter_context(
            patch(f"{_MODULE}.Fact2BookCheckRepository", return_value=self.check_repo)
        )
        self.module = MagicMock()
        self.module_cls = self.stack.enter_context(
            patch(f"{_MODULE}.BookCrossCheckModule", return_value=self.module)
        )
        self.gate = None
        self.build_gate = self.stack.enter_context(
            patch(f"{_MODULE}.build_thermal_gate", side_effect=lambda **kw: self.gate)
        )

    def _cases(self, *keys: tuple[str, str]) -> None:
        self.identity_repo.select_distinct_book_cases.return_value = [
            {"celex_id": c, "case_x": None, "case_id": k} for c, k in keys
        ]


class TestDryRun(_DriverHarness):
    """A dry run lists cases and touches nothing expensive."""

    def test_builds_no_lm_ranker_module_or_ledger(self) -> None:
        self._cases(("CELEX-1", "IV/1"), ("CELEX-2", "IV/2"))

        driver.run(dry_run=True)

        self.configure.assert_not_called()
        self.ranker.assert_not_called()
        self.module_cls.assert_not_called()
        self.check_repo.upsert_case_run.assert_not_called()
        self.build_gate.assert_not_called()


class TestCountersAndLedger(_DriverHarness):
    """Counters separate absence reasons and untouched facts; every case is ledgered."""

    def test_absence_reasons_and_already_checked_are_reported_apart(self) -> None:
        self._cases(("CELEX-1", "IV/1"), ("CELEX-2", "IV/2"), ("CELEX-3", "IV/3"))
        self.module.side_effect = [
            _result("CELEX-1", "IV/1", corroborated=2, skipped=1, already_checked=5),
            _result("CELEX-2", "IV/2", in_book=False, absence=BookAbsence.NO_SURFACE),
            _result("CELEX-3", "IV/3", in_book=False, absence=BookAbsence.NO_ANCHOR),
        ]

        with self.assertLogs(driver.logger, level="INFO") as logs:
            driver.run(run_id="r1")

        summary = logs.output[-1]
        self.assertIn("'corroborated': 2", summary)
        self.assertIn("'skipped': 1", summary)
        self.assertIn("already_checked=5", summary)
        self.assertIn("'no_surface': 1", summary)
        self.assertIn("'no_anchor': 1", summary)

    def test_case_run_row_written_per_case_with_run_id(self) -> None:
        self._cases(("CELEX-1", "IV/1"), ("CELEX-2", "IV/2"))
        self.module.side_effect = [
            _result("CELEX-1", "IV/1", corroborated=1, mismatch=1),
            _result("CELEX-2", "IV/2", in_book=False, absence=BookAbsence.NO_SURFACE),
        ]

        driver.run(run_id="r1")

        calls = self.check_repo.upsert_case_run.call_args_list
        self.assertEqual(len(calls), 2)
        first = calls[0].kwargs
        self.assertEqual((first["celex_id"], first["case_id"], first["run_id"]), ("CELEX-1", "IV/1", "r1"))
        self.assertTrue(first["in_book"])
        self.assertEqual(first["reference_type"], "landmark")
        self.assertEqual((first["corroborated"], first["mismatch"]), (1, 1))
        self.assertLessEqual(first["started_at"], first["finished_at"])
        second = calls[1].kwargs
        self.assertFalse(second["in_book"])
        self.assertEqual(second["absence_reason"], "no_surface")
        self.assertIsNone(second["reference_type"])

    def test_module_error_is_counted_and_case_not_ledgered(self) -> None:
        self._cases(("CELEX-1", "IV/1"))
        self.module.side_effect = RuntimeError("boom")

        with self.assertLogs(driver.logger, level="INFO") as logs:
            driver.run(run_id="r1")

        self.assertIn("errors=1", logs.output[-1])
        self.check_repo.upsert_case_run.assert_not_called()

    def test_default_run_id_is_a_timestamp(self) -> None:
        self._cases(("CELEX-1", "IV/1"))
        self.module.return_value = _result("CELEX-1", "IV/1")

        driver.run()

        run_id = self.check_repo.upsert_case_run.call_args.kwargs["run_id"]
        self.assertRegex(run_id, r"^book-\d{8}-\d{6}$")


class TestResume(_DriverHarness):
    """``--resume`` skips keys finished under the same run id."""

    def test_finished_keys_are_skipped_without_module_calls(self) -> None:
        self._cases(("CELEX-1", "IV/1"), ("CELEX-2", "IV/2"))
        self.check_repo.select_finished_case_keys.return_value = {("CELEX-1", "IV/1")}
        self.module.return_value = _result("CELEX-2", "IV/2")

        with self.assertLogs(driver.logger, level="INFO") as logs:
            driver.run(run_id="r1", resume=True)

        self.check_repo.select_finished_case_keys.assert_called_once_with("r1")
        self.module.assert_called_once_with(celex_id="CELEX-2", case_id="IV/2", case_x=None)
        self.assertIn("resumed_past=1", logs.output[-1])

    def test_without_resume_the_ledger_is_not_consulted(self) -> None:
        self._cases(("CELEX-1", "IV/1"))
        self.module.return_value = _result("CELEX-1", "IV/1")

        driver.run(run_id="r1")

        self.check_repo.select_finished_case_keys.assert_not_called()


class TestThermalGate(_DriverHarness):
    """The gate is checked before each case and an abort stops the run."""

    def test_gate_checked_per_case(self) -> None:
        self._cases(("CELEX-1", "IV/1"), ("CELEX-2", "IV/2"))
        self.gate = MagicMock()
        self.module.return_value = _result("CELEX-1", "IV/1")

        driver.run(run_id="r1")

        self.gate.clock_controller.ensure_default.assert_called_once_with()
        self.assertEqual(self.gate.check_or_wait.call_count, 2)
        self.build_gate.assert_called_once_with(dry_run=False, disabled=False)

    def test_abort_stops_with_resume_hint(self) -> None:
        self._cases(("CELEX-1", "IV/1"), ("CELEX-2", "IV/2"), ("CELEX-3", "IV/3"))
        self.gate = MagicMock()
        self.gate.check_or_wait.side_effect = [None, driver.ThermalAbort(MagicMock())]
        self.module.return_value = _result("CELEX-1", "IV/1")
        stop = self.stack.enter_context(
            patch(f"{_MODULE}.stop_on_thermal_abort", side_effect=SystemExit(2))
        )

        with self.assertRaises(SystemExit):
            driver.run(run_id="r1")

        self.module.assert_called_once()
        remaining = stop.call_args.args[3]
        self.assertEqual(remaining, 2)
        self.assertIn("--run-id r1 --resume", stop.call_args.args[4])


if __name__ == "__main__":
    unittest.main()
