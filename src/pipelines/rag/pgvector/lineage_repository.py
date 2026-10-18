"""
Data Lineage Repository — Option 2 (Relational) + selective Option 3 edge semantics.

Provides CRUD operations for:
  - ``celex_facts.fact_source_registry``  — normalized catalog of evidence sources
  - ``celex_facts.fact_source_link`` — join table linking facts to their sources

Source registration is **normalized**: ``register_*`` methods upsert on the
natural key for each source type (chunk_id, heideltime_event_id, lei_id,
``(celex_id, csv_row)``, or source_fact_id) so that the same piece of evidence
is catalogued once and shared across every fact it supports.

All write operations are idempotent: repeated calls return the existing
``source_id`` rather than creating duplicate rows.

Design follows the Provisioner pattern: the DB engine and session factory are
injected (or created once) in ``__init__`` and reused across method calls.
"""

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import Engine, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from pipelines.rag.pgvector.models import FactSourceLink, SourceRegistry
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

# Fallback trust rank used when no source-type default applies.
_DEFAULT_TRUST_RANK: int = 5

# Default trust ranks by source type
_TRUST_RANK_DEFAULTS: dict[str, int] = {
    "csv_human": 9,
    "book_case": 8,
    "lei_corporate": 7,
    "heideltime": 6,
    "chunk": 5,
    "fact": 4,
}

_TRUST_RANK_MIN: int = 1
_TRUST_RANK_MAX: int = 10

# Provenance lookup — the single top supporting source on a fact's lineage tree.
#
# The full recursive ``celex_facts.fact_provenance()`` traversal is retained so
# the ancestry path stays complete (e.g. ``fines -> fine -> [score=1, doc,
# pos=28]``), but
# a derived fact's tree also surfaces its *predecessor* facts' terminals (e.g.
# company chunks reached via ``derived_from``), which are noise for the fact the
# user asked about.  To show only the top supporting source we rank the terminals
# by the verifier's verdict and attach a ``final_score``.
#
# The verifier scores a *chunk* against the fact it directly extracted, but an
# aggregate / derived fact (e.g. ``fines``, built from per-company ``fine`` /
# ``companies`` facts) carries only a ``derived_from_facts`` evidence row with no
# chunk score of its own.  So the score is sourced from any fact in this fact's
# **lineage** — the fact itself plus its transitive ``derived_from`` predecessors
# (``lineage_facts``) — that the verifier confirmed on the *same terminal chunk*.
# A directly-verified fact still gets its own score (it is its own lineage root);
# a derived fact inherits its predecessor's score for the shared chunk (e.g.
# ``fines``'s pos=241 terminal inherits ``companies``'s score=1 on that chunk).
# Terminals with such a score sort first; ``LIMIT 1`` keeps the best.  Terminals
# with no lineage score (e.g. a chunk only verified for an unrelated fact) sort
# last, falling back to the most direct, most-trusted source (shallowest
# ``depth``, highest ``trust_rank``).
_FACT_PROVENANCE_SQL = text(
    """
    WITH RECURSIVE target_fact AS (
        SELECT f.id
        FROM celex_facts.facts f
        WHERE f.celex_id = :celex_id
          AND f.name     = :fact_name
    ),
    lineage_facts AS (
        SELECT id AS fact_id FROM target_fact
        UNION
        SELECT r.source_fact_id
        FROM lineage_facts lf
        JOIN celex_facts.fact_source_link l
          ON l.fact_id = lf.fact_id
         AND l.relation_type = 'derived_from'
        JOIN celex_facts.fact_source_registry r
          ON r.source_id = l.source_id
        WHERE r.source_fact_id IS NOT NULL
    )
    SELECT
        p.ancestry_path_names
          || ' → ['
          || CASE
               WHEN p.source_type = 'chunk' THEN
                   CASE WHEN ev.final_score IS NOT NULL
                        THEN 'score=' || trim_scale(round(ev.final_score::numeric, 2))::text || ', '
                        ELSE '' END
                   || 'doc'
                   || CASE WHEN array_length(p.chunk_seq, 1) > 0
                           THEN ', pos=' || p.chunk_seq[1]::text ELSE '' END
               ELSE coalesce(p.source_description, p.chunk_id, p.source_id::text)
             END
          || ']'                       AS ancestry_path_names,
        p.chunk_id,
        p.depth
    FROM target_fact f
    CROSS JOIN LATERAL celex_facts.fact_provenance(f.id) p
    LEFT JOIN LATERAL (
        SELECT max(e.final_score) AS final_score
        FROM celex_facts.fact_evidence e
        WHERE e.chunk_id = p.chunk_id
          AND e.fact_id IN (SELECT fact_id FROM lineage_facts)
    ) ev ON TRUE
    WHERE p.celex_id = :celex_id
    ORDER BY (ev.final_score IS NOT NULL) DESC,
             ev.final_score DESC NULLS LAST,
             p.depth ASC,
             p.trust_rank DESC
    LIMIT 1
    """
)


