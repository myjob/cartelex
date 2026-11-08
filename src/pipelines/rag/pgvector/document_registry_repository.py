"""
Persistence layer for the downloader's case / decision / document registry.

Writes the tables created by ``migrate_document_registry.sql``:
``competition_case``, ``decision_registry``, ``competition_case_decision``
and ``document_registry`` (schema ``settings.DB_SCHEMA``). Every write is an
upsert on the table's primary key (``ON CONFLICT ... DO UPDATE``), so a
re-run heals rather than duplicates.

Write order matters because of the foreign keys: ``upsert_decision`` first,
then ``upsert_cases_and_links`` and ``upsert_document``.

Follows the **Provisioner pattern**: the engine is injected so the
repository shares a connection pool with its siblings, and the tables are
probed at construction.

Exports:
    DocumentRegistryRepository -- registry writes, plus the read-only
        lookups of :mod:`pipelines.rag.pgvector.document_registry_reads`.
    DocumentOwnershipConflict -- a document is already owned by another
        decision.
    REGISTRY_MIGRATION -- path of the migration that creates the tables.

The lineage writers' cache over ``source_document_id`` lives in
:mod:`pipelines.rag.pgvector.source_document_resolver`.
"""

import datetime as dt
import json
import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from pipelines.download.identifiers import celex_key
from pipelines.download.local_pdf import markdown_path_for
from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    FetchResult,
    Outcome,
)
from pipelines.download.selector import Selection
from pipelines.rag.pgvector.document_registry_reads import DocumentRegistryReads

logger = logging.getLogger(__name__)

_CASE_TABLE = f"{settings.DB_SCHEMA}.competition_case"
_DECISION_TABLE = f"{settings.DB_SCHEMA}.decision_registry"
_LINK_TABLE = f"{settings.DB_SCHEMA}.competition_case_decision"
_DOCUMENT_TABLE = f"{settings.DB_SCHEMA}.document_registry"
_TABLES: tuple[str, ...] = (_CASE_TABLE, _DECISION_TABLE, _LINK_TABLE, _DOCUMENT_TABLE)

_SOURCE_URL_CONSTRAINT = "uq_document_registry_source_url"

REGISTRY_MIGRATION = "src/pipelines/rag/pgvector/migrate_document_registry.sql"

# Outcomes that leave a valid file on disk and may therefore be registered.
_REGISTRABLE: frozenset[Outcome] = frozenset(
    {Outcome.DOWNLOADED, Outcome.REGISTERED_EXISTING, Outcome.SKIPPED_EXISTS}
)

_UPSERT_DECISION = text(
    f"""
    INSERT INTO {_DECISION_TABLE}
        (decision_id, celex_number, decision_date, title, parties, document_class,
         selection_status, language_init, review_reasons, meta, loader_file)
    VALUES
        (:decision_id, :celex_number, :decision_date, :title, :parties,
         :document_class, :selection_status, :language_init,
         CAST(:review_reasons AS text[]), CAST(:meta AS jsonb), :loader_file)
    ON CONFLICT (decision_id) DO UPDATE SET
        -- A decision the source-document step resolved (selected) is not
        -- reset when a later loader run still sees it as undecided.
        celex_number     = EXCLUDED.celex_number,
        decision_date    = CASE WHEN decision_registry.selection_status = 'selected' AND EXCLUDED.selection_status = 'undecided' THEN decision_registry.decision_date ELSE EXCLUDED.decision_date END,
        title            = EXCLUDED.title,
        parties          = EXCLUDED.parties,
        document_class   = EXCLUDED.document_class,
        selection_status = CASE WHEN decision_registry.selection_status = 'selected' AND EXCLUDED.selection_status = 'undecided' THEN decision_registry.selection_status ELSE EXCLUDED.selection_status END,
        language_init    = CASE WHEN decision_registry.selection_status = 'selected' AND EXCLUDED.selection_status = 'undecided' THEN decision_registry.language_init ELSE EXCLUDED.language_init END,
        review_reasons   = EXCLUDED.review_reasons,
        meta             = EXCLUDED.meta,
        loader_file      = EXCLUDED.loader_file,
        updated_at       = now()
    """
)

_UPSERT_CASE = text(
    f"""
    INSERT INTO {_CASE_TABLE} (case_id)
    VALUES (:case_id)
    ON CONFLICT (case_id) DO UPDATE SET
        updated_at = now()
    """
)

_UPSERT_LINK = text(
    f"""
    INSERT INTO {_LINK_TABLE} (case_id, decision_id)
    VALUES (:case_id, :decision_id)
    ON CONFLICT (case_id, decision_id) DO UPDATE SET
        updated_at = now()
    """
)

# Runs before a source upsert, in the same transaction, so the partial unique
# index uq_document_registry_one_source never sees two sources.
_CLEAR_OTHER_SOURCES = text(
    f"""
    UPDATE {_DOCUMENT_TABLE}
    SET is_source = false, updated_at = now()
    WHERE decision_id = :decision_id
      AND is_source
      AND document_id <> :document_id
    """
)

