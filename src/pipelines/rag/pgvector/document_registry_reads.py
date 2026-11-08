"""Read-only lookups over ``document_registry`` used by the downloader.

Every method runs one ``SELECT``; nothing here writes. The class is the
read half of :class:`~pipelines.rag.pgvector.document_registry_repository.
DocumentRegistryRepository`, which inherits it and supplies the injected
``engine`` (Provisioner pattern).

Exports:
    DocumentRegistryReads -- source-document, content (sha256), registration
        and master alias URL lookups.
"""

import logging
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from config.settings import settings

logger = logging.getLogger(__name__)

_DOCUMENT_TABLE = f"{settings.DB_SCHEMA}.document_registry"
_PDF_FORMAT = "pdf"

_SOURCE_DOCUMENT = text(
    f"SELECT document_id FROM {_DOCUMENT_TABLE} "
    "WHERE decision_id = :decision_id AND is_source"
)

# Holders: another decision's source or selected document, or the same
# decision's source under another document id. The literal
# (is_source OR is_selected) term lets the planner use the partial index
# ix_document_registry_sha256. Ordered: own source, then sources, then id.
_DECISION_OF_CONTENT = text(
    f"SELECT decision_id FROM {_DOCUMENT_TABLE} "
    "WHERE sha256 = :sha256 AND (is_source OR is_selected) "
    "AND document_id <> :document_id "
    "AND (decision_id <> :decision_id OR is_source) "
    "ORDER BY decision_id = :decision_id DESC, is_source DESC, decision_id LIMIT 1"
)

_IS_REGISTERED = text(
    f"SELECT 1 FROM {_DOCUMENT_TABLE} "
    "WHERE document_id = :document_id AND local_file_pdf IS NOT NULL"
)

_PDF_URLS_OF = text(
    f"SELECT decision_id, source_url FROM {_DOCUMENT_TABLE} "
    "WHERE decision_id = ANY(:keys) AND file_format = :fmt "
    "ORDER BY decision_id, source_url"
)


class DocumentRegistryReads:
    """Read-only ``document_registry`` lookups (the engine is injected).

    Attributes:
        engine: SQLAlchemy engine set by the subclass constructor.
    """

    engine: Engine

    def source_document_id(self, decision_id: str) -> str | None:
        """Return the decision's processing source document, if any.

        Read-only; at most one row exists (partial unique index
        ``uq_document_registry_one_source``).

        Args:
            decision_id: Decision id (the project-wide ``celex_id``).

        Returns:
            The ``document_id`` flagged ``is_source``, or None.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        with self.engine.connect() as conn:
            row = conn.execute(_SOURCE_DOCUMENT, {"decision_id": decision_id}).first()
        return row[0] if row else None

    def decision_of_content(
        self, sha256: str, decision_id: str, document_id: str
    ) -> str | None:
        """Return the decision already holding a file with these bytes (read-only).

        Args:
            sha256: SHA-256 hex digest of the file.
            decision_id: The decision the file is for.
            document_id: The document the file is for (never its own holder).

        Returns:
            ``decision_id`` when its own ``is_source`` document (another id)
            has that ``sha256``; else the decision of another decision's
            ``is_source`` or ``is_selected`` document (a source first); else
            None.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        with self.engine.connect() as conn:
            row = conn.execute(_DECISION_OF_CONTENT, {
                "sha256": sha256, "decision_id": decision_id,
                "document_id": document_id}).first()
        return row[0] if row else None

    def is_registered(self, document_id: str) -> bool:
        """Return whether a document is registered with a stored file.

        A row without ``local_file_pdf`` (a candidate registered from URL
        metadata only) does not count, so a file found on disk for it is
        backfilled as ``registered_existing`` rather than skipped.

        Args:
            document_id: Document id (file stem).

        Returns:
            True if a row with a non-null ``local_file_pdf`` exists.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        with self.engine.connect() as conn:
            row = conn.execute(_IS_REGISTERED, {"document_id": document_id}).first()
        return row is not None

    def pdf_urls_of(self, keys: Iterable[str]) -> list[tuple[str, str]]:
        """Return the registered PDF URLs of the given decisions (read-only).

        Used once per downloader run with the master keys: the PDF rows of a
        master key (its own candidates and the rows the de-duplication
        re-pointed to it) are its alias URLs for the identity rule.

        Args:
            keys: Decision ids (the master keys); none gives an empty list
                without a query.

        Returns:
            (decision_id, source_url) pairs ordered by decision and URL.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        wanted = sorted(set(keys))
        if not wanted:
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(_PDF_URLS_OF, {"keys": wanted, "fmt": _PDF_FORMAT}).fetchall()
        logger.debug("registry alias urls: %d rows for %d keys", len(rows), len(wanted))
        return [(r[0], r[1]) for r in rows]
