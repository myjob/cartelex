"""Tests for the loader mappers."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import openpyxl
import polars as pl

from pipelines.download.reader import read_loader
from pipelines.download.mappers import (
    MAPPERS,
    CaseIndexMapper,
    FinalManifestMapper,
    UrlListMapper,
    build_mapper,
    missing_case_index_columns,
    mapper_for,
)

LOADER = Path("loader.csv")
EN = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31964D0233"
DE = "https://eur-lex.europa.eu/legal-content/DE/TXT/PDF/?uri=CELEX:31964D0233"
HTML = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:31964D0233"
EC = "https://ec.europa.eu/competition/antitrust/cases/dec_docs/36623/36623_338_1.pdf"


def _filtered(**over: str | None) -> pl.DataFrame:
    """Build a one-row filtered manifest frame with overrides."""
    row: dict[str, str | None] = {
        "manifest_row_id": "c_1 | c_2 | c_3",
        "date": "1964-03-11",
        "case_numbers": "IV/61",
        "title": "T",
        "parties": "P",
        "celex": "31964D0233",
        "document_class": "Negative clearance",
        "source_url": f"{HTML} | {EN}",
        "processing_status": "success",
        "raw_path": "a | b | c | d",
        "file_format": "html | pdf",
    }
    row.update(over)
    return pl.DataFrame([row], schema={k: pl.String for k in row})


def _files(rows: list[tuple[str, str, str | None]]) -> pl.DataFrame:
    """Build a per-file manifest frame from (id, format, url) tuples."""
    return pl.DataFrame(
        {
            "manifest_row_id": [r[0] for r in rows],
            "file_format": [r[1] for r in rows],
            "source_url": [r[2] for r in rows],
        },
        schema={"manifest_row_id": pl.String, "file_format": pl.String,
                "source_url": pl.String},
    )


STD_FILES = _files([("c_1", "html", HTML), ("c_2", "pdf", EN), ("c_3", "pdf", DE)])


class CaseIndexTests(unittest.TestCase):
    """CaseIndexMapper behaviour."""

    def test_case_index_mapper_uses_header_names_not_positions(self) -> None:
        """Column order is irrelevant; unknown columns land in meta."""
        df = pl.DataFrame(
            {
                "nace": ["27.1"],
                "title": ["Grosfillex"],
                "url_pdf": [EN],
                "case": ["AT.36623; AT.37275; AT.36820"],
                "celex": [""],
                "decision_date": ["2004-05-06"],
                "firms": ["A, B"],
                "comment": [None],
                "url_html": [HTML],
                "year": ["2004"],
            }
        )
        res = CaseIndexMapper().map(df, LOADER)
        self.assertEqual(res.failures, [])
        d = res.decisions[0]
        self.assertIsNone(d.celex_number)
        self.assertEqual(d.case_ids, ["AT.36623", "AT.37275", "AT.36820"])
        self.assertEqual(d.decision_date, dt.date(2004, 5, 6))
        self.assertEqual(d.decision_id, "DEC-AT36623-20040506")
        self.assertEqual(d.parties, "A, B")
        self.assertEqual(d.meta["nace"], "27.1")
        self.assertEqual(d.meta["comment"], "")
        self.assertEqual(d.loader_row, 1)
        self.assertEqual(sorted(c.file_format for c in d.candidates), ["html", "pdf"])

    def test_duplicate_celex_adds_review_reason(self) -> None:
        """Rows sharing a CELEX within the file are flagged."""
        df = pl.DataFrame(
            {"celex": ["CELEX-31964D0233", "31964D0233", "31964D0234"],
             "case": ["IV/1", "IV/2", "IV/3"], "url_pdf": [EN, DE, EC]}
        )
        res = CaseIndexMapper().map(df, LOADER)
        flags = ["duplicate CELEX within file" in d.review_reasons
                 for d in res.decisions]
        self.assertEqual(flags, [True, True, False])

    def test_bad_row_goes_to_failures_not_raised(self) -> None:
        """A row without usable identity is a RowFailure, others still map."""
        df = pl.DataFrame(
            {"celex": ["", "31964D0233"], "case": ["", "IV/1"],
             "url_pdf": [EN, EN], "decision_date": ["", ""]}
        )
        res = CaseIndexMapper().map(df, LOADER)
        self.assertEqual(len(res.decisions), 1)
        self.assertEqual(len(res.failures), 1)
        self.assertEqual(res.failures[0].loader_row, 1)
        self.assertEqual(res.failures[0].loader_file, "loader.csv")

    def test_unsafe_candidate_url_drops_only_the_candidate(self) -> None:
        """A candidate URL with no usable id is a failure; decision kept."""
        bad = "https://eur-lex.europa.eu/legal-content/TXT/PDF/?uri=X"
        df = pl.DataFrame({"celex": ["31964D0233"], "case": ["IV/1"],
                           "url_pdf": [bad], "url_html": [HTML]})
        res = CaseIndexMapper().map(df, LOADER)
        self.assertEqual(len(res.decisions), 1)
        self.assertEqual(len(res.decisions[0].candidates), 1)
        self.assertEqual(len(res.failures), 1)


class UrlListTests(unittest.TestCase):
    """UrlListMapper behaviour."""

    def test_one_decision_per_url(self) -> None:
        """Each URL becomes a decision; eur-lex URLs yield a CELEX id."""
        df = pl.DataFrame({"url": [EN, EC]})
        res = UrlListMapper().map(df, Path("u.txt"))
        self.assertEqual(res.decisions[0].decision_id, "CELEX-31964D0233")
        self.assertEqual(res.decisions[0].celex_number, "31964D0233")
        self.assertEqual(res.decisions[0].candidates[0].document_id,
                         "CELEX-31964D0233_EN")
        self.assertEqual(res.decisions[1].decision_id, "36623_338_1")
        self.assertIsNone(res.decisions[1].celex_number)


class FinalManifestTests(unittest.TestCase):
    """FinalManifestMapper behaviour."""

    def test_final_manifest_requires_file_manifest(self) -> None:
        """Construction without a usable per-file manifest is refused."""
        with self.assertRaises(ValueError):
            FinalManifestMapper(None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            FinalManifestMapper(pl.DataFrame({"x": ["1"]}))

    def test_final_manifest_explodes_manifest_row_id_and_joins_file_records(
        self,
    ) -> None:
        """Candidates come from the per-file records, in manifest order."""
        res = FinalManifestMapper(STD_FILES).map(_filtered(), LOADER)
        d = res.decisions[0]
        self.assertEqual(d.decision_id, "CELEX-31964D0233")
        self.assertEqual(d.case_ids, ["IV/61"])
        self.assertEqual([c.manifest_row_id for c in d.candidates],
                         ["c_1", "c_2", "c_3"])
        self.assertEqual(
            sorted(c.document_id for c in d.candidates),
            ["CELEX-31964D0233_DE", "CELEX-31964D0233_EN",
             "CELEX-31964D0233_EN_html"],
        )
        self.assertEqual(d.meta["raw_path"], "a | b | c | d")
        self.assertNotIn("celex", d.meta)

    def test_pipe_cells_never_split_positionally(self) -> None:
        """Null source_url recovered; counts differing from raw_path ignored."""
        df = _filtered(source_url=None, raw_path="only-one-path")
        res = FinalManifestMapper(STD_FILES).map(df, LOADER)
        urls = {c.source_url for c in res.decisions[0].candidates}
        self.assertEqual(urls, {HTML, EN, DE})

    def test_two_dates_take_first_and_add_review_reason(self) -> None:
        """Two dates: first wins, review reason added."""
        df = _filtered(date="1976-06-25 | 1978-07-20")
        d = FinalManifestMapper(STD_FILES).map(df, LOADER).decisions[0]
        self.assertEqual(d.decision_date, dt.date(1976, 6, 25))
        self.assertIn("two decision dates: 1976-06-25 | 1978-07-20", d.review_reasons)

    def test_html_only_decision_has_no_pdf_candidates(self) -> None:
        """HTML-only records stay as non-pdf candidates."""
        files = _files([("c_1", "html", HTML)])
        d = FinalManifestMapper(files).map(
            _filtered(manifest_row_id="c_1"), LOADER).decisions[0]
        self.assertEqual([c.file_format for c in d.candidates], ["html"])

    def test_xlsx_round_trip(self) -> None:
        """Both manifests written to xlsx and read back map identically."""
        with tempfile.TemporaryDirectory() as tmp:
            frames = {"filtered.xlsx": _filtered(), "files.xlsx": STD_FILES}
            for name, frame in frames.items():
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.append(frame.columns)
                for row in frame.iter_rows():
                    ws.append(list(row))
                wb.save(Path(tmp) / name)
            filtered = read_loader(Path(tmp) / "filtered.xlsx")
            files = read_loader(Path(tmp) / "files.xlsx")
        res = FinalManifestMapper(files).map(filtered, Path("filtered.xlsx"))
        self.assertEqual(res.failures, [])
        self.assertEqual(len(res.decisions[0].candidates), 3)

    def test_case_numbers_split_on_semicolon(self) -> None:
        """Semicolon separated case numbers become separate case ids."""
        df = _filtered(case_numbers="AT.36623; AT.37275; AT.36820")
        d = FinalManifestMapper(STD_FILES).map(df, LOADER).decisions[0]
        self.assertEqual(d.case_ids, ["AT.36623", "AT.37275", "AT.36820"])

    def test_blank_celex_is_none_and_falls_back_to_case_date(self) -> None:
        """Blank celex gives None, never an empty string."""
        d = FinalManifestMapper(STD_FILES).map(
            _filtered(celex=None), LOADER).decisions[0]
        self.assertIsNone(d.celex_number)
        self.assertEqual(d.decision_id, "DEC-IV61-19640311")

    def test_review_status_unmatched_ids_and_many_case_pdfs(self) -> None:
        """Upstream review, unmatched row ids and PDF overflow are flagged."""
        many = [(f"c_{i}", "pdf", f"https://ec.europa.eu/x/doc{i}.pdf")
                for i in range(9)]
        df = _filtered(
            manifest_row_id=" | ".join([r[0] for r in many] + ["c_missing"]),
            processing_status="review",
        )
        d = FinalManifestMapper(_files(many)).map(df, LOADER).decisions[0]
        reasons = " ".join(d.review_reasons)
        self.assertIn("upstream processing_status review", reasons)
        self.assertIn("unmatched manifest_row_id", reasons)
        self.assertIn("more than 8 distinct ec.europa.eu case PDFs", reasons)

    def test_own_source_url_cell_is_not_a_candidate_source(self) -> None:
        """A URL only in the row's source_url cell is ignored."""
        extra = "https://ec.europa.eu/x/only_in_row.pdf"
        df = _filtered(source_url=f"{extra} | {EN}")
        urls = {c.source_url for c in
                FinalManifestMapper(STD_FILES).map(df, LOADER).decisions[0].candidates}
        self.assertNotIn(extra, urls)

    def test_none_format_and_null_url_records(self) -> None:
        """Unknown format is inferred, never defaulted to pdf; null URL skipped."""
        files = _files([
            ("c_1", "pdf", "http://ec.europa.eu/competition/closed/audi.pdf"),
            ("c_2", None, "http://ec.europa.eu/competition/closed/en/1989.html"),  # type: ignore[list-item]
            ("c_3", None, "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ:C:1"),  # type: ignore[list-item]
            ("c_4", None, None),
        ])
        with self.assertLogs("pipelines.download.mappers", "WARNING") as logs:
            res = FinalManifestMapper(files).map(
                _filtered(manifest_row_id="c_1 | c_2 | c_3 | c_4"), LOADER
            )
        d = res.decisions[0]
        fmts = {c.manifest_row_id: c.file_format for c in d.candidates}
        # c_3 (eur-lex index page, no language marker) gets a fallback id.
        self.assertEqual(fmts, {"c_1": "pdf", "c_2": "html", "c_3": "other"})
        self.assertEqual(res.failures, [])
        other = next(c for c in d.candidates if c.manifest_row_id == "c_3")
        self.assertRegex(other.document_id, r"^CELEX-31964D0233_other_[0-9a-f]{8}$")
        self.assertTrue(d.candidates[0].source_url.startswith("http://"))
        self.assertIn("1 per-file records with blank URL", logs.output[0])

    def test_duplicate_url_in_decision_first_wins(self) -> None:
        """One candidate per distinct URL; the first record is kept."""
        files = _files([("c_1", "pdf", EN), ("c_2", "pdf", EN), ("c_3", "pdf", DE)])
        d = FinalManifestMapper(files).map(
            _filtered(manifest_row_id="c_1 | c_2 | c_3"), LOADER).decisions[0]
        self.assertEqual([c.manifest_row_id for c in d.candidates], ["c_1", "c_3"])

    def test_unknown_format_url_is_other_not_pdf(self) -> None:
        """A null-format record with an unrecognisable URL is non-pdf."""
        files = _files([("c_1", None, "https://example.org/registry/42")])  # type: ignore[list-item]
        d = FinalManifestMapper(files).map(
            _filtered(manifest_row_id="c_1"), LOADER).decisions[0]
        self.assertEqual(d.candidates[0].file_format, "other")

    def test_no_matching_ids_raises(self) -> None:
        """A per-file manifest matching nothing is the wrong file."""
        with self.assertRaises(ValueError):
            FinalManifestMapper(_files([("zz", "pdf", EN)])).map(_filtered(), LOADER)

    def test_unmatched_ids_logged(self) -> None:
        """Partially unmatched ids log a warning count."""
        with self.assertLogs("pipelines.download.mappers", "WARNING") as logs:
            FinalManifestMapper(STD_FILES).map(
                _filtered(manifest_row_id="c_1 | nope"), LOADER)
        self.assertIn("1 unmatched", logs.output[0])

    def test_duplicate_per_file_ids_logged(self) -> None:
        """Duplicate per-file manifest ids log a warning."""
        with self.assertLogs("pipelines.download.mappers", "WARNING"):
            FinalManifestMapper(_files([("c_1", "pdf", EN), ("c_1", "pdf", DE)]))

    def test_unparseable_date_review_reason(self) -> None:
        """A bad date yields a review reason."""
        d = FinalManifestMapper(STD_FILES).map(
            _filtered(date="soon"), LOADER).decisions[0]
        self.assertIn("unparseable decision date: soon", d.review_reasons)

    def test_case_pdf_threshold_boundary(self) -> None:
        """Exactly the limit gives no reason, one more does."""
        def reasons(n: int) -> list[str]:
            recs = [(f"c_{i}", "pdf", f"https://ec.europa.eu/x/doc{i}.pdf")
                    for i in range(n)]
            df = _filtered(manifest_row_id=" | ".join(r[0] for r in recs))
            return FinalManifestMapper(_files(recs)).map(
                df, LOADER).decisions[0].review_reasons
        self.assertEqual(reasons(8), [])
        self.assertEqual(len(reasons(9)), 1)


