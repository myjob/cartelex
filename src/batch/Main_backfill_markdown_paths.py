"""Backfill ``document_registry.local_file_md`` for existing registry rows.

Assigns the spec'd MinerU Markdown target path to every source or selected
registry row that has a PDF but no Markdown path yet (``local_file_md IS
NULL``). A source row whose decision has a master descriptor entry with the
same ``local_file_pdf`` and a non-empty ``txt_md_file`` copies that path
(source ``master``); every other row gets the layout path
``<pdf folder>/mineru/<pdf stem>.md`` (source ``layout``). The master JSON is
only read, never written. Rows that already carry a path are never touched, so
re-runs are no-ops. Logging is configured by ``config.settings`` on import.

All updates run in one transaction (``UPDATE ... WHERE local_file_md IS
NULL``, executemany). ``--dry-run`` plans and logs counts, writes nothing.

Exit codes: 0 success, 1 database error (the transaction rolls back; re-run),
2 usage error or the ``local_file_md`` migration not applied.
"""

import argparse
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.download.local_pdf import markdown_path_for
from pipelines.rag.pgvector.session import column_exists, make_engine

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_DB_ERROR = 1
EXIT_USAGE = 2
REGISTRY = f"{settings.DB_SCHEMA}.document_registry"

_SELECT_PENDING = text(
    "SELECT document_id, decision_id, is_source, local_file_pdf "
    f"FROM {REGISTRY} "
    "WHERE (is_source OR is_selected) AND local_file_pdf IS NOT NULL "
    "AND local_file_md IS NULL ORDER BY decision_id, document_id"
)
_UPDATE_MD = text(
    f"UPDATE {REGISTRY} SET local_file_md = :p, updated_at = now() "
    "WHERE document_id = :id AND local_file_md IS NULL"
)


class RegistryPdfRow(BaseModel):
    """One registry row that has a PDF but no Markdown path."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(description="Registry primary key.")
    decision_id: str = Field(description="Decision key; master descriptor key.")
    is_source: bool = Field(description="True for the decision's source document.")
    local_file_pdf: str = Field(description="Stored PDF path.")


class MarkdownPathAssignment(BaseModel):
    """The Markdown path to store on one registry row."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(description="Registry primary key.")
    local_file_md: str = Field(description="Markdown target path.")
    source: Literal["master", "layout"] = Field(
        description="Where the path came from: master descriptor or layout rule.")


def plan_markdown_paths(rows: list[RegistryPdfRow],
                        master: MasterDescriptorStore) -> list[MarkdownPathAssignment]:
    """Decide the Markdown path of each row.

    Args:
        rows: Registry rows lacking ``local_file_md``.
        master: Master descriptor store (read only).

    Returns:
        One assignment per row, in input order.
    """
    plan: list[MarkdownPathAssignment] = []
    for row in rows:
        md_path = str(markdown_path_for(Path(row.local_file_pdf)))
        source: Literal["master", "layout"] = "layout"
        if row.is_source:
            master_pdf, master_md = master.markdown_entry(row.decision_id)
            if master_md and master_pdf == row.local_file_pdf:
                md_path, source = master_md, "master"
        plan.append(MarkdownPathAssignment(
            document_id=row.document_id, local_file_md=md_path,
            source=source))
    return plan


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
        description="Backfill document_registry.local_file_md.")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan and count assignments; write nothing.")
    return p.parse_args(argv)


def fetch_pending(engine: Any) -> list[RegistryPdfRow]:
    """Read the registry rows that still need a Markdown path.

    Args:
        engine: SQLAlchemy engine.

    Returns:
        Validated rows ordered by ``decision_id, document_id``.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors.
        pydantic.ValidationError: If a row violates the row model.
    """
    with engine.connect() as conn:
        raw = conn.execute(_SELECT_PENDING).all()
    return [RegistryPdfRow(document_id=str(r[0]), decision_id=r[1],
                           is_source=bool(r[2]), local_file_pdf=r[3]) for r in raw]


def apply_plan(engine: Any, plan: list[MarkdownPathAssignment]) -> None:
    """Write all assignments in one transaction.

    Args:
        engine: SQLAlchemy engine.
        plan: Assignments to store.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On database errors (rolls back).
    """
    if not plan:
        return
    with engine.begin() as conn:
        conn.execute(_UPDATE_MD, [{"p": a.local_file_md, "id": a.document_id}
                                  for a in plan])


def main(argv: list[str] | None = None) -> int:
    """Run the Markdown path backfill.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).

    Returns:
        0 on success, 1 on a database error, 2 on a usage error or a missing
        ``local_file_md`` column.
    """
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    engine = make_engine()
    try:
        with engine.connect() as conn:
            ready = column_exists(conn, REGISTRY, "local_file_md")
        if not ready:
            logger.error("%s.local_file_md missing: apply the document_registry "
                         "local_file_md migration first", REGISTRY)
            return EXIT_USAGE
        rows = fetch_pending(engine)
        master = MasterDescriptorStore(Path(settings.MASTER_DOC_DESCRIPTORS_PATH))
        plan = plan_markdown_paths(rows, master)
        n_master = sum(1 for a in plan if a.source == "master")
        logger.info("%d assignments (%d master, %d layout)%s", len(plan),
                    n_master, len(plan) - n_master,
                    " [dry run]" if args.dry_run else "")
        if not args.dry_run:
            apply_plan(engine, plan)
    except SQLAlchemyError as exc:
        logger.error("markdown path backfill failed: %s (rolled back; re-run)", exc)
        return EXIT_DB_ERROR
    finally:
        engine.dispose()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
