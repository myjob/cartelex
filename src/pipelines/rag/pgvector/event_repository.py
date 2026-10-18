"""
Unified DAO for event-extraction persistence.

Consolidates the scattered event-related database access previously in:
  - ``HeidelTimeIngestor``         (heideltime_ingestor.py)          — read queries

All queries go through the shared engine from ``session.py`` (single
connection pool).

Tables covered:
  - ``celex_facts.heideltime_extractions``   (HeidelTime temporal expressions;
    read, plus the stale-chunk delete that precedes a re-extraction)
  - ``celex_facts.fact_source_registry``     (lineage rows cleared with them)
  - ``celex_facts.fact_source_link``         (idem — NO ACTION FK, must go first)
  - ``settings.EMBEDDING_TABLE``             (chunk text joined into the reads)

Exports:
    EventRepository — canonical persistence service for event tables
"""

import logging
from typing import Any, Dict, Iterable, List,  Set

from sqlalchemy import bindparam, text
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from pipelines.rag.pgvector.models import Base
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)


def _index_date_sources(
    date_entries: List[Dict[str, Any]] | None,
) -> Dict[str, List[Dict[str, str]]]:
    """Group raw ``heideltime_extractions`` entries by their canonical date.

    Parameters
    ----------
    date_entries:
        Rows aggregated by :meth:`EventRepository.select_heideltime_chunks_with_dates`,
        each a dict with keys ``id``, ``value`` and ``timex_text``.

    Returns
    -------
    Dict[str, List[Dict[str, str]]]
        ISO date string -> list of ``{"id", "timex_text"}`` dicts, insertion
        ordered so the mapping keys double as the deduplicated date list.  The
        same date can map to several extractions when a chunk mentions it more
        than once.
    """
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for entry in date_entries or []:
        canonical_date = entry.get("value")
        if not canonical_date:
            continue
        grouped.setdefault(canonical_date, []).append(
            {
                "id": str(entry["id"]),
                "timex_text": str(entry.get("timex_text") or ""),
            }
        )
    return grouped



def heideltime_invalidation_statements() -> List[TextClause]:
    """Return the ordered DELETEs that retire a chunk's HeidelTime extractions.

    Every statement takes one expanding bind parameter, ``chunk_ids``, and they
    must be executed in this order on a single connection:

    1. ``fact_source_link`` — its ``source_id`` FK onto ``fact_source_registry``
       is ``ON DELETE NO ACTION``, so step 3 raises 23503 without this.
    2. ``knowledge_edges`` — same constraint via ``dst_source_id``.
    3. ``fact_source_registry`` — ``heideltime_event_id`` carries **no** FK at
       all, so these rows are not cascaded away by step 4 and would survive as
       orphans that ``fact_provenance()`` still cites.
    4. ``heideltime_extractions`` — the stale rows themselves.

    Only ``heideltime``-typed registry rows are reached.  The ``chunk`` sources
    emitted beside them are shared with every other fact drawn from the same
    chunk and must survive.

    Exposed as statements rather than hidden inside
    :meth:`EventRepository.heideltime_delete_chunks` so that
    ``pipelines.heideltime.qa.scan_dates`` can run the same invalidation inside
    the *same* transaction as its chunk-text ``UPDATE``.  A text repair that
    committed without the invalidation is precisely how the stored extractions
    went stale in the first place.

    Returns
    -------
    List[TextClause]
        Statements to execute in order, each expecting ``{"chunk_ids": [...]}``.
    """
    doomed_sources = (
        " SELECT r.source_id"
        "   FROM celex_facts.fact_source_registry r"
        "   JOIN celex_facts.heideltime_extractions h"
        "     ON h.id = r.heideltime_event_id"
        "  WHERE h.fk_chunk_id IN :chunk_ids"
    )
    sql = [
        "DELETE FROM celex_facts.fact_source_link"
        f" WHERE source_id IN ({doomed_sources})",
        "DELETE FROM celex_facts.knowledge_edges"
        f" WHERE dst_source_id IN ({doomed_sources})",
        "DELETE FROM celex_facts.fact_source_registry"
        " WHERE heideltime_event_id IN ("
        "     SELECT id FROM celex_facts.heideltime_extractions"
        "      WHERE fk_chunk_id IN :chunk_ids)",
        "DELETE FROM celex_facts.heideltime_extractions"
        " WHERE fk_chunk_id IN :chunk_ids",
    ]
    return [
        text(statement).bindparams(bindparam("chunk_ids", expanding=True))
        for statement in sql
    ]


