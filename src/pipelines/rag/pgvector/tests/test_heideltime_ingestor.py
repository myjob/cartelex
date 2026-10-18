"""Tests for HeidelTimeIngestor.

Unit tests use an in-process SQLite database so no live PostgreSQL connection
is required.  The integration test class requires a running PostgreSQL instance
configured via DATABASE_URL.

Run with:
    .venv/bin/python -m unittest \
        src.pipelines.rag.pgvector.tests.test_heideltime_ingestor.TestHeidelTimeIngestorUnit \
        -v
"""

import unittest
import uuid
from unittest.mock import MagicMock, patch

from pipelines.rag.pgvector.models import HeidelTimeVO
from pipelines.rag.pgvector.event_repository import _index_date_sources
from pipelines.rag.pgvector.heideltime_ingestor import HeidelTimeIngestor

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------

_CELEX = "CELEX-31969D0477"
_CHUNK = "chunk-abc-001"


def _make_vo(
    text: str = "30 June 2013",
    tid: str = "t1",
    timex_type: str = "DATE",
    value: str = "2013-06-30",
    span: tuple = (117, 129),
) -> HeidelTimeVO:
    return HeidelTimeVO.from_dict(
        {
            "celex_id": _CELEX,
            "fk_chunk_id": _CHUNK,
            "text": text,
            "tid": tid,
            "type": timex_type,
            "value": value,
            "span": list(span),
        }
    )


# ---------------------------------------------------------------------------
# Unit tests — DB is fully mocked
# ---------------------------------------------------------------------------


