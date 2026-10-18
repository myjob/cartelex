"""Unit tests for :class:`FactEvidenceRepository`.

The session and engine boundaries are faked so the tests assert SQL branching
(chunk upsert vs. delete-then-insert summary row, status update, predecessor
join) without a live database.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/rag/pgvector/tests/test_fact_evidence_repository.py -v
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.rag.pgvector.fact_evidence_repository import FactEvidenceRepository


class _FakeSession:
    """Records executed statements; behaves as a context manager."""

    def __init__(self, records):
        self._records = records

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self._records.append((str(statement), params or {}))
        return MagicMock()

    def commit(self):
        self._records.append(("COMMIT", {}))


class _FakeConn:
    """Context-manager connection returning canned rows."""

    def __init__(self, records, rows):
        self._records = records
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self._records.append((str(statement), params or {}))
        return SimpleNamespace(fetchall=lambda: self._rows)


def _repo(records, rows=None):
    """Build a repository with faked Session + engine.connect."""
    repo = FactEvidenceRepository(engine=MagicMock())
    repo.Session = lambda: _FakeSession(records)
    repo.engine = SimpleNamespace(connect=lambda: _FakeConn(records, rows or []))
    return repo


class TestUpsertEvidence(unittest.TestCase):
    def test_chunk_row_replaces_fact_ledger(self):
        records: list = []
        repo = _repo(records)
        repo.upsert_evidence(
            fact_id="f-1",
            chunk_id="c-1",
            support_label="supports",
            final_score=0.9,
            sub_scores={"entity": 1.0, "semantic": 0.8},
            evidence_quote="quote",
            missing_elements=None,
        )
        statements = [sql for sql, _ in records]
        # Authoritative single-row semantics: delete the fact's rows, insert one.
        delete_idx = next(i for i, s in enumerate(statements) if "DELETE" in s)
        insert_idx = next(i for i, s in enumerate(statements) if "INSERT" in s)
        self.assertLess(delete_idx, insert_idx)
        # The DELETE clears ALL rows for the fact (not just one shape).
        del_sql, del_params = records[delete_idx]
        self.assertIn("WHERE fact_id = :fact_id", del_sql)
        self.assertNotIn("chunk_id", del_sql)
        self.assertEqual(del_params["fact_id"], "f-1")

    def test_summary_row_deletes_then_inserts(self):
        records: list = []
        repo = _repo(records)
        repo.upsert_evidence(
            fact_id="f-1",
            chunk_id=None,
            support_label="manual_approved",
            final_score=None,
        )
        statements = [sql for sql, _ in records]
        self.assertTrue(any("DELETE FROM celex_facts.fact_evidence" in s for s in statements))
        self.assertTrue(any("INSERT INTO celex_facts.fact_evidence" in s for s in statements))
        delete_idx = next(i for i, s in enumerate(statements) if "DELETE" in s)
        insert_idx = next(i for i, s in enumerate(statements) if "INSERT" in s)
        self.assertLess(delete_idx, insert_idx)

    def test_missing_elements_serialized_as_json(self):
        records: list = []
        repo = _repo(records)
        repo.upsert_evidence(
            fact_id="f-1",
            chunk_id="c-1",
            support_label="insufficient",
            final_score=0.1,
            missing_elements=["Acme", "leniency"],
        )
        _, params = next((s, p) for s, p in records if "INSERT" in s)
        self.assertEqual(params["missing_elements"], '["Acme", "leniency"]')


class TestStatusAndReads(unittest.TestCase):
    def test_set_verification_status_casts_enum(self):
        records: list = []
        repo = _repo(records)
        repo.set_verification_status("f-1", "verified_supported", 0.8)
        sql, params = next((s, p) for s, p in records if "UPDATE" in s)
        self.assertIn("verification_status_enum", sql)
        self.assertEqual(params["status"], "verified_supported")
        self.assertEqual(params["fact_id"], "f-1")

    def test_get_predecessor_facts_maps_rows(self):
        rows = [
            SimpleNamespace(fact_id="p-1", name="companies_investigated",
                            verification_status="verified_supported"),
        ]
        records: list = []
        repo = _repo(records, rows=rows)
        out = repo.get_predecessor_facts("f-1")
        self.assertEqual(out[0]["name"], "companies_investigated")
        self.assertEqual(out[0]["verification_status"], "verified_supported")
        sql = records[0][0]
        self.assertIn("sr.source_type = 'fact'", sql)


if __name__ == "__main__":
    unittest.main()
