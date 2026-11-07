"""Unit tests for ConversionRepository with a faked engine boundary.

Run with:
    .venv/bin/python -m unittest pipelines.rag.pgvector.tests.test_conversion_repository
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from pipelines.convert.models import (
    ConversionAuditRecord,
    ConversionOutcome,
    LanguageAction,
)
from pipelines.rag.pgvector import conversion_repository as mod
from pipelines.rag.pgvector.conversion_repository import (
    ConversionNotReady,
    ConversionRepository,
)


class _Conn:
    def __init__(self, engine: "_Engine", tx: bool) -> None:
        self.engine = engine
        self.tx = tx

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, statement: object, params: dict | None = None) -> SimpleNamespace:
        self.engine.records.append((str(statement), params or {}, self.tx))
        return SimpleNamespace(
            fetchall=lambda: self.engine.rows, scalar=lambda: self.engine.scalar)


class _Engine:
    def __init__(self, rows: list | None = None, scalar: object = "t") -> None:
        self.records: list[tuple[str, dict, bool]] = []
        self.rows = rows or []
        self.scalar = scalar

    def connect(self) -> _Conn:
        return _Conn(self, False)

    def begin(self) -> _Conn:
        return _Conn(self, True)


def _row(**kw: object) -> SimpleNamespace:
    return SimpleNamespace(_mapping=kw)


class QueueTests(unittest.TestCase):
    def test_scope_order_and_no_binds_by_default(self) -> None:
        engine = _Engine(rows=[_row(
            document_id="d1", decision_id="c1", is_source=True,
            local_file_pdf="/a.pdf", local_file_md=None, language=None)])
        items = ConversionRepository(engine).queue(None, None)
        sql, params, _ = engine.records[0]
        self.assertIn("(is_source OR is_selected) AND local_file_pdf IS NOT NULL", sql)
        self.assertIn("ORDER BY decision_id, document_id", sql)
        self.assertNotIn("local_file_md IS NOT NULL", sql)
        self.assertNotIn("LIMIT", sql)
        self.assertEqual(params, {})
        self.assertEqual(items[0].document_id, "d1")
        self.assertIsNone(items[0].local_file_md)

    def test_ids_and_limit_are_bound(self) -> None:
        engine = _Engine()
        ConversionRepository(engine).queue(["c1", "c2"], 5)
        sql, params, _ = engine.records[0]
        self.assertIn("decision_id = ANY(:ids)", sql)
        self.assertIn("LIMIT :limit", sql)
        self.assertEqual(params, {"ids": ["c1", "c2"], "limit": 5})


    def test_queue_selects_source_pdf(self) -> None:
        """Each row carries its decision's source PDF for the shared-PDF guard."""
        engine = _Engine(rows=[_row(
            document_id="d1_EN", decision_id="c1", is_source=False,
            local_file_pdf="/a.pdf", local_file_md="/m/a.md", language="EN",
            source_pdf="/a.pdf")])
        items = ConversionRepository(engine).queue(None, None)
        sql, _, _ = engine.records[0]
        self.assertIn("AS source_pdf", sql)
        self.assertIn("s.is_source", sql)
        self.assertEqual(items[0].source_pdf, "/a.pdf")

    def test_queue_flags_translated_source_rows(self) -> None:
        """Translated source rows carry their original and the translated flag."""
        engine = _Engine(rows=[_row(
            document_id="d1_DE", decision_id="c1", is_source=True,
            local_file_pdf="/a.pdf", local_file_md="/m/a_EN.md", language="DE",
            source_pdf="/a.pdf", original_file_md="/m/a.md", translated=True)])
        items = ConversionRepository(engine).queue(None, None)
        sql, _, _ = engine.records[0]
        self.assertIn("r.original_file_md", sql)
        self.assertIn("language_translated", sql)
        self.assertTrue(items[0].translated)
        self.assertEqual(items[0].original_file_md, "/m/a.md")


class WriteTests(unittest.TestCase):
    def test_set_language_guard_and_transaction(self) -> None:
        engine = _Engine()
        ConversionRepository(engine).set_language("d1", "EN")
        sql, params, tx = engine.records[0]
        self.assertIn("language IS NULL OR language = 'NA'", sql)
        self.assertEqual(params, {"document_id": "d1", "language": "EN"})
        self.assertTrue(tx)

    def test_record_binds_every_audit_field(self) -> None:
        engine = _Engine()
        audit = ConversionAuditRecord(
            run_id=uuid.uuid4(), outcome=ConversionOutcome.CONVERTED,
            decision_id="c1", document_id="d1", md_chars=10,
            language_confidence=0.9, language_action=LanguageAction.SET)
        ConversionRepository(engine).record(audit)
        sql, params, tx = engine.records[0]
        self.assertTrue(tx)
        self.assertEqual(set(params), set(ConversionAuditRecord.model_fields))
        for name in ConversionAuditRecord.model_fields:
            self.assertIn(f":{name}" if name != "run_id" else ":run_id", sql)
        self.assertEqual(params["outcome"], "converted")
        self.assertEqual(params["language_action"], "set")
        self.assertEqual(params["run_id"], str(audit.run_id))
        self.assertNotIn(":id", sql)
        self.assertNotIn("created_at", sql)


class ProbeTests(unittest.TestCase):
    def test_missing_column_raises(self) -> None:
        with patch.object(mod, "column_exists", return_value=False):
            with self.assertRaises(ConversionNotReady):
                ConversionRepository(_Engine()).probe()

    def test_missing_table_raises(self) -> None:
        with patch.object(mod, "column_exists", return_value=True):
            with self.assertRaises(ConversionNotReady):
                ConversionRepository(_Engine(scalar=None)).probe()

    def test_ready_passes(self) -> None:
        with patch.object(mod, "column_exists", return_value=True):
            ConversionRepository(_Engine()).probe()


if __name__ == "__main__":
    unittest.main()