class TestHeidelTimeIngestorUnit(unittest.TestCase):
    """Unit tests for HeidelTimeIngestor with a mock session factory."""

    def _make_ingestor(self) -> HeidelTimeIngestor:
        """Return an ingestor whose engine / Session are fully mocked."""
        with (
            patch("pipelines.rag.pgvector.heideltime_ingestor.make_engine"),
            patch(
                "pipelines.rag.pgvector.heideltime_ingestor.Base.metadata.create_all"
            ),
            patch("pipelines.rag.pgvector.heideltime_ingestor.EventRepository"),
        ):
            ingestor = HeidelTimeIngestor.__new__(HeidelTimeIngestor)

        # Replace the session factory with a context-manager-capable mock.
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.execute.return_value.all.return_value = []

        ingestor.Session = MagicMock(return_value=mock_session)
        ingestor._mock_session = mock_session

        # Mock the EventRepository delegate used by read-query methods.
        ingestor._event_repo = MagicMock()
        ingestor._event_repo.heideltime_fresh_chunk_ids.return_value = set()

        return ingestor

    # ------------------------------------------------------------------
    # ingest_vos — empty list
    # ------------------------------------------------------------------

    def test_ingest_vos_empty_returns_zero(self):
        """ingest_vos([]) must return 0 and never touch the session."""
        ingestor = self._make_ingestor()
        result = ingestor.ingest_vos([])
        self.assertEqual(result, 0)
        ingestor.Session.assert_not_called()

    # ------------------------------------------------------------------
    # ingest_vos — single VO
    # ------------------------------------------------------------------

    def test_ingest_vos_single_vo(self):
        """ingest_vos with one VO calls session.execute and commit once."""
        ingestor = self._make_ingestor()
        vo = _make_vo()
        result = ingestor.ingest_vos([vo])

        self.assertEqual(result, 1)
        ingestor._mock_session.execute.assert_called_once()
        ingestor._mock_session.commit.assert_called_once()

    def test_ingest_vos_maps_fields_correctly(self):
        """Params dict passed to session.execute must match the source VO fields."""
        import datetime

        ingestor = self._make_ingestor()
        vo = _make_vo(
            text="30 June 2013",
            tid="t3",
            timex_type="DATE",
            value="2013-06-30",
            span=(10, 22),
        )

        ingestor.ingest_vos([vo])

        # execute(stmt, records) — records is a list of dicts (executemany)
        call_args = ingestor._mock_session.execute.call_args
        records = call_args[0][1]  # positional arg 1 = the params list
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["p_celex_id"], _CELEX)
        self.assertEqual(rec["p_fk_chunk_id"], _CHUNK)
        self.assertEqual(rec["p_tid"], "t3")
        self.assertEqual(rec["p_timex_text"], "30 June 2013")
        self.assertEqual(rec["p_timex_type"], "DATE")
        # _parse_timex_date() returns datetime.date; SADate() bindparam handles the SQL cast.
        self.assertEqual(rec["p_timex_value"], datetime.date(2013, 6, 30))
        self.assertEqual(rec["p_span_start"], 10)
        self.assertEqual(rec["p_span_end"], 22)

    def test_ingest_vos_uuid_in_raw_jsonb(self):
        """raw_jsonb must contain the row UUID under the key 'id'."""
        ingestor = self._make_ingestor()
        vo = _make_vo()
        ingestor.ingest_vos([vo])

        rec = ingestor._mock_session.execute.call_args[0][1][0]
        self.assertIn("id", rec["p_raw_jsonb"])
        uuid.UUID(rec["p_raw_jsonb"]["id"])  # must be a valid UUID string

    def test_ingest_vos_uuid_matches_pk(self):
        """The UUID in raw_jsonb['id'] must match the row's p_id."""
        ingestor = self._make_ingestor()
        vo = _make_vo()
        ingestor.ingest_vos([vo])

        rec = ingestor._mock_session.execute.call_args[0][1][0]
        self.assertEqual(str(rec["p_id"]), rec["p_raw_jsonb"]["id"])

    # ------------------------------------------------------------------
    # ingest_vos — multiple VOs
    # ------------------------------------------------------------------

    def test_ingest_vos_multiple_returns_count(self):
        """ingest_vos returns the number of rows passed."""
        ingestor = self._make_ingestor()
        vos = [
            _make_vo(tid="t1", span=(0, 12)),
            _make_vo(tid="t2", span=(20, 32)),
            _make_vo(tid="t3", span=(50, 62)),
        ]
        result = ingestor.ingest_vos(vos)
        self.assertEqual(result, 3)
        records = ingestor._mock_session.execute.call_args[0][1]
        self.assertEqual(len(records), 3)

    # ------------------------------------------------------------------
    # source_hash
    # ------------------------------------------------------------------

    def test_ingest_vos_persists_source_hash(self):
        """The supplied source_hash is bound onto every record of the batch."""
        ingestor = self._make_ingestor()
        digest = "0" * 32
        ingestor.ingest_vos(
            [_make_vo(tid="t1"), _make_vo(tid="t2")], source_hash=digest
        )

        records = ingestor._mock_session.execute.call_args[0][1]
        self.assertEqual([r["p_source_hash"] for r in records], [digest, digest])

    def test_ingest_vos_source_hash_defaults_to_none(self):
        """Omitting source_hash writes NULL, which fresh_chunk_ids reads as stale."""
        ingestor = self._make_ingestor()
        ingestor.ingest_vos([_make_vo()])

        records = ingestor._mock_session.execute.call_args[0][1]
        self.assertIsNone(records[0]["p_source_hash"])

    # ------------------------------------------------------------------
    # fresh_chunk_ids / delete_chunks
    # ------------------------------------------------------------------

    def test_fresh_chunk_ids_returns_empty_set_when_no_rows(self):
        """fresh_chunk_ids returns an empty set when nothing matches."""
        ingestor = self._make_ingestor()
        # _event_repo mock already returns empty set by default
        result = ingestor.fresh_chunk_ids(_CELEX)
        self.assertIsInstance(result, set)
        self.assertEqual(len(result), 0)

    def test_fresh_chunk_ids_returns_chunk_ids(self):
        """fresh_chunk_ids passes the repository result straight through."""
        ingestor = self._make_ingestor()
        ingestor._event_repo.heideltime_fresh_chunk_ids.return_value = {
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        }
        result = ingestor.fresh_chunk_ids(_CELEX)
        self.assertIn("00000000-0000-0000-0000-000000000001", result)
        self.assertIn("00000000-0000-0000-0000-000000000002", result)
        self.assertEqual(len(result), 2)

    def test_delete_chunks_delegates_to_repository(self):
        """delete_chunks forwards the ids and returns the repository's row count."""
        ingestor = self._make_ingestor()
        ingestor._event_repo.heideltime_delete_chunks.return_value = 7

        deleted = ingestor.delete_chunks({"chunk-a", "chunk-b"})

        self.assertEqual(deleted, 7)
        ingestor._event_repo.heideltime_delete_chunks.assert_called_once_with(
            {"chunk-a", "chunk-b"}
        )


