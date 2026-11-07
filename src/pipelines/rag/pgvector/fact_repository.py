"""
Unified DAO (Data Access Object) for all celex_facts.facts persistence operations.

Consolidates the read methods previously split across:
  - FactTable (db.py)                 — ORM-based point reads
  - CelexDatabaseToolkit (celex_tools.py) — raw-SQL bulk reads

All queries go through the shared engine from ``session.py`` (single connection pool).
The class satisfies the ``SQLFactStore`` protocol, making it directly
injectable into every DSPy module via the Provisioner pattern.

Exports:
    FactRepository  — the canonical persistence service for celex_facts.facts
    FactTable       — backward-compatible alias; existing imports keep working
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from pipelines.agent_dspy.dspy_protocols import FirstPageVO
from pipelines.rag.pgvector.models import CaseCelexId, Facts, RuleFacts
from pipelines.rag.pgvector.session import column_exists, make_engine, warn_once
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

#: Lineage column added by ``migrate_document_lineage.sql``. Deliberately not
#: mapped on the ``Facts`` ORM model: an unmapped column never appears in an
#: ORM SELECT or INSERT, so every ``Facts`` query keeps working before the
#: migration is applied. It is written by ``_SET_DOCUMENT_ID`` only.
_DOCUMENT_ID_COLUMN = "document_id"
_LINEAGE_MIGRATION = "src/pipelines/rag/pgvector/migrate_document_lineage.sql"
_SET_DOCUMENT_ID = text(
    f"UPDATE {Facts.__table__.fullname} SET {_DOCUMENT_ID_COLUMN} = :document_id "
    "WHERE id = :fact_id"
)


class FactRepository:
    """Unified, protocol-compliant DAO for ``celex_facts.facts``.

    Satisfies the ``SQLFactStore`` protocol so it can be injected directly
    into DSPy modules without any adapter layer.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Defaults to ``DATABASE_URL`` from
        ``vector_config.py`` / environment.  The engine is created via
        ``session.make_engine`` so pool settings and ``search_path`` are
        applied consistently across the whole codebase.
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)
        #: Cached probe of ``facts.document_id`` (None until first needed).
        self._document_id_column: bool | None = None

    # ------------------------------------------------------------------
    # Point reads — previously in FactTable (db.py)
    # ------------------------------------------------------------------

    def select_all_facts_for_celex(self, celex_id: str) -> List[Facts]:
        """Return all ``Facts`` rows for a given celex_id regardless of name.

        Loads every fact for the CELEX document in one query, ordered by name,
        so callers such as the book cross-check loop can iterate all facts
        without knowing the fact vocabulary in advance.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        List[Facts]
            All fact rows ordered by ``name`` (empty list when none exist).
        """
        with self.Session() as session:
            return (
                session.query(Facts)
                .filter(Facts.celex_id == celex_id)
                .order_by(Facts.name)
                .all()
            )

    def select_fact4celex(self, celex_id: str, name: str) -> List[Facts]:
        """Return all ``Facts`` rows for a given celex_id and fact name.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        name:
            The fact name (e.g. ``"fines"``, ``"commissioner"``).

        Returns
        -------
        List[Facts]
            Ordered as returned by the database (insertion order).
        """
        with self.Session() as session:
            return (
                session.query(Facts)
                .filter(Facts.celex_id == celex_id, Facts.name == name)
                .all()
            )

    def select_legacy_fact4celex(self, celex_id: str, name: str) -> List[RuleFacts]:
        """Return legacy ``rule_facts.facts`` rows for a given celex_id and fact name.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        name:
            The legacy fact name (mapped to ``RuleFacts.fact`` column).

        Returns
        -------
        List[RuleFacts]
        """
        with self.Session() as session:
            return (
                session.query(RuleFacts)
                .filter(RuleFacts.celex_id == celex_id, RuleFacts.fact == name)
                .all()
            )

    def select_case_ids_for_celex(self, celex_id: str) -> List[str]:
        """Return distinct case IDs associated with a CELEX ID.

        Executes:
            ``SELECT DISTINCT case_id FROM celex_facts.case_celex_id WHERE celex_id = :celex_id``

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        List[str]
            Distinct ``case_id`` strings.
        """
        with self.Session() as session:
            rows = (
                session.query(CaseCelexId.case_id)
                .filter(CaseCelexId.celex_id == celex_id)
                .distinct()
                .all()
            )
            return [row.case_id for row in rows]

    def select_case_refs(self, celex_id: str, title_fact_name: str) -> List[str]:
        """Return the raw case references of a document: case ids and title values.

        Feeds ``period_inheritance.case_numbers``, which extracts the 5-digit
        case numbers; ``case_celex_id`` alone misses ``DEC-*`` ids and holds
        prefix-only junk (``COMP/E``), so the case title is read alongside.

        Parameters
        ----------
        celex_id:
            The document identifier.
        title_fact_name:
            Name of the case-title fact (``settings.PERIOD_INHERITANCE_CASE_TITLE_FACT``).

        Returns
        -------
        List[str]
            ``case_celex_id.case_id`` values followed by the title fact's ``value_md``.
        """
        sql = text("""
            SELECT case_id FROM celex_facts.case_celex_id WHERE celex_id = :celex_id
            UNION ALL
            SELECT value_md FROM celex_facts.facts
             WHERE celex_id = :celex_id AND name = :title AND value_md IS NOT NULL
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"celex_id": celex_id, "title": title_fact_name}).fetchall()
        return [str(row[0]) for row in rows]

    def select_period_donor_rows(
        self, fact_name: str, gate_fact_name: str, title_fact_name: str, exclude_method: str
    ) -> List[Dict[str, Any]]:
        """Return every stored period fact that could be inherited, with its case refs.

        Parameters
        ----------
        fact_name:
            The period fact (``cartel_period``); ``NA`` rows are excluded.
        gate_fact_name:
            The infringement gate fact (``settings.PERIOD_INHERITANCE_GATE_FACT``).
        title_fact_name:
            The case-title fact whose value carries the case number.
        exclude_method:
            ``fact_source_link.extraction_method`` of inherited values
            (``period_inheritance``); such facts are borrowed, not first-hand,
            and never serve as donors.

        Returns
        -------
        List[Dict[str, Any]]
            Keys ``fact_id`` (UUID), ``celex_id``, ``value_json`` (text),
            ``gate`` (gate ``value_md`` or None) and ``case_refs`` (list of
            case ids and title values).
        """
        sql = text("""
            SELECT f.id AS fact_id, f.celex_id, f.value_json::text AS value_json,
                   g.value_md AS gate,
                   ARRAY(SELECT c.case_id FROM celex_facts.case_celex_id c
                          WHERE c.celex_id = f.celex_id)
                   || ARRAY(SELECT t.value_md FROM celex_facts.facts t
                             WHERE t.celex_id = f.celex_id AND t.name = :title
                               AND t.value_md IS NOT NULL) AS case_refs
              FROM celex_facts.facts f
              LEFT JOIN celex_facts.facts g ON g.celex_id = f.celex_id AND g.name = :gate
             WHERE f.name = :fact AND f.value_md IS NOT NULL AND f.value_md <> 'NA'
               AND NOT EXISTS (SELECT 1 FROM celex_facts.fact_source_link l
                                WHERE l.fact_id = f.id AND l.extraction_method = :exclude)
        """)
        params = {
            "fact": fact_name, "gate": gate_fact_name,
            "title": title_fact_name, "exclude": exclude_method,
        }
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row._mapping) for row in rows]

    def select_newer_cross_document_sources(self, celex_id: str, name: str) -> List[str]:
        """Return the other documents whose source fact was written after this fact.

        A fact copied from another decision (``fact_source_link`` to a ``fact``
        source of a different ``celex_id``, e.g. an inherited cartel period) is
        stale once that source fact is re-extracted: ``upsert_fact`` keeps the
        source's id and moves its ``created_at``. Same-document predecessors
        are not considered; the batch orders those itself.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier of the dependent fact.
        name:
            The dependent fact's name.

        Returns
        -------
        List[str]
            Distinct ``celex_id`` values of the newer source facts, sorted;
            empty when the fact is fresh, has no cross-document source, or
            does not exist.
        """
        sql = text("""
            SELECT DISTINCT s.celex_id
              FROM celex_facts.facts f
              JOIN celex_facts.fact_source_link l ON l.fact_id = f.id
              JOIN celex_facts.fact_source_registry r
                ON r.source_id = l.source_id AND r.source_type = 'fact'
              JOIN celex_facts.facts s ON s.id = r.source_fact_id
             WHERE f.celex_id = :celex_id AND f.name = :name
               AND s.celex_id <> f.celex_id
               AND s.created_at > f.created_at
             ORDER BY s.celex_id
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"celex_id": celex_id, "name": name}).fetchall()
        return [row.celex_id for row in rows]

    # ------------------------------------------------------------------
    # Bulk reads — previously in CelexDatabaseToolkit (celex_tools.py)
    # ------------------------------------------------------------------

    def select_facts4celex(self, celex_id: str) -> Dict[str, str]:
        """Return all facts for a CELEX ID as a ``{name: value_md}`` dict.

        Previously implemented as raw SQL inside ``CelexDatabaseToolkit``.
        Rewritten here as an ORM query so it benefits from the shared
        session/engine and is mockable via the ``SQLFactStore`` protocol.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        Dict[str, str]
            Mapping of fact name → ``value_md`` (empty string when NULL).
        """
        with self.Session() as session:
            rows = (
                session.query(Facts.name, Facts.value_md)
                .filter(Facts.celex_id == celex_id)
                .all()
            )
            return {row.name: row.value_md or "" for row in rows}

    def select_facts4celex_with_ids(
        self, celex_id: str
    ) -> List[tuple[str, str, str]]:
        """Return all facts for a CELEX ID as ``(fact_id, name, value_md)`` rows.

        Identity-preserving sibling of :meth:`select_facts4celex`: it keeps the
        ``Facts.id`` UUID alongside each name/value so callers can attach real
        fact edges (e.g. the knowledge-extraction queue and provenance) instead
        of re-deriving names from rendered text.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        List[tuple[str, str, str]]
            One ``(fact_id, name, value_md)`` tuple per fact row; ``fact_id`` is
            the stringified UUID and ``value_md`` is the empty string when NULL.
        """
        with self.Session() as session:
            rows = (
                session.query(Facts.id, Facts.name, Facts.value_md)
                .filter(Facts.celex_id == celex_id)
                .all()
            )
            return [
                (str(row.id), row.name, row.value_md or "") for row in rows
            ]

    def select_facts_by_ids(
        self, fact_ids: List[str]
    ) -> Dict[str, tuple[str, str]]:
        """Return a ``{fact_id: (name, value_md)}`` map for the given fact UUIDs.

        Identity-keyed lookup used when callers already hold fact UUIDs (e.g. the
        knowledge-extraction queue) and need each fact's name/value to render a
        citable catalog.  The returned mapping is keyed by the stringified UUID
        so the caller can preserve its own input ordering; rows whose ``id`` is
        not found are simply absent from the result.

        Parameters
        ----------
        fact_ids:
            Fact UUIDs (as strings or ``uuid.UUID``).  An empty list yields an
            empty mapping without touching the database.

        Returns
        -------
        Dict[str, tuple[str, str]]
            ``{fact_id: (name, value_md)}``; ``value_md`` is the empty string
            when NULL.
        """
        if not fact_ids:
            return {}
        normalized_ids = [str(fact_id) for fact_id in fact_ids]
        with self.Session() as session:
            rows = (
                session.query(Facts.id, Facts.name, Facts.value_md)
                .filter(Facts.id.in_(normalized_ids))
                .all()
            )
            return {
                str(row.id): (row.name, row.value_md or "") for row in rows
            }

    def select_named_facts4celex(
        self, celex_id: str, names: List[str]
    ) -> List[Facts]:
        """Return the facts of a CELEX ID whose name is in ``names``, in that order.

        An allow-list rather than a deny-list: a fact name retired from
        ``FACT_EXTRACTOR.yaml`` keeps its rows in the table, so a query over
        "everything except X" silently feeds outdated facts to its caller.
        Names with no stored row are simply absent from the result.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        names:
            Fact names to fetch; their order determines the result order.

        Returns
        -------
        List[Facts]
            Detached ``Facts`` rows ordered by their position in ``names``.
        """
        if not names:
            return []
        with self.Session() as session:
            rows = (
                session.query(Facts)
                .filter(Facts.celex_id == celex_id)
                .filter(Facts.name.in_(names))
                .all()
            )
        position = {name: index for index, name in enumerate(names)}
        return sorted(rows, key=lambda row: position[row.name])

    def select_available_fact_names(self, celex_id: str) -> List[str]:
        """Return the distinct fact names stored for a CELEX ID.

        Previously implemented as raw SQL inside ``CelexDatabaseToolkit``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        List[str]
            Sorted list of distinct fact names.
        """
        with self.Session() as session:
            rows = (
                session.query(Facts.name)
                .filter(Facts.celex_id == celex_id)
                .distinct()
                .all()
            )
            return sorted(row.name for row in rows)

    def select_distinct_fact_names(self) -> List[str]:
        """Return every distinct fact ``name`` in the table, across all cases.

        Equivalent to::

            SELECT DISTINCT name FROM celex_facts.facts

        Used to ground the Master Planner in the live fact vocabulary instead
        of a frozen enum (see ``master_planner_module``).

        No name-pattern exclusion is applied: integration tests delete their own
        scaffolding rows in ``tearDown``, so the table holds only real extracted
        facts.  Infrastructure names are dropped downstream by
        ``build_fact_name_catalog`` from ``settings.EVIDENCE_EXCLUDED_FACT_NAMES``.

        Returns
        -------
        List[str]
            Sorted list of distinct fact names.
        """
        with self.Session() as session:
            rows = session.query(Facts.name).distinct().all()
            return sorted(row.name for row in rows if row.name)

    def select_boolean_fact_names(self) -> List[str]:
        """Return the distinct fact names whose value is a boolean literal.

        A fact is treated as a boolean (polarity) claim when its ``value_md`` is
        exactly ``True`` or ``False`` (case-insensitive). The set is derived from
        the data rather than a hardcoded list, equivalent to::

            SELECT DISTINCT name FROM celex_facts.facts
            WHERE lower(value_md) IN ('true', 'false')

        Returns
        -------
        List[str]
            Sorted list of distinct boolean fact names.
        """
        from sqlalchemy import func

        with self.Session() as session:
            rows = (
                session.query(Facts.name)
                .filter(func.lower(Facts.value_md).in_(("true", "false")))
                .distinct()
                .all()
            )
            return sorted(row.name for row in rows)

    def select_first_page4celex(self, celex_id: str) -> Optional["FirstPageVO"]:
        """Return the first page (doc_pos=0) chunk for a celex_id from pg_embedding_qwen.

        Lineage-aware variant: unlike ``ChunkRepository.select_first_page`` (which
        returns only text), this carries the immutable chunk ``id`` and ``doc_pos``
        so callers can record exact chunk provenance instead of assuming a
        positional reference.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        Optional[FirstPageVO]
            The first-page chunk identity and text if found, otherwise None.
        """
        from sqlalchemy import text

        with self.Session() as session:
            result = session.execute(
                text(
                    "SELECT id, doc_pos, document FROM celex_facts.pg_embedding_qwen "
                    "WHERE celex_id = :celex_id AND doc_pos = 0"
                ),
                {"celex_id": celex_id},
            ).fetchone()
            if not result:
                return None
            return FirstPageVO(
                chunk_id=str(result[0]), doc_pos=int(result[1]), document=result[2]
            )

    # ------------------------------------------------------------------
    # Existence check — used by FactIngestor / FactRagPipeline
    # ------------------------------------------------------------------

    def exists(self, celex_id: str, name: str) -> bool:
        """Return ``True`` when at least one fact row exists for the given key.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        name:
            The fact name.
        """
        with self.Session() as session:
            return (
                session.query(Facts)
                .filter(Facts.celex_id == celex_id, Facts.name == name)
                .first()
            ) is not None

    # ------------------------------------------------------------------
    # Cartel / legal period context (view-based read)
    # ------------------------------------------------------------------

    def select_cartel_and_legal_period_context(self, celex_id: str) -> str:
        """Return cartel and legal period context info for *celex_id*.

        Queries ``celex_facts.facts_cartel_legal_period_category_vw`` and
        returns a JSON string.

        Previously inline in ``CelexDatabaseToolkit.select_cartel_and_legal_period_context``.
        """
        import json as _json
        from sqlalchemy import text

        with self.engine.connect() as conn:
            query = text("""
                SELECT celex_id, cartel_period, legal_period, periods_category
                FROM celex_facts.facts_cartel_legal_period_category_vw
                WHERE celex_id = :celex_id
            """)
            result = conn.execute(query, {"celex_id": celex_id}).fetchone()

            if result:
                return _json.dumps({
                    "celex_id": result[0],
                    "cartel_period": result[1],
                    "legal_period": result[2],
                    "periods_category": result[3]
                }, indent=2, default=str)
            else:
                return _json.dumps({"error": "No data found for the given celex_id"})

    def select_latest_question(self, celex_id: str, fact_name: str) -> str | None:
        """Return the most recent extraction question for a fact, or ``None``.

        Reads ``celex_facts.fact_ingestion_audit_trail``, preferring successful
        runs and the newest ``created_at``. A fact may have several audit rows
        across re-runs (and a NULL ``fact_id`` for failed runs), so the lookup
        is keyed on ``(celex_id, target_fact_name)`` rather than ``fact_id``.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        fact_name:
            The fact name (matches ``target_fact_name`` in the audit trail).

        Returns
        -------
        str or None
            The recorded extraction question, or ``None`` when none exists.
        """
        from sqlalchemy import text

        query = text(
            "SELECT question "
            "FROM celex_facts.fact_ingestion_audit_trail "
            "WHERE celex_id = :celex_id AND target_fact_name = :fact_name "
            "  AND question IS NOT NULL AND question <> '' "
            "ORDER BY success DESC, created_at DESC "
            "LIMIT 1"
        )
        with self.engine.connect() as conn:
            row = conn.execute(
                query, {"celex_id": celex_id, "fact_name": fact_name}
            ).fetchone()
        return row.question if row is not None else None

    # ------------------------------------------------------------------
    # Upsert — atomic insert-or-update with self-healing dedup
    # ------------------------------------------------------------------

    def upsert_fact(
        self,
        celex_id: str,
        name: str,
        value_md: str,
        value_json: str,
        references_doc_json=None,
        document_id: str | None = None,
    ):
        """Atomic upsert: insert-or-update with self-healing duplicate cleanup.

        If duplicate rows already exist for the ``(celex_id, name)`` key (from
        a prior race or migration), the extras are deleted in the same
        transaction so the table converges back to at most one row per key.

        ``document_id`` is written in the same transaction by a separate
        UPDATE, and only when it is not None and the ``facts.document_id``
        column exists (probed once per repository). A None never clears a
        stored value, and a missing column (migration not applied) logs one
        warning per process and skips the lineage write.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        name:
            The fact name (e.g. ``"timeline"``).
        value_md:
            Markdown-formatted fact value.
        value_json:
            JSON-serialised fact value.
        references_doc_json:
            Optional JSONB references payload.
        document_id:
            The decision's source document (``document_registry``), or None
            to leave the stored lineage untouched.

        Returns
        -------
        uuid.UUID
            The primary-key ``id`` of the surviving row.

        Raises
        ------
        sqlalchemy.exc.SQLAlchemyError
            On database errors (the transaction is rolled back).
        """
        import datetime
        import uuid

        # Probe before the row-locking session: the probe needs its own pooled
        # connection, which must never be awaited while FOR UPDATE locks are held.
        write_lineage = document_id is not None and self._has_document_id_column()

        with self.Session() as session:
            existing_rows = (
                session.query(Facts)
                .filter(Facts.celex_id == celex_id, Facts.name == name)
                .order_by(Facts.created_at.desc())
                .with_for_update()
                .all()
            )

            if existing_rows:
                # Keep the most recent row, purge accidental duplicates.
                survivor = existing_rows[0]
                for dup in existing_rows[1:]:
                    logger.warning(
                        "Removing duplicate fact row %s for (%s, %s)",
                        dup.id, celex_id, name,
                    )
                    session.delete(dup)

                survivor.value_md = value_md
                survivor.value_json = value_json
                survivor.references_doc_json = references_doc_json
                survivor.created_at = datetime.datetime.now()
                fact_id = survivor.id
            else:
                new_id = uuid.uuid4()
                new_fact = Facts(
                    id=new_id,
                    celex_id=celex_id,
                    name=name,
                    value_md=value_md,
                    value_json=value_json,
                    references_doc_json=references_doc_json,
                )
                session.add(new_fact)
                session.flush()
                fact_id = new_fact.id

            if write_lineage:
                session.execute(
                    _SET_DOCUMENT_ID, {"document_id": document_id, "fact_id": fact_id}
                )

            try:
                session.commit()
                logger.info(
                    "Upserted fact '%s' for %s (id=%s)", name, celex_id, fact_id
                )
                return fact_id
            except Exception as e:
                session.rollback()
                logger.error("DB Error upserting fact for %s: %s", celex_id, e)
                raise

    def _has_document_id_column(self) -> bool:
        """Return whether ``facts.document_id`` exists, probing once.

        A failed probe counts as absent for the rest of the process and logs
        the exception in one warning; a genuinely absent column logs a
        separate one-time "missing" warning.

        Returns
        -------
        bool
            True once ``migrate_document_lineage.sql`` has been applied.
        """
        if self._document_id_column is None:
            try:
                with self.engine.connect() as conn:
                    present = column_exists(
                        conn, Facts.__table__.fullname, _DOCUMENT_ID_COLUMN
                    )
            except Exception as exc:
                warn_once(
                    logger, "facts.document_id.probe",
                    "facts.document_id lineage probe failed, writing NULL: %s", exc,
                )
                self._document_id_column = False
                return False
            if not present:
                warn_once(
                    logger, "facts.document_id",
                    "Column %s.%s is missing; facts are saved without lineage. "
                    "Apply %s.", Facts.__table__.fullname, _DOCUMENT_ID_COLUMN,
                    _LINEAGE_MIGRATION,
                )
            self._document_id_column = present
        return self._document_id_column



# ---------------------------------------------------------------------------
# Backward-compatible alias
# ---------------------------------------------------------------------------
# All existing imports of the form ``from pipelines.rag.pgvector.db import FactTable``
# continue to work without modification.  New code should import FactRepository directly.
FactTable = FactRepository

