"""
Tests for CorpLEIIngestor.

Structure
---------
TestCorpLeiIngestorUnit
    Pure-unit tests – no DB, XML file or network required.
    All external dependencies are patched.

TestCorpLeiIngestorIntegration
    Integration test that exercises a real round-trip:
      ingest_xml() → INSERT → SELECT from celex_facts.corp_lei.
    Requires the celex PostgreSQL database to be reachable.
    A sentinel LEI is inserted and removed in tearDown() so the
    test is fully idempotent.
"""

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from pipelines.rag.pgvector.corp_lei_index_ingestor import (
    CorpLEIIngestor,
    elem_to_dict,
    strip_ns,
)
from pipelines.rag.pgvector.models import CorpLei
from pipelines.rag.pgvector.vector_config import DATABASE_URL

# ---------------------------------------------------------------------------
# Shared fixture XML
# ---------------------------------------------------------------------------

DUMMY_LEI = "TEST0000000DUMMY0001"

DUMMY_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<lei:LEIData xmlns:lei="http://www.gleif.org/data/schema/leidata/2016">
  <lei:LEIRecords>
    <lei:LEIRecord>
      <lei:LEI>{DUMMY_LEI}</lei:LEI>
      <lei:Entity>
        <lei:LegalName xml:lang="en">DUMMY CORPORATION INC</lei:LegalName>
        <lei:EntityCategory>GENERAL</lei:EntityCategory>
        <lei:LegalJurisdiction>US</lei:LegalJurisdiction>
        <lei:EntityStatus>ACTIVE</lei:EntityStatus>
        <lei:LegalAddress xml:lang="en">
          <lei:FirstAddressLine>123 Fake Street</lei:FirstAddressLine>
          <lei:AdditionalAddressLine>Suite 99</lei:AdditionalAddressLine>
          <lei:City>Springfield</lei:City>
          <lei:Region>US-IL</lei:Region>
          <lei:PostalCode>62701</lei:PostalCode>
          <lei:Country>US</lei:Country>
        </lei:LegalAddress>
        <lei:HeadquartersAddress xml:lang="en">
          <lei:FirstAddressLine>456 HQ Boulevard</lei:FirstAddressLine>
          <lei:City>Chicago</lei:City>
          <lei:Region>US-IL</lei:Region>
          <lei:PostalCode>60601</lei:PostalCode>
          <lei:Country>US</lei:Country>
        </lei:HeadquartersAddress>
      </lei:Entity>
      <lei:Registration>
        <lei:RegistrationStatus>ISSUED</lei:RegistrationStatus>
      </lei:Registration>
    </lei:LEIRecord>
  </lei:LEIRecords>
</lei:LEIData>
"""


# ---------------------------------------------------------------------------
# Unit tests (no DB / no filesystem side-effects beyond a temp file)
# ---------------------------------------------------------------------------

class TestCorpLeiIngestorUnit(unittest.TestCase):
    """Fast, fully mocked tests – safe to run offline."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_ingestor(self) -> CorpLEIIngestor:
        """Build a CorpLEIIngestor with all DB dependencies mocked out."""
        with patch("pipelines.rag.pgvector.corp_lei_index_ingestor.make_engine"), \
             patch("pipelines.rag.pgvector.corp_lei_index_ingestor.Base.metadata.create_all"):
            return CorpLEIIngestor("dummy://")

    # ------------------------------------------------------------------
    # strip_ns
    # ------------------------------------------------------------------

    def test_strip_ns_removes_namespace(self):
        self.assertEqual(
            strip_ns("{http://www.gleif.org/data/schema/leidata/2016}LEI"),
            "LEI",
        )

    def test_strip_ns_no_namespace_unchanged(self):
        self.assertEqual(strip_ns("LEI"), "LEI")

    # ------------------------------------------------------------------
    # elem_to_dict
    # ------------------------------------------------------------------

    def test_elem_to_dict_extracts_text_and_attributes(self):
        root = ET.fromstring(
            '<Entity><LegalName xml:lang="en">DUMMY INC</LegalName></Entity>'
        )
        d = elem_to_dict(root)
        self.assertEqual(d["LegalName"]["#text"], "DUMMY INC")
        self.assertEqual(d["LegalName"]["@lang"], "en")

    def test_elem_to_dict_leaf_returns_string(self):
        elem = ET.fromstring("<City>Springfield</City>")
        self.assertEqual(elem_to_dict(elem), "Springfield")

    # ------------------------------------------------------------------
    # _extract_address
    # ------------------------------------------------------------------

    def test_extract_address_nested_text(self):
        ingestor = self._make_ingestor()
        addr = {
            "FirstAddressLine": {"#text": "123 Fake Street"},
            "City": "Springfield",
            "Country": "US",
            "PostalCode": "62701",
        }
        result = ingestor._extract_address(addr)
        self.assertEqual(result["first_line"], "123 Fake Street")
        self.assertEqual(result["city"], "Springfield")
        self.assertEqual(result["country"], "US")
        self.assertEqual(result["postal_code"], "62701")

    def test_extract_address_flat_string_values(self):
        ingestor = self._make_ingestor()
        addr = {"City": "Berlin", "Country": "DE"}
        result = ingestor._extract_address(addr)
        self.assertEqual(result["city"], "Berlin")
        self.assertEqual(result["country"], "DE")

    def test_extract_address_empty_returns_empty(self):
        ingestor = self._make_ingestor()
        self.assertEqual(ingestor._extract_address({}), {})
        self.assertEqual(ingestor._extract_address(None), {})

    # ------------------------------------------------------------------
    # ingest_xml (mocked DB)
    # ------------------------------------------------------------------

    def test_ingest_xml_parses_and_calls_insert(self):
        with patch("pipelines.rag.pgvector.corp_lei_index_ingestor.make_engine"), \
             patch("pipelines.rag.pgvector.corp_lei_index_ingestor.sessionmaker") as mock_sm, \
             patch("pipelines.rag.pgvector.corp_lei_index_ingestor.Base.metadata.create_all"):

            mock_session = MagicMock()
            mock_sm.return_value = MagicMock(return_value=mock_session)
            mock_session.__enter__.return_value = mock_session

            ingestor = CorpLEIIngestor("dummy://")

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".xml", encoding="utf-8"
        ) as f:
            f.write(DUMMY_XML)
            tmp_path = f.name

        try:
            with patch(
                "pipelines.rag.pgvector.corp_lei_index_ingestor.insert"
            ) as mock_insert:
                mock_insert.return_value \
                    .values.return_value \
                    .on_conflict_do_nothing.return_value = "sentinel_stmt"

                ingestor.ingest_xml(tmp_path)

                # DB execute must have been called with our sentinel
                mock_session.execute.assert_called_with("sentinel_stmt")

                # Examine the batch that was passed to insert().values()
                batch = mock_insert.return_value.values.call_args[0][0]
                self.assertEqual(len(batch), 1)
                row = batch[0]

                self.assertEqual(row["lei"], DUMMY_LEI)
                self.assertEqual(row["legal_name"], "DUMMY CORPORATION INC")
                self.assertEqual(row["entity_category"], "GENERAL")
                self.assertEqual(row["entity_status"], "ACTIVE")
                self.assertEqual(row["country"], "US")
                self.assertEqual(row["legal_address_city"], "Springfield")
                self.assertEqual(row["legal_address_postal_code"], "62701")
                self.assertEqual(row["headquarters_address_city"], "Chicago")
                self.assertEqual(row["registration_status"], "ISSUED")
        finally:
            os.remove(tmp_path)


