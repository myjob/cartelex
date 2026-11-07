"""One-off clean-up: de-duplicate registry decisions against the master.

Evidence rules and the plan live in :mod:`pipelines.download.dedupe_plan`:
a registry decision D that is not a master key is merged into master key K
only on hard evidence (identity rule celex/url/url+case, or the sha256 of D's
own selected/source PDF); ``case+date`` alone, a date-guarded URL match and
a sha shared only by a non-selected copy are ``review`` rows; conflicts,
ambiguous matches and refused decisions are reported only. Master keys are
never candidates, except those named with ``--include-master-key`` (keys the
downloader created after the committed master).

``--apply`` handles each delete in this order, so a partial failure is
re-runnable (the registry row, deleted last, keeps the decision a candidate):

1. remove the master entry of an included key and flush (backup first);
2. move ``<CELEX_DOCS_DIR>/<D>/`` to
   ``<CELEX_DOCS_DIR>/_dedupe_quarantine/<run_ts>/<D>/`` (nothing is deleted);
3. in ONE transaction: re-point D's document rows to K (not selected, not
   source, no stored file; a row whose re-derived id already exists is
   dropped), move D's case links to K, delete D's registry row (the audit
   table is untouched). The re-pointed rows keep D's URLs as K's alias URLs,
   which the downloader's identity rule reads, so the next downloader run
   maps those manifest rows to K instead of recreating D.

``--apply`` flushes the master JSON when an included key is removed: commit
or back up ``config_main/master_doc_descriptors.json`` first (the store also
writes a ``.bak-<ts>`` copy).

An included key without a registry row is still planned from its audited
URLs and removed from the master and the disk. Without ``--apply`` nothing
but the report is written. The CSV report ``dedupe_registry_<ts>.csv`` goes to
``settings.REPORT_PATH``: after the apply, with the final ``outcome`` per row.

Run from the repo root::

    .venv/bin/python -m batch.Main_dedupe_registry --include-master-key CELEX-31992D0568
    .venv/bin/python -m batch.Main_dedupe_registry --include-master-key CELEX-31992D0568 --apply

Exit codes: 0 success, 1 database or I/O error (including an unreachable
database), 2 usage error or registry tables not migrated.
"""

import argparse
import logging
import os
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import polars as pl
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from config.settings import settings
from pipelines.download.dedupe_plan import PlanItem, build_plan
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.rag.pgvector.document_registry_repository import (
    DocumentRegistryRepository,
)
from pipelines.rag.pgvector.registry_dedupe_repository import (
    MergeCounts,
    RegistryDedupeRepository,
)
from pipelines.rag.pgvector.session import make_engine

logger = logging.getLogger(__name__)

EXIT_IO = 1
EXIT_USAGE = 2
QUARANTINE_DIR = "_dedupe_quarantine"
REPORT_PREFIX = "dedupe_registry_"
REPORT_COLUMNS = (
    "decision_id", "duplicate_of", "signal", "action", "documents_repointed",
    "case_links_moved", "files_moved", "master_entry_removed", "outcome",
)


class RegistryMerger(Protocol):
    """Registry surface used by the apply step."""

    def merge_decision(self, decision_id: str, key: str) -> MergeCounts:
        """Fold D into K in one transaction; return what changed."""
        ...


def _quarantine(docs_dir: Path, decision_id: str, run_ts: str) -> None:
    """Move a decision's folder into the quarantine (never deletes).

    Args:
        docs_dir: Document store root.
        decision_id: Decision whose folder is moved.
        run_ts: Run timestamp naming the quarantine subfolder.

    Raises:
        OSError: If the target exists or the move fails.
    """
    source = docs_dir / decision_id
    if not source.is_dir():
        return
    target = docs_dir / QUARANTINE_DIR / run_ts / decision_id
    if target.exists():
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _apply_one(
    item: PlanItem, repo: RegistryMerger, store: MasterDescriptorStore,
    docs_dir: Path, run_ts: str,
) -> PlanItem:
    """Execute one delete: master entry, folder, then the DB merge into K.

    Args:
        item: A plan item with action ``delete``.
        repo: Registry repository.
        store: Master store.
        docs_dir: Document store root.
        run_ts: Run timestamp for the quarantine folder.

    Returns:
        The item with its final outcome and the actual merge counts.
    """
    d_id = item.decision_id
    try:
        if item.master_entry_removed and store.remove(d_id):
            store.flush()
    except (OSError, ValueError):
        logger.exception("%s: master entry removal failed", d_id)
        return item.model_copy(update={"outcome": "master_failed"})
    try:
        _quarantine(docs_dir, d_id, run_ts)
    except OSError:
        logger.exception("%s: folder move failed; rows kept (re-run to resume)", d_id)
        return item.model_copy(update={"outcome": "move_failed"})
    try:
        counts = repo.merge_decision(d_id, item.duplicate_of)
    except (SQLAlchemyError, ValueError):
        logger.exception("%s: merge into %s failed and rolled back (re-run to resume)",
                         d_id, item.duplicate_of)
        return item.model_copy(update={"outcome": "db_failed"})
    return item.model_copy(update={
        "outcome": "done", "documents_repointed": counts.documents_repointed,
        "case_links_moved": counts.case_links_moved})