class TestHeidelTimeInvalidationStatements(unittest.TestCase):
    """The delete order that keeps NO ACTION foreign keys from raising 23503."""

    def test_statements_are_ordered_children_before_parents(self):
        """Link rows precede registry rows, which precede the extractions."""
        from pipelines.rag.pgvector.event_repository import (
            heideltime_invalidation_statements,
        )

        targets = [
            str(stmt).split("DELETE FROM ")[1].split()[0]
            for stmt in heideltime_invalidation_statements()
        ]
        self.assertEqual(
            targets,
            [
                "celex_facts.fact_source_link",
                "celex_facts.knowledge_edges",
                "celex_facts.fact_source_registry",
                "celex_facts.heideltime_extractions",
            ],
        )

    def test_registry_delete_is_scoped_to_heideltime_sources(self):
        """Chunk-typed registry rows are shared with other facts and must survive."""
        from pipelines.rag.pgvector.event_repository import (
            heideltime_invalidation_statements,
        )

        registry_delete = str(heideltime_invalidation_statements()[2])
        self.assertIn("heideltime_event_id IN", registry_delete)
        self.assertNotIn("chunk_id =", registry_delete)


# ---------------------------------------------------------------------------
# Integration tests — live PostgreSQL required
# ---------------------------------------------------------------------------