# ---------------------------------------------------------------------------
# Integration tests – live PostgreSQL round-trip
# ---------------------------------------------------------------------------

class TestCorpLeiIngestorIntegration(unittest.TestCase):
    """
    Requires a running PostgreSQL `celex` instance accessible via DATABASE_URL.
    The sentinel row (DUMMY_LEI) is cleaned up in tearDown() so the test
    is re-entrant.
    """

    @classmethod
    def setUpClass(cls):
        cls.ingestor = CorpLEIIngestor(DATABASE_URL)

    def tearDown(self):
        """Remove any sentinel rows left behind by the test."""
        with self.ingestor.Session() as session:
            session.execute(
                text(
                    "DELETE FROM celex_facts.corp_lei WHERE lei = :lei"
                ),
                {"lei": DUMMY_LEI},
            )
            session.commit()

    # ------------------------------------------------------------------
    # insert + select round-trip via ingest_xml
    # ------------------------------------------------------------------

    def test_insert_and_select_dummy_record(self):
        """Ingest a one-record XML and verify SELECT returns the expected data."""
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".xml", encoding="utf-8"
        ) as f:
            f.write(DUMMY_XML)
            tmp_path = f.name

        try:
            self.ingestor.ingest_xml(tmp_path)
        finally:
            os.remove(tmp_path)

        # SELECT the row we just inserted
        with self.ingestor.Session() as session:
            row: CorpLei = (
                session.query(CorpLei)
                .filter(CorpLei.lei == DUMMY_LEI)
                .one()
            )

        self.assertEqual(row.lei, DUMMY_LEI)
        self.assertEqual(row.legal_name, "DUMMY CORPORATION INC")
        self.assertEqual(row.entity_category, "GENERAL")
        self.assertEqual(row.entity_status, "ACTIVE")
        self.assertEqual(row.registration_status, "ISSUED")
        self.assertEqual(row.country, "US")

        # Legal address
        self.assertEqual(row.legal_address_first_line, "123 Fake Street")
        self.assertEqual(row.legal_address_additional_line, "Suite 99")
        self.assertEqual(row.legal_address_city, "Springfield")
        self.assertEqual(row.legal_address_region, "US-IL")
        self.assertEqual(row.legal_address_postal_code, "62701")

        # HQ address
        self.assertEqual(row.headquarters_address_first_line, "456 HQ Boulevard")
        self.assertEqual(row.headquarters_address_city, "Chicago")
        self.assertEqual(row.headquarters_address_region, "US-IL")
        self.assertEqual(row.headquarters_address_postal_code, "60601")

        # JSONB record is present and contains LEI key
        self.assertIsNotNone(row.record_json)
        self.assertIn("LEI", row.record_json)

    def test_on_conflict_do_nothing_is_idempotent(self):
        """Ingesting the same XML twice must not raise and must yield exactly 1 row."""
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".xml", encoding="utf-8"
        ) as f:
            f.write(DUMMY_XML)
            tmp_path = f.name

        try:
            self.ingestor.ingest_xml(tmp_path)
            # Second call — on_conflict_do_nothing must absorb the duplicate
            self.ingestor.ingest_xml(tmp_path)
        finally:
            os.remove(tmp_path)

        with self.ingestor.Session() as session:
            count = (
                session.query(CorpLei)
                .filter(CorpLei.lei == DUMMY_LEI)
                .count()
            )

        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
