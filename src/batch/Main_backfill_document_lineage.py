"""Backfill ``document_id`` lineage on existing facts and chunks.

Links every ``facts`` and ``pg_embedding_qwen`` row whose ``document_id`` is
still NULL to the source document of its decision
(``document_registry.is_source``; ``celex_id`` == ``decision_id``). Rows that
already carry a ``document_id`` are never touched, so re-runs are no-ops.
Logging is configured by ``config.settings`` on import.

Batching: keyset batches by primary key. Each batch is one transaction with
``lock_timeout`` set locally: select at most ``--batch-size`` candidate ids
(``id > last_id ORDER BY id LIMIT n``), then ``UPDATE ... WHERE t.id =
ANY(:ids)`` with the join and NULL guards repeated, then commit. Keyset paging
walks the PK index forward once (no rescans of already-linked rows), and no
UPDATE touches more than ``--batch-size`` rows, so the live UI readers of
``pg_embedding_qwen`` never wait on a long transaction.

Orphans (distinct ``celex_id`` without a source document) are logged and, in
a real run, written to ``settings.REPORT_PATH/lineage_orphans_<ts>.csv``. They
are reported, not failures. ``--dry-run`` counts and logs only, writes
nothing (no CSV either).

Testing: ``--limit N`` restricts the run to the rows of the first N source
documents (ordered by ``decision_id``) and skips the full-table orphan scan,
so a small trial run is quick. Combine with ``--dry-run`` to preview it.

Exit codes: 0 success (orphans included), 1 database error mid-run (batches
committed so far stay; re-run to resume), 2 usage error or the lineage
migration not applied.
"""

import argparse
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from pipelines.rag.pgvector.session import column_exists, make_engine

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_DB_ERROR = 1
EXIT_USAGE = 2
MIGRATION_FILE = "migrate_document_lineage.sql"
REGISTRY = f"{settings.DB_SCHEMA}.document_registry"
ORPHAN_COLUMNS = ("table", "celex_id", "rows")


class LineageNotReady(RuntimeError):
    """The lineage column or the document registry is missing."""


class TableSpec(BaseModel):
    """One table carrying ``celex_id`` and a ``document_id`` lineage column."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="CLI name of the table (facts or chunks).")
    qualified: str = Field(description="Schema-qualified table name.")


TABLES: dict[str, TableSpec] = {
    spec.key: spec for spec in (
        TableSpec(key="facts", qualified=f"{settings.DB_SCHEMA}.facts"),
        TableSpec(key="chunks",
                  qualified=f"{settings.DB_SCHEMA}.{settings.EMBEDDING_TABLE}"),
    )
}

_SET_LOCK_TIMEOUT = text("SELECT set_config('lock_timeout', :timeout, true)")
_LIMITED_DECISIONS = text(
    f"SELECT d.decision_id FROM {REGISTRY} d WHERE d.is_source "
    "ORDER BY d.decision_id LIMIT :limit"
)


def _scope(scoped: bool) -> str:
    """Return the decision filter of a ``--limit`` run.

    Args:
        scoped: True when the run is restricted to selected decisions.

    Returns:
        ``AND t.celex_id = ANY(:decisions) `` or an empty string.
    """
    return "AND t.celex_id = ANY(:decisions) " if scoped else ""


def _keyset_sql(spec: TableSpec, first: bool, scoped: bool = False) -> Any:
    """Return the candidate-id keyset select of one batch.

    Args:
        spec: Target table.
        first: First batch (no ``last_id`` lower bound).
        scoped: Restrict to ``:decisions`` (``--limit`` run).

    Returns:
        A text clause with ``:batch_size`` (and ``:last_id`` unless first,
        ``:decisions`` when scoped).
    """
    bound = "" if first else "AND t.id > :last_id "
    return text(
        f"SELECT t.id FROM {spec.qualified} t "
        f"JOIN {REGISTRY} d ON d.decision_id = t.celex_id AND d.is_source "
        f"WHERE t.document_id IS NULL {bound}{_scope(scoped)}"
        "ORDER BY t.id LIMIT :batch_size"
    )


def _update_sql(spec: TableSpec, scoped: bool = False) -> Any:
    """Return the guarded UPDATE of one batch of ids.

    Args:
        spec: Target table.
        scoped: Restrict to ``:decisions`` (``--limit`` run).

    Returns:
        A text clause with ``:ids`` (and ``:decisions`` when scoped).
    """
    return text(
        f"UPDATE {spec.qualified} t SET document_id = d.document_id "
        f"FROM {REGISTRY} d "
        "WHERE t.id = ANY(:ids) AND t.celex_id = d.decision_id "
        f"AND d.is_source AND t.document_id IS NULL {_scope(scoped)}"
    )


def _count_sql(spec: TableSpec, scoped: bool = False) -> Any:
    """Return the dry-run count of rows that would be linked.

    Args:
        spec: Target table.
        scoped: Restrict to ``:decisions`` (``--limit`` run).

    Returns:
        A text clause (with ``:decisions`` when scoped).
    """
    return text(
        f"SELECT count(*) FROM {spec.qualified} t "
        f"JOIN {REGISTRY} d ON d.decision_id = t.celex_id AND d.is_source "
        f"WHERE t.document_id IS NULL {_scope(scoped)}"
    )


def _orphan_sql(spec: TableSpec) -> Any:
    """Return the orphan query (celex_id without a source document).

    Args:
        spec: Target table.

    Returns:
        A text clause yielding ``(celex_id, rows)``.
    """
    return text(
        f"SELECT t.celex_id, count(*) FROM {spec.qualified} t "
        f"WHERE NOT EXISTS (SELECT 1 FROM {REGISTRY} d "
        "WHERE d.decision_id = t.celex_id AND d.is_source) "
        "GROUP BY t.celex_id ORDER BY t.celex_id"
    )


def _positive(text_value: str) -> int:
    """Parse a positive integer for argparse.

    Args:
        text_value: Raw argument.

    Returns:
        The integer.

    Raises:
        argparse.ArgumentTypeError: If not an integer or not > 0.
    """
    try:
        value = int(text_value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text_value!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        The parsed namespace.

    Raises:
        SystemExit: On a usage error (argparse exits with code 2).
    """
    p = argparse.ArgumentParser(
        description="Backfill document_id on existing facts and chunks.")
    p.add_argument("--dry-run", action="store_true",
                   help="Count rows to link and orphans; write nothing.")
    p.add_argument("--table", choices=[*TABLES, "all"], default="all",
                   help="Table to backfill (chunks = settings.EMBEDDING_TABLE).")
    p.add_argument("--batch-size", type=_positive,
                   default=settings.LINEAGE_BACKFILL_BATCH_SIZE,
                   help="Rows per UPDATE batch; each batch commits separately.")
    p.add_argument("--limit", type=_positive, default=None,
                   help="Testing: only the rows of the first N source documents "
                        "(ordered by decision_id); skips the orphan scan.")
    return p.parse_args(argv)


