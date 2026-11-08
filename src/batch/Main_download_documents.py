"""Batch entry point of the HTTP document downloader.

Reads a loader file (csv, xlsx or txt), maps it to decisions, downloads the
selected PDFs and registers them in the document registry, the master
descriptor JSON and the download audit table. Logging is configured by
``config.settings`` on import (``logging_config``), like ``BatchJobRunner``.

Exit codes: 0 success, 1 failed candidates or errored decisions (from
``RunSummary.exit_code``), 2 usage error, unreadable loader or registry
tables not yet migrated.
"""

import argparse
import json
import logging
import os
import tempfile
import uuid
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import polars as pl
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from pipelines.download.cellar import cellar_request_for
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.download.fetcher import HttpDocumentFetcher, make_client
from pipelines.download.mappers import (
    MAPPERS,
    FinalManifestMapper,
    build_mapper,
    mapper_for,
)
from pipelines.download.models import DecisionRequest, MapperResult, RowFailure
from pipelines.download.orchestrator import DownloadOrchestrator
from pipelines.download.master_backfill import CaseLookup, CelexCaseLookup
from pipelines.download.reader import read_loader
from pipelines.download.run_summary import RunSummary
from pipelines.download.selector import CandidateSelector
from pipelines.rag.pgvector.celex_identity_repository import CelexIdentityRepository
from pipelines.rag.pgvector.document_registry_repository import (
    DocumentRegistryRepository,
)
from pipelines.rag.pgvector.download_audit_repository import DownloadAuditRepository
from pipelines.rag.pgvector.session import make_engine

logger = logging.getLogger(__name__)

EXIT_USAGE = 2
MIGRATION_FILE = "migrate_document_registry.sql"
REPORT_COLUMNS = (
    "decision_id", "celex_number", "loader_file", "loader_row", "source_url",
    "outcome", "failure_reason", "http_status", "title",
)


class RegistryNotReady(RuntimeError):
    """The registry or audit tables are missing (migration not applied)."""


@runtime_checkable
class Runner(Protocol):
    """Orchestrator surface used by the CLI."""

    def run(
        self,
        decisions: list[DecisionRequest],
        *,
        run_id: UUID,
        force: bool,
        dry_run: bool,
        max_workers: int,
        failures: Sequence[RowFailure] = (),
        offline: bool = False,
    ) -> RunSummary:
        """Run the batch (see ``DownloadOrchestrator.run``)."""
        ...


@runtime_checkable
class FailureSource(Protocol):
    """Audit repository surface used by the failure report."""

    def failures(self, run_id: UUID | None) -> list[dict[str, object]]:
        """Return the failures view rows of a run."""
        ...


class Pipeline(BaseModel):
    """The DB- and network-bound collaborators of one run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    orchestrator: Runner = Field(description="Orchestrator running the batch.")
    audit: FailureSource = Field(description="Audit repository for the report.")
    client: Any = Field(default=None, description="httpx.Client to close.")
    engine: Any = Field(default=None, description="SQLAlchemy Engine to dispose.")

    def close(self) -> None:
        """Close the HTTP client and dispose the engine (idempotent).

        Each resource is released independently; a failure is logged as a
        warning and never raised, so the run result is not masked.
        """
        for label, release in (("http client", getattr(self.client, "close", None)),
                               ("db engine", getattr(self.engine, "dispose", None))):
            if release is None:
                continue
            try:
                release()
            except Exception:
                logger.warning("releasing %s failed", label, exc_info=True)


def _non_negative(text: str) -> int:
    """Parse a non-negative integer for argparse.

    Args:
        text: Raw argument.

    Returns:
        The integer.

    Raises:
        argparse.ArgumentTypeError: If not an integer or negative.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
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
    p = argparse.ArgumentParser(description="Download and register decision PDFs.")
    p.add_argument("--loader-file", type=Path, required=True,
                   help="Loader file (csv, xlsx or txt url list). With --mapper "
                        "master_backfill: the master descriptor JSON, which must "
                        "be settings.MASTER_DOC_DESCRIPTORS_PATH (writes go "
                        "there).")
    p.add_argument("--mapper", choices=sorted(MAPPERS), default=None,
                   help="Loader mapper. Default detected: final_manifest when "
                        "the loader has manifest_row_id and case_numbers "
                        "columns, url_list for .txt, else case_index. "
                        "master_backfill is never detected and requires "
                        "--offline.")
    p.add_argument("--file-manifest", type=Path,
                   default=Path(settings.DOWNLOAD_FILE_MANIFEST_PATH),
                   help="Per-file manifest (only read by the final_manifest mapper).")
    p.add_argument("--force", action="store_true",
                   help="Re-download files that already exist and are valid.")
    p.add_argument("--dry-run", action="store_true",
                   help="Plan and log only; write no file, master JSON or DB row.")
    p.add_argument("--offline", action="store_true",
                   help="Never fetch: register valid files already on disk, "
                        "fail missing ones with file_missing_offline.")
    p.add_argument("--initial-loader", type=Path, default=None,
                   help="Initial loader spreadsheet (only read by the "
                        "master_backfill mapper, whose --loader-file is the "
                        "master descriptor JSON; registry and master JSON "
                        "writes go to the settings paths).")
    p.add_argument("--limit", type=_non_negative, default=0,
                   help="Limit the number of decisions (after mapping). 0 for all. "
                        "Mapper row failures beyond the limit are still reported.")
    p.add_argument("--max-workers", type=int,
                   default=settings.DOWNLOAD_DEFAULT_MAX_WORKERS,
                   help="Concurrent download threads.")
    p.add_argument("--report-failures", type=Path, default=None,
                   help="Write this run's failures to a CSV (not in --dry-run).")
    return p.parse_args(argv)