# File-evidence columns are COALESCEd so a metadata-only upsert (no fetch)
# never erases what an earlier download recorded. is_source is sticky: only
# _CLEAR_OTHER_SOURCES (a new source of the same decision) clears it, so a
# non-source re-upsert never leaves the decision without a source. The target
# Markdown path follows the PDF: it is re-derived when the PDF path changes,
# otherwise an already stored path is kept.
_UPSERT_DOCUMENT = text(
    f"""
    INSERT INTO {_DOCUMENT_TABLE} AS doc
        (document_id, decision_id, source_url, file_format, origin, language,
         is_selected, is_source, local_file_pdf, local_file_md, sha256,
         size_bytes, content_type, downloaded_at)
    VALUES
        (:document_id, :decision_id, :source_url, :file_format, :origin,
         :language, :is_selected, :is_source, :local_file_pdf, :local_file_md,
         :sha256, :size_bytes, :content_type, :downloaded_at)
    ON CONFLICT (document_id) DO UPDATE SET
        decision_id    = EXCLUDED.decision_id,
        source_url     = EXCLUDED.source_url,
        file_format    = EXCLUDED.file_format,
        origin         = EXCLUDED.origin,
        language       = EXCLUDED.language,
        is_selected    = EXCLUDED.is_selected,
        is_source      = doc.is_source OR EXCLUDED.is_source,
        local_file_pdf = COALESCE(EXCLUDED.local_file_pdf, doc.local_file_pdf),
        local_file_md  = CASE
            WHEN EXCLUDED.local_file_pdf IS NOT NULL
                 AND EXCLUDED.local_file_pdf IS DISTINCT FROM doc.local_file_pdf
            THEN EXCLUDED.local_file_md
            ELSE COALESCE(doc.local_file_md, EXCLUDED.local_file_md)
        END,
        sha256         = COALESCE(EXCLUDED.sha256, doc.sha256),
        size_bytes     = COALESCE(EXCLUDED.size_bytes, doc.size_bytes),
        content_type   = COALESCE(EXCLUDED.content_type, doc.content_type),
        downloaded_at  = COALESCE(EXCLUDED.downloaded_at, doc.downloaded_at),
        updated_at     = now()
    WHERE doc.decision_id = EXCLUDED.decision_id
    RETURNING document_id
    """
)

_OWNER_BY_ID = text(
    f"SELECT decision_id FROM {_DOCUMENT_TABLE} WHERE document_id = :document_id"
)

_OWNER_BY_URL = text(
    f"SELECT document_id, decision_id FROM {_DOCUMENT_TABLE} "
    "WHERE source_url = :source_url"
)

class DocumentOwnershipConflict(ValueError):
    """A document id or source URL is already registered under another decision.

    Raised instead of silently moving the document; the orchestrator audits
    the candidate as failed (``document_owned_by_other_decision``).
    """