def apply_plan(
    plan: Sequence[PlanItem], repo: RegistryMerger, store: MasterDescriptorStore,
    docs_dir: Path, run_ts: str,
) -> list[PlanItem]:
    """Execute the delete actions of a plan.

    Args:
        plan: The plan.
        repo: Registry repository.
        store: Master store.
        docs_dir: Document store root.
        run_ts: Run timestamp for the quarantine folder.

    Returns:
        The plan with final outcomes: ``done``, a failure outcome, ``refused``
        or ``none`` for rows that need no action.
    """
    return [_apply_one(i, repo, store, docs_dir, run_ts) if i.action == "delete"
            else i.model_copy(update={"outcome": "refused" if i.action == "refused" else "none"})
            for i in plan]


def write_report(plan: Sequence[PlanItem], directory: Path, stamp: str) -> Path:
    """Write the CSV report atomically (temp file + replace).

    Args:
        plan: The plan with outcomes.
        directory: Report directory (created).
        stamp: Timestamp for the file name.

    Returns:
        The report path.

    Raises:
        OSError: If the file cannot be written.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{REPORT_PREFIX}{stamp}.csv"
    frame = pl.DataFrame(
        {c: [getattr(i, c) for i in plan] for c in REPORT_COLUMNS},
        schema={"decision_id": pl.String, "duplicate_of": pl.String,
                "signal": pl.String, "action": pl.String,
                "documents_repointed": pl.Int64, "case_links_moved": pl.Int64,
                "files_moved": pl.Int64, "master_entry_removed": pl.Boolean,
                "outcome": pl.String})
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    os.close(fd)
    try:
        frame.write_csv(tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments (default ``sys.argv[1:]``).

    Returns:
        The namespace.

    Raises:
        SystemExit: On a usage error (code 2).
    """
    p = argparse.ArgumentParser(
        description="De-duplicate registry decisions against the master "
                    "(dry run unless --apply).")
    p.add_argument("--apply", action="store_true",
                   help="Merge duplicates into their master key (re-point "
                        "documents, move case links, delete the decision row), "
                        "quarantine folders, remove included master entries. "
                        "Default: dry run (report only).")
    p.add_argument("--include-master-key", action="append", default=[], metavar="KEY",
                   help="Master key created by the downloader to treat as a "
                        "candidate (repeatable), e.g. CELEX-31992D0568.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the clean-up.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).

    Returns:
        0 on success, 1 on a database/I-O error, 2 on a usage error or
        unmigrated registry tables.
    """
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    stamp = time.strftime("%Y%m%d-%H%M%S")
    docs_dir = Path(settings.CELEX_DOCS_DIR)
    engine = make_engine()
    try:
        try:
            DocumentRegistryRepository(engine)
        except RuntimeError as exc:
            if isinstance(exc.__cause__, OperationalError):
                logger.error("database unreachable: %s", exc.__cause__)
                return EXIT_IO
            logger.error("document registry not ready: %s", exc)
            return EXIT_USAGE
        repo = RegistryDedupeRepository(engine)
        try:
            store = MasterDescriptorStore(Path(settings.MASTER_DOC_DESCRIPTORS_PATH))
            plan = build_plan(repo.load_decisions(args.include_master_key), store,
                              docs_dir, args.include_master_key, repo.referenced)
            if args.apply:
                plan = apply_plan(plan, repo, store, docs_dir, stamp)
            report = write_report(plan, Path(settings.REPORT_PATH), stamp)
        except (SQLAlchemyError, OSError, ValueError) as exc:
            logger.error("dedupe failed: %s", exc)
            return EXIT_IO
    finally:
        engine.dispose()
    counts = Counter(i.action for i in plan)
    failed = [i for i in plan if i.outcome.endswith("_failed")]
    logger.info("%s: %d candidates with evidence; actions %s; delete by signal %s; "
                "outcomes %s; report %s",
                "applied" if args.apply else "dry run", len(plan), dict(counts),
                dict(Counter(i.signal for i in plan if i.action == "delete")),
                dict(Counter(i.outcome for i in plan)), report)
    return EXIT_IO if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