def _load(args: argparse.Namespace) -> MapperResult:
    """Read and map the loader file.

    Args:
        args: Parsed arguments.

    Returns:
        Mapped decisions and row failures.

    Raises:
        OSError: If a file cannot be read.
        zipfile.BadZipFile: If an xlsx file is corrupt.
        ValueError: If a file or the mapper choice is unusable.
        polars.exceptions.PolarsError: If a table cannot be parsed.
    """
    if args.mapper is not None and mapper_for(args.loader_file, args.mapper).needs_master:
        return _load_master(args)
    df = read_loader(args.loader_file)
    cls = mapper_for(args.loader_file, args.mapper, df.columns)
    manifest = read_loader(args.file_manifest) if cls.needs_file_manifest else None
    name = args.mapper or ("final_manifest" if cls is FinalManifestMapper else None)
    mapper = build_mapper(args.loader_file, name, manifest, df.columns)
    return mapper.map(df, args.loader_file)


def _case_lookup() -> CaseLookup:
    """Snapshot ``celex_case_id`` (one read-only SELECT) into a case lookup.

    Returns:
        The lookup; the repository's engine is disposed after the read.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: If the table cannot be read.
    """
    repo = CelexIdentityRepository()
    try:
        return CelexCaseLookup(repo)
    finally:
        repo.engine.dispose()


def _load_master(args: argparse.Namespace) -> MapperResult:
    """Map the master descriptor JSON (``--loader-file``) for the backfill.

    Args:
        args: Parsed arguments (``--initial-loader`` is optional).

    Returns:
        Mapped decisions and row failures.

    Raises:
        OSError: If a file cannot be read.
        ValueError: If the file is not the settings master file, the JSON is
            invalid or has no ``documents`` object, or the case lookup fails.
        polars.exceptions.PolarsError: If the initial loader cannot be parsed.
    """
    if args.loader_file.resolve() != Path(settings.MASTER_DOC_DESCRIPTORS_PATH).resolve():
        raise ValueError(
            f"master_backfill reads and writes the settings master file "
            f"{settings.MASTER_DOC_DESCRIPTORS_PATH}; got {args.loader_file}")
    raw = json.loads(args.loader_file.read_text(encoding="utf-8"))
    documents = raw.get("documents") if isinstance(raw, dict) else None
    if not isinstance(documents, dict):
        raise ValueError(f"{args.loader_file} has no 'documents' object")
    initial = read_loader(args.initial_loader) if args.initial_loader else None
    try:
        cases = _case_lookup()
    except SQLAlchemyError as exc:
        raise ValueError(f"case lookup failed: {exc}") from exc
    mapper = build_mapper(args.loader_file, args.mapper, None, master=documents,
                          initial_loader=initial, cases=cases)
    return mapper.map(pl.DataFrame(), args.loader_file)