class FactProvenanceRow(BaseModel):
    """The top supporting source on a fact's lineage tree.

    Boundary VO for the row selected from the recursive
    ``celex_facts.fact_provenance`` traversal — the terminal the verifier
    confirmed as best supporting this fact (see ``_FACT_PROVENANCE_SQL``).  Only
    ``ancestry_path_names`` is meant for user display; ``chunk_id`` and ``depth``
    are retained so a later step can fetch and present the underlying chunk.

    Attributes
    ----------
    ancestry_path_names:
        Human-readable lineage trail to the source, e.g.
        ``"fines → fine → [score=1, doc, pos=28]"``.  The fact-name prefix is
        built by ``fact_provenance``; the terminal ``[score=…, doc, pos=…]`` is
        appended by ``_FACT_PROVENANCE_SQL`` (it carries the verifier score).
    chunk_id:
        Stable id of the terminal chunk source, or ``None`` for non-chunk
        terminals (e.g. csv_human, lei_corporate).
    depth:
        Distance from the root fact (0 = direct source).
    """

    model_config = {"frozen": True}

    ancestry_path_names: str | None = Field(
        default=None, description="Human-readable lineage trail for display."
    )
    chunk_id: str | None = Field(
        default=None, description="Terminal chunk id, retained for later chunk display."
    )
    depth: int = Field(description="Distance from the root fact; used for ordering.")


