"""Unit tests for the human-index reconciliation driver.

The workbook is a small file written to a temporary directory; the repository
is a mock, so no database is touched.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/batch/tests/test_Main_recon_human_index.py -v
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import openpyxl

from batch import Main_recon_human_index as driver
from pipelines.legal.human_index_recon import (
    FACT_COMMISSIONER,
    FACT_COMPANIES,
    FACT_INFRINGEMENT,
    RECON_FACT_NAMES,
    ReconStatus,
    ReconTarget,
    SizeBand,
)

_HEADER = [
    "celex",
    "case",
    "year",
    "title",
    "firms",
    "Formal Decision",
    "Nature of Conduct",
    "Indicator_Conduct",
    "Responsible Commissioner",
]
_VITAMINS = [
    "CELEX-32003D0002",
    "37512",
    2001,
    "Vitamins",
    "BASF AG;Aventis SA",
    "Infringement",
    "Horizontal",
    "1",
    "Mario Monti (M)",
]
_UNLABELLED = ["AT_40000_1", "40000", None, None, None, None, None, None, None]


def _write_workbook(directory: str, rows: list[list], header: list[str] | None = None) -> str:
    """Write a one-sheet workbook and return its path."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(header or _HEADER)
    for row in rows:
        sheet.append(row)
    path = str(Path(directory) / "index.xlsx")
    workbook.save(path)
    return path


def _repository() -> MagicMock:
    """Build a repository mock holding the Vitamins facts."""
    repository = MagicMock()
    repository.select_fact_values.return_value = {
        "CELEX-32003D0002": {
            FACT_INFRINGEMENT: ("True", ""),
            FACT_COMMISSIONER: ("Mario Monti", ""),
            FACT_COMPANIES: ("| company |", json.dumps([{"company": "BASF AG"}])),
        }
    }
    repository.select_content_tokens.return_value = {"CELEX-32003D0002": 55_000}
    repository.upsert_records.side_effect = len
    return repository


class _WorkbookCase(unittest.TestCase):
    """Provides a temporary directory for workbooks."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name


class TestReadHumanIndex(_WorkbookCase):
    def test_reads_needed_columns_as_strings_with_sheet_row_numbers(self) -> None:
        frame = driver.read_human_index(_write_workbook(self.directory, [_VITAMINS, _UNLABELLED]))
        self.assertEqual(frame.height, 2)
        first = frame.row(0, named=True)
        self.assertEqual(first["source_row"], 2)
        self.assertEqual(first["celex_id"], "CELEX-32003D0002")
        self.assertEqual(first["decision_year"], "2001")
        self.assertEqual(first["firms"], "BASF AG;Aventis SA")
        self.assertIsNone(frame.row(1, named=True)["formal_decision"])

    def test_rows_without_identifier_are_skipped(self) -> None:
        blank = [None, "x", 1999, "No id", None, None, None, None, None]
        frame = driver.read_human_index(_write_workbook(self.directory, [blank, _VITAMINS]))
        self.assertEqual(frame.get_column("source_row").to_list(), [3])

    def test_missing_header_is_reported(self) -> None:
        path = _write_workbook(self.directory, [], header=["celex", "title"])
        with self.assertRaises(ValueError) as raised:
            driver.read_human_index(path)
        self.assertIn("Formal Decision", str(raised.exception))


class TestToIndexRows(_WorkbookCase):
    def test_repeated_identifier_keeps_the_first_row_and_is_reported(self) -> None:
        second = [*_VITAMINS[:3], "Vitamins II", *_VITAMINS[4:]]
        frame = driver.read_human_index(_write_workbook(self.directory, [_VITAMINS, second]))
        rows, duplicates = driver.to_index_rows(frame)
        self.assertEqual(duplicates, ["CELEX-32003D0002"])
        self.assertEqual([row.title for row in rows], ["Vitamins"])
        self.assertEqual(rows[0].decision_year, 2001)

    def test_unparseable_year_becomes_none(self) -> None:
        odd = [*_VITAMINS[:2], "n/a", *_VITAMINS[3:]]
        rows, _ = driver.to_index_rows(
            driver.read_human_index(_write_workbook(self.directory, [odd]))
        )
        self.assertIsNone(rows[0].decision_year)


class TestRun(_WorkbookCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = _write_workbook(self.directory, [_VITAMINS, _UNLABELLED])
        self.repository = _repository()

    def test_writes_one_record_per_decision_and_target(self) -> None:
        records = driver.run(xlsx_path=self.path, repository=self.repository)
        self.repository.probe_table.assert_called_once()
        self.repository.select_fact_values.assert_called_once_with(RECON_FACT_NAMES)
        self.repository.upsert_records.assert_called_once_with(records)
        self.assertEqual(len(records), 2 * len(ReconTarget))

    def test_records_carry_outcome_and_decision_context(self) -> None:
        records = driver.run(xlsx_path=self.path, repository=self.repository)
        by_key = {(r.result.celex_id, r.result.target): r for r in records}
        decision = by_key[("CELEX-32003D0002", ReconTarget.FORMAL_DECISION)]
        self.assertEqual(decision.result.status, ReconStatus.AGREE)
        self.assertEqual(decision.title, "Vitamins")
        self.assertEqual(decision.decision_year, 2001)
        self.assertEqual(decision.content_tokens, 55_000)
        self.assertEqual(decision.size_band, SizeBand.LARGE)
        self.assertEqual(decision.source_file, "index.xlsx")
        self.assertEqual(decision.source_row, 2)
        # Aventis SA is in the index but not on the stored roster.
        firms = by_key[("CELEX-32003D0002", ReconTarget.FIRMS)].result
        self.assertEqual(firms.status, ReconStatus.CONFLICT)
        self.assertEqual(firms.detail["missing"], ["Aventis SA"])

    def test_unlabelled_decision_without_facts_is_blank_and_unbanded(self) -> None:
        records = driver.run(xlsx_path=self.path, repository=self.repository)
        unlabelled = [r for r in records if r.result.celex_id == "AT_40000_1"]
        self.assertEqual({r.result.status for r in unlabelled}, {ReconStatus.HUMAN_BLANK})
        self.assertEqual({r.size_band for r in unlabelled}, {None})

    def test_dry_run_compares_but_neither_probes_nor_writes(self) -> None:
        records = driver.run(xlsx_path=self.path, dry_run=True, repository=self.repository)
        self.assertEqual(len(records), 2 * len(ReconTarget))
        self.repository.probe_table.assert_not_called()
        self.repository.upsert_records.assert_not_called()

    def test_celex_filter_limits_the_run_to_one_decision(self) -> None:
        records = driver.run(
            xlsx_path=self.path, celex_filter="AT_40000_1", repository=self.repository
        )
        self.assertEqual({r.result.celex_id for r in records}, {"AT_40000_1"})

    def test_missing_table_stops_before_the_workbook_is_read(self) -> None:
        self.repository.probe_table.side_effect = RuntimeError("apply the migration")
        with self.assertRaises(RuntimeError):
            driver.run(xlsx_path=self.path, repository=self.repository)
        self.repository.select_fact_values.assert_not_called()


if __name__ == "__main__":
    unittest.main()
