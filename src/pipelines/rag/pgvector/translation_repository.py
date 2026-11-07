"""
Persistence for the Markdown translation batch.

Reads the translation queue (non-English source rows of
``document_registry``), fills a missing registry language, commits a finished
translation (source row switched to the English Markdown, original kept in
``original_file_md``, decision marked ``language_translated = 'en'``) and
appends one ``document_translation_audit`` row per item.

Exports:
    TranslationNotReady -- migration not applied.
    TranslationRepository -- queue read, language update, commit, audit append.
"""

import logging
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config.settings import settings
from pipelines.rag.pgvector.session import column_exists
from pipelines.translate.models import TranslationAuditRecord, TranslationItem

logger = logging.getLogger(__name__)

_REGISTRY_TABLE = f"{settings.DB_SCHEMA}.document_registry"
_DECISION_TABLE = f"{settings.DB_SCHEMA}.decision_registry"
_AUDIT_TABLE = f"{settings.DB_SCHEMA}.document_translation_audit"
_ORIGINAL_COLUMN = "original_file_md"
_MIGRATION = "src/pipelines/rag/pgvector/migrate_document_translation.sql"

_QUEUE_BASE = f"""
    SELECT r.document_id, r.decision_id, r.language, r.local_file_md,
           r.original_file_md, d.language_translated
    FROM {_REGISTRY_TABLE} r
    JOIN {_DECISION_TABLE} d ON d.decision_id = r.decision_id
    WHERE r.is_source AND r.local_file_md IS NOT NULL
      AND coalesce(r.language, '') <> 'EN'
"""
_QUEUE_LAZY = " AND coalesce(d.language_translated, 'NA') <> 'en'"
_QUEUE_IDS = " AND r.decision_id = ANY(:ids)"
_QUEUE_ORDER = " ORDER BY r.decision_id"

_SET_LANGUAGE = text(
    f"""
    UPDATE {_REGISTRY_TABLE}
    SET language = :language
    WHERE document_id = :document_id
      AND (language IS NULL OR language = 'NA')
    """
)
_SWITCH_SOURCE = text(
    f"""
    UPDATE {_REGISTRY_TABLE}
    SET local_file_md = :target,
        original_file_md = coalesce(original_file_md, :original),
        updated_at = now()
    WHERE document_id = :document_id
    """
)
_MARK_TRANSLATED = text(
    f"UPDATE {_DECISION_TABLE} SET language_translated = 'en' "
    "WHERE decision_id = :decision_id"
)
_AUDIT_COLUMNS: tuple[str, ...] = tuple(TranslationAuditRecord.model_fields)
_INSERT_AUDIT = text(
    f"""
    INSERT INTO {_AUDIT_TABLE} ({", ".join(_AUDIT_COLUMNS)})
    VALUES (CAST(:run_id AS uuid), {", ".join(f":{c}" for c in _AUDIT_COLUMNS[1:])})
    """
)


class TranslationNotReady(RuntimeError):
    """Raised when the translation migration has not been applied."""


class TranslationRepository:
    """DAO for the translation queue, registry switch and audit ledger.

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
        """Check that ``original_file_md`` and the audit table exist.

        Raises:
            TranslationNotReady: When either is missing, naming the migration.
        """
        with self.engine.connect() as conn:
            has_column = column_exists(conn, _REGISTRY_TABLE, _ORIGINAL_COLUMN)
            has_table = (
                conn.execute(text("SELECT to_regclass(:name)"), {"name": _AUDIT_TABLE}).scalar()
                is not None
            )
        if not (has_column and has_table):
            raise TranslationNotReady(
                f"{_REGISTRY_TABLE}.{_ORIGINAL_COLUMN} or {_AUDIT_TABLE} is missing. "
                f"Apply {_MIGRATION} and schema_comments.sql with psql."
            )

    def queue(self, decision_ids: list[str] | None, force: bool) -> list[TranslationItem]:
        """Read the non-English source rows in translation scope.

        Args:
            decision_ids: Restrict to these decisions; None for all.
            force: Include decisions already marked ``language_translated = 'en'``.

        Returns:
            Items ordered by ``decision_id``.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        sql = _QUEUE_BASE + ("" if force else _QUEUE_LAZY)
        params: dict[str, object] = {}
        if decision_ids is not None:
            sql += _QUEUE_IDS
            params["ids"] = list(decision_ids)
        sql += _QUEUE_ORDER
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [TranslationItem.model_validate(dict(r._mapping)) for r in rows]

    def set_language(self, document_id: str, language: str) -> None:
        """Set the registry language if it is still unset (NULL or 'NA').

        Args:
            document_id: Registry document id.
            language: Upper-case language code.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        with self.engine.begin() as conn:
            conn.execute(_SET_LANGUAGE, {"document_id": document_id, "language": language})

    def commit_translation(
        self,
        item: TranslationItem,
        target_md: str,
        original_md: str,
        before_commit: Callable[[], None],
    ) -> None:
        """Switch the source row to the English Markdown in one transaction.

        ``original_file_md`` keeps an existing value (a forced re-translation
        never replaces the original with an English file).
        ``before_commit`` runs inside the transaction after both updates, so
        the master flush and the registry commit stand or fall together.

        Args:
            item: The translated queue item.
            target_md: English Markdown path.
            original_md: Foreign-language Markdown that was translated.
            before_commit: Called inside the transaction before COMMIT.

        Raises:
            Exception: Anything ``before_commit`` raises (the transaction is
                rolled back) or a database error.
        """
        with self.engine.begin() as conn:
            conn.execute(_SWITCH_SOURCE, {
                "target": target_md, "original": original_md, "document_id": item.document_id})
            conn.execute(_MARK_TRANSLATED, {"decision_id": item.decision_id})
            before_commit()

    def record(self, audit: TranslationAuditRecord) -> None:
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