def _build_pipeline(args: argparse.Namespace) -> Pipeline:
    """Construct the repositories, fetcher, store and orchestrator.

    Args:
        args: Parsed arguments.

    Returns:
        The wired pipeline.

    Raises:
        RegistryNotReady: If the registry or audit tables are missing (the
            engine and client are released on any construction error).
    """
    engine = make_engine()
    client = None
    try:
        try:
            registry = DocumentRegistryRepository(engine)
            audit = DownloadAuditRepository(engine)
        except RuntimeError as exc:
            raise RegistryNotReady(str(exc)) from exc
        client = make_client()
        fetcher = HttpDocumentFetcher(
            client, max_retries=settings.DOWNLOAD_MAX_RETRIES,
            backoff_seconds=settings.DOWNLOAD_RETRY_BACKOFF_SECONDS,
            route=cellar_request_for if settings.DOWNLOAD_CELLAR_ENABLED else None)
        docs_dir = Path(settings.CELEX_DOCS_DIR)
        store = MasterDescriptorStore(Path(settings.MASTER_DOC_DESCRIPTORS_PATH))
        orchestrator = DownloadOrchestrator(
            CandidateSelector(), fetcher, store, registry, audit, docs_dir)
    except BaseException:
        if client is not None:
            client.close()
        engine.dispose()
        raise
    return Pipeline(orchestrator=orchestrator, audit=audit, client=client,
                    engine=engine)


def _log_summary(summary: RunSummary) -> None:
    """Log the end-of-run counters.

    Args:
        summary: Run summary.
    """
    logger.info("run %s finished%s", summary.run_id,
                " (DRY RUN, planned counts, nothing written)" if summary.dry_run
                else "")
    for outcome, count in summary.outcomes.items():
        logger.info("  outcome %-20s %d", outcome.value, count)
    for reason, count in sorted(summary.failure_reasons.items()):
        logger.info("  failure reason %-30s %d", reason, count)
    logger.info("  row failures %d, review reasons %d", summary.row_failures,
                summary.review_reasons)
    logger.info("  decisions registered %d, skipped %d, errored %d",
                summary.decisions_processed, summary.decisions_skipped,
                summary.decisions_errored)


def _write_report(rows: list[dict[str, object]], path: Path) -> None:
    """Write the failure rows as a UTF-8 CSV via a temp file and os.replace.

    Args:
        rows: Rows of the failures view.
        path: Target CSV path; parent directories are created.

    Raises:
        OSError: If the file cannot be written.
    """
    frame = pl.DataFrame(
        {c: [None if r.get(c) is None else str(r[c]) for r in rows]
         for c in REPORT_COLUMNS},
        schema=dict.fromkeys(REPORT_COLUMNS, pl.String))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    os.close(fd)
    try:
        frame.write_csv(tmp_name)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    logger.info("wrote %d failure rows to %s", len(rows), path)


def main(argv: list[str] | None = None) -> int:
    """Run the download batch.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).

    Returns:
        0 on success, 1 when candidates failed or decisions errored, 2 on a
        usage error (including a master mapper without ``--offline``), an
        unreadable loader or missing registry tables.
    """
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if args.mapper is not None and MAPPERS[args.mapper].needs_master \
            and not args.offline:
        logger.error("--mapper %s requires --offline: a backfill never downloads",
                     args.mapper)
        return EXIT_USAGE
    if not args.loader_file.is_file():
        logger.error("loader file not found: %s", args.loader_file)
        return EXIT_USAGE
    try:
        mapped = _load(args)
    except (OSError, ValueError, zipfile.BadZipFile,
            pl.exceptions.PolarsError) as exc:
        logger.error("cannot read or map loader %s: %s", args.loader_file, exc)
        return EXIT_USAGE
    decisions = mapped.decisions[: args.limit] if args.limit > 0 else mapped.decisions
    logger.info("mapped %d decisions, %d row failures; using %d", len(mapped.decisions),
                len(mapped.failures), len(decisions))
    try:
        pipeline = _build_pipeline(args)
    except RegistryNotReady as exc:
        logger.error("document registry not ready (%s): apply %s first", exc,
                     MIGRATION_FILE)
        return EXIT_USAGE
    run_id = uuid.uuid4()
    logger.info("run %s: %d decisions (%d mapper row failures)%s", run_id,
                len(decisions), len(mapped.failures),
                (" [dry run]" if args.dry_run else "")
                + (" [offline]" if args.offline else ""))
    try:
        summary = pipeline.orchestrator.run(
            decisions, run_id=run_id, force=args.force, dry_run=args.dry_run,
            max_workers=args.max_workers, failures=mapped.failures,
            offline=args.offline)
        _log_summary(summary)
        if args.report_failures is not None:
            if args.dry_run:
                logger.warning("--report-failures ignored in --dry-run: no audit rows")
            else:
                _write_report(pipeline.audit.failures(run_id), args.report_failures)
    finally:
        pipeline.close()
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
