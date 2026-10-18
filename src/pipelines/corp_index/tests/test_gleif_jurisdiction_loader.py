"""Unit tests for the GLEIF jurisdiction code-list loader.

No DB or network access: the CSV parsing and the ISO-part derivation are
exercised against temporary files, and the repository upsert is driven through a
mock engine so only the SQL-building boundary is touched.
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy.dialects import postgresql

# Ensure src is on the path (src/pipelines/corp_index/tests -> src).
sys.path.append(
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
)

from pydantic import ValidationError  # noqa: E402

from pipelines.corp_index.gleif_jurisdiction_loader import (  # noqa: E402
    GleifJurisdiction,
    GleifJurisdictionRepository,
    load_jurisdictions,
)

_CSV_HEADER = "Jurisdiction,Code,Type\n"

# SQLAlchemy suffixes bind-parameter names per VALUES row (``..._m0``); strip it
# so assertions read against plain column names.
_MULTI_ROW_SUFFIX = re.compile(r"_m\d+$")


def _bound_columns(statement: object) -> dict[str, object]:
    """Compile an INSERT and return its bind values keyed by column name.

    Args:
        statement: The SQLAlchemy insert construct captured from the repository.

    Returns:
        Mapping of column name to the value bound for the first VALUES row.
    """
    compiled = statement.compile(dialect=postgresql.dialect())
    return {
        _MULTI_ROW_SUFFIX.sub("", name): value
        for name, value in compiled.params.items()
    }


def _write_csv(rows: str, *, with_bom: bool = False) -> str:
    """Write a GLEIF-shaped CSV to a temp file and return its path.

    Args:
        rows: Data lines (without the header), newline-terminated.
        with_bom: When True, prefix the file with a UTF-8 BOM, as the real
            GLEIF download ships.

    Returns:
        The path to the written file.
    """
    encoding = "utf-8-sig" if with_bom else "utf-8"
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding=encoding
    )
    with handle:
        handle.write(_CSV_HEADER + rows)
    return handle.name


class TestGleifJurisdictionDerivation(unittest.TestCase):
    """ISO country / subdivision derivation from a GLEIF jurisdiction code."""

    def test_country_only_code_has_no_subdivision(self) -> None:
        """A 2-letter code is its own country code with a NULL subdivision."""
        record = GleifJurisdiction(
            jurisdiction_code="AT",
            jurisdiction_name="Austria",
            jurisdiction_type="COUNTRY_ONLY",
        )

        self.assertEqual(record.iso_country_code, "AT")
        self.assertIsNone(record.iso_subdivision_code)
        self.assertFalse(record.is_subdivision)

    def test_subdivision_code_splits_into_parent_and_subdivision(self) -> None:
        """A hyphenated code yields the ISO 3166-1 parent and 3166-2 part."""
        record = GleifJurisdiction(
            jurisdiction_code="AE-DU",
            jurisdiction_name="Dubai (United Arab Emirates)",
            jurisdiction_type="COUNTRY_AND_SUBDIVISION",
        )

        self.assertEqual(record.iso_country_code, "AE")
        self.assertEqual(record.iso_subdivision_code, "DU")
        self.assertTrue(record.is_subdivision)

    def test_three_character_and_numeric_subdivisions_are_accepted(self) -> None:
        """GB-SCT (3 letters) and PT-20 (numeric) are both valid ISO 3166-2."""
        scotland = GleifJurisdiction(
            jurisdiction_code="GB-SCT",
            jurisdiction_name="Scotland (United Kingdom)",
            jurisdiction_type="COUNTRY_AND_SUBDIVISION",
        )
        azores = GleifJurisdiction(
            jurisdiction_code="PT-20",
            jurisdiction_name="Regiao Autonoma dos Acores (Portugal)",
            jurisdiction_type="COUNTRY_AND_SUBDIVISION",
        )

        self.assertEqual(
            (scotland.iso_country_code, scotland.iso_subdivision_code), ("GB", "SCT")
        )
        self.assertEqual(
            (azores.iso_country_code, azores.iso_subdivision_code), ("PT", "20")
        )

    def test_code_is_normalized_before_derivation(self) -> None:
        """A lowercase / padded code is uppercased and trimmed, then split."""
        record = GleifJurisdiction(
            jurisdiction_code="  us-de  ",
            jurisdiction_name="Delaware (United States)",
            jurisdiction_type="COUNTRY_AND_SUBDIVISION",
        )

        self.assertEqual(record.jurisdiction_code, "US-DE")
        self.assertEqual(record.iso_country_code, "US")
        self.assertEqual(record.iso_subdivision_code, "DE")

    def test_non_iso_shaped_code_is_rejected(self) -> None:
        """A code that is not ISO 3166 shaped fails validation at the boundary."""
        with self.assertRaises(ValidationError):
            GleifJurisdiction(
                jurisdiction_code="DUBAI",
                jurisdiction_name="Dubai",
                jurisdiction_type="COUNTRY_AND_SUBDIVISION",
            )

    def test_derived_parts_cannot_be_overridden_by_input(self) -> None:
        """Supplied ISO parts are ignored; the code alone determines them."""
        record = GleifJurisdiction(
            jurisdiction_code="AE-DU",
            jurisdiction_name="Dubai (United Arab Emirates)",
            jurisdiction_type="COUNTRY_AND_SUBDIVISION",
            iso_country_code="ZZ",
            iso_subdivision_code="ZZZ",
        )

        self.assertEqual(record.iso_country_code, "AE")
        self.assertEqual(record.iso_subdivision_code, "DU")


class TestLoadJurisdictions(unittest.TestCase):
    """CSV parsing behaviour."""

    def test_loads_rows_in_file_order(self) -> None:
        """Each data line becomes one validated record, order preserved."""
        path = _write_csv(
            "Andorra,AD,COUNTRY_ONLY\n"
            "United Arab Emirates,AE,COUNTRY_AND_SUBDIVISION\n"
            "Dubai (United Arab Emirates),AE-DU,COUNTRY_AND_SUBDIVISION\n"
        )
        self.addCleanup(os.unlink, path)

        records = load_jurisdictions(path)

        self.assertEqual(
            [record.jurisdiction_code for record in records], ["AD", "AE", "AE-DU"]
        )
        self.assertEqual(records[2].iso_country_code, "AE")

    def test_utf8_bom_and_non_ascii_names_are_handled(self) -> None:
        """The real download's BOM is stripped and unicode names survive."""
        path = _write_csv(
            "‘Ajmān (United Arab Emirates),AE-AJ,COUNTRY_AND_SUBDIVISION\n",
            with_bom=True,
        )
        self.addCleanup(os.unlink, path)

        records = load_jurisdictions(path)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].jurisdiction_code, "AE-AJ")
        self.assertEqual(
            records[0].jurisdiction_name, "‘Ajmān (United Arab Emirates)"
        )

    def test_missing_column_raises_value_error(self) -> None:
        """A CSV without the expected header is rejected with a clear error."""
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        )
        with handle:
            handle.write("Name,Iso\nAustria,AT\n")
        self.addCleanup(os.unlink, handle.name)

        with self.assertRaises(ValueError) as context:
            load_jurisdictions(handle.name)

        self.assertIn("missing column", str(context.exception))

    def test_absent_file_raises_file_not_found(self) -> None:
        """A non-existent path fails before any parsing is attempted."""
        with self.assertRaises(FileNotFoundError):
            load_jurisdictions(Path("/nonexistent/gleif.csv"))


