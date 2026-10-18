"""
HeidelTime extraction → PostgreSQL persistence.

Responsibility: persist :class:`~pipelines.rag.pgvector.models.HeidelTimeVO` value objects
produced by :class:`~pipelines.heideltime.extract_heideltime.HeidelTime` into
``celex_facts.heideltime_extractions``.

Design (Provisioner Pattern):
  - Read queries (``select_chunks_with_dates``, ``fresh_chunk_ids``,
    ``build_proxy_fact_payload``, ``select_extraction_sources``) and the
    stale-row invalidation (``delete_chunks``) are delegated to
    :class:`~pipelines.rag.pgvector.event_repository.EventRepository`.
  - Write/ingestion (``ingest_vos``) stays here because it uses HeidelTime-specific
    VO mapping and the ``CelexHeidelTimeExtraction`` ORM model directly.
  - The engine / session factory is shared with ``EventRepository`` via
    ``make_engine`` from ``session.py``.
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from typing import Any, Iterable, List, Optional, Set

from sqlalchemy.orm import sessionmaker

from pipelines.rag.pgvector.models import HeidelTimeVO
from pipelines.rag.pgvector.models import Base, CelexHeidelTimeExtraction
from pipelines.rag.pgvector.event_repository import EventRepository
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

# ISO-8601 calendar date pattern — the only shape HeidelTime produces for
# DATE / TIME TIMEX3 values that can be stored in a SQL DATE column.
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def _parse_timex_date(value: str) -> Optional[datetime.date]:
    """Try to parse *value* as an ISO-8601 date (YYYY-MM-DD).

    Returns:
        A :class:`datetime.date` when *value* starts with a full calendar date,
        ``None`` for DURATION (``P3Y``), SET, partial dates (``1998``), or any
        other non-date string.
    """
    m = _ISO_DATE_RE.match(value)
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


class HeidelTimeIngestor:
    """Provisioner for HeidelTime temporal-expression persistence.

    Args:
        db_url: PostgreSQL connection string. Defaults to the value from
             
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)
        # Ensure the heideltime_extractions table exists on first use.
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        # Delegate read queries to EventRepository (shared engine pool)
        self._event_repo = EventRepository(db_url=db_url)

    # ------------------------------------------------------------------
    # Query helpers (delegated to EventRepository)
    # ------------------------------------------------------------------

    def select_chunks_with_dates(self, celex_id: str) -> list[dict[str, Any]]:
        """Return chunks with their canonical HeidelTime dates for LLM event enrichment.

        Joins ``celex_facts.heideltime_extractions`` with the configured embedding
        table. Only chunks that have at least one valid (non-null) ``timex_value``
        are included.

        Args:
            celex_id: CELEX document identifier.

        Returns:
            List of dicts, each with keys ``fk_chunk_id`` (str), ``chunk_text``
            (str), ``canonical_dates`` (list[str] ISO dates, deduplicated,
            ascending order), and ``date_sources`` (dict mapping each ISO date
            to the ``heideltime_extractions`` rows that produced it, as
            ``{"id", "timex_text"}`` dicts).
        """
        return self._event_repo.select_heideltime_chunks_with_dates(celex_id)

    def select_extraction_sources(self, celex_id: str) -> list[dict[str, Any]]:
        """Return every extraction row of a CELEX with its chunk identity.

        Delegates to
        :meth:`~pipelines.rag.pgvector.event_repository.EventRepository.select_heideltime_extraction_sources`.

        Args:
            celex_id: CELEX document identifier.

        Returns:
            List of dicts with keys ``id``, ``fk_chunk_id``, ``timex_text`` and
            ``doc_pos``, used to build the lineage of the ``heideltime_raw`` fact.
        """
        return self._event_repo.select_heideltime_extraction_sources(celex_id)

    def build_proxy_fact_payload(self, celex_id: str) -> list[dict[str, Any]]:
        """Return compact metadata describing persisted HeidelTime extractions.

        This powers the lightweight ``heideltime_raw`` proxy fact stored in
        ``celex_facts.facts``. The raw TIMEX rows remain in
        ``celex_facts.heideltime_extractions`` and are not duplicated into the
        fact table.
        """
        return self._event_repo.build_heideltime_proxy_fact(celex_id)

    def fresh_chunk_ids(self, celex_id: str) -> Set[str]:
        """Return the ``fk_chunk_id`` values that need no re-extraction.

        A chunk is fresh only while every stored row's ``source_hash`` still
        equals ``md5()`` of the chunk text as it reads today; rows written
        before the column existed carry NULL and are read as stale.  This
        replaces the old ``already_processed_chunk_ids``, whose "has at least
        one row" test could not tell a complete result from one extracted
        before the chunk text was repaired.

        One bulk query — callers should call this once per document and then
        filter locally rather than invoking per-chunk existence checks.
        """
        return self._event_repo.heideltime_fresh_chunk_ids(celex_id)

    def delete_chunks(self, chunk_ids: Iterable[str]) -> int:
        """Delete stale extractions for *chunk_ids* and their lineage rows.

        Delegates to
        :meth:`~pipelines.rag.pgvector.event_repository.EventRepository.heideltime_delete_chunks`,
        which also clears the ``fact_source_registry`` (and dependent
        ``fact_source_link``) rows citing the doomed extractions.  Call this
        before re-running the JAR over a chunk, so the replacement rows are not
        blocked by the ``(celex_id, fk_chunk_id, tid)`` conflict clause.

        Returns:
            Number of ``heideltime_extractions`` rows deleted.
        """
        return self._event_repo.heideltime_delete_chunks(chunk_ids)

    # ------------------------------------------------------------------
    # Ingestion (stays here — HeidelTime-specific VO → ORM mapping)
    # ------------------------------------------------------------------

    def ingest_vos(
        self, vos: List[HeidelTimeVO], source_hash: str | None = None
    ) -> int:
        """Persist a batch of :class:`HeidelTimeVO` objects to the database.

        Each VO is mapped 1-to-1 to a ``CelexHeidelTimeExtraction`` row.  A
        fresh server-side UUID is assigned by PostgreSQL; the same UUID is
        back-filled into ``raw_jsonb["id"]`` so the row is self-describing.

        Args:
            vos: Value objects to persist.  May be empty — the method returns 0
                 immediately in that case.
            source_hash: ``md5()`` hex digest of the chunk text these
                 expressions were extracted from, stamped on every row so
                 :meth:`fresh_chunk_ids` can later tell whether the rows still
                 describe the text that is stored.  ``None`` writes NULL, which
                 reads back as stale.

        Returns:
            Number of rows inserted.
        """
        if not vos:
            return 0

        records: list[dict[str, Any]] = []
        for vo in vos:
            row_id = uuid.uuid4()
            enriched_jsonb = {**vo.raw_jsonb, "id": str(row_id)}
            records.append(
                {
                    "p_id": row_id,
                    "p_celex_id": vo.celex_id,
                    "p_fk_chunk_id": vo.fk_chunk_id,
                    "p_tid": vo.tid,
                    "p_timex_text": vo.text,
                    "p_timex_type": vo.type,
                    # datetime.date or None — SADate() bindparam type handles conversion.
                    "p_timex_value": _parse_timex_date(vo.value),
                    "p_span_start": vo.span_start,
                    "p_span_end": vo.span_end,
                    # Plain dict — PG_JSONB() bindparam type handles serialisation.
                    "p_raw_jsonb": enriched_jsonb,
                    "p_source_hash": source_hash,
                }
            )

        # Use PostgreSQL-dialect insert so we can add ON CONFLICT DO NOTHING
        # against the (celex_id, fk_chunk_id, tid) unique constraint.  This
        # guards concurrent workers racing on the same chunk.
        #
        # It does NOT make a re-run correct on its own: idempotence is not
        # freshness.  Once the chunk text changes, "changes nothing" is exactly
        # the wrong guarantee — the caller must delete the chunk's stale rows
        # first (HeidelTimeIngestor.delete_chunks), which HeidelTimeModule does
        # off the source_hash comparison.
        from sqlalchemy import bindparam
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.types import Date as SADate
        from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

        stmt = (
            pg_insert(CelexHeidelTimeExtraction)
            .values(
                id=bindparam("p_id"),
                celex_id=bindparam("p_celex_id"),
                fk_chunk_id=bindparam("p_fk_chunk_id"),
                tid=bindparam("p_tid"),
                timex_text=bindparam("p_timex_text"),
                timex_type=bindparam("p_timex_type"),
                timex_value=bindparam("p_timex_value", type_=SADate()),
                span_start=bindparam("p_span_start"),
                span_end=bindparam("p_span_end"),
                raw_jsonb=bindparam("p_raw_jsonb", type_=PG_JSONB()),
                source_hash=bindparam("p_source_hash"),
            )
            .on_conflict_do_nothing(
                index_elements=["celex_id", "fk_chunk_id", "tid"]
            )
        )

        with self.Session() as session:
            session.execute(stmt, records)
            session.commit()

        logger.info(
            "Inserted %d HeidelTime expressions for celex_id=%s chunk_id=%s.",
            len(records),
            vos[0].celex_id,
            vos[0].fk_chunk_id,
        )
        return len(records)