def select_limited_decisions(engine: Any, limit: int) -> list[str]:
    """Return the decision keys of the first ``limit`` source documents.

    Args:
        engine: SQLAlchemy engine.
        limit: Number of source documents.

    Returns:
        Decision keys ordered by ``decision_id``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors.
    """
    with engine.connect() as conn:
        return list(conn.execute(_LIMITED_DECISIONS, {"limit": limit}).scalars().all())


def check_schema(engine: Any, specs: list[TableSpec]) -> None:
    """Verify the registry and every lineage column exist.

    Args:
        engine: SQLAlchemy engine.
        specs: Tables to be backfilled.

    Raises:
        LineageNotReady: If a column or the registry table is missing.
        sqlalchemy.exc.SQLAlchemyError: On database errors.
    """
    with engine.connect() as conn:
        if not column_exists(conn, REGISTRY, "is_source"):
            raise LineageNotReady(f"{REGISTRY}.is_source missing")
        for spec in specs:
            if not column_exists(conn, spec.qualified, "document_id"):
                raise LineageNotReady(f"{spec.qualified}.document_id missing")


def count_pending(engine: Any, spec: TableSpec,
                  decisions: list[str] | None = None) -> int:
    """Count rows a real run would link.

    Args:
        engine: SQLAlchemy engine.
        spec: Target table.
        decisions: Restrict to these decision keys (``--limit``); None = all.

    Returns:
        The number of NULL-lineage rows whose decision has a source document.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors.
    """
    scoped = decisions is not None
    params = {"decisions": decisions} if scoped else {}
    with engine.connect() as conn:
        return int(conn.execute(_count_sql(spec, scoped), params).scalar_one())


