"""
Persistence for the source-document decision step.

Loads the ``undecided`` manifest decisions without a source row and their
selected candidates, the existing source documents of the corpus, and applies
one :class:`~pipelines.decide.models.DecisionPlan` per decision in a single
transaction: re-point known documents (same per-document move as the
registry de-duplication), promote sources, create split decisions, merge or
flag the decision. A merge is recorded in the ``decision_merge`` ledger under
the injected :class:`MergeStep`. ``before_commit`` runs inside the transaction so the
master JSON flush and the registry commit stand or fall together.

Exports:
    CandidateRow -- registry columns of a candidate.
    ExistingRow -- registry columns of an existing source.
    SourceDecisionRepository -- load and apply.
"""

import datetime as dt
import logging
from collections.abc import Callable

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine

from config.settings import settings
from pipelines.decide.models import DecisionPlan, UndecidedDecision, Verdict
from pipelines.rag.pgvector.registry_dedupe_repository import MergeStep, record_merge, repoint_document

logger = logging.getLogger(__name__)

_DECISION_TABLE = f"{settings.DB_SCHEMA}.decision_registry"
_DOCUMENT_TABLE = f"{settings.DB_SCHEMA}.document_registry"
_LINK_TABLE = f"{settings.DB_SCHEMA}.competition_case_decision"

_SELECT_UNDECIDED = f"""
    SELECT d.decision_id, d.decision_date, d.title,
           coalesce(array_agg(l.case_id ORDER BY l.case_id)
                    FILTER (WHERE l.case_id IS NOT NULL), '{{}}') AS case_ids
    FROM {_DECISION_TABLE} d
    LEFT JOIN {_LINK_TABLE} l ON l.decision_id = d.decision_id
    WHERE d.selection_status = 'undecided'
      AND NOT EXISTS (SELECT 1 FROM {_DOCUMENT_TABLE} s
                      WHERE s.decision_id = d.decision_id AND s.is_source)
"""
_SELECT_CANDIDATES = f"""
    SELECT r.document_id, r.decision_id, r.origin, r.sha256, r.source_url, r.file_format,
           r.local_file_pdf, r.local_file_md, r.language
    FROM {_DOCUMENT_TABLE} r
    JOIN {_DECISION_TABLE} d ON d.decision_id = r.decision_id
    WHERE d.selection_status = 'undecided'
      AND r.is_selected AND NOT r.is_source AND r.local_file_pdf IS NOT NULL
"""
_IDS_FILTER = " AND d.decision_id = ANY(:ids)"
_GROUP_UNDECIDED = " GROUP BY d.decision_id, d.decision_date, d.title ORDER BY d.decision_id"
_ORDER_CANDIDATES = " ORDER BY r.decision_id, r.document_id"
_SELECT_EXISTING = text(
    f"""
    SELECT s.decision_id, s.document_id, s.sha256, s.local_file_md, s.original_file_md, s.language,
           d.decision_date,
           coalesce((SELECT array_agg(l.case_id) FROM {_LINK_TABLE} l
                     WHERE l.decision_id = s.decision_id), '{{}}') AS case_ids
    FROM {_DOCUMENT_TABLE} s JOIN {_DECISION_TABLE} d ON d.decision_id = s.decision_id
    WHERE s.is_source
    """
)
_SELECT_DECISION_IDS = text(f"SELECT decision_id FROM {_DECISION_TABLE}")

_SET_SOURCE = text(
    f"UPDATE {_DOCUMENT_TABLE} SET is_source = true, "
    "language = CASE WHEN language IS NULL OR language = 'NA' THEN :language ELSE language END, "
    "updated_at = now() WHERE document_id = :document_id"
)
_MARK_SELECTED = text(
    f"UPDATE {_DECISION_TABLE} SET selection_status = 'selected', language_init = :language_init, "
    "decision_date = :decision_date, updated_at = now() WHERE decision_id = :decision_id"
)
_INSERT_SPLIT = text(
    f"""
    INSERT INTO {_DECISION_TABLE}
        (decision_id, decision_date, title, selection_status, language_init, meta, loader_file)
    SELECT :decision_id, :decision_date, :title, 'selected', :language_init,
           jsonb_build_object('split_from', CAST(:split_from AS text)), d.loader_file
    FROM {_DECISION_TABLE} d WHERE d.decision_id = :split_from
    """
)
_COPY_LINKS = text(
    f"INSERT INTO {_LINK_TABLE} (case_id, decision_id) "
    f"SELECT case_id, :new FROM {_LINK_TABLE} WHERE decision_id = :d "
    "ON CONFLICT (case_id, decision_id) DO NOTHING"
)
_MOVE_MEMBERS = text(
    f"UPDATE {_DOCUMENT_TABLE} SET decision_id = :new, updated_at = now() "
    "WHERE document_id = ANY(:members) AND decision_id = :d"
)
_MOVE_LINKS = text(
    f"INSERT INTO {_LINK_TABLE} (case_id, decision_id) "
    f"SELECT case_id, :k FROM {_LINK_TABLE} WHERE decision_id = :d "
    "ON CONFLICT (case_id, decision_id) DO NOTHING"
)
_DEMOTE_SOURCE = text(
    f"UPDATE {_DOCUMENT_TABLE} SET is_source = false, updated_at = now() "
    "WHERE decision_id = :owner AND is_source"
)
_MOVE_AS_SOURCE = text(
    f"UPDATE {_DOCUMENT_TABLE} SET decision_id = :owner, is_source = true, is_selected = true, "
    "language = CASE WHEN language IS NULL OR language = 'NA' THEN :language ELSE language END, "
    "updated_at = now() WHERE document_id = :document_id AND decision_id = :d"
)
_OWNER_LANGUAGE = text(
    f"UPDATE {_DECISION_TABLE} SET language_init = :language_init, language_translated = 'NA', "
    "updated_at = now() WHERE decision_id = :decision_id"
)
_SELECT_DOCUMENT_ROWS = text(
    f"SELECT document_id, decision_id, origin, sha256, source_url, file_format, local_file_pdf, "
    f"local_file_md, language FROM {_DOCUMENT_TABLE} "
    "WHERE decision_id = :d AND local_file_pdf IS NOT NULL ORDER BY document_id"
)
_SELECT_OWN_DOCUMENTS = text(
    f"SELECT document_id, source_url, file_format FROM {_DOCUMENT_TABLE} "
    "WHERE decision_id = :d ORDER BY document_id FOR UPDATE")
