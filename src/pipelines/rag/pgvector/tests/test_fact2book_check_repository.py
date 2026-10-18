"""Unit tests for :class:`Fact2BookCheckRepository`.

Session and engine boundaries are faked so the tests assert SQL shape
(upsert on conflict, column mapping, read filters) without a live database.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/rag/pgvector/tests/test_fact2book_check_repository.py -v
"""

import unittest
import unittest.mock
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.rag.pgvector.fact2book_check_repository import (
    Fact2BookCheckRepository,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """Records executed statements; behaves as a context manager."""

    def __init__(self, records: list) -> None:
        self._records = records

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, statement, params=None) -> MagicMock:
        self._records.append((str(statement), params or {}))
        return MagicMock()

    def commit(self) -> None:
        self._records.append(("COMMIT", {}))


class _FakeConn:
    """Context-manager connection returning canned rows."""

    def __init__(self, records: list, rows: list) -> None:
        self._records = records
        self._rows = rows

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, statement, params=None) -> SimpleNamespace:
        self._records.append((str(statement), params or {}))
        return SimpleNamespace(fetchall=lambda: self._rows)


def _repo(records: list, rows: list | None = None) -> Fact2BookCheckRepository:
    """Build a repository with faked Session + engine.connect, bypassing the table probe."""
    with unittest.mock.patch.object(
        Fact2BookCheckRepository, "_probe_table", return_value=None
    ):
        repo = Fact2BookCheckRepository(engine=MagicMock())
    repo.Session = lambda: _FakeSession(records)
    repo.engine = SimpleNamespace(connect=lambda: _FakeConn(records, rows or []))
    return repo


# ---------------------------------------------------------------------------
# upsert_check
# ---------------------------------------------------------------------------


class TestUpsertCheck(unittest.TestCase):
    def test_issues_insert_on_conflict_update(self):
        records: list = []
        repo = _repo(records)
        repo.upsert_check(
            fact_id="f-1",
            celex_id="CELEX-31990D0645",
            book_chunk_id="bc-1",
            explanation="Book says fines were not imposed.",
            action="mismatch",
        )
        sqls = [sql for sql, _ in records]
        insert_sql = next(s for s in sqls if "INSERT INTO" in s)
        self.assertIn("ON CONFLICT (fact_id, celex_id) DO UPDATE", insert_sql)
        self.assertIn("action", insert_sql)
        self.assertIn("COMMIT", sqls)

    def test_conflict_update_refreshes_the_citation_anchor(self):
        """A re-run with a drifted anchor must overwrite, not insert a second row."""
        records: list = []
        repo = _repo(records)
        repo.upsert_check(
            fact_id="f-1b",
            celex_id="CELEX-31990D0645",
            book_chunk_id="bc-new",
            explanation="book confirms the fact",
            action="corroborated",
        )
        insert_sql = next(s for s, _ in records if "INSERT INTO" in s)
        self.assertIn("book_chunk_id  = EXCLUDED.book_chunk_id", insert_sql)

    def test_required_params_forwarded(self):
        records: list = []
        repo = _repo(records)
        repo.upsert_check(
            fact_id="f-2",
            celex_id="CELEX-31990D0645",
            book_chunk_id="bc-2",
            explanation="contradicts fine amount",
            action="mismatch",
            case_id="COMP/35.587",
            book_path="Infringements -> 2 Horizontal -> A",
            support_label="contradicts",
            final_score=0.85,
            reference_type="landmark",
        )
        _, params = next((s, p) for s, p in records if "INSERT INTO" in s)
        self.assertEqual(params["fact_id"], "f-2")
        self.assertEqual(params["celex_id"], "CELEX-31990D0645")
        self.assertEqual(params["book_chunk_id"], "bc-2")
        self.assertEqual(params["explanation"], "contradicts fine amount")
        self.assertEqual(params["action"], "mismatch")
        self.assertEqual(params["case_id"], "COMP/35.587")
        self.assertEqual(params["book_path"], "Infringements -> 2 Horizontal -> A")
        self.assertEqual(params["support_label"], "contradicts")
        self.assertAlmostEqual(params["final_score"], 0.85)
        self.assertEqual(params["reference_type"], "landmark")

    def test_corroborated_action_persisted(self):
        records: list = []
        repo = _repo(records)
        repo.upsert_check(
            fact_id="f-2b",
            celex_id="CELEX-31990D0645",
            book_chunk_id="bc-2b",
            explanation="book confirms the fact",
            action="corroborated",
        )
        _, params = next((s, p) for s, p in records if "INSERT INTO" in s)
        self.assertEqual(params["action"], "corroborated")

    def test_optional_params_default_to_none(self):
        records: list = []
        repo = _repo(records)
        repo.upsert_check(
            fact_id="f-3",
            celex_id="CELEX-31990D0645",
            book_chunk_id="bc-3",
            explanation="book contradicts",
            action="mismatch",
        )
        _, params = next((s, p) for s, p in records if "INSERT INTO" in s)
        self.assertEqual(params["action"], "mismatch")
        self.assertIsNone(params["case_id"])
        self.assertIsNone(params["book_path"])
        self.assertIsNone(params["support_label"])
        self.assertIsNone(params["final_score"])
        self.assertIsNone(params["reference_type"])

    def test_fact_id_cast_to_str(self):
        import uuid
        records: list = []
        repo = _repo(records)
        uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
        repo.upsert_check(
            fact_id=uid,
            celex_id="CELEX-31990D0645",
            book_chunk_id="bc-4",
            explanation="test",
            action="skipped",
        )
        _, params = next((s, p) for s, p in records if "INSERT INTO" in s)
        self.assertEqual(params["fact_id"], str(uid))


# ---------------------------------------------------------------------------
# select_checked_fact_ids
# ---------------------------------------------------------------------------