def backfill_table(engine: Any, spec: TableSpec, batch_size: int,
                   lock_timeout: str, decisions: list[str] | None = None) -> int:
    """Link NULL-lineage rows of one table in keyset batches.

    Args:
        engine: SQLAlchemy engine.
        spec: Target table.
        batch_size: Maximum ids per batch (and per UPDATE).
        lock_timeout: Postgres ``lock_timeout`` set per batch transaction.
        decisions: Restrict to these decision keys (``--limit``); None = all.

    Returns:
        The number of rows updated.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors (for example a
            lock timeout); batches committed before it stay committed.
    """
    scoped = decisions is not None
    scope_params: dict[str, Any] = {"decisions": decisions} if scoped else {}
    total = 0
    last_id: Any = None
    batches = 0
    while True:
        with engine.begin() as conn:
            conn.execute(_SET_LOCK_TIMEOUT, {"timeout": lock_timeout})
            params: dict[str, Any] = {"batch_size": batch_size, **scope_params}
            if last_id is not None:
                params["last_id"] = last_id
            ids = list(conn.execute(_keyset_sql(spec, last_id is None, scoped),
                                    params).scalars().all())
            if not ids:
                break
            updated = conn.execute(_update_sql(spec, scoped),
                                   {"ids": ids, **scope_params}).rowcount
        total += updated
        batches += 1
        last_id = ids[-1]
        logger.info("%s: batch %d linked %d rows (total %d)", spec.qualified,
                    batches, updated, total)
    return total


def find_orphans(engine: Any, spec: TableSpec) -> list[tuple[str | None, int]]:
    """Return the decision keys of a table that have no source document.

    Args:
        engine: SQLAlchemy engine.
        spec: Target table.

    Returns:
        ``(celex_id, row_count)`` pairs ordered by ``celex_id``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors.
    """
    with engine.connect() as conn:
        return [(row[0], int(row[1])) for row in conn.execute(_orphan_sql(spec)).all()]


def _write_orphans(rows: list[tuple[str, str | None, int]], path: Path) -> None:
    """Write the orphan list as a UTF-8 CSV via a temp file and os.replace.

    Args:
        rows: ``(table, celex_id, rows)`` triples.
        path: Target CSV path; parent directories are created.

    Raises:
        OSError: If the file cannot be written.
    """
    frame = pl.DataFrame(rows, schema={"table": pl.String, "celex_id": pl.String,
                                       "rows": pl.Int64}, orient="row")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        frame.write_csv(tmp_name)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    logger.info("wrote %d orphan keys to %s", len(rows), path)


def _run(engine: Any, specs: list[TableSpec], args: argparse.Namespace) -> None:
    """Backfill (or count) each table and report its orphans.

    Args:
        engine: SQLAlchemy engine.
        specs: Tables to process.
        args: Parsed arguments.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors.
        OSError: If the orphan CSV cannot be written.
    """
    decisions: list[str] | None = None
    if args.limit is not None:
        decisions = select_limited_decisions(engine, args.limit)
        logger.info("limited test run: %d source documents (%s); orphan scan "
                    "skipped", len(decisions), ", ".join(decisions))
    orphans: list[tuple[str, str | None, int]] = []
    for spec in specs:
        if args.dry_run:
            logger.info("%s: %d rows would be linked [dry run]", spec.qualified,
                        count_pending(engine, spec, decisions))
        else:
            linked = backfill_table(engine, spec, args.batch_size,
                                    settings.LINEAGE_BACKFILL_LOCK_TIMEOUT,
                                    decisions)
            logger.info("%s: %d rows linked", spec.qualified, linked)
        if decisions is not None:
            continue
        table_orphans = find_orphans(engine, spec)
        for celex_id, count in table_orphans:
            logger.warning("%s: orphan celex_id %s (%d rows, no source document)",
                           spec.qualified, celex_id, count)
        logger.info("%s: %d orphan keys", spec.qualified, len(table_orphans))
        orphans.extend((spec.qualified, c, n) for c, n in table_orphans)
    if orphans and not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _write_orphans(orphans,
                       Path(settings.REPORT_PATH) / f"lineage_orphans_{stamp}.csv")


def main(argv: list[str] | None = None) -> int:
    """Run the lineage backfill.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).

    Returns:
        0 on success (orphans included), 1 on a database or report write
        error, 2 on a usage error or a missing lineage migration.
    """
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    specs = list(TABLES.values()) if args.table == "all" else [TABLES[args.table]]
    engine = make_engine()
    try:
        check_schema(engine, specs)
        _run(engine, specs, args)
    except LineageNotReady as exc:
        logger.error("lineage schema not ready (%s): apply %s first", exc,
                     MIGRATION_FILE)
        return EXIT_USAGE
    except (SQLAlchemyError, OSError) as exc:
        logger.error("lineage backfill failed: %s (committed batches stay; "
                     "re-run to resume)", exc)
        return EXIT_DB_ERROR
    finally:
        engine.dispose()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
