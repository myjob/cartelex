"""Tests for the Main_backfill_document_lineage CLI (fake engine, no DB)."""

import re
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from batch import Main_backfill_document_lineage as cli
from config.settings import settings

FACTS = f"{settings.DB_SCHEMA}.facts"
CHUNKS = f"{settings.DB_SCHEMA}.{settings.EMBEDDING_TABLE}"


class FakeResult:
    """Minimal SQLAlchemy result stand-in."""

    def __init__(self, rows: list[tuple] | None = None, rowcount: int = 0) -> None:
        """Create the result.

        Args:
            rows: Result rows as tuples.
            rowcount: Affected row count of a DML statement.
        """
        self._rows = rows or []
        self.rowcount = rowcount

    def all(self) -> list[tuple]:
        """Return all rows.

        Returns:
            The rows.
        """
        return list(self._rows)

    def first(self) -> tuple | None:
        """Return the first row or None.

        Returns:
            The first row.
        """
        return self._rows[0] if self._rows else None

    def scalar_one(self) -> Any:
        """Return the single scalar.

        Returns:
            First column of the first row.
        """
        return self._rows[0][0]

    def scalars(self) -> "FakeResult":
        """Return a result over the first column.

        Returns:
            A result whose rows are the bare first-column values.
        """
        return FakeResult([r[0] for r in self._rows])

    def __iter__(self):
        """Iterate the rows.

        Returns:
            Row iterator.
        """
        return iter(self._rows)


class FakeDb:
    """In-memory model of the two tables and the registry, plus a statement log.

    ``rows`` maps a qualified table to ``{id: [celex_id, document_id]}``;
    ``sources`` maps a decision_id to its ``is_source`` document_id.
    """

    def __init__(self, rows: dict[str, dict[str, list]], sources: dict[str, str],
                 missing_column: bool = False) -> None:
        """Create the fake.

        Args:
            rows: Table rows keyed by qualified table name.
            sources: decision_id to source document_id.
            missing_column: Report every lineage column as absent.
        """
        self.rows = rows
        self.sources = sources
        self.missing_column = missing_column
        self.statements: list[tuple[str, dict]] = []
        self.transactions = 0
        self.disposed = False

    @contextmanager
    def begin(self):
        """Open a fake transaction (counted; one per begin()).

        Yields:
            A connection whose ``execute`` is routed to this fake.
        """
        self.transactions += 1
        self.statements.append(("BEGIN", {}))
        yield self
        self.statements.append(("COMMIT", {}))

    def connect(self):
        """Return a connection-like context manager (for probes).

        Returns:
            The begin() context manager.
        """
        return self.begin()

    def dispose(self) -> None:
        """Record disposal."""
        self.disposed = True

    @staticmethod
    def _table(sql: str) -> str:
        """Return the qualified table named in ``sql``.

        Args:
            sql: Statement text.

        Returns:
            FACTS or CHUNKS.
        """
        return CHUNKS if CHUNKS in sql else FACTS

    def execute(self, stmt: Any, params: dict | None = None) -> FakeResult:
        """Simulate one statement against the in-memory state.

        Args:
            stmt: SQLAlchemy text clause.
            params: Bound parameters.

        Returns:
            A fake result.
        """
        sql = " ".join(str(stmt).split())
        params = dict(params or {})
        self.statements.append((sql, params))
        if "pg_attribute" in sql:
            return FakeResult([] if self.missing_column else [(1,)])
        if "set_config" in sql:
            return FakeResult([(params.get("timeout"),)])
        if "ORDER BY d.decision_id" in sql:
            return FakeResult([(k,) for k in sorted(self.sources)[: params["limit"]]])
        scope = params.get("decisions")
        table = self._table(sql)
        data = {rid: row for rid, row in self.rows.get(table, {}).items()
                if scope is None or row[0] in scope}
        if sql.startswith("UPDATE"):
            n = 0
            for rid in params["ids"]:
                celex, doc = data[rid]
                if doc is None and celex in self.sources:
                    data[rid][1] = self.sources[celex]
                    n += 1
            return FakeResult(rowcount=n)
        if "NOT EXISTS" in sql:
            counts: dict[str | None, int] = {}
            for celex, _doc in data.values():
                if celex not in self.sources:
                    counts[celex] = counts.get(celex, 0) + 1
            return FakeResult(sorted(counts.items(), key=lambda kv: str(kv[0])))
        if "count(*)" in sql:
            return FakeResult([(sum(1 for c, d in data.values()
                                    if d is None and c in self.sources),)])
        if "ORDER BY t.id" in sql:
            last = params.get("last_id")
            ids = sorted(rid for rid, (c, d) in data.items()
                         if d is None and c in self.sources
                         and (last is None or rid > last))
            return FakeResult([(i,) for i in ids[: params["batch_size"]]])
        raise AssertionError(f"unexpected SQL: {sql}")

    def updates(self) -> list[tuple[str, dict]]:
        """Return the logged UPDATE statements.

        Returns:
            (sql, params) pairs.
        """
        return [s for s in self.statements if s[0].startswith("UPDATE")]


