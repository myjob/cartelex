"""
Single source of truth for SQLAlchemy engine and session creation.

All database-touching modules in this package should import `make_engine`
(or the default `engine` / `Session`) from here instead of instantiating
their own `create_engine` calls.

Exports:
    make_engine(db_url)  — create a configured engine for the given URL.
                           Applies the correct PostgreSQL search_path so that
                           unqualified table names resolve to the right schema.
    engine               — default engine built from settings.DATABASE_URL.
    Session              — sessionmaker bound to the default engine; suitable
                           for use when no custom db_url override is needed.
    column_exists(conn, table, column) — probe for a column that a pending
                           migration adds, so writers work before and after it.
    warn_once(logger, key, message, *args) — log a warning once per process.
"""

import logging
import threading

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from pipelines.rag.pgvector.vector_config import DATABASE_URL, SEARCH_PATH


def make_engine(db_url: str = DATABASE_URL):
    """Return a SQLAlchemy engine configured with the project search_path.

    Args:
        db_url: PostgreSQL connection string. Defaults to
                settings.DATABASE_URL (process environment first, then the
                gitignored config_main/.env). config.yaml holds no credential.
    """
    return create_engine(
        db_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={SEARCH_PATH}"},
    )


_COLUMN_EXISTS = text(
    "SELECT 1 FROM pg_attribute "
    "WHERE attrelid = to_regclass(:table) AND attname = :column "
    "AND attnum > 0 AND NOT attisdropped"
)

#: Keys already warned about by :func:`warn_once` in this process.
_WARNED_ONCE: set[str] = set()
_WARNED_LOCK = threading.Lock()


def column_exists(conn, table: str, column: str) -> bool:
    """Return whether ``table`` (resolved via ``search_path``) has ``column``.

    A missing table counts as a missing column (``to_regclass`` yields NULL).

    Args:
        conn: SQLAlchemy connection or session exposing ``execute``.
        table: Table name, optionally schema-qualified.
        column: Column name.

    Returns:
        True if the column exists.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors.
    """
    return conn.execute(_COLUMN_EXISTS, {"table": table, "column": column}).first() is not None


def warn_once(logger: logging.Logger, key: str, message: str, *args: object) -> None:
    """Log ``message`` at WARNING the first time ``key`` is seen in this process.

    Args:
        logger: Logger to emit on.
        key: Deduplication key (one warning per key per process).
        message: ``logging`` format string.
        *args: Format arguments.

    Returns:
        None.
    """
    with _WARNED_LOCK:
        if key in _WARNED_ONCE:
            return
        _WARNED_ONCE.add(key)
    logger.warning(message, *args)


# Default engine and session factory — use these when no custom URL is needed.
engine = make_engine()
Session = sessionmaker(bind=engine)
