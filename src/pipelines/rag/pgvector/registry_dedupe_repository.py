"""Registry reads and the per-decision merge used by the de-duplication batch.

Reads are ``SELECT`` only and run in a read-only transaction. The merge folds
a duplicate decision D into master key K in a single transaction: D's
``document_registry`` rows are re-pointed to K (not selected, not source,
no stored file; the document id re-derived for K), D's case links move to K,
then D's ``decision_registry`` row is deleted and the merge D -> K is recorded
in the ``decision_merge`` ledger (:func:`record_merge`), so ids that still name
D (``document_download_audit.decision_id``) resolve to K. The re-pointed rows
keep D's URLs registered under K, so the downloader's identity index finds K
by them (alias URLs). ``document_download_audit`` is append-only and never
touched.

Follows the **Provisioner pattern**: the engine is injected.

Exports:
    DocumentRow, DecisionRow, MergeCounts -- validated rows and merge result.
    MergeStep -- the step that merged a decision (``decision_merge.merged_by``).
    repointed_id -- the document id a re-pointed row gets under K.
    repoint_document, record_merge -- per-merge writes shared with the
        source-document decision step.
    RegistryDedupeRepository -- the reads and the merge.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from config.settings import settings
from pipelines.download.identifiers import UnsafeIdentifier, document_id_for

logger = logging.getLogger(__name__)

_DECISION_TABLE = f"{settings.DB_SCHEMA}.decision_registry"
_LINK_TABLE = f"{settings.DB_SCHEMA}.competition_case_decision"
_DOCUMENT_TABLE = f"{settings.DB_SCHEMA}.document_registry"
_AUDIT_TABLE = f"{settings.DB_SCHEMA}.document_download_audit"
_MERGE_TABLE = f"{settings.DB_SCHEMA}.decision_merge"
_REFERENCING_TABLES = ("facts", settings.EMBEDDING_TABLE)
_PDF_FORMAT = "pdf"
_PDF_LIKE = ("/pdf/", ".pdf")

_SELECT_DECISIONS = text(
    f"SELECT decision_id, celex_number, decision_date FROM {_DECISION_TABLE}")
_SELECT_LINKS = text(f"SELECT decision_id, case_id FROM {_LINK_TABLE}")
_SELECT_DOCUMENTS = text(
    f"SELECT document_id, decision_id, source_url, file_format, is_selected, "
    f"is_source, local_file_pdf, sha256 FROM {_DOCUMENT_TABLE}")
_SELECT_AUDIT_URLS = text(
    f"SELECT DISTINCT decision_id, source_url FROM {_AUDIT_TABLE} "
    f"WHERE source_url IS NOT NULL")
_HAS_DOCUMENT_COLUMN = text(
    "SELECT 1 FROM information_schema.columns WHERE table_schema = :s "
    "AND table_name = :t AND column_name = 'document_id'")
_SELECT_OWN_DOCUMENTS = text(
    f"SELECT document_id, source_url, file_format FROM {_DOCUMENT_TABLE} "
    "WHERE decision_id = :d ORDER BY document_id FOR UPDATE")
_DOCUMENT_EXISTS = text(f"SELECT 1 FROM {_DOCUMENT_TABLE} WHERE document_id = :i")
_DECISION_EXISTS = text(f"SELECT 1 FROM {_DECISION_TABLE} WHERE decision_id = :k")
_REPOINT_DOCUMENT = text(
    f"UPDATE {_DOCUMENT_TABLE} SET document_id = :new, decision_id = :k, "
    "is_selected = false, is_source = false, local_file_pdf = NULL, "
    "updated_at = now() WHERE document_id = :old AND decision_id = :d")
_DELETE_DOCUMENT = text(
    f"DELETE FROM {_DOCUMENT_TABLE} WHERE document_id = :old AND decision_id = :d")
_MOVE_LINKS = text(
    f"INSERT INTO {_LINK_TABLE} (case_id, decision_id) "
    f"SELECT case_id, :k FROM {_LINK_TABLE} WHERE decision_id = :d "
    "ON CONFLICT (case_id, decision_id) DO NOTHING")
_DELETE_LINKS = text(f"DELETE FROM {_LINK_TABLE} WHERE decision_id = :d")
_DELETE_DECISION = text(f"DELETE FROM {_DECISION_TABLE} WHERE decision_id = :d")
_RECORD_MERGE = text(
    f"INSERT INTO {_MERGE_TABLE} (merged_decision_id, merged_into, merged_by) "
    "VALUES (:d, :k, :by) ON CONFLICT (merged_decision_id) DO UPDATE SET "
    "merged_into = EXCLUDED.merged_into, merged_by = EXCLUDED.merged_by, merged_at = now()")


class MergeStep(StrEnum):
    """The step that folded a decision into another (``decision_merge.merged_by``)."""

    DEDUPE_REGISTRY = "dedupe_registry"
    DECIDE_SOURCE_DOCUMENT = "decide_source_document"
    REPAIR_DECIDED_DUPLICATES = "repair_decided_duplicates"


def repointed_id(document_id: str, source_url: str, file_format: str, key: str) -> str:
    """Return the id a document of D gets when it is re-pointed to K.

    The id is re-derived from (URL, K, format) like the downloader's planner
    does, so a later run that maps the decision to K finds the same row
    (eur-lex ids embed the decision id; URL-stem ids stay unchanged).

    Args:
        document_id: Current id (kept when no safe id can be derived).
        source_url: Document URL.
        file_format: Document format.
        key: Master key K.

    Returns:
        The document id under K.
    """
    try:
        return document_id_for(source_url, key, file_format)
    except UnsafeIdentifier:
        return document_id


def repoint_document(
    conn: Connection, *, document_id: str, source_url: str, file_format: str,
    from_decision: str, to_decision: str,
) -> bool:
    """Move one document row of decision D to decision K inside an open transaction.

    The id is re-derived for K (:func:`repointed_id`). When that id already
    exists, the existing row is kept and D's row is deleted; otherwise D's
    row is re-pointed with ``is_selected`` and ``is_source`` false and
    ``local_file_pdf`` NULL, keeping its URL and sha256.

    Args:
        conn: Connection inside a transaction.
        document_id: Current document id under D.
        source_url: Document URL.
        file_format: Document format.
        from_decision: Decision D the row belongs to.
        to_decision: Decision K that receives it.

    Returns:
        True when the row was re-pointed, False when it was dropped.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On a database error.
    """
    new = repointed_id(document_id, source_url, file_format, to_decision)
    if new != document_id and conn.execute(_DOCUMENT_EXISTS, {"i": new}).first():
        conn.execute(_DELETE_DOCUMENT, {"old": document_id, "d": from_decision})
        return False
    conn.execute(_REPOINT_DOCUMENT,
                 {"new": new, "k": to_decision, "old": document_id, "d": from_decision})
    return True


def record_merge(conn: Connection, *, decision_id: str, merged_into: str, merged_by: MergeStep) -> None:
    """Record that decision D was folded into K, inside the merge's transaction.

    The ledger row is the only trace of D once its registry row is deleted.
    A repeated merge of the same id (D re-registered and merged again)
    replaces the earlier row; a chain D -> K -> J stays as two rows and is
    resolved by the reader.

    Args:
        conn: Connection inside the merge transaction.
        decision_id: Merged decision D.
        merged_into: Decision K that received D's documents and case links.
        merged_by: Step performing the merge.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: On a database error (e.g. D == K,
            refused by ``ck_decision_merge_not_self``).
    """
    conn.execute(_RECORD_MERGE, {"d": decision_id, "k": merged_into, "by": merged_by.value})


class DocumentRow(BaseModel):
    """One ``document_registry`` row (the columns the de-duplication needs)."""

    document_id: str = Field(description="Document primary key.")
    source_url: str = Field(description="Source URL of the document.")
    file_format: str = Field(description="File format (pdf, html, ...).")
    is_selected: bool = Field(description="Selected by the candidate selector.")
    is_source: bool = Field(description="Processing source of the decision.")
    local_file_pdf: str | None = Field(default=None, description="Stored PDF path.")
    sha256: str | None = Field(default=None, description="Content hash, if downloaded.")

    @property
    def is_pdf(self) -> bool:
        """Report whether the document is a PDF.

        Returns:
            True when ``file_format`` is pdf.
        """
        return self.file_format == _PDF_FORMAT


class MergeCounts(BaseModel):
    """What one merge of D into K changed."""

    documents_repointed: int = Field(description="Rows of D re-pointed to K.")
    documents_dropped: int = Field(
        description="Rows of D deleted because their re-derived id already exists.")
    case_links_moved: int = Field(description="(case, K) links added from D's links.")


class DecisionRow(BaseModel):
    """One registry decision with its case ids and documents."""

    decision_id: str = Field(description="Decision primary key.")
    celex_number: str | None = Field(default=None, description="CELEX number, if any.")
    decision_date: dt.date | None = Field(default=None, description="Decision date.")
    case_ids: list[str] = Field(default_factory=list, description="Linked case ids.")
    documents: list[DocumentRow] = Field(
        default_factory=list, description="Registered documents.")
    audit_urls: list[str] = Field(
        default_factory=list,
        description="PDF-like candidate URLs seen in the download audit "
                    "(includes candidates refused at registration).")




class RegistryDedupeRepository:
    """Reads the registry and merges a duplicate decision in one transaction."""

    def __init__(self, engine: Engine) -> None:
        """Bind the engine.

        Args:
            engine: SQLAlchemy engine (e.g. ``session.make_engine()``).
        """
        self._engine = engine

    def load_decisions(self, extra_ids: Sequence[str] = ()) -> list[DecisionRow]:
        """Read every decision with its case ids and documents (read-only).

        One REPEATABLE READ, READ ONLY snapshot serves all SELECTs.

        Args:
            extra_ids: Ids without a registry row (included master keys)
                that get an id-only row carrying their audited URLs.

        Returns:
            The decisions, ordered by id.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On a database error.
        """
        with self._engine.connect().execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True) as conn:
            decisions = conn.execute(_SELECT_DECISIONS).mappings().all()
            links = conn.execute(_SELECT_LINKS).mappings().all()
            documents = conn.execute(_SELECT_DOCUMENTS).mappings().all()
            audited = conn.execute(_SELECT_AUDIT_URLS).mappings().all()
        rows = {r["decision_id"]: DecisionRow(
            decision_id=r["decision_id"], celex_number=r["celex_number"],
            decision_date=r["decision_date"]) for r in decisions}
        for link in links:
            if link["decision_id"] in rows:
                rows[link["decision_id"]].case_ids.append(link["case_id"])
        for doc in documents:
            if doc["decision_id"] in rows:
                rows[doc["decision_id"]].documents.append(DocumentRow(
                    **{k: doc[k] for k in DocumentRow.model_fields}))
        for extra in extra_ids:
            rows.setdefault(extra, DecisionRow(decision_id=extra))
        for row in audited:
            url = row["source_url"]
            if row["decision_id"] in rows and any(m in url.lower() for m in _PDF_LIKE):
                rows[row["decision_id"]].audit_urls.append(url)
        return [rows[k] for k in sorted(rows)]

    def referenced(self, decisions: Sequence[DecisionRow]) -> set[str]:
        """Return the decisions that facts or embeddings reference (read-only).

        A decision is referenced when ``facts`` or the embedding table has a
        row whose ``celex_id`` equals its id, or (when the lineage migration
        added the column) whose ``document_id`` is one of its documents.

        Args:
            decisions: Candidate decisions with their documents.

        Returns:
            The ids of the referenced decisions.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On a database error.
        """
        if not decisions:
            return set()
        by_doc = {d.document_id: row.decision_id for row in decisions
                  for d in row.documents}
        ids = [row.decision_id for row in decisions]
        found: set[str] = set()
        with self._engine.connect().execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True) as conn:
            for table in _REFERENCING_TABLES:
                full = f"{settings.DB_SCHEMA}.{table}"
                by_celex = text(f"SELECT DISTINCT celex_id FROM {full} "
                                "WHERE celex_id = ANY(:ids)")
                found |= {value for (value,) in conn.execute(by_celex, {"ids": ids})}
                has_doc = conn.execute(
                    _HAS_DOCUMENT_COLUMN, {"s": settings.DB_SCHEMA, "t": table}).first()
                if has_doc and by_doc:
                    by_document = text(f"SELECT DISTINCT document_id FROM {full} "
                                       "WHERE document_id = ANY(:ids)")
                    found |= {by_doc[value] for (value,) in
                              conn.execute(by_document, {"ids": list(by_doc)})}
        return found

    def merge_decision(self, decision_id: str, key: str) -> MergeCounts:
        """Fold duplicate decision D into master key K in one transaction.

        Per document of D: re-derive its id for K (:func:`repointed_id`);
        when that id already exists (K's own row, or another decision's),
        the existing row is kept and D's row is deleted; otherwise D's row
        is re-pointed to K with ``is_selected`` and ``is_source`` false and
        ``local_file_pdf`` NULL (D's folder is quarantined), keeping its URL
        and sha256. D's case links are copied to K (missing pairs only) and
        deleted; then D's registry row is deleted and the merge recorded in
        ``decision_merge`` (:func:`record_merge`).

        Args:
            decision_id: Duplicate decision D.
            key: Master key K; its registry row must exist.

        Returns:
            The counts of re-pointed and dropped documents and added links.

        Raises:
            ValueError: If K has no registry row (nothing is changed).
            sqlalchemy.exc.SQLAlchemyError: On a database error (nothing is
                changed then).
        """
        repointed = dropped = 0
        with self._engine.begin() as conn:
            if conn.execute(_DECISION_EXISTS, {"k": key}).first() is None:
                raise ValueError(f"master key {key} has no registry row")
            for old, url, fmt in conn.execute(
                    _SELECT_OWN_DOCUMENTS, {"d": decision_id}).fetchall():
                if repoint_document(conn, document_id=old, source_url=url, file_format=fmt,
                                    from_decision=decision_id, to_decision=key):
                    repointed += 1
                else:
                    dropped += 1
            moved = conn.execute(_MOVE_LINKS, {"k": key, "d": decision_id}).rowcount
            conn.execute(_DELETE_LINKS, {"d": decision_id})
            conn.execute(_DELETE_DECISION, {"d": decision_id})
            record_merge(conn, decision_id=decision_id, merged_into=key,
                         merged_by=MergeStep.DEDUPE_REGISTRY)
        logger.info("merged %s into %s: %d documents re-pointed, %d dropped, %d links",
                    decision_id, key, repointed, dropped, moved)
        return MergeCounts(documents_repointed=repointed, documents_dropped=dropped,
                           case_links_moved=int(moved))
