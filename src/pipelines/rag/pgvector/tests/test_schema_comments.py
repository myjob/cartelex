"""Enforcement tests for ``schema_comments.sql`` (live database).

These are integration tests: they read ``pg_description`` from the configured
``DATABASE_URL`` per project convention, and they parse the SQL file itself so
that the file and the database cannot drift apart unnoticed.

Apply the file before running::

    psql -h localhost -p 5433 -U celex_facts -d celex \
         -f src/pipelines/rag/pgvector/schema_comments.sql

Prose reference: ``docs/Data Table Catalogue.md``.
"""

import re
import unittest
from pathlib import Path

from sqlalchemy import text

from config.settings import settings
from pipelines.rag.pgvector.session import make_engine

# The legacy rule-engine schema is not represented in settings because no
# pipeline targets it any more; it is still enforced so the drained table
# keeps its "retired, do not use" comment.
LEGACY_SCHEMA = "rule_facts"

DOCUMENTED_SCHEMAS = (settings.DB_SCHEMA, LEGACY_SCHEMA)

SCHEMA_COMMENTS_SQL = Path(__file__).resolve().parents[1] / "schema_comments.sql"

# A comment shorter than this is a placeholder, not documentation.
MIN_COMMENT_LENGTH = 30

_RELATION_PATTERN = re.compile(
    r"^COMMENT ON (?:TABLE|VIEW|MATERIALIZED VIEW) ([\w.]+) IS", re.MULTILINE
)
_COLUMN_PATTERN = re.compile(r"^COMMENT ON COLUMN ([\w.]+) IS", re.MULTILINE)


def _parse_sql_file() -> tuple[set[str], set[str]]:
    """Extract the relations and columns the SQL file claims to document.

    Returns:
        A tuple of (relation names, column names), both fully qualified as
        ``schema.relation`` and ``schema.relation.column``.

    Raises:
        FileNotFoundError: if ``schema_comments.sql`` is missing.
    """
    sql = SCHEMA_COMMENTS_SQL.read_text(encoding="utf-8")
    return set(_RELATION_PATTERN.findall(sql)), set(_COLUMN_PATTERN.findall(sql))


class TestSchemaComments(unittest.TestCase):
    """Guard that every relation is documented and the file matches the schema."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = make_engine()
        cls.file_relations, cls.file_columns = _parse_sql_file()

    def _fetch(self, sql: str) -> list[tuple]:
        """Run a read-only query against the configured database.

        Args:
            sql: Statement to execute. Must be a SELECT.

        Returns:
            All result rows as tuples.
        """
        with self.engine.connect() as conn:
            return list(
                conn.execute(text(sql), {"schemas": list(DOCUMENTED_SCHEMAS)}).all()
            )

    def test_every_relation_has_a_comment(self) -> None:
        """No table, view or materialized view may be left undocumented."""
        rows = self._fetch(
            """
            SELECT c.relnamespace::regnamespace::text || '.' || c.relname
            FROM pg_class c
            WHERE c.relnamespace::regnamespace::text = ANY(:schemas)
              AND c.relkind IN ('r', 'v', 'm')
              AND obj_description(c.oid, 'pg_class') IS NULL
            ORDER BY 1
            """
        )
        undocumented = [row[0] for row in rows]
        self.assertEqual(
            [],
            undocumented,
            "Undocumented relations -- add COMMENT ON to schema_comments.sql "
            f"and re-run it: {undocumented}",
        )

    def test_file_covers_exactly_the_live_relations(self) -> None:
        """The SQL file and the database must name the same set of relations.

        Catches both directions of drift: a relation added without a comment,
        and a comment left behind for a relation that was renamed or dropped.
        """
        rows = self._fetch(
            """
            SELECT c.relnamespace::regnamespace::text || '.' || c.relname
            FROM pg_class c
            WHERE c.relnamespace::regnamespace::text = ANY(:schemas)
              AND c.relkind IN ('r', 'v', 'm')
            """
        )
        live = {row[0] for row in rows}
        self.assertEqual(
            set(),
            live - self.file_relations,
            f"In the database, absent from schema_comments.sql: "
            f"{sorted(live - self.file_relations)}",
        )
        self.assertEqual(
            set(),
            self.file_relations - live,
            f"In schema_comments.sql, absent from the database (renamed or "
            f"dropped?): {sorted(self.file_relations - live)}",
        )

    def test_commented_columns_still_exist(self) -> None:
        """Every column the file comments must exist in the live schema."""
        rows = self._fetch(
            """
            SELECT c.relnamespace::regnamespace::text || '.' || c.relname
                   || '.' || a.attname
            FROM pg_class c
            JOIN pg_attribute a ON a.attrelid = c.oid
            WHERE c.relnamespace::regnamespace::text = ANY(:schemas)
              AND c.relkind IN ('r', 'v', 'm')
              AND a.attnum > 0
              AND NOT a.attisdropped
            """
        )
        live = {row[0] for row in rows}
        self.assertEqual(
            set(),
            self.file_columns - live,
            f"Commented columns that no longer exist: "
            f"{sorted(self.file_columns - live)}",
        )

    def test_comments_are_not_placeholders(self) -> None:
        """Reject stub comments that satisfy coverage without informing."""
        rows = self._fetch(
            f"""
            SELECT c.relnamespace::regnamespace::text || '.' || c.relname,
                   obj_description(c.oid, 'pg_class')
            FROM pg_class c
            WHERE c.relnamespace::regnamespace::text = ANY(:schemas)
              AND c.relkind IN ('r', 'v', 'm')
              AND length(trim(obj_description(c.oid, 'pg_class')))
                  < {MIN_COMMENT_LENGTH}
            ORDER BY 1
            """
        )
        self.assertEqual(
            [], [row[0] for row in rows], f"Comments under {MIN_COMMENT_LENGTH} chars"
        )


if __name__ == "__main__":
    unittest.main()