class LegacyHeaderTests(unittest.TestCase):
    """CaseIndexMapper on the real legacy csv header shape."""

    def test_case_x_duplicates_master_keys_and_html_in_pdf_column(self) -> None:
        """case.x alias, shadow duplicates ignored, master keys as ids."""
        df = pl.DataFrame({
            "celex": ["33585_2_4", "iv34801_en", "31964D0233"],
            "case.x": ["IV/33.585", "IV/34.801", "IV/61"],
            "url_pdf": [EC, EN, HTML],
            "url_pdf_duplicated_0": ["z", "z", "z"],
            "title": ["a", "b", "c"],
        })
        res = CaseIndexMapper().map(df, LOADER)
        self.assertEqual(res.failures, [])
        ids = [d.decision_id for d in res.decisions]
        self.assertEqual(ids, ["33585_2_4", "iv34801_en", "CELEX-31964D0233"])
        first, _, third = res.decisions
        self.assertIsNone(first.celex_number)
        self.assertIn("non-CELEX master key", first.review_reasons)
        self.assertEqual(first.case_ids, ["IV/33.585"])
        self.assertNotIn("url_pdf_duplicated_0", first.meta)
        self.assertEqual(third.candidates[0].file_format, "html")

    def test_lax_celex_lookalikes_fall_back_to_master_key(self) -> None:
        """c1417 and 35388_9_5 are master keys; CELEX-32002D0758 stays CELEX."""
        df = pl.DataFrame({
            "celex": ["c1417", "35388_9_5", "CELEX-32002D0758"],
            "case.x": ["IV/1", "IV/2", "IV/3"],
            "url_pdf": [EC, EC, EN],
        })
        res = CaseIndexMapper().map(df, LOADER)
        self.assertEqual(res.failures, [])
        ids = [d.decision_id for d in res.decisions]
        self.assertEqual(ids, ["c1417", "35388_9_5", "CELEX-32002D0758"])
        self.assertEqual([d.celex_number for d in res.decisions],
                         [None, None, "CELEX-32002D0758"])
        self.assertIn("non-CELEX master key", res.decisions[0].review_reasons)

    def test_default_mapper_checks_headers(self) -> None:
        """Unrecognisable headers are refused for the default mapper."""
        cols = ["manifest_row_id", "date", "celex"]
        self.assertEqual(missing_case_index_columns(cols), ["case or case.x"])
        with self.assertRaises(ValueError) as ctx:
            build_mapper(Path("a.xlsx"), None, None, cols)
        self.assertIn("--mapper", str(ctx.exception))