class TestGleifJurisdictionRepository(unittest.TestCase):
    """Upsert and prune behaviour with the session boundary mocked."""

    def setUp(self) -> None:
        """Build a repository over a mock engine and capture its session."""
        self.repository = GleifJurisdictionRepository(engine=mock.MagicMock())
        self.session = mock.MagicMock()
        self.repository._session_factory = mock.MagicMock(return_value=self.session)

    def test_upsert_writes_derived_columns_and_commits_once(self) -> None:
        """One statement carries the derived ISO parts and is committed."""
        records = [
            GleifJurisdiction(
                jurisdiction_code="AE-DU",
                jurisdiction_name="Dubai (United Arab Emirates)",
                jurisdiction_type="COUNTRY_AND_SUBDIVISION",
            )
        ]

        written = self.repository.upsert(records, source_version="1.5")

        self.assertEqual(written, 1)
        self.session.execute.assert_called_once()
        self.session.commit.assert_called_once()
        statement = self.session.execute.call_args.args[0]
        bound = _bound_columns(statement)
        self.assertEqual(bound["jurisdiction_code"], "AE-DU")
        self.assertEqual(bound["iso_country_code"], "AE")
        self.assertEqual(bound["iso_subdivision_code"], "DU")
        self.assertTrue(bound["is_subdivision"])
        self.assertEqual(bound["source_version"], "1.5")
        # The upsert must be conflict-tolerant so re-runs refresh in place.
        self.assertIn(
            "ON CONFLICT",
            str(statement.compile(dialect=postgresql.dialect())).upper(),
        )

    def test_upsert_of_empty_list_is_a_no_op(self) -> None:
        """No statement is issued for an empty batch."""
        self.assertEqual(self.repository.upsert([]), 0)
        self.session.execute.assert_not_called()

    def test_upsert_rolls_back_on_error(self) -> None:
        """A failing execute rolls the transaction back and re-raises."""
        self.session.execute.side_effect = RuntimeError("boom")
        records = [
            GleifJurisdiction(
                jurisdiction_code="AT",
                jurisdiction_name="Austria",
                jurisdiction_type="COUNTRY_ONLY",
            )
        ]

        with self.assertRaises(RuntimeError):
            self.repository.upsert(records)

        self.session.rollback.assert_called_once()
        self.session.commit.assert_not_called()

    def test_prune_absent_with_empty_keep_list_never_truncates(self) -> None:
        """An empty keep-list is a no-op, not a delete-everything."""
        self.assertEqual(self.repository.prune_absent([]), 0)
        self.session.query.assert_not_called()
        self.session.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