class LineageRepository:
    """Persistence layer for the data lineage registry.

    Write methods accept an optional ``session``.  When omitted, each method
    opens its own session and commits immediately.  When a session is supplied,
    the method enrolls in that transaction and leaves the commit to the caller,
    enabling atomic multi-step writes (e.g. register + link for one fact).

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Defaults to ``DATABASE_URL`` from
        environment configuration.  Ignored when ``engine`` is supplied.
    engine:
        Pre-built SQLAlchemy engine to share a connection pool with another
        repository (Provisioner pattern).  When provided, ``db_url`` is unused.
    """

    def __init__(self, db_url: str = DATABASE_URL, engine: Engine | None = None) -> None:
        self.engine = engine if engine is not None else make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)

    @contextmanager
    def _session_scope(self, session: Session | None) -> Iterator[Session]:
        """Yield a session, committing/closing it only when this repo owns it.

        When ``session`` is supplied the caller owns the transaction lifecycle
        and this scope is a no-op wrapper.  When ``session`` is ``None`` a fresh
        session is opened, committed on success, rolled back on error, and
        closed.
        """
        if session is not None:
            yield session
            return
        own_session = self.Session()
        try:
            yield own_session
            own_session.commit()
        except Exception:
            own_session.rollback()
            raise
        finally:
            own_session.close()

    # ------------------------------------------------------------------
    # Source registration
    # ------------------------------------------------------------------

    def register_chunk_source(
        self,
        celex_id: str,
        chunk_id: str | None = None,
        chunk_seq: list[int] | None = None,
        description: str | None = None,
        session: Session | None = None,
    ) -> UUID:
        """Register a document chunk from ``pg_embedding_qwen`` as a source.

        Idempotent on ``(source_type, chunk_id)`` when ``chunk_id`` is provided.

        Parameters
        ----------
        celex_id:
            CELEX document identifier.
        chunk_id:
            Stable id of the chunk row (``pg_embedding_qwen.id``), a
            UUID-format varchar string.
        chunk_seq:
            List of ``doc_pos`` values — used for display and backfill only.
        description:
            Optional human-readable label for this source.
        session:
            Optional session to enroll in for atomic multi-step writes.

        Returns
        -------
        UUID
            ``source_id`` of the new or existing ``fact_source_registry`` row.
        """
        return self._upsert_source(
            source_type="chunk",
            celex_id=celex_id,
            chunk_id=chunk_id,
            chunk_seq=chunk_seq,
            description=description,
            session=session,
        )

    def register_book_source(
        self,
        celex_id: str,
        chunk_id: str | None = None,
        chunk_seq: list[int] | None = None,
        description: str | None = None,
        session: Session | None = None,
    ) -> UUID:
        """Register a book-case chunk from ``pg_embedding_book_qwen`` as a source.

        Idempotent on ``(source_type, chunk_id)`` when ``chunk_id`` is provided.

        Parameters
        ----------
        celex_id:
            CELEX document identifier (or book name).
        chunk_id:
            Stable id of the book-chunk row, a UUID-format varchar string.
        chunk_seq:
            Positional doc_pos values.
        description:
            Optional human-readable label.
        session:
            Optional session to enroll in for atomic multi-step writes.

        Returns
        -------
        UUID
            ``source_id`` of the new or existing row.
        """
        return self._upsert_source(
            source_type="book_case",
            celex_id=celex_id,
            chunk_id=chunk_id,
            chunk_seq=chunk_seq,
            description=description,
            session=session,
        )

    def register_heideltime_source(
        self,
        celex_id: str,
        heideltime_event_id: UUID,
        description: str | None = None,
        session: Session | None = None,
    ) -> UUID:
        """Register a HeidelTime temporal expression as a source.

        Idempotent on ``heideltime_event_id``.

        Parameters
        ----------
        celex_id:
            CELEX document the expression was extracted from.
        heideltime_event_id:
            UUID of the ``heideltime_extractions`` row.
        description:
            Optional human-readable label (e.g. the surface timex text).
        session:
            Optional session to enroll in for atomic multi-step writes.

        Returns
        -------
        UUID
            ``source_id`` of the new or existing row.
        """
        return self._upsert_source(
            source_type="heideltime",
            celex_id=celex_id,
            heideltime_event_id=heideltime_event_id,
            description=description,
            session=session,
        )

    def register_csv_source(
        self,
        celex_id: str,
        csv_row: int,
        description: str | None = None,
        session: Session | None = None,
    ) -> UUID:
        """Register a human-curated CSV index row as a source.

        Idempotent on ``(celex_id, csv_row)``.

        Parameters
        ----------
        celex_id:
            CELEX document the row refers to.
        csv_row:
            Zero-based or one-based row number in the CSV export.
        description:
            Optional human-readable label.
        session:
            Optional session to enroll in for atomic multi-step writes.

        Returns
        -------
        UUID
            ``source_id`` of the new or existing row.
        """
        return self._upsert_source(
            source_type="csv_human",
            celex_id=celex_id,
            csv_row=csv_row,
            description=description,
            session=session,
        )

    def register_lei_source(
        self,
        lei_id: str,
        description: str | None = None,
        session: Session | None = None,
    ) -> UUID:
        """Register a LEI corporate registry record as a source.

        Idempotent on ``lei_id``.

        Parameters
        ----------
        lei_id:
            Legal Entity Identifier (e.g. ``"549300XYZ..."``).
        description:
            Optional human-readable label (e.g. the legal name).
        session:
            Optional session to enroll in for atomic multi-step writes.

        Returns
        -------
        UUID
            ``source_id`` of the new or existing row.
        """
        return self._upsert_source(
            source_type="lei_corporate",
            lei_id=lei_id,
            description=description,
            session=session,
        )

    def register_fact_source(
        self,
        celex_id: str,
        source_fact_id: UUID,
        description: str | None = None,
        session: Session | None = None,
    ) -> UUID:
        """Register a predecessor fact as a source for a derived fact.

        Used when a fact is computed from other facts (declared via
        ``predecessors`` in ``FACT_EXTRACTOR.yaml``, e.g. ``court_list`` derived
        from ``court_list_raw``).  The predecessor fact itself becomes an
        evidence row so the derived fact can be linked to it with
        ``relation_type="derived_from"``.

        Idempotent on ``source_fact_id``.

        Parameters
        ----------
        celex_id:
            CELEX document the predecessor fact belongs to.
        source_fact_id:
            UUID of the predecessor ``celex_facts.facts`` row.
        description:
            Optional human-readable label (e.g. the predecessor fact name).
        session:
            Optional session to enroll in for atomic multi-step writes.

        Returns
        -------
        UUID
            ``source_id`` of the new or existing ``fact_source_registry`` row.
        """
        return self._upsert_source(
            source_type="fact",
            celex_id=celex_id,
            source_fact_id=source_fact_id,
            description=description,
            session=session,
        )

    # ------------------------------------------------------------------
    # Fact ↔ Source linking
    # ------------------------------------------------------------------

    def link_fact_to_source(
        self,
        fact_id: UUID,
        source_id: UUID,
        extraction_method: str,
        relation_type: str = "extracted_from",
        trust_rank: int | None = None,
        conflict_flag: bool = False,
        conflict_note: str | None = None,
        source_type: str | None = None,
        pipeline: str | None = None,
        dspy_module: str | None = None,
        session: Session | None = None,
    ) -> None:
        """Create or update a link between a fact and one of its sources.

        Uses PostgreSQL ``ON CONFLICT DO UPDATE`` on the composite PK
        ``(fact_id, source_id)`` so repeated calls are idempotent.

        Parameters
        ----------
        fact_id:
            UUID of the ``celex_facts.facts`` row.
        source_id:
            UUID of the ``celex_facts.fact_source_registry`` row.
        extraction_method:
            Pipeline / module that produced this link (e.g. ``"universal"``).
        relation_type:
            Edge semantic: ``"extracted_from"`` | ``"corroborated_by"`` |
            ``"entity_resolved_via"`` | ``"derived_from"``.
        trust_rank:
            1–10 scale.  If ``None``, the default for ``source_type`` is used
            (resolved in Python when ``source_type`` is supplied, otherwise via
            a registry lookup on ``source_id``).
        conflict_flag:
            Set to ``True`` when this source contradicts another source for
            the same fact.
        conflict_note:
            Human-readable explanation of the conflict.
        source_type:
            Source type of ``source_id``.  When supplied, the default trust
            rank is resolved without a database round-trip.
        pipeline:
            Producing pipeline name (e.g. ``"universal"``).  Mirrors the
            ``fact_ingestion_audit_trail.pipeline`` column.
        dspy_module:
            Producing DSPy module class name (e.g.
            ``"UniversalFactExtractionModule"``).
        session:
            Optional session to enroll in for atomic multi-step writes.

        Raises
        ------
        ValueError
            If ``trust_rank`` is outside the 1–10 range.
        """
        if trust_rank is not None and not (_TRUST_RANK_MIN <= trust_rank <= _TRUST_RANK_MAX):
            raise ValueError(
                f"trust_rank must be between {_TRUST_RANK_MIN} and {_TRUST_RANK_MAX}, got {trust_rank}"
            )

        with self._session_scope(session) as active_session:
            effective_rank = (
                trust_rank
                if trust_rank is not None
                else self._resolve_trust_rank(source_id, source_type, active_session)
            )
            stmt = (
                pg_insert(FactSourceLink)
                .values(
                    fact_id=fact_id,
                    source_id=source_id,
                    extraction_method=extraction_method,
                    relation_type=relation_type,
                    trust_rank=effective_rank,
                    conflict_flag=conflict_flag,
                    conflict_note=conflict_note,
                    pipeline=pipeline,
                    dspy_module=dspy_module,
                )
                .on_conflict_do_update(
                    index_elements=["fact_id", "source_id"],
                    set_={
                        "extraction_method": extraction_method,
                        "relation_type": relation_type,
                        "trust_rank": effective_rank,
                        "conflict_flag": conflict_flag,
                        "conflict_note": conflict_note,
                        "pipeline": pipeline,
                        "dspy_module": dspy_module,
                    },
                )
            )
            active_session.execute(stmt)
            logger.debug("Linked fact %s → source %s (%s)", fact_id, source_id, relation_type)

    def retract_fact_links(
        self,
        fact_id: UUID,
        relation_type: str,
        extraction_method: str,
        source_type: str,
        keep_source_id: UUID | None = None,
        session: Session | None = None,
    ) -> int:
        """Delete one producer's links of one edge kind for a fact.

        Used when a producer's verdict no longer holds (e.g. a book
        corroboration overturned by a re-run) or its evidence anchor drifted to
        a different source. Only links matching all of ``relation_type``,
        ``extraction_method`` and the source's ``source_type`` are removed, so
        other producers' provenance for the same fact is untouched. Registry
        rows are left in place: they are shared across facts and may still be
        referenced elsewhere.

        Parameters
        ----------
        fact_id:
            UUID of the ``celex_facts.facts`` row.
        relation_type:
            Edge semantic of the links to remove (e.g. ``"corroborated_by"``).
        extraction_method:
            Producer that wrote the links (e.g. ``"book_cross_check"``).
        source_type:
            ``fact_source_registry.source_type`` of the linked sources
            (e.g. ``"book_case"``).
        keep_source_id:
            Source whose link is preserved (the current evidence anchor);
            ``None`` removes every matching link.
        session:
            Optional session to enroll in for atomic multi-step writes.

        Returns
        -------
        int
            Number of links deleted.
        """
        typed_sources = select(SourceRegistry.source_id).where(
            SourceRegistry.source_type == source_type
        )
        stmt = delete(FactSourceLink).where(
            FactSourceLink.fact_id == fact_id,
            FactSourceLink.relation_type == relation_type,
            FactSourceLink.extraction_method == extraction_method,
            FactSourceLink.source_id.in_(typed_sources),
        )
        if keep_source_id is not None:
            stmt = stmt.where(FactSourceLink.source_id != keep_source_id)

        with self._session_scope(session) as active_session:
            removed = active_session.execute(stmt).rowcount
        logger.debug(
            "Retracted %d %s link(s) of %s for fact %s", removed, relation_type,
            extraction_method, fact_id,
        )
        return removed

    def flag_conflict(
        self,
        fact_id: UUID,
        source_id: UUID,
        note: str,
        session: Session | None = None,
    ) -> None:
        """Mark an existing fact-source link as conflicted.

        Parameters
        ----------
        fact_id:
            UUID of the fact.
        source_id:
            UUID of the conflicting source.
        note:
            Description of the conflict (e.g. which other source disagrees).
        session:
            Optional session to enroll in for atomic multi-step writes.

        Raises
        ------
        LookupError
            If no ``fact_source_link`` row exists for ``(fact_id, source_id)``.
        """
        with self._session_scope(session) as active_session:
            link = active_session.get(FactSourceLink, (fact_id, source_id))
            if link is None:
                raise LookupError(
                    f"No fact_source_link for fact_id={fact_id}, source_id={source_id}"
                )
            link.conflict_flag = True
            link.conflict_note = note
            logger.info("Flagged conflict on fact %s / source %s: %s", fact_id, source_id, note)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_sources_for_fact(self, fact_id: UUID) -> list[FactSourceLink]:
        """Return all source links for a given fact, ordered by trust_rank desc.

        Parameters
        ----------
        fact_id:
            UUID of the ``celex_facts.facts`` row.

        Returns
        -------
        list[FactSourceLink]
            Detached ORM instances (expiry disabled).
        """
        with self.Session() as session:
            rows = (
                session.query(FactSourceLink)
                .filter(FactSourceLink.fact_id == fact_id)
                .order_by(FactSourceLink.trust_rank.desc())
                .all()
            )
            session.expunge_all()
            return rows

    def get_facts_for_source(self, source_id: UUID) -> list[FactSourceLink]:
        """Return all fact links for a given source.

        Parameters
        ----------
        source_id:
            UUID of the ``celex_facts.fact_source_registry`` row.

        Returns
        -------
        list[FactSourceLink]
            Detached ORM instances.
        """
        with self.Session() as session:
            rows = (
                session.query(FactSourceLink)
                .filter(FactSourceLink.source_id == source_id)
                .all()
            )
            session.expunge_all()
            return rows

    def get_source(self, source_id: UUID) -> SourceRegistry | None:
        """Fetch a single ``SourceRegistry`` row by primary key.

        Parameters
        ----------
        source_id:
            UUID primary key.

        Returns
        -------
        SourceRegistry or None
        """
        with self.Session() as session:
            row = session.get(SourceRegistry, source_id)
            if row is not None:
                session.expunge(row)
            return row

    def get_fact_provenance(
        self, celex_id: str, fact_name: str
    ) -> list[FactProvenanceRow]:
        """Return the top supporting source for a named fact within one case.

        Runs the full recursive ``celex_facts.fact_provenance`` traversal from
        the fact ``(celex_id, fact_name)`` and returns the single terminal the
        verifier confirmed as best supporting it — the highest-scoring
        ``fact_evidence`` chunk, falling back to the most direct, most-trusted
        source when none is verifier-confirmed.  Predecessor-fact terminals
        reachable through ``derived_from`` edges are ranked out, so a derived
        fact does not surface its predecessors' (e.g. company) chunks.

        Parameters
        ----------
        celex_id:
            CELEX document identifier scoping the fact and its sources.
        fact_name:
            Root fact ``name`` to trace (e.g. ``"fines"``).

        Returns
        -------
        list[FactProvenanceRow]
            At most one row (the top supporting source); empty when the fact has
            no recorded lineage for this case.
        """
        with self.Session() as session:
            rows = session.execute(
                _FACT_PROVENANCE_SQL,
                {"celex_id": celex_id, "fact_name": fact_name},
            ).all()
        return [
            FactProvenanceRow(
                ancestry_path_names=row.ancestry_path_names,
                chunk_id=str(row.chunk_id) if row.chunk_id is not None else None,
                depth=row.depth,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upsert_source(
        self,
        source_type: str,
        celex_id: str | None = None,
        chunk_id: str | None = None,
        chunk_seq: list[int] | None = None,
        heideltime_event_id: UUID | None = None,
        lei_id: str | None = None,
        csv_row: int | None = None,
        source_fact_id: UUID | None = None,
        description: str | None = None,
        session: Session | None = None,
    ) -> UUID:
        """Return the ``source_id`` for a piece of evidence, creating it if new.

        The registry is normalized: each distinct piece of evidence (identified
        by its natural key — ``chunk_id``, ``heideltime_event_id``, ``lei_id``,
        ``(celex_id, csv_row)``, or ``source_fact_id``) maps to exactly one
        ``fact_source_registry`` row, shared across every fact it supports.  Existing
        rows are reused; a non-``None`` ``description`` refreshes the stored
        label (so re-runs heal stale text), while ``None`` never clears it.
        """
        with self._session_scope(session) as active_session:
            existing = self._find_existing_source(
                active_session,
                source_type=source_type,
                celex_id=celex_id,
                chunk_id=chunk_id,
                heideltime_event_id=heideltime_event_id,
                lei_id=lei_id,
                csv_row=csv_row,
                source_fact_id=source_fact_id,
            )
            if existing is not None:
                if description and existing.description != description:
                    existing.description = description
                logger.debug(
                    "Reusing %s source %s (celex=%s)", source_type, existing.source_id, celex_id
                )
                return existing.source_id

            new_id = uuid.uuid4()
            row = SourceRegistry(
                source_id=new_id,
                source_type=source_type,
                celex_id=celex_id,
                chunk_id=chunk_id,
                chunk_seq=chunk_seq,
                heideltime_event_id=heideltime_event_id,
                lei_id=lei_id,
                csv_row=csv_row,
                source_fact_id=source_fact_id,
                description=description,
            )
            active_session.add(row)
            active_session.flush()
            logger.debug("Registered %s source %s (celex=%s)", source_type, new_id, celex_id)
            return new_id

    @staticmethod
    def _find_existing_source(
        session: Session,
        source_type: str,
        celex_id: str | None,
        chunk_id: str | None,
        heideltime_event_id: UUID | None,
        lei_id: str | None,
        csv_row: int | None,
        source_fact_id: UUID | None,
    ) -> SourceRegistry | None:
        """Locate an existing source by its natural key, or ``None``.

        Returns ``None`` when the natural key is incomplete (e.g. a chunk
        source registered without a ``chunk_id``), forcing a fresh insert since
        the evidence cannot be de-duplicated.
        """
        query = session.query(SourceRegistry).filter(
            SourceRegistry.source_type == source_type
        )
        match source_type:
            case "chunk" | "book_case":
                if chunk_id is None:
                    return None
                query = query.filter(SourceRegistry.chunk_id == chunk_id)
            case "heideltime":
                if heideltime_event_id is None:
                    return None
                query = query.filter(
                    SourceRegistry.heideltime_event_id == heideltime_event_id
                )
            case "lei_corporate":
                if lei_id is None:
                    return None
                query = query.filter(SourceRegistry.lei_id == lei_id)
            case "csv_human":
                if celex_id is None or csv_row is None:
                    return None
                query = query.filter(
                    SourceRegistry.celex_id == celex_id,
                    SourceRegistry.csv_row == csv_row,
                )
            case "fact":
                if source_fact_id is None:
                    return None
                query = query.filter(SourceRegistry.source_fact_id == source_fact_id)
            case _:
                return None
        return query.first()

    def _resolve_trust_rank(
        self,
        source_id: UUID,
        source_type: str | None,
        session: Session,
    ) -> int:
        """Resolve the default trust rank for a source.

        Prefers the in-Python lookup keyed on ``source_type``; falls back to a
        registry read on ``source_id`` only when the type is unknown.
        """
        if source_type is not None:
            return _TRUST_RANK_DEFAULTS.get(source_type, _DEFAULT_TRUST_RANK)
        source = session.get(SourceRegistry, source_id)
        if source is None:
            return _DEFAULT_TRUST_RANK
        return _TRUST_RANK_DEFAULTS.get(source.source_type, _DEFAULT_TRUST_RANK)
