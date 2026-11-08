"""
Persistence layer for the downloader's audit ledger and failures view.

Appends one ``document_download_audit`` row per candidate attempt (and per
row-level mapper failure) and reads ``document_download_failures_vw`` for
``--report-failures``: every candidate whose latest attempt failed, the
no_pdf_source rows of decisions still lacking a usable document, and
row-level failures no later run has mapped.

The audit table is append-only: one row per candidate per run, keyed by a
generated uuid, so it has no upsert target. The registry upserts live in
:mod:`pipelines.rag.pgvector.document_registry_repository`.

Follows the **Provisioner pattern**: the engine is injected and the table
and view are probed at construction.

Exports:
    DownloadAuditRepository -- audit writes and the failures read.
"""

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config.settings import settings
from pipelines.download.models import FetchResult, Outcome, RowFailure

logger = logging.getLogger(__name__)

_AUDIT_TABLE = f"{settings.DB_SCHEMA}.document_download_audit"
_FAILURES_VIEW = f"{settings.DB_SCHEMA}.document_download_failures_vw"
_RELATIONS: tuple[str, ...] = (_AUDIT_TABLE, _FAILURES_VIEW)

_MIGRATION = "src/pipelines/rag/pgvector/migrate_document_registry.sql"

_INSERT_AUDIT = text(
    f"""
    INSERT INTO {_AUDIT_TABLE}
        (run_id, decision_id, document_id, source_url, outcome,
         failure_reason, http_status, content_type, size_bytes, sha256,
         duration_ms, loader_file, loader_row)
    VALUES
        (CAST(:run_id AS uuid), :decision_id, :document_id, :source_url,
         :outcome, :failure_reason, :http_status, :content_type, :size_bytes,
         :sha256, :duration_ms, :loader_file, :loader_row)
    """
)

_SELECT_FAILURES = text(
    f"""
    SELECT run_id, decision_id, celex_number, title, document_id, source_url,
           outcome, failure_reason, http_status, loader_file, loader_row,
           created_at
    FROM {_FAILURES_VIEW}
    WHERE CAST(:run_id AS uuid) IS NULL OR run_id = CAST(:run_id AS uuid)
    ORDER BY created_at DESC, decision_id, document_id
    """
)


class DownloadAuditRepository:
    """DAO for ``document_download_audit`` and its failures view.

    Attributes:
        engine: Injected SQLAlchemy engine (shared pool).
    """

    engine: Engine

    def __init__(self, engine: Engine) -> None:
        """Bind the engine and probe the audit table and failures view.

        Args:
            engine: SQLAlchemy engine, e.g. from ``session.make_engine``.

        Raises:
            RuntimeError: If the table or the view is missing.
        """
        self.engine = engine
        self._probe_table()

    def _probe_table(self) -> None:
        """Raise immediately if the audit table or failures view is absent.

        Raises:
            RuntimeError: When a relation does not exist, naming the
                migration that creates it.
        """
        for relation in _RELATIONS:
            try:
                with self.engine.connect() as conn:
                    conn.execute(text(f"SELECT 1 FROM {relation} LIMIT 0"))
            except Exception as exc:
                raise RuntimeError(
                    f"Relation {relation} does not exist. Apply {_MIGRATION} "
                    "and schema_comments.sql with psql."
                ) from exc

    def record(
        self,
        run_id: UUID,
        decision_id: str | None,
        result: FetchResult | RowFailure,
        loader_file: str,
        loader_row: int | None,
    ) -> None:
        """Append one audit row.

        A :class:`RowFailure` is recorded as outcome ``failed`` with its
        reason and no document fields; ``decision_id`` is then normally None.

        Args:
            run_id: Id of the current batch run.
            decision_id: Decision the attempt belongs to, None for a
                row-level mapper failure.
            result: Fetch / existence-check result, or a row failure.
            loader_file: Loader file the row came from.
            loader_row: Row number within the loader file, None if unknown.

        Returns:
            None.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        params: dict[str, object] = {
            "run_id": str(run_id),
            "decision_id": decision_id,
            "loader_file": loader_file,
            "loader_row": loader_row,
        }
        match result:
            case FetchResult():
                params |= {
                    # Decision-level rows (no_pdf_source) carry no document.
                    "document_id": result.document_id or None,
                    "source_url": result.source_url or None,
                    "outcome": result.outcome.value,
                    "failure_reason": result.failure_reason,
                    "http_status": result.http_status,
                    "content_type": result.content_type,
                    "size_bytes": result.size_bytes,
                    "sha256": result.sha256,
                    "duration_ms": result.duration_ms,
                }
            case RowFailure():
                params |= {
                    "document_id": None,
                    "source_url": None,
                    "outcome": Outcome.FAILED.value,
                    "failure_reason": result.reason,
                    "http_status": None,
                    "content_type": None,
                    "size_bytes": None,
                    "sha256": None,
                    "duration_ms": None,
                }
        with self.engine.begin() as conn:
            conn.execute(_INSERT_AUDIT, params)
        logger.debug(
            "Audited run=%s decision=%s outcome=%s", run_id, decision_id, params["outcome"]
        )

    def failures(self, run_id: UUID | None) -> list[dict[str, object]]:
        """Return the rows of the failures view, newest first.

        Args:
            run_id: Restrict to failures whose latest attempt belongs to this
                run; None returns every outstanding failure.

        Returns:
            One dict per view row with keys ``run_id``, ``decision_id``,
            ``celex_number``, ``title``, ``document_id``, ``source_url``,
            ``outcome``, ``failure_reason``, ``http_status``,
            ``loader_file``, ``loader_row`` and ``created_at``.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        params = {"run_id": str(run_id) if run_id is not None else None}
        with self.engine.connect() as conn:
            rows = conn.execute(_SELECT_FAILURES, params).fetchall()
        return [dict(row._mapping) for row in rows]