class TestHeidelTimeIngestorIntegration(unittest.TestCase):
    """Integration tests that write to and read from a real PostgreSQL instance.

    A randomised ``celex_id`` is used as a test-isolation fence so that every
    run is independent and the tearDown cleanup is surgical (no accidental
    deletion of production rows).

    Prerequisites:
        - DATABASE_URL env var (or config.yaml) points to a reachable PG.
        - ``celex_facts.heideltime_extractions`` table exists
          (auto-created by HeidelTimeIngestor.__init__).
        - A row in the configured EMBEDDING_TABLE is inserted in setUp and
          removed in tearDown, mirroring the pattern in test_event_ingestion.py.

    Run with::

        .venv/bin/python -m unittest \\
            src.pipelines.rag.pgvector.tests.test_heideltime_ingestor.TestHeidelTimeIngestorIntegration \\
            -v
    """

    # ------------------------------------------------------------------
    # setUp / tearDown
    # ------------------------------------------------------------------

    def setUp(self) -> None:
        from sqlalchemy import text as sa_text
        from config.settings import settings as _settings

        self._settings = _settings

        # Unique sentinel IDs — no collision with production data.
        self.celex_id = f"TEST-HT-{uuid.uuid4().hex[:12].upper()}"
        self.chunk_id = str(uuid.uuid4())

        self.ingestor = HeidelTimeIngestor()

        # Insert a minimal row into the embedding table so FK constraints
        # (if any) are satisfied, matching the pattern in test_event_ingestion.
        with self.ingestor.Session() as session:
            session.execute(
                sa_text(
                    f"INSERT INTO celex_facts.{_settings.EMBEDDING_TABLE}"
                    " (id, celex_id) VALUES (:id, :celex_id)"
                ),
                {"id": self.chunk_id, "celex_id": self.celex_id},
            )
            session.commit()

    def tearDown(self) -> None:
        from sqlalchemy import text as sa_text
        from pipelines.rag.pgvector.models import CelexHeidelTimeExtraction

        with self.ingestor.Session() as session:
            # Remove HeidelTime rows first.
            session.query(CelexHeidelTimeExtraction).filter(
                CelexHeidelTimeExtraction.celex_id == self.celex_id
            ).delete(synchronize_session=False)

            # Remove the sentinel embedding row.
            session.execute(
                sa_text(
                    f"DELETE FROM celex_facts.{self._settings.EMBEDDING_TABLE}"
                    " WHERE id = :id"
                ),
                {"id": self.chunk_id},
            )
            session.commit()

        # Explicitly close all pooled connections so psycopg3 does not emit
        # ResourceWarning about connections being garbage-collected while open.
        self.ingestor.engine.dispose()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_vo(
        self,
        tid: str = "t1",
        text: str = "30 June 2013",
        timex_type: str = "DATE",
        value: str = "2013-06-30",
        span: tuple = (0, 12),
    ) -> HeidelTimeVO:
        return HeidelTimeVO.from_dict(
            {
                "celex_id": self.celex_id,
                "fk_chunk_id": self.chunk_id,
                "text": text,
                "tid": tid,
                "type": timex_type,
                "value": value,
                "span": list(span),
            }
        )

    def _query_rows(self):
        from pipelines.rag.pgvector.models import CelexHeidelTimeExtraction

        with self.ingestor.Session() as session:
            return (
                session.query(CelexHeidelTimeExtraction)
                .filter(CelexHeidelTimeExtraction.celex_id == self.celex_id)
                .all()
            )

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_ingest_single_vo_persists_row(self):
        """A single HeidelTimeVO is persisted and all columns round-trip correctly."""
        import datetime

        vo = self._make_vo(
            tid="t1", text="30 June 2013", value="2013-06-30", span=(5, 17)
        )

        count = self.ingestor.ingest_vos([vo])
        self.assertEqual(count, 1)

        rows = self._query_rows()
        self.assertEqual(len(rows), 1)

        row = rows[0]
        self.assertEqual(row.celex_id, self.celex_id)
        self.assertEqual(row.fk_chunk_id, self.chunk_id)
        self.assertEqual(row.tid, "t1")
        self.assertEqual(row.timex_text, "30 June 2013")
        self.assertEqual(row.timex_type, "DATE")
        # timex_value is now a DATE column — ORM returns a datetime.date
        self.assertEqual(row.timex_value, datetime.date(2013, 6, 30))
        self.assertEqual(row.span_start, 5)
        self.assertEqual(row.span_end, 17)
        # created_at must be populated by the DB default
        self.assertIsNotNone(row.created_at)

    def test_ingest_multiple_vos_persists_all_rows(self):
        """Three VOs produce three rows with distinct TIDs."""
        vos = [
            self._make_vo(
                tid="t1", text="30 June 2013", value="2013-06-30", span=(0, 12)
            ),
            self._make_vo(
                tid="t2", text="22 March 2006", value="2006-03-22", span=(20, 33)
            ),
            self._make_vo(
                tid="t3", text="1 January 2000", value="2000-01-01", span=(50, 64)
            ),
        ]
        count = self.ingestor.ingest_vos(vos)
        self.assertEqual(count, 3)

        rows = self._query_rows()
        self.assertEqual(len(rows), 3)
        tids = {r.tid for r in rows}
        self.assertEqual(tids, {"t1", "t2", "t3"})

    def test_ingest_uuid_in_raw_jsonb_matches_pk(self):
        """raw_jsonb['id'] round-trips to the server-assigned UUID primary key."""
        vo = self._make_vo(tid="t1")
        self.ingestor.ingest_vos([vo])

        row = self._query_rows()[0]
        self.assertIn("id", row.raw_jsonb)
        self.assertEqual(str(row.id), row.raw_jsonb["id"])

    def test_raw_jsonb_contains_all_vo_fields(self):
        """raw_jsonb preserves the original VO payload verbatim."""
        vo = self._make_vo(
            tid="t99", text="15 August 1998", value="1998-08-15", span=(7, 21)
        )
        self.ingestor.ingest_vos([vo])

        row = self._query_rows()[0]
        j = row.raw_jsonb
        self.assertEqual(j["tid"], "t99")
        self.assertEqual(j["text"], "15 August 1998")
        self.assertEqual(j["value"], "1998-08-15")
        self.assertEqual(j["span"], [7, 21])

    def _set_chunk_document(self, document: str) -> str:
        """Set the test chunk's text and return its md5, as the module computes it."""
        import hashlib

        from sqlalchemy import text as sa_text

        with self.ingestor.Session() as session:
            session.execute(
                sa_text(
                    f"UPDATE celex_facts.{self._settings.EMBEDDING_TABLE}"
                    " SET document = :doc WHERE id = :id"
                ),
                {"doc": document, "id": self.chunk_id},
            )
            session.commit()
        return hashlib.md5(document.encode("utf-8")).hexdigest()

    def test_fresh_chunk_ids_requires_a_matching_hash(self):
        """A chunk is fresh only while its rows carry md5() of the current text."""
        digest = self._set_chunk_document("Decided on 7 April 1965.")

        self.assertNotIn(
            self.chunk_id, self.ingestor.fresh_chunk_ids(self.celex_id)
        )

        self.ingestor.ingest_vos([self._make_vo()], source_hash=digest)
        self.assertIn(self.chunk_id, self.ingestor.fresh_chunk_ids(self.celex_id))

    def test_null_source_hash_counts_as_stale(self):
        """Rows written before the column existed re-extract instead of being skipped."""
        self._set_chunk_document("Decided on 7 April 1965.")

        self.ingestor.ingest_vos([self._make_vo()])

        self.assertNotIn(
            self.chunk_id, self.ingestor.fresh_chunk_ids(self.celex_id)
        )

    def test_chunk_goes_stale_when_its_text_is_repaired(self):
        """The regression this whole change exists for: text edit invalidates rows."""
        digest = self._set_chunk_document("Decided on 7April1965.")
        self.ingestor.ingest_vos([self._make_vo()], source_hash=digest)
        self.assertIn(self.chunk_id, self.ingestor.fresh_chunk_ids(self.celex_id))

        # scan_dates.py rewriting the chunk in place, exactly as on 2026-04-29.
        self._set_chunk_document("Decided on 7 April 1965.")

        self.assertNotIn(
            self.chunk_id, self.ingestor.fresh_chunk_ids(self.celex_id)
        )

    def test_partially_hashed_chunk_is_stale_as_a_whole(self):
        """One unhashed row poisons the chunk — bool_and, not bool_or."""
        digest = self._set_chunk_document("Decided on 7 April 1965.")
        self.ingestor.ingest_vos([self._make_vo(tid="t1")], source_hash=digest)
        self.ingestor.ingest_vos([self._make_vo(tid="t2")])

        self.assertNotIn(
            self.chunk_id, self.ingestor.fresh_chunk_ids(self.celex_id)
        )

    def test_delete_chunks_removes_the_extractions(self):
        """delete_chunks clears the stale rows so a re-run is not blocked."""
        digest = self._set_chunk_document("Decided on 7 April 1965.")
        self.ingestor.ingest_vos(
            [self._make_vo(tid="t1"), self._make_vo(tid="t2")], source_hash=digest
        )
        self.assertEqual(len(self._query_rows()), 2)

        deleted = self.ingestor.delete_chunks([self.chunk_id])

        self.assertEqual(deleted, 2)
        self.assertEqual(self._query_rows(), [])

    def test_delete_chunks_with_no_ids_is_a_noop(self):
        """An empty id list must not issue a DELETE with an empty IN list."""
        self.assertEqual(self.ingestor.delete_chunks([]), 0)

    def test_duplicate_ingest_appends_rows(self):
        """Calling ingest_vos twice with the same VOs should dedup (did not in former version)."""
        vo = self._make_vo(tid="t1")
        self.ingestor.ingest_vos([vo])
        self.ingestor.ingest_vos([vo])

        rows = self._query_rows()
        # Two rows — the ingestor itself does  deduplicate;
        self.assertEqual(len(rows), 1)

    def test_ingest_empty_list_leaves_table_unchanged(self):
        """ingest_vos([]) inserts nothing."""
        self.ingestor.ingest_vos([])
        self.assertEqual(len(self._query_rows()), 0)

    def test_ingest_duration_timex_type(self):
        """DURATION values (e.g. P3Y) cannot be stored as a DATE and become NULL."""
        vo = self._make_vo(
            tid="t1",
            text="three years",
            timex_type="DURATION",
            value="P3Y",
            span=(0, 11),
        )
        self.ingestor.ingest_vos([vo])

        row = self._query_rows()[0]
        self.assertEqual(row.timex_type, "DURATION")
        # P3Y is not an ISO date — stored as NULL in the Date column.
        self.assertIsNone(row.timex_value)
        # The raw string is still preserved in raw_jsonb for round-trip access.
        self.assertEqual(row.raw_jsonb["value"], "P3Y")

    def test_ingest_partial_date_becomes_null(self):
        """Year-only values (e.g. '1998') cannot be stored as a DATE and become NULL."""
        vo = self._make_vo(
            tid="t1", text="1998", timex_type="DATE", value="1998", span=(0, 4)
        )
        self.ingestor.ingest_vos([vo])

        row = self._query_rows()[0]
        self.assertIsNone(row.timex_value)
        self.assertEqual(row.raw_jsonb["value"], "1998")

    def test_created_at_is_populated_by_server(self):
        """created_at must be set by the PostgreSQL DEFAULT (not NULL)."""
        import datetime

        self.ingestor.ingest_vos([self._make_vo()])
        row = self._query_rows()[0]
        self.assertIsNotNone(row.created_at)
        self.assertIsInstance(row.created_at, datetime.datetime)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Unit tests — EventRepository._index_date_sources (pure, no DB)