_DELETE_LINKS = text(f"DELETE FROM {_LINK_TABLE} WHERE decision_id = :d")
_DELETE_DECISION = text(f"DELETE FROM {_DECISION_TABLE} WHERE decision_id = :d")
_APPEND_REASON = text(
    f"UPDATE {_DECISION_TABLE} SET review_reasons = array_append(review_reasons, CAST(:reason AS text)), "
    "updated_at = now() WHERE decision_id = :decision_id AND NOT (:reason = ANY(review_reasons))"
)


class CandidateRow(BaseModel):
    """Registry columns of one candidate document."""

    document_id: str = Field(description="Document id.")
    decision_id: str = Field(description="Manifest decision id.")
    origin: str = Field(description="Source site.")
    sha256: str | None = Field(description="PDF hash.")
    source_url: str = Field(description="Document URL.")
    file_format: str = Field(description="Document format.")
    local_file_pdf: str = Field(description="Stored PDF path.")
    local_file_md: str | None = Field(description="MinerU Markdown path.")
    language: str | None = Field(description="Registry language.")


class ExistingRow(BaseModel):
    """Registry columns of one existing source document."""

    decision_id: str = Field(description="Owning decision.")
    document_id: str = Field(description="Source document id.")
    sha256: str | None = Field(description="PDF hash.")
    local_file_md: str | None = Field(description="Markdown path (may be an English translation).")
    original_file_md: str | None = Field(default=None, description="Original-language Markdown of a translated source.")
    language: str | None = Field(default=None, description="Registry language of the source row.")
    decision_date: dt.date | None = Field(default=None, description="Registry date of the owning decision.")
    case_ids: list[str] = Field(description="Case ids of the owning decision.")


