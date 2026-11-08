"""
Persistence for the PDF-to-Markdown conversion batch.

Reads the conversion queue from ``document_registry``, fills a missing
registry language and appends one ``document_conversion_audit`` row per
attempt. Every write runs in its own transaction (commit per file).

Exports:
    ConversionNotReady -- migration not applied.
    ConversionRepository -- queue read, language update, audit append.
"""

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config.settings import settings
from pipelines.convert.models import ConversionAuditRecord, ConversionItem
from pipelines.rag.pgvector.session import column_exists

logger = logging.getLogger(__name__)

_REGISTRY_TABLE = f"{settings.DB_SCHEMA}.document_registry"
_AUDIT_TABLE = f"{settings.DB_SCHEMA}.document_conversion_audit"
_DECISION_TABLE = f"{settings.DB_SCHEMA}.decision_registry"
_MD_COLUMN = "local_file_md"
_MIGRATION = "src/pipelines/rag/pgvector/migrate_document_conversion.sql"

_QUEUE_BASE = f"""
    SELECT document_id, decision_id, is_source, local_file_pdf,
           local_file_md, language,
           (SELECT s.local_file_pdf FROM {_REGISTRY_TABLE} s
            WHERE s.decision_id = r.decision_id AND s.is_source) AS source_pdf,
           r.original_file_md,
           (r.is_source AND EXISTS (
                SELECT 1 FROM {_DECISION_TABLE} d
                WHERE d.decision_id = r.decision_id AND d.language_translated = 'en'
           )) AS translated
    FROM {_REGISTRY_TABLE} r
    WHERE (is_source OR is_selected) AND local_file_pdf IS NOT NULL
"""
_QUEUE_IDS = " AND decision_id = ANY(:ids)"
_QUEUE_ORDER = " ORDER BY decision_id, document_id"
_QUEUE_LIMIT = " LIMIT :limit"

_SET_LANGUAGE = text(
    f"""
    UPDATE {_REGISTRY_TABLE}
    SET language = :language
    WHERE document_id = :document_id
      AND (language IS NULL OR language = 'NA')
    """
)

_AUDIT_COLUMNS: tuple[str, ...] = tuple(ConversionAuditRecord.model_fields)
_INSERT_AUDIT = text(
    f"""
    INSERT INTO {_AUDIT_TABLE} ({", ".join(_AUDIT_COLUMNS)})
    VALUES (CAST(:run_id AS uuid), {", ".join(f":{c}" for c in _AUDIT_COLUMNS[1:])})
    """
)


class ConversionNotReady(RuntimeError):
    """Raised when the conversion migration has not been applied."""


class ConversionRepository:
    """DAO for the conversion queue, registry language and audit ledger.

    Attributes:
        engine: Injected SQLAlchemy engine (shared pool).
    """

    engine: Engine

    def __init__(self, engine: Engine) -> None:
        """Bind the engine.

        Args:
            engine: SQLAlchemy engine, e.g. from ``session.make_engine``.
        """
        self.engine = engine

    def probe(self) -> None:
        """Check that the registry column and the audit table exist.

        Raises:
            ConversionNotReady: When ``local_file_md`` or the audit table is
                missing, naming the migration to apply.
        """
        with self.engine.connect() as conn:
            has_column = column_exists(conn, _REGISTRY_TABLE, _MD_COLUMN)
            has_table = (
                conn.execute(
                    text("SELECT to_regclass(:name)"), {"name": _AUDIT_TABLE}
                ).scalar()
                is not None
            )
        if not (has_column and has_table):
            raise ConversionNotReady(
                f"{_REGISTRY_TABLE}.{_MD_COLUMN} or {_AUDIT_TABLE} is missing. "
                f"Apply {_MIGRATION} and schema_comments.sql with psql."
            )

    def queue(
        self, decision_ids: list[str] | None, limit: int | None
    ) -> list[ConversionItem]:
        """Read the registry rows in conversion scope.

        Rows with a NULL ``local_file_md`` are included so the caller can
        audit them as ``no_target_path``.

        Args:
            decision_ids: Restrict to these decisions; None for all.
            limit: Maximum number of items; None for no limit.

        Returns:
            Items ordered by ``decision_id, document_id``.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        sql = _QUEUE_BASE
        params: dict[str, object] = {}
        if decision_ids is not None:
            sql += _QUEUE_IDS
            params["ids"] = list(decision_ids)
        sql += _QUEUE_ORDER
        if limit is not None:
            sql += _QUEUE_LIMIT
            params["limit"] = limit
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [ConversionItem.model_validate(dict(r._mapping)) for r in rows]

    def set_language(self, document_id: str, language: str) -> None:
        """Set the registry language if it is still unset (NULL or 'NA').

        Args:
            document_id: Registry document id.
            language: Upper-case language code to store.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        with self.engine.begin() as conn:
            conn.execute(
                _SET_LANGUAGE, {"document_id": document_id, "language": language}
            )

    def record(self, audit: ConversionAuditRecord) -> None:
        """Append one audit row in its own transaction.

        Args:
            audit: The audit record; ``id`` and ``created_at`` are DB defaults.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        params = audit.model_dump(mode="json")
        with self.engine.begin() as conn:
            conn.execute(_INSERT_AUDIT, params)
        logger.debug("Audited document=%s outcome=%s", audit.document_id, audit.outcome)