class EventRepository:
    """Unified DAO for event-extraction tables in ``celex_facts``.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Defaults to ``DATABASE_URL``.
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    # ------------------------------------------------------------------
    # HeidelTime reads and stale-row invalidation
    # (row insertion stays in HeidelTimeIngestor)
    # ------------------------------------------------------------------

    def select_heideltime_chunks_with_dates(
        self, celex_id: str
    ) -> List[Dict[str, Any]]:
        """Return chunks with their canonical HeidelTime dates and source identities.

        Previously ``HeidelTimeIngestor.select_chunks_with_dates``.

        Each date is returned together with the ``heideltime_extractions`` rows
        that produced it, so a consumer can record audit-grade provenance
        (``fact_source_registry.heideltime_event_id``) instead of re-deriving the
        link by matching date strings across the whole document afterwards.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        List[Dict]
            Each dict has keys ``fk_chunk_id``, ``chunk_text``,
            ``canonical_dates`` (deduplicated ISO date strings, ascending) and
            ``date_sources`` (ISO date -> list of ``{"id", "timex_text"}`` dicts
            naming the extraction rows in this chunk that yielded the date).
        """
        stmt = text(
            "SELECT h.fk_chunk_id,"
            "       e.document AS chunk_text,"
            "       jsonb_agg("
            "           jsonb_build_object("
            "               'id', h.id,"
            "               'value', h.timex_value::text,"
            "               'timex_text', h.timex_text"
            "           ) ORDER BY h.timex_value::text, h.span_start"
            "       ) AS date_entries"
            " FROM celex_facts.heideltime_extractions h"
            f" JOIN celex_facts.{settings.EMBEDDING_TABLE} e"
            "   ON h.fk_chunk_id = e.id::text"
            " WHERE h.celex_id = :celex_id"
            "   AND h.timex_value IS NOT NULL"
            " GROUP BY h.fk_chunk_id, e.document"
            " ORDER BY h.fk_chunk_id"
        )
        with self.Session() as session:
            rows = session.execute(stmt, {"celex_id": celex_id}).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            date_sources = _index_date_sources(row.date_entries)
            results.append(
                {
                    "fk_chunk_id": str(row.fk_chunk_id),
                    "chunk_text": str(row.chunk_text) if row.chunk_text else "",
                    "canonical_dates": list(date_sources),
                    "date_sources": date_sources,
                }
            )
        return results

    def select_heideltime_extraction_sources(
        self, celex_id: str
    ) -> List[Dict[str, Any]]:
        """Return every extraction row of a CELEX with its chunk identity.

        Supplies the evidence behind the ``heideltime_raw`` fact, whose value is
        a count of exactly these rows: each extraction is a ``heideltime``
        source, and the chunk it was found in is a ``chunk`` source.  Rows
        without a ``timex_value`` are included — the JAR produced them and the
        proxy payload counts them.

        The join is a LEFT JOIN so an extraction whose chunk has since been
        removed from the embedding table is still reported, with ``doc_pos``
        ``None``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        List[Dict]
            One dict per extraction row with keys ``id`` (str), ``fk_chunk_id``
            (str), ``timex_text`` (str) and ``doc_pos`` (int or ``None``),
            ordered by document position then span offset.
        """
        stmt = text(
            "SELECT h.id, h.fk_chunk_id, h.timex_text, e.doc_pos"
            " FROM celex_facts.heideltime_extractions h"
            f" LEFT JOIN celex_facts.{settings.EMBEDDING_TABLE} e"
            "   ON h.fk_chunk_id = e.id::text"
            " WHERE h.celex_id = :celex_id"
            " ORDER BY e.doc_pos NULLS LAST, h.span_start"
        )
        with self.Session() as session:
            rows = session.execute(stmt, {"celex_id": celex_id}).fetchall()

        return [
            {
                "id": str(row.id),
                "fk_chunk_id": str(row.fk_chunk_id),
                "timex_text": str(row.timex_text or ""),
                "doc_pos": row.doc_pos,
            }
            for row in rows
        ]

    def heideltime_fresh_chunk_ids(
        self, celex_id: str
    ) -> Set[str]:
        """Return chunk IDs whose stored extractions still match the chunk text.

        Replaces ``heideltime_already_processed_chunk_ids``, which answered the
        weaker question "does this chunk have at least one row?".  That could
        not distinguish a complete result from one extracted before the chunk
        text was repaired in place, so stale rows were skipped forever.

        A chunk is fresh only when **every** one of its rows carries a
        ``source_hash`` equal to ``md5(document)`` of the chunk as it reads
        today.  ``bool_and`` over the group enforces the "every" -- a chunk
        whose rows were written across two different text versions is stale as
        a whole, which is the safe reading.  A NULL ``source_hash`` (written
        before the column existed) and a NULL ``document`` both make the
        predicate NULL, so ``HAVING`` drops the chunk: unknown provenance is
        treated as stale.

        Chunks with no rows at all are simply absent from the result, exactly
        as they were absent from the old "already processed" set.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        Set[str]
            Set of ``fk_chunk_id`` values that need no re-extraction.
        """
        stmt = text(
            "SELECT h.fk_chunk_id"
            " FROM celex_facts.heideltime_extractions h"
            f" JOIN celex_facts.{settings.EMBEDDING_TABLE} p"
            "   ON p.id::text = h.fk_chunk_id"
            " WHERE h.celex_id = :celex_id"
            " GROUP BY h.fk_chunk_id"
            " HAVING bool_and("
            "        h.source_hash IS NOT NULL"
            "    AND h.source_hash = md5(p.document))"
        )
        with self.Session() as session:
            rows = session.execute(stmt, {"celex_id": celex_id}).all()
        return {str(row[0]) for row in rows}

    def heideltime_delete_chunks(self, chunk_ids: Iterable[str]) -> int:
        """Delete extractions for *chunk_ids* together with their lineage rows.

        Called before re-extracting a stale chunk.  Deleting only the
        ``heideltime_extractions`` rows is not enough: ``fact_source_registry``
        has **no** foreign key on ``heideltime_event_id``, so its rows would
        survive as orphans and ``fact_provenance()`` would keep citing
        extraction ids that no longer exist.

        The registry rows cannot go first either.  Two constraints reference
        ``fact_source_registry(source_id)`` with ``ON DELETE NO ACTION`` --
        ``fact_source_link.source_id`` and ``knowledge_edges.dst_source_id`` --
        so a direct delete raises 23503 as soon as any fact cites the source.
        :func:`heideltime_invalidation_statements` holds that ordering; this
        method just runs it in one transaction.

        Parameters
        ----------
        chunk_ids:
            ``fk_chunk_id`` values whose extractions are stale.  May be empty.

        Returns
        -------
        int
            Number of ``heideltime_extractions`` rows deleted.
        """
        ids = [str(chunk_id) for chunk_id in chunk_ids]
        if not ids:
            return 0

        deleted = 0
        with self.Session() as session:
            for stmt in heideltime_invalidation_statements():
                # The last statement is the extractions delete, so its rowcount
                # is the number the caller is told about.
                deleted = session.execute(stmt, {"chunk_ids": ids}).rowcount
            session.commit()

        logger.info(
            "Deleted %d stale HeidelTime extractions across %d chunks.",
            deleted,
            len(ids),
        )
        return deleted

    def build_heideltime_proxy_fact(
        self, celex_id: str
    ) -> List[Dict[str, Any]]:
        """Return compact metadata for the ``heideltime_raw`` proxy fact.

        Previously ``HeidelTimeIngestor.build_proxy_fact_payload``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        List[Dict]
            Single-element list with extraction statistics, or empty list.
        """
        stmt = text(
            "SELECT COUNT(*) AS extraction_count,"
            "       COUNT(DISTINCT fk_chunk_id) AS chunk_count,"
            "       COUNT(timex_value) AS canonical_date_count,"
            "       MIN(timex_value::text) AS min_date,"
            "       MAX(timex_value::text) AS max_date"
            " FROM celex_facts.heideltime_extractions"
            " WHERE celex_id = :celex_id"
        )
        with self.Session() as session:
            row = session.execute(stmt, {"celex_id": celex_id}).fetchone()

        extraction_count = int(row.extraction_count or 0) if row else 0
        if extraction_count == 0:
            return []

        summary: dict = {
            "status": "available",
            "source_table": "celex_facts.heideltime_extractions",
            "extraction_count": extraction_count,
            "chunk_count": int(row.chunk_count or 0),
            "canonical_date_count": int(row.canonical_date_count or 0),
        }
        if row.min_date is not None:
            summary["min_date"] = row.min_date
        if row.max_date is not None:
            summary["max_date"] = row.max_date
        return [summary]