class SourceDecisionRepository:
    """DAO for the source-document decision step.

    Attributes:
        engine: Injected SQLAlchemy engine.
    """

    engine: Engine

    def __init__(self, engine: Engine, merge_step: MergeStep = MergeStep.DECIDE_SOURCE_DOCUMENT) -> None:
        """Bind the engine and the step that MERGE plans are recorded under.

        Args:
            engine: SQLAlchemy engine, e.g. from ``session.make_engine``.
            merge_step: ``decision_merge.merged_by`` of the merges this
                repository applies (the repair script passes its own step).
        """
        self.engine = engine
        self.merge_step = merge_step

    def load_undecided(
        self, ids: list[str] | None,
    ) -> list[tuple[UndecidedDecision, list[CandidateRow]]]:
        """Read the undecided decisions without a source and their candidates.

        Args:
            ids: Restrict to these decisions; None for all.

        Returns:
            Decisions in id order, each with its selected candidates.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        params: dict[str, object] = {}
        dec_sql, cand_sql = _SELECT_UNDECIDED, _SELECT_CANDIDATES
        if ids is not None:
            dec_sql += _IDS_FILTER
            cand_sql += _IDS_FILTER
            params["ids"] = list(ids)
        with self.engine.connect() as conn:
            decisions = conn.execute(text(dec_sql + _GROUP_UNDECIDED), params).fetchall()
            candidates = conn.execute(text(cand_sql + _ORDER_CANDIDATES), params).fetchall()
        by_decision: dict[str, list[CandidateRow]] = {}
        for r in candidates:
            row = CandidateRow.model_validate(dict(r._mapping))
            by_decision.setdefault(row.decision_id, []).append(row)
        return [
            (UndecidedDecision(decision_id=r.decision_id, decision_date=r.decision_date,
                               case_ids=list(r.case_ids), title=r.title or ""),
             by_decision.get(r.decision_id, []))
            for r in decisions
        ]

    def existing_sources(self) -> list[ExistingRow]:
        """Read every source document of the corpus with its decision's case links.

        Returns:
            The rows; the caller reads their Markdown for title keys.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        with self.engine.connect() as conn:
            rows = conn.execute(_SELECT_EXISTING).fetchall()
        return [ExistingRow.model_validate({**dict(r._mapping), "case_ids": list(r.case_ids)}) for r in rows]

    def document_rows(self, decision_id: str) -> dict[str, CandidateRow]:
        """Read the document rows with a PDF of one decision.

        Args:
            decision_id: Decision id.

        Returns:
            Rows by document id.

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        with self.engine.connect() as conn:
            raw = conn.execute(_SELECT_DOCUMENT_ROWS, {"d": decision_id}).fetchall()
        return {r.document_id: r for r in (CandidateRow.model_validate(dict(x._mapping)) for x in raw)}

    def taken_decision_ids(self) -> set[str]:
        """Return every decision id in the registry.

        Returns:
            The ids (split ids must not collide with them).

        Raises:
            sqlalchemy.exc.SQLAlchemyError: On database errors.
        """
        with self.engine.connect() as conn:
            return {r[0] for r in conn.execute(_SELECT_DECISION_IDS).fetchall()}

    def apply(
        self, plan: DecisionPlan, rows: dict[str, CandidateRow], before_commit: Callable[[], None],
    ) -> None:
        """Apply one plan in a single transaction.

        Re-points run for every verdict (they are byte- or identity-certain);
        a REVIEW additionally records its reason, a MERGE moves every remaining
        document row and the case links to ``merge_into``, deletes the
        manifest decision and records the merge in ``decision_merge``.

        Args:
            plan: The ladder's plan.
            rows: Candidate rows of the decision by document id.
            before_commit: Called inside the transaction before COMMIT.

        Raises:
            Exception: Anything ``before_commit`` raises (rolled back) or a
                database error.
        """
        d = plan.decision_id
        with self.engine.begin() as conn:
            for rp in plan.repoints:
                row = rows[rp.document_id]
                if rp.make_source:
                    # Better copy: the owner's source is demoted (row and files
                    # kept), this row moves over with its files as the source.
                    language = (row.language or "").upper() or None
                    conn.execute(_DEMOTE_SOURCE, {"owner": rp.owner_decision_id})
                    conn.execute(_MOVE_AS_SOURCE, {"owner": rp.owner_decision_id, "document_id": row.document_id,
                                                   "d": d, "language": language})
                    conn.execute(_OWNER_LANGUAGE, {"decision_id": rp.owner_decision_id,
                                                   "language_init": language.lower() if language else "NA"})
                    continue
                repoint_document(conn, document_id=row.document_id, source_url=row.source_url,
                                 file_format=row.file_format, from_decision=d,
                                 to_decision=rp.owner_decision_id)
            for p in plan.promotions:
                if p.is_new:
                    key = p.winner.title_key
                    title = f"{', '.join(key.case_numbers)} - {key.subject}" if key else ""
                    conn.execute(_INSERT_SPLIT, {
                        "decision_id": p.decision_id, "decision_date": p.decision_date, "title": title,
                        "language_init": p.language_init, "split_from": d})
                    conn.execute(_COPY_LINKS, {"new": p.decision_id, "d": d})
                    conn.execute(_MOVE_MEMBERS, {"new": p.decision_id, "members": p.members, "d": d})
                conn.execute(_SET_SOURCE, {"document_id": p.winner.document_id,
                                           "language": p.winner.language})
                conn.execute(_MARK_SELECTED, {"decision_id": p.decision_id,
                                                "language_init": p.language_init,
                                                "decision_date": p.decision_date})
            if plan.verdict is Verdict.MERGE and plan.merge_into:
                # D also owns rows that were never candidates (HTML versions,
                # not-selected links); they follow D into the owner.
                for old, url, fmt in conn.execute(_SELECT_OWN_DOCUMENTS, {"d": d}).fetchall():
                    repoint_document(conn, document_id=old, source_url=url, file_format=fmt,
                                     from_decision=d, to_decision=plan.merge_into)
                conn.execute(_MOVE_LINKS, {"k": plan.merge_into, "d": d})
                conn.execute(_DELETE_LINKS, {"d": d})
                conn.execute(_DELETE_DECISION, {"d": d})
                record_merge(conn, decision_id=d, merged_into=plan.merge_into, merged_by=self.merge_step)
            if plan.review_reason:
                conn.execute(_APPEND_REASON, {"reason": plan.review_reason, "decision_id": d})
            before_commit()
        logger.info("applied %s: %s, %d repointed, %d promoted%s", d, plan.verdict.value,
                    len(plan.repoints), len(plan.promotions),
                    f", merged into {plan.merge_into}" if plan.merge_into else "")