class DocumentRegistryRepository(DocumentRegistryReads):
    """DAO for the downloader's case, decision, link and document tables.

    The read-only lookups (source document, content, registration, alias
    URLs) are inherited from ``DocumentRegistryReads``.

    Attributes:
        engine: Injected SQLAlchemy engine (shared pool).
    """

    engine: Engine

    def __init__(self, engine: Engine) -> None:
        """Bind the engine and probe the registry tables.

        Args:
            engine: SQLAlchemy engine, e.g. from ``session.make_engine``.

        Raises:
            RuntimeError: If any registry table is missing.
        """
        self.engine = engine
        self._probe_table()

    def _probe_table(self) -> None:
        """Raise immediately if a registry table is absent.

        Raises:
            RuntimeError: When a table does not exist, naming the migration
                that creates it.
        """
        for table in _TABLES:
            try:
                with self.engine.connect() as conn:
                    conn.execute(text(f"SELECT 1 FROM {table} LIMIT 0"))
            except Exception as exc:
                raise RuntimeError(
                    f"Table {table} does not exist. Apply {REGISTRY_MIGRATION} "
                    "and schema_comments.sql with psql."
                ) from exc

    def upsert_decision(self, decision: DecisionRequest, selection: Selection) -> None:
        """Insert or update one decision row.

        Upserts on ``decision_id``. ``celex_number`` is stored in the prefixed
        :func:`~pipelines.download.identifiers.celex_key` spelling
        (``CELEX-31964D0233``); None stays None. ``language_translated`` is written only
        on insert (column default ``'NA'``) and never overwritten: it belongs
        to the translate service.

        Args:
            decision: Mapped decision; its loader meta and review reasons are
                stored verbatim.
            selection: Candidate selection of the decision; provides
                ``selection_status`` and ``language_init``.

        Returns:
            None.

        Raises:
            UnsafeIdentifier: If ``decision.celex_number`` is not a usable CELEX
                number.
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        params = {
            "decision_id": decision.decision_id,
            "celex_number": (
                celex_key(decision.celex_number) if decision.celex_number is not None else None
            ),
            "decision_date": decision.decision_date,
            "title": decision.title,
            "parties": decision.parties,
            "document_class": decision.document_class,
            "selection_status": selection.status.value,
            "language_init": selection.language_init,
            "review_reasons": list(decision.review_reasons),
            "meta": json.dumps(decision.meta, ensure_ascii=False),
            "loader_file": decision.loader_file,
        }
        with self.engine.begin() as conn:
            conn.execute(_UPSERT_DECISION, params)
        logger.debug("Upserted decision %s (%s)", decision.decision_id, selection.status)

    def upsert_cases_and_links(self, decision: DecisionRequest) -> None:
        """Upsert the decision's competition cases and case-decision links.

        Additive only: links missing from this loader row are kept, because
        another loader (e.g. a plain URL list) may know fewer case ids for
        the same decision. Cases and links share one transaction. The
        decision row must already exist (foreign key).

        Args:
            decision: Decision whose ``case_ids`` are linked; no-op when it
                has none.

        Returns:
            None.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors, e.g. a
                missing decision row.
        """
        if not decision.case_ids:
            return
        with self.engine.begin() as conn:
            for case_id in decision.case_ids:
                conn.execute(_UPSERT_CASE, {"case_id": case_id})
                conn.execute(
                    _UPSERT_LINK,
                    {"case_id": case_id, "decision_id": decision.decision_id},
                )
        logger.debug(
            "Linked decision %s to %d case(s)", decision.decision_id, len(decision.case_ids)
        )

    def upsert_document(
        self,
        document: CandidateDocument,
        fetch: FetchResult | None,
        is_selected: bool,
        *,
        decision_id: str,
        is_source: bool = False,
    ) -> None:
        """Insert or update one candidate document row.

        Upserts on ``document_id``, but only while the stored row belongs to
        the same decision: a document id or source URL already registered
        under another decision raises instead of moving. Without a fetch result only the URL
        metadata is written; file evidence recorded by an earlier download is
        kept. ``downloaded_at`` is set only for a ``downloaded`` outcome.

        With ``is_source`` the decision's other source rows are cleared first,
        in the same transaction as the guarded upsert, so the decision never
        has two sources; an ownership conflict rolls the clear back, so the
        earlier source stays. Without it a stored ``is_source`` is kept.

        Args:
            document: Candidate document (URL, origin, language).
            fetch: Result of the download or existence check, or None for a
                candidate that was not fetched (e.g. not selected).
            is_selected: Whether the selector chose this candidate.
            decision_id: Owning decision; its row must already exist.
            is_source: True when the document's file is the one the
                processing pipeline uses for the decision (master entry PDF).

        Returns:
            None.

        Raises:
            ValueError: If ``fetch`` belongs to another document or its
                outcome does not leave a valid file (failed, not_selected,
                no_pdf_source) -- such documents are audited, not registered.
            DocumentOwnershipConflict: If the document id or its source URL
                is already registered under another decision (subclass of
                ValueError).
            sqlalchemy.exc.SQLAlchemyError: On other database errors.
        """
        if fetch is not None:
            if fetch.document_id != document.document_id:
                raise ValueError(
                    f"fetch result {fetch.document_id!r} does not belong to "
                    f"document {document.document_id!r}"
                )
            if fetch.outcome not in _REGISTRABLE:
                raise ValueError(
                    f"outcome {fetch.outcome.value!r} leaves no valid file; "
                    f"document {document.document_id!r} is not registered"
                )
        downloaded = fetch is not None and fetch.outcome is Outcome.DOWNLOADED
        params = {
            "document_id": document.document_id,
            "decision_id": decision_id,
            "source_url": document.source_url,
            "file_format": document.file_format,
            "origin": document.origin,
            "language": document.language,
            "is_selected": is_selected,
            "is_source": is_source,
            "local_file_pdf": fetch.local_path if fetch else None,
            "local_file_md": (
                str(markdown_path_for(Path(fetch.local_path)))
                if fetch and fetch.local_path
                else None
            ),
            "sha256": fetch.sha256 if fetch else None,
            "size_bytes": fetch.size_bytes if fetch else None,
            "content_type": fetch.content_type if fetch else None,
            "downloaded_at": dt.datetime.now(dt.UTC) if downloaded else None,
        }
        try:
            with self.engine.begin() as conn:
                if is_source:
                    conn.execute(_CLEAR_OTHER_SOURCES, {
                        "decision_id": decision_id,
                        "document_id": document.document_id,
                    })
                written = conn.execute(_UPSERT_DOCUMENT, params).first()
                if written is None:
                    owner = conn.execute(
                        _OWNER_BY_ID, {"document_id": document.document_id}
                    ).first()
                    raise DocumentOwnershipConflict(
                        f"document {document.document_id!r} is owned by decision "
                        f"{owner[0] if owner else None!r}, not {decision_id!r}"
                    )
        except IntegrityError as exc:
            if _SOURCE_URL_CONSTRAINT not in str(exc):
                raise
            with self.engine.connect() as conn:
                owner = conn.execute(
                    _OWNER_BY_URL, {"source_url": document.source_url}
                ).first()
            raise DocumentOwnershipConflict(
                f"document {document.document_id!r}: source URL already registered "
                f"as {owner[0] if owner else None!r} of decision "
                f"{owner[1] if owner else None!r}, not {decision_id!r}"
            ) from exc
        logger.debug("Upserted document %s of %s", document.document_id, decision_id)
