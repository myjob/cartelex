"""Unit tests for TranslationRepository with a faked engine boundary.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest pipelines.rag.pgvector.tests.test_translation_repository
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from pipelines.rag.pgvector import translation_repository as mod
from pipelines.rag.pgvector.translation_repository import (
    TranslationNotReady,
    TranslationRepository,
)
from pipelines.translate.models import (
    TranslationAuditRecord,
    TranslationItem,
    TranslationOutcome,
)

ITEM = TranslationItem(
    document_id="X_DE", decision_id="X", language="DE",
    local_file_md="/d/X/mineru/X.md", original_file_md=None, language_translated="NA")


class _Conn:
    def __init__(self, engine: "_Engine", tx: bool) -> None:
        self.engine = engine
        self.tx = tx

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, exc_type: object, *exc: object) -> bool:
        if self.tx:
            self.engine.outcomes.append("rollback" if exc_type else "commit")
        return False

    def execute(self, statement: object, params: dict | None = None) -> SimpleNamespace:
        self.engine.records.append((str(statement), params or {}, self.tx))
        return SimpleNamespace(
            fetchall=lambda: self.engine.rows, scalar=lambda: self.engine.scalar)


class _Engine:
    def __init__(self, rows: list | None = None, scalar: object = "t") -> None:
        self.records: list[tuple[str, dict, bool]] = []
        self.outcomes: list[str] = []
        self.rows = rows or []
        self.scalar = scalar

    def connect(self) -> _Conn:
        return _Conn(self, False)

    def begin(self) -> _Conn:
        return _Conn(self, True)


def _row(**kw: object) -> SimpleNamespace:
    return SimpleNamespace(_mapping=kw)


class QueueTests(unittest.TestCase):
    def test_lazy_queue_excludes_translated(self) -> None:
        engine = _Engine()
        TranslationRepository(engine).queue(None, force=False)
        sql, params, _ = engine.records[0]
        self.assertIn("r.is_source", sql)
        self.assertIn("r.local_file_md IS NOT NULL", sql)
        self.assertIn("coalesce(r.language, '') <> 'EN'", sql)
        self.assertIn("coalesce(d.language_translated, 'NA') <> 'en'", sql)
        self.assertEqual(params, {})

    def test_force_queue_includes_translated(self) -> None:
        engine = _Engine()
        TranslationRepository(engine).queue(["X"], force=True)
        sql, params, _ = engine.records[0]
        self.assertNotIn("language_translated, 'NA') <> 'en'", sql)
        self.assertIn("r.decision_id = ANY(:ids)", sql)
        self.assertEqual(params, {"ids": ["X"]})

    def test_force_uses_original_file_md(self) -> None:
        """A translated row exposes its original for re-translation."""
        engine = _Engine(rows=[_row(
            document_id="X_DE", decision_id="X", language="DE",
            local_file_md="/d/X/mineru/X_EN.md", original_file_md="/d/X/mineru/X.md",
            language_translated="en")])
        items = TranslationRepository(engine).queue(None, force=True)
        self.assertEqual(items[0].original_file_md, "/d/X/mineru/X.md")


class CommitTests(unittest.TestCase):
    def test_commit_runs_hook_inside_transaction(self) -> None:
        engine = _Engine()
        seen: list[int] = []
        TranslationRepository(engine).commit_translation(
            ITEM, "/d/X/mineru/X_EN.md", "/d/X/mineru/X.md",
            before_commit=lambda: seen.append(len(engine.records)))
        self.assertEqual(seen, [2])
        self.assertEqual(engine.outcomes, ["commit"])
        (reg_sql, reg_params, tx1), (dec_sql, dec_params, tx2) = engine.records
        self.assertTrue(tx1 and tx2)
        self.assertIn("local_file_md = :target", reg_sql)
        self.assertIn("coalesce(original_file_md, :original)", reg_sql)
        self.assertEqual(reg_params, {"target": "/d/X/mineru/X_EN.md",
                                      "original": "/d/X/mineru/X.md", "document_id": "X_DE"})
        self.assertIn("language_translated = 'en'", dec_sql)
        self.assertEqual(dec_params, {"decision_id": "X"})

    def test_commit_hook_error_propagates(self) -> None:
        engine = _Engine()

        def boom() -> None:
            raise OSError("disk full")

        with self.assertRaises(OSError):
            TranslationRepository(engine).commit_translation(
                ITEM, "/t.md", "/o.md", before_commit=boom)
        self.assertEqual(engine.outcomes, ["rollback"])


class WriteTests(unittest.TestCase):
    def test_set_language_guarded(self) -> None:
        engine = _Engine()
        TranslationRepository(engine).set_language("X_DE", "DE")
        sql, params, tx = engine.records[0]
        self.assertIn("language IS NULL OR language = 'NA'", sql)
        self.assertEqual(params, {"document_id": "X_DE", "language": "DE"})
        self.assertTrue(tx)

    def test_record_inserts_audit(self) -> None:
        engine = _Engine()
        run_id = uuid.uuid4()
        TranslationRepository(engine).record(TranslationAuditRecord(
            run_id=run_id, document_id="X_DE", outcome=TranslationOutcome.FAILED,
            reason="chunk_rejected"))
        sql, params, tx = engine.records[0]
        self.assertIn("document_translation_audit", sql)
        self.assertEqual(params["outcome"], "failed")
        self.assertEqual(params["run_id"], str(run_id))
        self.assertTrue(tx)


class ProbeTests(unittest.TestCase):
    def test_probe_raises_when_not_ready(self) -> None:
        with patch.object(mod, "column_exists", return_value=False):
            with self.assertRaises(TranslationNotReady):
                TranslationRepository(_Engine()).probe()
        with patch.object(mod, "column_exists", return_value=True):
            with self.assertRaises(TranslationNotReady):
                TranslationRepository(_Engine(scalar=None)).probe()

    def test_ready_passes(self) -> None:
        with patch.object(mod, "column_exists", return_value=True):
            TranslationRepository(_Engine()).probe()


if __name__ == "__main__":
    unittest.main()