class BlankRowTests(unittest.TestCase):
    """Blank rows are dropped by the reader."""

    def test_blank_csv_and_xlsx_rows(self) -> None:
        """Blank rows vanish so loader_row counts data rows."""
        with tempfile.TemporaryDirectory() as tmp:
            c = Path(tmp) / "a.csv"
            c.write_text("celex;case\n;\n31964D0233;IV/1\n")
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.append(["celex", "case"])
            ws.append([None, None])
            ws.append(["31964D0233", "IV/1"])
            wb.save(Path(tmp) / "a.xlsx")
            self.assertEqual(read_loader(c).height, 1)
            self.assertEqual(read_loader(Path(tmp) / "a.xlsx").height, 1)


class RegistryTests(unittest.TestCase):
    """Mapper selection."""

    def test_default_by_extension_and_name(self) -> None:
        """txt defaults to the URL list mapper, others to the case index."""
        self.assertIs(mapper_for(Path("a.txt"), None), UrlListMapper)
        self.assertIs(mapper_for(Path("a.csv"), None), CaseIndexMapper)
        self.assertIs(mapper_for(Path("a.xlsx"), "final_manifest"),
                      FinalManifestMapper)
        self.assertIn("case_index", MAPPERS)
        with self.assertRaises(ValueError):
            mapper_for(Path("a.csv"), "nope")

    def test_build_mapper_passes_file_manifest_only_when_needed(self) -> None:
        """build_mapper hands the per-file manifest to the mapper that needs it."""
        m = build_mapper(Path("a.xlsx"), "final_manifest", STD_FILES)
        self.assertIsInstance(m, FinalManifestMapper)
        self.assertIsInstance(build_mapper(Path("a.csv"), None, None),
                              CaseIndexMapper)
        with self.assertRaises(ValueError):
            build_mapper(Path("a.xlsx"), "final_manifest", None)

    def test_final_manifest_headers_are_auto_detected(self) -> None:
        """manifest_row_id plus case_numbers selects the final manifest mapper."""
        cols = ["manifest_row_id", "case_numbers", "celex"]
        self.assertIs(mapper_for(Path("a.xlsx"), None, cols), FinalManifestMapper)
        m = build_mapper(Path("a.xlsx"), None, STD_FILES, cols)
        self.assertIsInstance(m, FinalManifestMapper)
        self.assertIs(mapper_for(Path("a.xlsx"), "case_index", cols),
                      CaseIndexMapper)
        self.assertIs(mapper_for(Path("a.xlsx"), None, ["celex", "case"]),
                      CaseIndexMapper)
        with self.assertRaises(ValueError):
            build_mapper(Path("a.xlsx"), None, None, ["x", "y"])


if __name__ == "__main__":
    unittest.main()
