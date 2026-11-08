"""One-off repair: make the existing English markdown the master again.

ISSUES 20261031: 54 pre-1973 decisions have an ``_EN`` candidate registry row
that carried the German source's PDF path (copied by the markdown-path
backfill, never downloaded). The first MinerU run converted that German PDF
into the candidate's markdown and switched master ``txt_md_file`` from the
English ``mineru/auto/<id>_en.md`` to the German output.

For every non-English source row whose ``local_file_md`` is an English
``*_en.md`` this script:

* sets ``document_registry.original_file_md`` to the German/French/...
  MinerU markdown (``<pdf dir>/mineru/<pdf stem>.md``) when it exists;
* clears ``local_file_pdf``, ``local_file_md`` and ``sha256`` of the phantom
  candidate row (the row and its ``source_url`` are kept);
* sets ``decision_registry.language_translated = 'en'``;
* switches the master entry back: ``txt_md_file`` = English file,
  ``original_file_md``, ``language_init`` = source language,
  ``language_translated`` = ``en``.

Dry run by default; ``--apply`` writes. All DB updates run in one
transaction and the master is flushed inside it, so a flush failure rolls the
database back.

Usage (from the repo root):
    .venv/bin/python -m batch.utils.repair_translated_markdown [--apply]
"""

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import text

from config.settings import settings
from pipelines.download.descriptor_store import MasterDescriptorStore, TranslationEntry
from pipelines.download.local_pdf import markdown_path_for
from pipelines.rag.pgvector.session import make_engine

logger = logging.getLogger(__name__)

_REGISTRY = f"{settings.DB_SCHEMA}.document_registry"
_DECISIONS = f"{settings.DB_SCHEMA}.decision_registry"
_TRANSLATED = "en"
_ENGLISH_MD_PATTERN = "%\\_en.md"

_SELECT_ROWS = text(
    f"""
    SELECT s.decision_id, s.document_id AS source_document_id,
           s.language AS source_language, s.local_file_pdf AS source_pdf,
           s.local_file_md AS english_md, c.document_id AS phantom_document_id
    FROM {_REGISTRY} s
    LEFT JOIN {_REGISTRY} c
      ON c.decision_id = s.decision_id AND NOT c.is_source AND c.is_selected
     AND c.local_file_pdf = s.local_file_pdf
    WHERE s.is_source AND s.language <> 'EN' AND s.local_file_pdf IS NOT NULL
      AND s.local_file_md LIKE :pattern
    ORDER BY s.decision_id
    """
)
_SET_ORIGINAL = text(
    f"UPDATE {_REGISTRY} SET original_file_md = :original, updated_at = now() "
    "WHERE document_id = :document_id"
)
_CLEAR_PHANTOM = text(
    f"UPDATE {_REGISTRY} SET local_file_pdf = NULL, local_file_md = NULL, "
    "sha256 = NULL, updated_at = now() WHERE document_id = :document_id"
)
_SET_TRANSLATED = text(
    f"UPDATE {_DECISIONS} SET language_translated = :translated "
    "WHERE decision_id = :decision_id"
)


class RepairRow(BaseModel):
    """A non-English source row that already points at an English markdown."""

    decision_id: str = Field(description="Decision id (also the master key).")
    source_document_id: str = Field(description="Registry id of the source row.")
    source_language: str = Field(description="Upper-case language of the source PDF.")
    source_pdf: str = Field(description="Path of the source PDF.")
    english_md: str = Field(description="English markdown the source row points at.")
    phantom_document_id: str | None = Field(
        description="Selected candidate sharing the source PDF; None when absent.")


class RepairAction(BaseModel):
    """What the repair writes for one decision."""

    decision_id: str = Field(description="Decision id (also the master key).")
    source_document_id: str = Field(description="Registry id of the source row.")
    original_file_md: str | None = Field(
        description="Foreign-language markdown to reference; None when not on disk.")
    clear_document_id: str | None = Field(
        description="Phantom candidate whose copied file columns are cleared.")
    master: TranslationEntry = Field(description="Master entry values to store.")


def plan_repair(
    rows: list[RepairRow], file_exists: Callable[[str], bool]
) -> list[RepairAction]:
    """Plan the repair actions (pure).

    Args:
        rows: Candidate rows from the registry.
        file_exists: Predicate telling whether a path exists on disk.

    Returns:
        One action per row whose English markdown exists.
    """
    actions: list[RepairAction] = []
    for row in rows:
        if not file_exists(row.english_md):
            logger.warning("%s: English markdown %s missing, not repaired",
                           row.decision_id, row.english_md)
            continue
        original = str(markdown_path_for(Path(row.source_pdf)))
        original_md = original if file_exists(original) else None
        actions.append(RepairAction(
            decision_id=row.decision_id,
            source_document_id=row.source_document_id,
            original_file_md=original_md,
            clear_document_id=row.phantom_document_id,
            master=TranslationEntry(
                txt_md_file=row.english_md, original_file_md=original_md,
                language_init=row.source_language.lower(),
                language_translated=_TRANSLATED),
        ))
    return actions


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name; None reads ``sys.argv``.

    Returns:
        Namespace with ``apply``.
    """
    p = argparse.ArgumentParser(description="Repair decisions whose English markdown lost master status.")
    p.add_argument("--apply", action="store_true", help="Write; default is a dry run.")
    return p.parse_args(argv)


def _apply(engine: object, master: MasterDescriptorStore, actions: list[RepairAction]) -> None:
    """Write all actions in one transaction with the master flush inside it.

    Args:
        engine: SQLAlchemy engine.
        master: Master descriptor store.
        actions: Planned actions.

    Raises:
        Exception: Any DB or flush error; the transaction is rolled back.
    """
    with engine.begin() as conn:  # type: ignore[attr-defined]
        for a in actions:
            if a.original_file_md is not None:
                conn.execute(_SET_ORIGINAL, {"original": a.original_file_md,
                                             "document_id": a.source_document_id})
            if a.clear_document_id is not None:
                conn.execute(_CLEAR_PHANTOM, {"document_id": a.clear_document_id})
            conn.execute(_SET_TRANSLATED, {"translated": _TRANSLATED,
                                           "decision_id": a.decision_id})
            if master.has_key(a.decision_id):
                master.set_translation(a.decision_id, a.master)
            else:
                logger.warning("%s: no master entry, registry only", a.decision_id)
        master.flush()


def main(argv: list[str] | None = None) -> int:
    """Run the repair.

    Args:
        argv: Arguments without the program name.

    Returns:
        0 on success.
    """
    args = parse_args(argv)
    engine = make_engine()
    try:
        with engine.connect() as conn:
            raw = conn.execute(_SELECT_ROWS, {"pattern": _ENGLISH_MD_PATTERN}).fetchall()
        rows = [RepairRow.model_validate(dict(r._mapping)) for r in raw]
        actions = plan_repair(rows, file_exists=lambda p: Path(p).is_file())
        for a in actions:
            logger.info("%s: original %s, clear %s, master -> %s",
                        a.decision_id, a.original_file_md, a.clear_document_id,
                        a.master.txt_md_file)
        logger.info(
            "%d rows, %d actions: %d originals referenced, %d phantoms cleared, "
            "%d without original%s",
            len(rows), len(actions),
            sum(a.original_file_md is not None for a in actions),
            sum(a.clear_document_id is not None for a in actions),
            sum(a.original_file_md is None for a in actions),
            "" if args.apply else " [dry run]")
        if args.apply and actions:
            master = MasterDescriptorStore(Path(settings.MASTER_DOC_DESCRIPTORS_PATH))
            _apply(engine, master, actions)
            logger.info("repair applied")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