# ---------------------------------------------------------------------------


class TestIndexDateSources(unittest.TestCase):
    """Unit tests for the grouping of aggregated heideltime_extractions rows."""

    def test_groups_entries_by_canonical_date(self) -> None:
        """Each canonical date maps to the extraction rows that produced it."""
        entries = [
            {"id": "id-a", "value": "2005-06-08", "timex_text": "8 June 2005"},
            {"id": "id-b", "value": "2005-07-04", "timex_text": "4 July 2005"},
        ]
        grouped = _index_date_sources(entries)

        self.assertEqual(list(grouped), ["2005-06-08", "2005-07-04"])
        self.assertEqual(
            grouped["2005-06-08"], [{"id": "id-a", "timex_text": "8 June 2005"}]
        )

    def test_same_date_twice_in_a_chunk_keeps_both_extractions(self) -> None:
        """A date mentioned twice yields one key with both extraction rows."""
        entries = [
            {"id": "id-a", "value": "2005-06-08", "timex_text": "8 June 2005"},
            {"id": "id-b", "value": "2005-06-08", "timex_text": "8.6.2005"},
        ]
        grouped = _index_date_sources(entries)

        self.assertEqual(list(grouped), ["2005-06-08"])
        self.assertEqual(
            [src["id"] for src in grouped["2005-06-08"]], ["id-a", "id-b"]
        )

    def test_keys_are_deduplicated_dates_in_aggregation_order(self) -> None:
        """The mapping keys double as the deduplicated canonical_dates list."""
        entries = [
            {"id": "id-a", "value": "2005-06-08", "timex_text": "8 June 2005"},
            {"id": "id-b", "value": "2005-06-08", "timex_text": "8.6.2005"},
            {"id": "id-c", "value": "2006-01-01", "timex_text": "1 January 2006"},
        ]
        self.assertEqual(
            list(_index_date_sources(entries)), ["2005-06-08", "2006-01-01"]
        )

    def test_uuid_ids_are_stringified(self) -> None:
        """UUID ids coming back from jsonb are normalised to strings."""
        extraction_id = uuid.uuid4()
        entries = [
            {"id": extraction_id, "value": "2005-06-08", "timex_text": "8 June 2005"}
        ]
        grouped = _index_date_sources(entries)

        self.assertEqual(grouped["2005-06-08"][0]["id"], str(extraction_id))

    def test_entries_without_a_value_are_skipped(self) -> None:
        """Rows with a null canonical date contribute no group."""
        entries = [
            {"id": "id-a", "value": None, "timex_text": "sometime"},
            {"id": "id-b", "value": "2005-06-08", "timex_text": "8 June 2005"},
        ]
        self.assertEqual(list(_index_date_sources(entries)), ["2005-06-08"])

    def test_missing_timex_text_becomes_empty_string(self) -> None:
        """A null timex_text is normalised so callers never see None."""
        entries = [{"id": "id-a", "value": "2005-06-08", "timex_text": None}]
        grouped = _index_date_sources(entries)
        self.assertEqual(grouped["2005-06-08"][0]["timex_text"], "")

    def test_empty_input_returns_empty_mapping(self) -> None:
        """None and [] both produce an empty mapping."""
        self.assertEqual(_index_date_sources(None), {})
        self.assertEqual(_index_date_sources([]), {})
