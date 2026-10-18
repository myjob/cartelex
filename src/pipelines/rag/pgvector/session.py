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
"""

from sqlalchemy import create_engine
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


# Default engine and session factory — use these when no custom URL is needed.
engine = make_engine()
Session = sessionmaker(bind=engine)
