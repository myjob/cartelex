"""Tests for the Main_backfill_markdown_paths CLI (fake engine, no DB)."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from batch import Main_backfill_markdown_paths as cli
from pipelines.download.descriptor_store import MasterDescriptorStore


class FakeResult:
    """Minimal SQLAlchemy result stand-in."""

    def __init__(self, rows: list[tuple]) -> None:
        """Create the result.

        Args:
            rows: Result rows as tuples.
        """
        self._rows = rows

    def first(self) -> tuple | None:
        """Return the first row or None.

        Returns:
            The first row.
        """
        return self._rows[0] if self._rows else None

    def all(self) -> list[tuple]:
        """Return all rows.

        Returns:
            The rows.
        """
        return list(self._rows)


class FakeDb:
    """Fake engine recording statements."""

    def __init__(self, rows: list[tuple], has_column: bool = True) -> None:
        """Create the fake.

        Args:
            rows: Rows returned by the pending select.
            has_column: Whether ``local_file_md`` exists.
        """
        self.rows = rows
        self.has_column = has_column
        self.statements: list[tuple[str, Any]] = []
        self.disposed = False

    @contextmanager
    def connect(self):
        """Yield a connection-like object.

        Yields:
            This fake.
        """
        yield self

    begin = connect

    def dispose(self) -> None:
        """Record disposal."""
        self.disposed = True

    def execute(self, stmt: Any, params: Any = None) -> FakeResult:
        """Simulate one statement.

        Args:
            stmt: SQLAlchemy text clause.
            params: Bound parameters.

        Returns:
            A fake result.
        """
        sql = " ".join(str(stmt).split())
        self.statements.append((sql, params))
        if "pg_attribute" in sql:
            return FakeResult([(1,)] if self.has_column else [])
        if sql.startswith("SELECT document_id"):
            return FakeResult(self.rows)
        return FakeResult([])

    def updates(self) -> list[tuple[str, Any]]:
        """Return logged UPDATE statements.

        Returns:
            (sql, params) pairs.
        """
        return [s for s in self.statements if s[0].startswith("UPDATE")]


def _store(tmp: str, entries: dict[str, dict]) -> MasterDescriptorStore:
    """Build a master store from a temp JSON file.

    Args:
        tmp: Temp directory.
        entries: Master documents by key.

    Returns:
        The loaded store.
    """
    path = Path(tmp) / "master.json"
    path.write_text(json.dumps({"documents": entries}), encoding="utf-8")
    return MasterDescriptorStore(path)


def _row(pdf: str = "/d/K/AT_1.pdf", is_source: bool = True) -> cli.RegistryPdfRow:
    """Build a registry row.

    Args:
        pdf: PDF path.
        is_source: Source flag.

    Returns:
        The row.
    """
    return cli.RegistryPdfRow(document_id="id1", decision_id="K",
                              is_source=is_source, local_file_pdf=pdf)


class PlanTest(unittest.TestCase):
    """plan_markdown_paths decisions."""

    def setUp(self) -> None:
        """Create a temp dir."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_source_row_copies_master_txt_md_file(self) -> None:
        """A matching master entry supplies its txt_md_file."""
        md = "/d/K/mineru/AT_1/auto/AT_1.md"
        master = _store(self._tmp.name, {"K": {
            "local_file_pdf": "/d/K/AT_1.pdf", "txt_md_file": md}})
        (a,) = cli.plan_markdown_paths([_row()], master)
        self.assertEqual((a.local_file_md, a.source), (md, "master"))

    def test_source_row_without_master_md_uses_layout(self) -> None:
        """A master entry lacking txt_md_file falls back to the layout."""
        master = _store(self._tmp.name, {"K": {"local_file_pdf": "/d/K/AT_1.pdf"}})
        (a,) = cli.plan_markdown_paths([_row()], master)
        self.assertEqual((a.local_file_md, a.source),
                         ("/d/K/mineru/AT_1.md", "layout"))

    def test_source_row_with_different_master_pdf_uses_layout(self) -> None:
        """A master entry for another PDF is ignored."""
        master = _store(self._tmp.name, {"K": {
            "local_file_pdf": "/d/K/other.pdf", "txt_md_file": "/x.md"}})
        (a,) = cli.plan_markdown_paths([_row()], master)
        self.assertEqual((a.local_file_md, a.source),
                         ("/d/K/mineru/AT_1.md", "layout"))

    def test_candidate_row_uses_pdf_stem_layout(self) -> None:
        """A non-source row never reads the master."""
        master = _store(self._tmp.name, {"K": {
            "local_file_pdf": "/d/K/cand.pdf", "txt_md_file": "/x.md"}})
        (a,) = cli.plan_markdown_paths(
            [_row("/d/K/cand.pdf", is_source=False)], master)
        self.assertEqual((a.local_file_md, a.source),
                         ("/d/K/mineru/cand.md", "layout"))


class MainTest(unittest.TestCase):
    """Behaviour of main() against the fake engine."""

    def setUp(self) -> None:
        """Point the master path at an empty temp store."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch.object(cli.settings, "MASTER_DOC_DESCRIPTORS_PATH",
                         str(Path(self._tmp.name) / "master.json"))
        p.start()
        self.addCleanup(p.stop)

    def _run(self, db: FakeDb, *argv: str) -> int:
        """Run main() with ``db`` as the engine.

        Args:
            db: Fake engine.
            *argv: CLI arguments.

        Returns:
            The exit code.
        """
        with patch.object(cli, "make_engine", return_value=db):
            return cli.main(list(argv))

    def test_main_dry_run_writes_nothing(self) -> None:
        """--dry-run executes no UPDATE."""
        db = FakeDb([("id1", "K", True, "/d/K/AT_1.pdf")])
        self.assertEqual(self._run(db, "--dry-run"), 0)
        self.assertEqual(db.updates(), [])
        self.assertTrue(db.disposed)

    def test_main_real_run_updates_guarded(self) -> None:
        """A real run issues one guarded executemany UPDATE."""
        db = FakeDb([("id1", "K", True, "/d/K/AT_1.pdf")])
        self.assertEqual(self._run(db), 0)
        ((sql, params),) = db.updates()
        self.assertIn("local_file_md IS NULL", sql)
        self.assertEqual(params, [{"p": "/d/K/mineru/AT_1.md", "id": "id1"}])

    def test_main_exits_2_without_column(self) -> None:
        """A missing column exits 2 with no writes."""
        db = FakeDb([], has_column=False)
        with self.assertLogs(cli.logger, level="ERROR"):
            self.assertEqual(self._run(db), 2)
        self.assertEqual(db.updates(), [])


if __name__ == "__main__":
    unittest.main()
