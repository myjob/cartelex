import os
import tempfile
import unittest
import polars as pl
from unittest.mock import patch
from reporting.report_celex_facts import fetch_and_pivot_celex_facts, export_report_to_excel


def _mock_db_df() -> pl.DataFrame:
    """Mock Polars DataFrame representing the result of the database query."""
    return pl.DataFrame({
        "celex_id": ["C1", "C1", "C2", "C3"],
        "name": ["fact_A", "fact_B", "fact_A", "fact_C"],
        "value_json": ['{"val": 1}', '{"val": 2}', '{"val": 3}', '{"val": 4}']
    })


class TestFetchAndPivotCelexFacts(unittest.TestCase):

    @patch("reporting.report_celex_facts.pl.read_database_uri")
    def test_fetch_and_pivot_celex_facts(self, mock_read_db):
        """Test that the query results are correctly pivoted."""
        mock_read_db.return_value = _mock_db_df()

        pivoted_df = fetch_and_pivot_celex_facts(db_url="sqlite:///:memory:")

        mock_read_db.assert_called_once()
        self.assertFalse(pivoted_df.is_empty())
        self.assertEqual(pivoted_df.height, 3)  # 3 distinct celex_ids
        self.assertIn("celex_id", pivoted_df.columns)
        self.assertIn("fact_A", pivoted_df.columns)
        self.assertIn("fact_B", pivoted_df.columns)
        self.assertIn("fact_C", pivoted_df.columns)

        # C1 should have fact_A and fact_B
        c1_row = pivoted_df.filter(pl.col("celex_id") == "C1")
        self.assertEqual(c1_row["fact_A"][0], '{"val": 1}')
        self.assertEqual(c1_row["fact_B"][0], '{"val": 2}')
        self.assertIsNone(c1_row["fact_C"][0])

        # C2 should only have fact_A
        c2_row = pivoted_df.filter(pl.col("celex_id") == "C2")
        self.assertEqual(c2_row["fact_A"][0], '{"val": 3}')
        self.assertIsNone(c2_row["fact_B"][0])


class TestExportReportToExcel(unittest.TestCase):

    def test_export_report_to_excel(self):
        """Test that the DataFrame is exported to an Excel file on disk."""
        df_pivoted = pl.DataFrame({
            "celex_id": ["C1", "C2"],
            "fact_A": ['{"val": 1}', '{"val": 3}']
        })
        df_empty = pl.DataFrame()

        with tempfile.TemporaryDirectory() as tmp_dir:
            filepath = export_report_to_excel(
                df_pivoted,
                df_markdown=df_empty,
                df_plaintext=df_empty,
                df_fact_names=df_empty,
                df_ingestion_errors=df_empty,
                df_ingestion_error_pivot=df_empty,
                output_dir=tmp_dir,
            )

            self.assertIsNotNone(filepath)
            self.assertTrue(os.path.isfile(filepath))
            self.assertTrue(filepath.endswith(".xlsx"))

    def test_export_report_empty_df(self):
        """Test export behavior with all empty DataFrames."""
        df_empty = pl.DataFrame()
        filepath = export_report_to_excel(
            df_empty,
            df_markdown=df_empty,
            df_plaintext=df_empty,
            df_fact_names=df_empty,
            df_ingestion_errors=df_empty,
            df_ingestion_error_pivot=df_empty,
        )
        self.assertIsNone(filepath)


if __name__ == "__main__":
    unittest.main()
