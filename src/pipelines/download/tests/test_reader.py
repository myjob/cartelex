"""Tests for the loader reader."""

import tempfile
import unittest
from pathlib import Path

import openpyxl

from pipelines.download.reader import read_loader


class ReaderTests(unittest.TestCase):
    """Reader behaviour on csv and txt files."""

    def setUp(self) -> None:
        """Create a temp dir."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_csv_with_bom_and_semicolon_reads_headers(self) -> None:
        """BOM, semicolons and CRLF do not leak into header names."""
        path = self.dir / "a.csv"
        path.write_bytes(
            "﻿celex; case ;url_pdf\r\n31964D0233;IV/61;http://x/a.pdf\r\n"
            .encode("utf-8")
        )
        df = read_loader(path)
        self.assertEqual(df.columns, ["celex", "case", "url_pdf"])
        self.assertEqual(df["url_pdf"][0], "http://x/a.pdf")
        self.assertTrue(all(t == df.schema["celex"] for t in df.schema.values()))

    def test_txt_skips_comments_and_blanks(self) -> None:
        """Only real URL lines survive in the url column."""
        path = self.dir / "u.txt"
        path.write_text("# header\n\nhttp://a/1.pdf\n  \n# c\nhttp://a/2.pdf\n")
        df = read_loader(path)
        self.assertEqual(df.columns, ["url"])
        self.assertEqual(df["url"].to_list(), ["http://a/1.pdf", "http://a/2.pdf"])

    def test_xlsx_reads_all_columns_as_strings(self) -> None:
        """Numeric-looking cells keep their text; headers are trimmed."""
        path = self.dir / "a.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([" id ", "n"])
        ws.append(["00123", "7"])
        wb.save(path)
        df = read_loader(path)
        self.assertEqual(df.columns, ["id", "n"])
        self.assertEqual(df["id"][0], "00123")
        self.assertEqual(set(df.dtypes), {df.schema["id"]})
        self.assertEqual(str(df.schema["n"]), "String")

    def test_unsupported_extension_raises(self) -> None:
        """Unknown extensions are refused."""
        with self.assertRaises(ValueError):
            read_loader(self.dir / "x.json")


if __name__ == "__main__":
    unittest.main()