def _db(missing_column: bool = False) -> FakeDb:
    """Build a fake with one orphan, one pre-linked row and fillable rows.

    Args:
        missing_column: Report the lineage column as absent.

    Returns:
        The fake.
    """
    return FakeDb(
        rows={
            FACTS: {
                "f1": ["CELEX-31990D0645", None],
                "f2": ["CELEX-31990D0645", "doc-keep"],
                "f3": ["AT_39001", None],
                "f4": ["ORPHAN_1", None],
            },
            CHUNKS: {
                "c1": ["CELEX-31990D0645", None],
                "c2": ["ORPHAN_2", None],
            },
        },
        sources={"CELEX-31990D0645": "doc-a", "AT_39001": "doc-b"},
        missing_column=missing_column,
    )


class BackfillCliTest(unittest.TestCase):
    """Behaviour of main() against the fake engine."""

    def setUp(self) -> None:
        """Redirect REPORT_PATH to a temp dir."""
        self._tmp = tempfile.TemporaryDirectory()
        self.report_dir = Path(self._tmp.name)
        patcher = patch.object(cli.settings, "REPORT_PATH", str(self.report_dir))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

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

    def test_update_sets_only_null_document_id(self) -> None:
        """The UPDATE joins the source document and guards document_id IS NULL."""
        db = _db()
        self.assertEqual(self._run(db, "--table", "facts"), 0)
        updates = db.updates()
        self.assertEqual(len(updates), 1)
        sql, params = updates[0]
        self.assertIn(f"UPDATE {FACTS} t SET document_id = d.document_id", sql)
        self.assertIn(f"FROM {settings.DB_SCHEMA}.document_registry d", sql)
        self.assertIn("t.id = ANY(:ids)", sql)
        self.assertIn("t.celex_id = d.decision_id", sql)
        self.assertIn("d.is_source", sql)
        self.assertIn("t.document_id IS NULL", sql)
        self.assertEqual(sorted(params["ids"]), ["f1", "f3"])
        self.assertEqual(db.rows[FACTS]["f1"][1], "doc-a")
        self.assertEqual(db.rows[FACTS]["f2"][1], "doc-keep")
        self.assertEqual(db.rows[FACTS]["f3"][1], "doc-b")
        self.assertIsNone(db.rows[FACTS]["f4"][1])
        self.assertTrue(db.disposed)

    def test_dry_run_counts_without_update(self) -> None:
        """--dry-run counts pending rows and writes nothing (no UPDATE, no CSV)."""
        db = _db()
        with self.assertLogs(cli.logger, level="INFO") as logs:
            self.assertEqual(self._run(db, "--dry-run"), 0)
        self.assertEqual(db.updates(), [])
        counts = [s for s in db.statements
                  if "count(*)" in s[0] and "NOT EXISTS" not in s[0]]
        self.assertEqual(len(counts), 2)
        for sql, _ in counts:
            self.assertIn("t.document_id IS NULL", sql)
            self.assertIn("d.is_source", sql)
        text = "\n".join(logs.output)
        self.assertRegex(text, rf"{re.escape(FACTS)}: 2 rows would be linked")
        self.assertRegex(text, rf"{re.escape(CHUNKS)}: 1 rows would be linked")
        self.assertEqual(list(self.report_dir.iterdir()), [])
        self.assertIsNone(db.rows[FACTS]["f1"][1])

    def test_orphans_logged(self) -> None:
        """Decision keys without a source document are logged and written to CSV."""
        db = _db()
        with self.assertLogs(cli.logger, level="WARNING") as logs:
            self.assertEqual(self._run(db), 0)
        text = "\n".join(logs.output)
        self.assertIn("ORPHAN_1", text)
        self.assertIn("ORPHAN_2", text)
        csvs = list(self.report_dir.glob("lineage_orphans_*.csv"))
        self.assertEqual(len(csvs), 1)
        content = csvs[0].read_text(encoding="utf-8")
        self.assertIn("table,celex_id,rows", content.splitlines()[0])
        self.assertIn(f"{FACTS},ORPHAN_1,1", content)
        self.assertIn(f"{CHUNKS},ORPHAN_2,1", content)

    def test_rerun_is_noop(self) -> None:
        """A second run finds no candidate and issues no UPDATE."""
        db = _db()
        self.assertEqual(self._run(db), 0)
        first = len(db.updates())
        self.assertEqual(first, 2)
        snapshot = {t: {k: list(v) for k, v in rows.items()}
                    for t, rows in db.rows.items()}
        self.assertEqual(self._run(db), 0)
        self.assertEqual(len(db.updates()), first)
        self.assertEqual(db.rows, snapshot)

    def test_exit_code_zero_with_orphans(self) -> None:
        """Orphans are reported, not failures."""
        db = _db()
        db.sources.clear()
        self.assertEqual(self._run(db), 0)
        self.assertEqual(db.updates(), [])

    def test_missing_column_exits_2(self) -> None:
        """A missing lineage column (migration not applied) exits 2, no writes."""
        db = _db(missing_column=True)
        with self.assertLogs(cli.logger, level="ERROR") as logs:
            self.assertEqual(self._run(db), 2)
        self.assertIn("apply migrate_document_lineage.sql first",
                      "\n".join(logs.output))
        self.assertEqual(db.updates(), [])

    def test_batches_commit_separately(self) -> None:
        """Each keyset batch is its own transaction with lock_timeout set."""
        db = _db()
        db.rows[FACTS].update({f"f{i}": ["AT_39001", None] for i in range(5, 9)})
        self.assertEqual(self._run(db, "--table", "facts", "--batch-size", "2"), 0)
        updates = db.updates()
        self.assertEqual([len(p["ids"]) for _, p in updates], [2, 2, 2])
        self.assertTrue(all(len(p["ids"]) <= 2 for _, p in updates))
        keysets = [p for s, p in db.statements if "ORDER BY t.id" in s]
        self.assertEqual([p.get("last_id") for p in keysets], [None, "f3", "f6", "f8"])
        self.assertTrue(all(p["batch_size"] == 2 for p in keysets))
        # every UPDATE sits in its own BEGIN ... COMMIT after set_config
        seq = [s for s, _ in db.statements]
        for i, (sql, _) in enumerate(db.statements):
            if sql.startswith("UPDATE"):
                self.assertEqual(seq[i + 1], "COMMIT")
                block = seq[: i][::-1]
                begin = block.index("BEGIN")
                self.assertTrue(any("set_config" in s for s in block[:begin]))
        timeouts = [p for s, p in db.statements if "set_config" in s]
        self.assertTrue(all(p["timeout"] == settings.LINEAGE_BACKFILL_LOCK_TIMEOUT
                            for p in timeouts))
        for rid in ("f1", "f3", "f5", "f6", "f7", "f8"):
            self.assertIsNotNone(db.rows[FACTS][rid][1])

    def test_default_batch_size_from_settings(self) -> None:
        """--batch-size defaults to settings.LINEAGE_BACKFILL_BATCH_SIZE."""
        args = cli.parse_args([])
        self.assertEqual(args.batch_size, settings.LINEAGE_BACKFILL_BATCH_SIZE)
        self.assertEqual(args.table, "all")
        self.assertIsNone(args.limit)

    def test_limit_restricts_to_first_n_source_documents(self) -> None:
        """--limit N links only rows of the first N source decisions (sorted)."""
        db = _db()
        self.assertEqual(self._run(db, "--limit", "1"), 0)
        # sorted sources: AT_39001 < CELEX-31990D0645 -> only AT_39001 in scope
        self.assertEqual(db.rows[FACTS]["f3"][1], "doc-b")
        self.assertIsNone(db.rows[FACTS]["f1"][1])
        self.assertIsNone(db.rows[CHUNKS]["c1"][1])
        scoped = [p for s, p in db.statements if "ANY(:decisions)" in s]
        self.assertTrue(scoped)
        self.assertTrue(all(p["decisions"] == ["AT_39001"] for p in scoped))

    def test_limit_skips_orphan_scan_and_csv(self) -> None:
        """A limited test run skips the full-table orphan scan and writes no CSV."""
        db = _db()
        with self.assertLogs(cli.logger, level="INFO") as logs:
            self.assertEqual(self._run(db, "--limit", "10"), 0)
        self.assertFalse(any("NOT EXISTS" in s for s, _ in db.statements))
        self.assertEqual(list(self.report_dir.iterdir()), [])
        self.assertIn("orphan scan skipped", "\n".join(logs.output))

    def test_limit_with_dry_run_counts_scoped(self) -> None:
        """--dry-run --limit counts only the scoped rows."""
        db = _db()
        with self.assertLogs(cli.logger, level="INFO") as logs:
            self.assertEqual(self._run(db, "--dry-run", "--limit", "1"), 0)
        self.assertEqual(db.updates(), [])
        self.assertRegex("\n".join(logs.output),
                         rf"{re.escape(FACTS)}: 1 rows would be linked")

    def test_limit_must_be_positive(self) -> None:
        """--limit 0 is a usage error (exit 2)."""
        self.assertEqual(self._run(_db(), "--limit", "0"), 2)


if __name__ == "__main__":
    unittest.main()
