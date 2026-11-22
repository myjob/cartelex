"""Unit tests for :class:`HumanIndexReconRepository`.

Session and engine boundaries are faked so the tests assert SQL shape (upsert
key, verdict retention, bind parameters) and result mapping without a live
database.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/rag/pgvector/tests/test_human_index_recon_repository.py -v
"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.legal.human_index_recon import (
    ReconResult,
    ReconStatus,
    ReconTarget,
    SizeBand,
)
from pipelines.rag.pgvector.human_index_recon_repository import (
    HumanIndexReconRepository,
    ReconRecord,
)


class _FakeSession:
    """Records executed statements; behaves as a context manager."""

    def __init__(self, records: list) -> None:
        self._records = records

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, statement, params=None) -> MagicMock:
        self._records.append((str(statement), params))
        return MagicMock()

    def commit(self) -> None:
        self._records.append(("COMMIT", None))


class _FakeConn:
    """Context-manager connection whose execute yields canned rows."""

    def __init__(self, records: list, rows: list, error: Exception | None) -> None:
        self._records = records
        self._rows = rows
        self._error = error

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, statement, params=None) -> list:
        self._records.append((str(statement), params))
        if self._error is not None:
            raise self._error
        return self._rows


def _repo(
    records: list, rows: list | None = None, error: Exception | None = None
) -> HumanIndexReconRepository:
    """Build a repository whose Session and engine.connect are faked."""
    repo = HumanIndexReconRepository(engine=MagicMock())
    repo.Session = lambda: _FakeSession(records)
    repo.engine = SimpleNamespace(connect=lambda: _FakeConn(records, rows or [], error))
    return repo


def _record(celex_id: str = "39125_2726_3") -> ReconRecord:
    """Build a firms record with a non-ASCII detail value."""
    return ReconRecord(
        result=ReconResult(
            celex_id=celex_id,
            target=ReconTarget.FIRMS,
            sheet_column="I firms",
            fact_names="companies_investigated",
            human_raw="Saint-Gobain (France)",
            human_norm="Saint-Gobain",
            extracted_raw="La Compagnie de Saint-Gobain SA; Soliver NV",
            status=ReconStatus.EXTRACTION_SUPERSET,
            detail={"extra": ["Société Soliver"]},
        ),
        title="Carglass",
        decision_year=2008,
        content_tokens=150_000,
        size_band=SizeBand.LARGE,
        source_file="index.xlsx",
        source_row=7,
    )


class TestConstruction(unittest.TestCase):
    def test_rejects_an_embedding_table_that_is_not_an_identifier(self) -> None:
        with self.assertRaises(ValueError):
            HumanIndexReconRepository(engine=MagicMock(), embedding_table="t; DROP TABLE x")


class TestUpsertRecords(unittest.TestCase):
    def test_upserts_on_the_decision_target_key_and_commits(self) -> None:
        records: list = []
        written = _repo(records).upsert_records([_record(), _record("CELEX-32003D0002")])
        sql, params = records[0]
        self.assertEqual(written, 2)
        self.assertIn("INSERT INTO celex_facts.human_index_recon", sql)
        self.assertIn("ON CONFLICT (celex_id, target) DO UPDATE", sql)
        self.assertEqual([p["celex_id"] for p in params], ["39125_2726_3", "CELEX-32003D0002"])
        self.assertEqual(records[-1][0], "COMMIT")

    def test_verdict_is_kept_only_while_both_values_are_unchanged(self) -> None:
        records: list = []
        _repo(records).upsert_records([_record()])
        sql = records[0][0]
        self.assertNotIn("verdict = EXCLUDED", sql)
        self.assertIn("human_raw IS NOT DISTINCT FROM EXCLUDED.human_raw", sql)
        self.assertIn("extracted_raw IS NOT DISTINCT FROM EXCLUDED.extracted_raw", sql)
        # The comparison must read the stored values, so they are assigned last.
        self.assertLess(sql.index("verdict_note = CASE"), sql.index("human_raw      = EXCLUDED"))

    def test_params_carry_enum_values_and_json_detail(self) -> None:
        params = _record().as_params()
        self.assertEqual(params["target"], "firms")
        self.assertEqual(params["status"], "extraction_superset")
        self.assertEqual(params["size_band"], "L")
        self.assertEqual(json.loads(params["detail"]), {"extra": ["Société Soliver"]})
        self.assertIn("Société", params["detail"])

    def test_no_records_issues_no_statement(self) -> None:
        records: list = []
        self.assertEqual(_repo(records).upsert_records([]), 0)
        self.assertEqual(records, [])


class TestReads(unittest.TestCase):
    def test_fact_values_are_grouped_by_decision(self) -> None:
        records: list = []
        rows = [
            ("C-1", "case_commissioner", "Mario Monti", ""),
            ("C-1", "companies_investigated", "| x |", '[{"companies": "X SA"}]'),
            ("C-2", "case_commissioner", "NA", None),
        ]
        values = _repo(records, rows).select_fact_values(["case_commissioner"])
        self.assertEqual(values["C-1"]["case_commissioner"], ("Mario Monti", ""))
        self.assertEqual(values["C-1"]["companies_investigated"][1], '[{"companies": "X SA"}]')
        self.assertEqual(set(values), {"C-1", "C-2"})
        self.assertEqual(records[0][1], {"names": ["case_commissioner"]})

    def test_no_fact_names_reads_nothing(self) -> None:
        records: list = []
        self.assertEqual(_repo(records).select_fact_values([]), {})
        self.assertEqual(records, [])

    def test_content_tokens_are_summed_from_the_configured_chunk_table(self) -> None:
        records: list = []
        repo = HumanIndexReconRepository(engine=MagicMock(), embedding_table="my_chunks")
        repo.engine = SimpleNamespace(connect=lambda: _FakeConn(records, [("C-1", 4321)], None))
        self.assertEqual(repo.select_content_tokens(), {"C-1": 4321})
        self.assertIn("FROM celex_facts.my_chunks GROUP BY celex_id", records[0][0])

    def test_status_counts(self) -> None:
        rows = [("firms", "agree", 3), ("firms", "conflict", 1)]
        self.assertEqual(_repo([], rows).select_status_counts(), rows)


class TestProbeTable(unittest.TestCase):
    def test_missing_table_names_the_migration(self) -> None:
        repo = _repo([], error=RuntimeError("relation does not exist"))
        with self.assertRaises(RuntimeError) as raised:
            repo.probe_table()
        self.assertIn("migrate_human_index_recon.sql", str(raised.exception))

    def test_existing_table_passes(self) -> None:
        _repo([]).probe_table()


if __name__ == "__main__":
    unittest.main()
