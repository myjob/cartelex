"""Unit tests for reporting.report_eddy_celex_facts (no database)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import polars as pl
from pydantic import ValidationError

from reporting import report_eddy_celex_facts as eddy


def _manifest() -> pl.DataFrame:
    """Nine manifest rows, one per reason code plus a merged and a shared row."""
    return pl.DataFrame({
        "loader_row": list(range(1, 10)),
        "manifest_row_id": [f"commission_{i}" for i in range(1, 10)],
        "celex": ["A", None, "C", "D", "E", "F", "G", "H", "A"],
        "title": [f"t{i}" for i in range(1, 10)],
    }, schema_overrides={"loader_row": pl.Int64})


def _lineage() -> pl.DataFrame:
    """Download lineage: row 2 mapper failure, row 3 merged, row 8 never audited."""
    return pl.DataFrame({
        "loader_row": [1, 2, 3, 4, 5, 6, 7, 9],
        "audit_decision_id": ["DA", None, "DOLD", "DD", "DE", "DF", "DG", "DA"],
        "row_failure": [None, "no CELEX, existing key, or case plus date", None, None, None,
                        None, None, None],
        "download_failures": [None, None, None, None, "http 404", None, None, None],
        "owner_decision_id": ["DA", None, "DOWN", "DD", "DE", "DF", "DG", "DA"],
    }, schema=eddy._LINEAGE_SCHEMA)


def _decisions() -> pl.DataFrame:
    """Registry status; DOLD is gone (merged into DOWN)."""
    return pl.DataFrame({
        "decision_id": ["DA", "DOWN", "DD", "DE", "DF", "DG"],
        "selection_status": ["selected", "selected", "no_pdf_source", "selected", "selected", "selected"],
        "review_reasons": [None, None, "letters only", None, None, None],
        "n_source_docs": [1, 1, 0, 0, 1, 1],
        "n_chunks": [10, 10, 0, 0, 0, 7],
        "n_facts_db": [20, 20, 0, 0, 0, 0],
        "n_fact_errors": [0, 0, 0, 0, 0, 3],
        "conversion": [None, None, None, None, "failed timeout", None],
        "translation": [None, None, None, None, None, None],
    }, schema=eddy._STATUS_SCHEMA)


def _facts() -> pl.DataFrame:
    """Facts report sheet: DA and DOWN processed; 'title' collides with the manifest."""
    return pl.DataFrame({"celex_id": ["DA", "DOWN"], "fines": ["10", "20"], "title": ["x", "y"]})


def _explained() -> pl.DataFrame:
    """Run explain_rows over the fixtures."""
    return eddy.explain_rows(_manifest(), _lineage(), _decisions(), _facts()["celex_id"])


class TestExplainRows(unittest.TestCase):
    """Decision resolution and the reason ladder."""

    def setUp(self) -> None:
        """Index the explained rows by loader_row."""
        self.rows = {r["loader_row"]: r for r in _explained().iter_rows(named=True)}

    def test_reason_codes_follow_the_ladder(self) -> None:
        """Each row stops at its first failing stage."""
        codes = {n: r["reason_code"] for n, r in self.rows.items()}
        self.assertEqual(codes, {1: "processed", 2: "no_decision", 3: "processed",
                                 4: "no_pdf_source", 5: "no_source_document",
                                 6: "not_embedded", 7: "no_facts", 8: "no_decision",
                                 9: "processed"})

    def test_stale_audit_id_resolves_to_owner(self) -> None:
        """A merged decision is traced to the owner of its documents."""
        row = self.rows[3]
        self.assertEqual(row["decision_id"], "DOWN")
        self.assertEqual(row["merged_into"], "DOWN")
        self.assertEqual(row["reason"], "merged DOLD into DOWN")

    def test_unmerged_row_has_no_merge_note(self) -> None:
        """A processed row whose audit id is still registered has no reason."""
        self.assertIsNone(self.rows[1]["merged_into"])
        self.assertIsNone(self.rows[1]["reason"])

    def test_reason_texts_carry_details(self) -> None:
        """Reasons quote the mapper failure, review, download and conversion details."""
        self.assertIn("no CELEX, existing key", self.rows[2]["reason"])
        self.assertIn("never run", self.rows[8]["reason"])
        self.assertIn("review: letters only", self.rows[4]["reason"])
        self.assertIn("download failures: http 404", self.rows[5]["reason"])
        self.assertIn("conversion: failed timeout", self.rows[6]["reason"])
        self.assertIn("failed fact ingestions: 3", self.rows[7]["reason"])

    def test_rows_sharing_decision(self) -> None:
        """Two manifest rows on one decision both count two."""
        self.assertEqual(self.rows[1]["rows_sharing_decision"], 2)
        self.assertEqual(self.rows[9]["rows_sharing_decision"], 2)
        self.assertIsNone(self.rows[2]["rows_sharing_decision"])

    def test_facts_in_db_but_not_in_report(self) -> None:
        """A decision with DB facts that the report lacks is flagged as a stale snapshot."""
        out = eddy.explain_rows(_manifest(), _lineage(), _decisions(), pl.Series(["DOWN"]))
        row = out.filter(pl.col("loader_row") == 1).row(0, named=True)
        self.assertEqual(row["reason_code"], "facts_not_in_report")


class TestSheets(unittest.TestCase):
    """Sheet builders."""

    def test_extended_manifest_keeps_rows_and_order(self) -> None:
        """All manifest rows survive, manifest columns first, then diagnostics, then facts."""
        out = eddy.build_extended_manifest(_explained(), _facts(), _manifest().columns)
        self.assertEqual(out["loader_row"].to_list(), list(range(1, 10)))
        self.assertEqual(out.columns[:4], _manifest().columns)
        self.assertEqual(out.columns[4:11], list(eddy.DIAGNOSTIC_COLUMNS[1:]))
        self.assertEqual(out.columns[11:], ["fines", "title_fact"])
        self.assertEqual(out.filter(pl.col("loader_row") == 3)["fines"][0], "20")

    def test_not_processed_lists_only_gaps(self) -> None:
        """The not_processed sheet holds exactly the unprocessed rows."""
        out = eddy.build_not_processed(_explained())
        self.assertEqual(sorted(out["loader_row"].to_list()), [2, 4, 5, 6, 7, 8])
        self.assertTrue(out["reason"].is_not_null().all())

    def test_reason_summary_counts(self) -> None:
        """The summary counts every row once."""
        out = eddy.build_reason_summary(_explained())
        self.assertEqual(out["rows"].sum(), 9)
        self.assertEqual(out.row(0), ("processed", 3))


class TestIo(unittest.TestCase):
    """File helpers and the input model."""

    def test_explicit_facts_report_skips_standard_report(self) -> None:
        """A given workbook is used as is; the standard report does not run."""
        with patch.object(eddy, "create_celex_fact_report") as create:
            out = eddy.resolve_facts_report(Path("given.xlsx"), Path("out"))
        self.assertEqual(out, Path("given.xlsx"))
        create.assert_not_called()

    def test_missing_facts_report_runs_standard_report_first(self) -> None:
        """Without a workbook the standard report is created into the output dir."""
        with patch.object(eddy, "create_celex_fact_report",
                          return_value="out/celex_fact_report_20261102000000.xlsx") as create:
            out = eddy.resolve_facts_report(None, Path("out"))
        create.assert_called_once_with(output_dir="out")
        self.assertEqual(out, Path("out/celex_fact_report_20261102000000.xlsx"))

    def test_standard_report_without_workbook_raises(self) -> None:
        """An empty standard report stops the run instead of joining nothing."""
        with patch.object(eddy, "create_celex_fact_report", return_value=None), \
                self.assertRaises(RuntimeError):
            eddy.resolve_facts_report(None, Path("out"))

    def test_inputs_reject_missing_file(self) -> None:
        """The input model validates the input paths."""
        with self.assertRaises(ValidationError):
            eddy.EddyReportInputs(manifest_path=Path("/nonexistent.xlsx"),
                                  facts_report_path=Path("/nonexistent.xlsx"), output_dir=Path("."))

    def test_read_manifest_numbers_rows_like_the_loader(self) -> None:
        """loader_row is 1-based over the rows read_loader returns."""
        with patch.object(eddy, "read_loader", return_value=pl.DataFrame({"celex": ["A", "B"]})):
            out = eddy.read_manifest(Path("m.xlsx"))
        self.assertEqual(out["loader_row"].to_list(), [1, 2])
        self.assertEqual(out.schema["loader_row"], pl.Int64)

    def test_build_report_writes_three_sheets(self) -> None:
        """End to end over mocked DB fetches and real workbooks."""
        with tempfile.TemporaryDirectory() as tmp:
            facts_path = Path(tmp) / "celex_fact_report_20260101000000.xlsx"
            _facts().write_excel(facts_path, worksheet=eddy.FACTS_SHEET)
            manifest_path = Path(tmp) / "filtered_final_manifest.xlsx"
            manifest_path.touch()
            inputs = eddy.EddyReportInputs(manifest_path=manifest_path,
                                           facts_report_path=facts_path, output_dir=Path(tmp) / "out")
            with patch.object(eddy, "read_manifest", return_value=_manifest()), \
                    patch.object(eddy, "fetch_row_lineage", return_value=_lineage()) as lineage, \
                    patch.object(eddy, "fetch_decision_status", return_value=_decisions()):
                path = eddy.build_report(inputs, MagicMock())
            lineage.assert_called_once()
            self.assertEqual(lineage.call_args.args[1], "filtered_final_manifest.xlsx")
            sheets = pl.read_excel(path, sheet_id=0, engine="openpyxl")
            self.assertEqual(list(sheets), [eddy.SHEET_MANIFEST, eddy.SHEET_NOT_PROCESSED,
                                            eddy.SHEET_SUMMARY])
            self.assertEqual(sheets[eddy.SHEET_MANIFEST].height, 9)


if __name__ == "__main__":
    unittest.main()