class TestSelectCheckedFactIds(unittest.TestCase):
    def test_filters_by_celex_id(self):
        records: list = []
        repo = _repo(records, rows=[("f-1",), ("f-2",)])
        repo.select_checked_fact_ids("CELEX-31990D0645")
        sql, params = next((s, p) for s, p in records if "SELECT DISTINCT" in s)
        self.assertIn("WHERE celex_id = :celex_id", sql)
        self.assertEqual(params["celex_id"], "CELEX-31990D0645")

    def test_returns_string_set(self):
        repo = _repo([], rows=[("f-1",), ("f-2",)])
        out = repo.select_checked_fact_ids("CELEX-31990D0645")
        self.assertEqual(out, {"f-1", "f-2"})

    def test_empty_rows_returns_empty_set(self):
        repo = _repo([], rows=[])
        out = repo.select_checked_fact_ids("CELEX-31990D0645")
        self.assertEqual(out, set())


# ---------------------------------------------------------------------------
# list_for_fact / list_for_celex
# ---------------------------------------------------------------------------


_ROW = SimpleNamespace(
    _mapping={
        "id": "row-1",
        "fact_id": "f-1",
        "celex_id": "CELEX-31990D0645",
        "case_id": "COMP/35.587",
        "book_chunk_id": "bc-1",
        "book_path": "Infringements -> 2 Horizontal",
        "explanation": "contradicts",
        "support_label": "contradicts",
        "final_score": 0.9,
        "action": "mismatch",
        "reference_type": "landmark",
        "created_at": None,
    }
)


class TestListForFact(unittest.TestCase):
    def test_filters_by_fact_id(self):
        records: list = []
        repo = _repo(records, rows=[_ROW])
        repo.list_for_fact("f-1")
        sql, params = next((s, p) for s, p in records if "SELECT" in s)
        self.assertIn("WHERE fact_id = :fact_id", sql)
        self.assertIn("action", sql)
        self.assertEqual(params["fact_id"], "f-1")

    def test_returns_dict_list(self):
        records: list = []
        repo = _repo(records, rows=[_ROW])
        out = repo.list_for_fact("f-1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["book_chunk_id"], "bc-1")
        self.assertEqual(out[0]["action"], "mismatch")

    def test_orders_by_final_score(self):
        records: list = []
        repo = _repo(records, rows=[])
        repo.list_for_fact("f-1")
        sql, _ = next((s, p) for s, p in records if "SELECT" in s)
        self.assertIn("ORDER BY final_score DESC NULLS LAST", sql)

    def test_empty_rows_returns_empty_list(self):
        repo = _repo([], rows=[])
        out = repo.list_for_fact("f-1")
        self.assertEqual(out, [])


class TestListForCelex(unittest.TestCase):
    def test_filters_by_celex_id(self):
        records: list = []
        repo = _repo(records, rows=[_ROW])
        repo.list_for_celex("CELEX-31990D0645")
        sql, params = next((s, p) for s, p in records if "SELECT" in s)
        self.assertIn("WHERE celex_id = :celex_id", sql)
        self.assertEqual(params["celex_id"], "CELEX-31990D0645")

    def test_returns_dict_list(self):
        records: list = []
        repo = _repo(records, rows=[_ROW])
        out = repo.list_for_celex("CELEX-31990D0645")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["fact_id"], "f-1")
        self.assertEqual(out[0]["action"], "mismatch")

    def test_orders_by_fact_id_then_score(self):
        records: list = []
        repo = _repo(records, rows=[])
        repo.list_for_celex("CELEX-31990D0645")
        sql, _ = next((s, p) for s, p in records if "SELECT" in s)
        self.assertIn("ORDER BY fact_id, final_score DESC NULLS LAST", sql)

    def test_empty_rows_returns_empty_list(self):
        repo = _repo([], rows=[])
        out = repo.list_for_celex("CELEX-31990D0645")
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Case-level run ledger
# ---------------------------------------------------------------------------


class TestCaseRunLedger(unittest.TestCase):
    """``upsert_case_run`` upserts per case; ``select_finished_case_keys`` reads a run."""

    def _kwargs(self):
        from datetime import datetime, timezone

        now = datetime(2026, 9, 17, tzinfo=timezone.utc)
        return dict(
            celex_id="CELEX-1",
            case_id="IV/1",
            run_id="r1",
            in_book=True,
            absence_reason="none",
            reference_type="landmark",
            corroborated=1,
            mismatch=2,
            skipped=3,
            already_checked=4,
            started_at=now,
            finished_at=now,
        )

    def test_upsert_on_case_key(self):
        records: list = []
        _repo(records).upsert_case_run(**self._kwargs())
        sql, params = records[0]
        self.assertIn("INSERT INTO celex_facts.fact2book_case_runs", sql)
        self.assertIn("ON CONFLICT (celex_id, case_id) DO UPDATE", sql)
        self.assertEqual(params["run_id"], "r1")
        self.assertEqual(params["already_checked"], 4)
        self.assertEqual(records[-1][0], "COMMIT")

    def test_finished_keys_filter_by_run_id(self):
        records: list = []
        repo = _repo(records, rows=[("CELEX-1", "IV/1"), ("CELEX-2", "IV/2")])
        keys = repo.select_finished_case_keys("r1")
        sql, params = records[0]
        self.assertIn("run_id = :run_id", sql)
        self.assertIn("finished_at IS NOT NULL", sql)
        self.assertEqual(params, {"run_id": "r1"})
        self.assertEqual(keys, {("CELEX-1", "IV/1"), ("CELEX-2", "IV/2")})

    def test_no_finished_keys_returns_empty_set(self):
        self.assertEqual(_repo([]).select_finished_case_keys("r1"), set())
