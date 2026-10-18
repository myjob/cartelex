"""Repository for the typed entity graph (``entities`` / ``entity_edges``).

Follows the Provisioner pattern: the SQLAlchemy engine and session factory are
injected (or built once) in ``__init__`` and reused.  Write methods accept an
optional ``session`` so the materialization batch can compose one transaction
per CELEX document; read methods answer the cross-case questions the graph
exists for (company overlap across cartels, fines per company, co-participation
networks) using recursive CTEs modelled on ``celex_facts.fact_provenance()``.

Company resolution is a tiered ladder, cheapest and most trustworthy first:
alias canonicalization, then ``celex_facts.company_registry_master_vw`` at
``source = 'lei'`` (the roster's already-resolved ``fk_lei``, joined on the
``corp_lei`` primary key — 0.45 ms), then the pg_trgm gist probe against
``corp_lei.legal_name`` as the fallback for off-roster mentions.  The view is
the intended identity interface; the trigram probe is the only remaining direct
read of a corp table and exists because the view is roster-anchored and cannot
answer for a ``fines`` / ``leniency_*`` spelling that never became a roster row.
The gist index is mandatory for that fallback, so the fuzzy match must not wrap
the column in a function.  Results are cached per run because the same company
recurs across many decisions.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings
from pipelines.graph.entity_graph_models import (
    EdgeRelation,
    EntityKind,
    GraphNeighbourhood,
    GraphPeer,
    GraphSeed,
    ValidatedRelation,
)
from pipelines.rag.pgvector.models import Entity, EntityEdge
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")

# Characters that would change the meaning of the ``~*`` pattern a name stem is
# spliced into.  The stem is a bound parameter, so this is not an injection
# guard - it stops a stray bracket or quantifier from turning a company name
# into a regex that matches everything (or nothing).
_STEM_METACHAR_RE = re.compile(r"[\\^$.|?*+()\[\]{}]")

# Company nodes whose canonical_name carries the stem as a WHOLE WORD and that
# actually have an edge of the traversed relation.  Word-boundary matching is
# deliberate: substring matching on 'bayer' also pulls in "Bayerische Hypo- und
# Vereinsbank AG" and "Bayerischer Rundfunk", unrelated companies that merely
# share a Bavarian prefix.  The inner join doubles as the edge-count projection
# and skips company nodes left edge-less by retired fact families.
_SEED_CTE = r"""
seeds AS (
    SELECT e.entity_id, e.canonical_name, e.lei_id,
           count(ed.edge_id) AS edge_count
    FROM celex_facts.entities e
    JOIN celex_facts.entity_edges ed
      ON ed.src_entity_id = e.entity_id
     AND ed.relation = CAST(:relation AS celex_facts.edge_relation_enum)
    WHERE e.kind = 'company'
      AND e.canonical_name ~* ('\m' || :stem || '\M')
    GROUP BY e.entity_id, e.canonical_name, e.lei_id
    ORDER BY count(ed.edge_id) DESC, e.canonical_name
    LIMIT :max_seeds
)
"""

_SEEDS_SQL = f"""
WITH {_SEED_CTE}
SELECT entity_id, canonical_name, lei_id, edge_count
FROM seeds
ORDER BY edge_count DESC, canonical_name
"""

# Recursive company -> case -> company walk.  ``visited`` starts as the whole
# seed array rather than a single root, so an alias variant of the seed company
# is never reported as its own peer; it doubles as the cycle guard.  The linking
# case and its edge lineage anchor ride along so a peer can cite its evidence.
_CO_PARTICIPANTS_SQL = f"""
WITH RECURSIVE {_SEED_CTE},
seed_ids AS (SELECT array_agg(entity_id) AS ids FROM seeds),
cohort AS (
    SELECT s.entity_id AS company_id, 0 AS depth,
           (SELECT ids FROM seed_ids) AS visited,
           NULL::text AS via_celex, NULL::uuid AS via_fact
    FROM seeds s
    UNION ALL
    SELECT peer.src_entity_id, c.depth + 1,
           c.visited || peer.src_entity_id,
           peer.celex_id, peer.source_fact_id
    FROM cohort c
    JOIN celex_facts.entity_edges mine
      ON mine.src_entity_id = c.company_id
     AND mine.relation = CAST(:relation AS celex_facts.edge_relation_enum)
    JOIN celex_facts.entity_edges peer
      ON peer.dst_entity_id = mine.dst_entity_id
     AND peer.relation = CAST(:relation AS celex_facts.edge_relation_enum)
    WHERE c.depth < :max_hops
      AND NOT peer.src_entity_id = ANY(c.visited)
)
SELECT e.entity_id, e.canonical_name, e.lei_id, min(c.depth) AS hops,
       array_agg(DISTINCT c.via_celex) FILTER (WHERE c.via_celex IS NOT NULL)
           AS shared_celex_ids,
       array_agg(DISTINCT c.via_fact) FILTER (WHERE c.via_fact IS NOT NULL)
           AS source_fact_ids
FROM cohort c
JOIN celex_facts.entities e ON e.entity_id = c.company_id
WHERE c.depth > 0
GROUP BY e.entity_id, e.canonical_name, e.lei_id
ORDER BY hops, e.canonical_name
LIMIT :max_peers
"""


def _edge_amount(value_json: str | None) -> str | None:
    """Lift the ``amount`` field out of an edge's JSON payload.

    Args:
        value_json: The edge's ``value_json`` column, or ``None``.

    Returns:
        The amount string, or ``None`` when the payload is absent, unparseable
        or carries no amount.  A malformed payload is treated as "no amount"
        rather than raised: the column is written by the materialization batch
        and a decode failure must not break an unrelated read.
    """
    if not value_json:
        return None
    try:
        payload = json.loads(value_json)
    except (ValueError, TypeError):
        logger.warning("Unparseable edge value_json: %r", value_json[:120])
        return None
    return payload.get("amount") if isinstance(payload, dict) else None


def _sanitize_stem(name_stem: str) -> str:
    """Strip regex metacharacters and collapse whitespace in a seed stem.

    Args:
        name_stem: Raw stem as written by the caller or the planner LLM.

    Returns:
        The cleaned stem, or ``""`` when nothing usable remains.
    """
    cleaned = _STEM_METACHAR_RE.sub(" ", name_stem or "")
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


class CompanyResolution:
    """Resolved identity of a company mention.

    Attributes
    ----------
    natural_key:
        Dedup identity for the ``entities`` row — the LEI when resolved, else
        ``'name:'<normalized>`` so aliases of the same company collapse.
    canonical_name:
        Human-readable label (alias-canonical form when known, else the mention).
    lei_id:
        The resolved LEI, or ``None`` when no tier accepted one.
    source:
        Which tier produced ``lei_id`` — ``"registry"`` for the roster-resolved
        ``company_registry_master_vw`` lookup, ``"trigram"`` for the
        ``corp_lei`` fallback, ``"below_floor"`` when the mention was too short
        to be keyed at all, and ``"unresolved"`` when every tier declined.
        Carried so the batch can report the tier mix without the repository
        owning a counter dict.
    """

    __slots__ = ("natural_key", "canonical_name", "lei_id", "source")

    def __init__(
        self,
        natural_key: str,
        canonical_name: str,
        lei_id: str | None,
        source: str = "unresolved",
    ) -> None:
        self.natural_key = natural_key
        self.canonical_name = canonical_name
        self.lei_id = lei_id
        self.source = source


class EntityGraphRepository:
    """CRUD + query surface for ``celex_facts.entities`` / ``entity_edges``.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string; defaults to ``DATABASE_URL``.
    engine:
        Pre-built engine to reuse a connection pool (the batch injects one);
        created from ``db_url`` when omitted.
    lei_match_threshold:
        Minimum pg_trgm similarity to accept a fuzzy LEI match; defaults to
        ``settings.ENTITY_LEI_MATCH_THRESHOLD``.
    lei_min_name_length:
        Shortest canonical mention that may be keyed on an LEI at all; defaults
        to ``settings.ENTITY_LEI_MIN_NAME_LENGTH``.
    """

    def __init__(
        self,
        db_url: str = DATABASE_URL,
        engine: Engine | None = None,
        lei_match_threshold: float | None = None,
        lei_min_name_length: int | None = None,
    ) -> None:
        self.engine = engine if engine is not None else make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)
        self.lei_match_threshold = (
            lei_match_threshold
            if lei_match_threshold is not None
            else settings.ENTITY_LEI_MATCH_THRESHOLD
        )
        self.lei_min_name_length = (
            lei_min_name_length
            if lei_min_name_length is not None
            else settings.ENTITY_LEI_MIN_NAME_LENGTH
        )
        # Per-run cache: raw company mention -> resolution (transaction-independent).
        self._company_cache: dict[str, CompanyResolution] = {}
        # Per-run cache: mention -> canonical name (or None). Separate from the
        # cache above because the temporal pass calls canonicalize_alias for
        # every distinct HeidelTime actor, most of which never reach
        # resolve_company. Without it that is ~13k uncached lookups against a
        # predicate (lower(company_alias)) that no index on the table supports.
        self._alias_cache: dict[str, str | None] = {}

    @contextmanager
    def _session_scope(self, session: Session | None) -> Iterator[Session]:
        """Yield a session, owning its lifecycle only when one is not supplied."""
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
    # Reads driving materialization
    # ------------------------------------------------------------------

    def distinct_celex_ids(self, session: Session | None = None) -> list[str]:
        """Return every CELEX id that has at least one fact, sorted."""
        sql = text(
            "SELECT DISTINCT celex_id FROM celex_facts.facts "
            "WHERE celex_id IS NOT NULL ORDER BY celex_id"
        )
        with self._session_scope(session) as active:
            return [row[0] for row in active.execute(sql).fetchall()]

    def fetch_facts_for_celex(
        self, celex_id: str, session: Session | None = None
    ) -> list[dict[str, Any]]:
        """Fetch ``id, name, value_md, value_json`` for one document's facts."""
        sql = text(
            "SELECT id, name, value_md, value_json FROM celex_facts.facts "
            "WHERE celex_id = :celex_id"
        )
        with self._session_scope(session) as active:
            rows = active.execute(sql, {"celex_id": celex_id}).fetchall()
        return [dict(row._mapping) for row in rows]

    def case_name(self, celex_id: str, session: Session | None = None) -> str:
        """Return a human-readable case label for a CELEX id.

        Uses ``celex_case_id.case_id`` when present, else the CELEX id itself.
        """
        sql = text(
            "SELECT case_id FROM celex_facts.celex_case_id WHERE celex_id = :celex_id"
        )
        with self._session_scope(session) as active:
            row = active.execute(sql, {"celex_id": celex_id}).fetchone()
        if row is not None and row[0]:
            return str(row[0])
        return celex_id

    # ------------------------------------------------------------------
    # Entity resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Casefold, NFKC-fold and collapse whitespace for name-based keys.

        NFKC matches what ``Main_ingest_company_investigated2celex`` applied
        when it wrote the roster, so a mention carrying a compatibility
        character keys onto the same node as its roster spelling instead of
        splitting off a near-duplicate.
        """
        folded = unicodedata.normalize("NFKC", name)
        return _WHITESPACE_RE.sub(" ", folded).strip().casefold()

    def resolve_company(self, name: str, session: Session) -> CompanyResolution:
        """Resolve a company mention to a dedup identity, LEI when confident.

        A four-tier ladder, most trustworthy first:

        1. ``company_canonical2alias`` maps an alias mention onto its canonical
           spelling (a keyed table lookup, so it is evidence, not a guess).  It
           runs first because it can turn a short mention into a long canonical
           name that then clears the length floor.
        2. The **length floor** (``settings.ENTITY_LEI_MIN_NAME_LENGTH``).  A
           mention shorter than the floor is never keyed on an LEI by *any*
           tier.  Both identity sources demonstrably fail there and fail
           differently — the roster's substring match gives ``Gant -> GANTE``,
           the trigram probe gives ``AT&T -> AT`` — so the floor, not the choice
           of source, is what stops false merges.
        3. ``celex_facts.company_registry_master_vw`` at ``source = 'lei'``.
           This is the roster's own ``fk_lei``, joined on the ``corp_lei``
           primary key: the identity the rest of the system already treats as
           authoritative, reached in 0.45 ms against 0.825 s for the probe below.
        4. The pg_trgm fallback against ``corp_lei.legal_name``, kept because
           the view is roster-anchored and cannot answer for a ``fines`` /
           ``leniency_*`` spelling that never became a roster row (1,373 of the
           7,015 distinct mentions, measured 20260909).

        A match yields an LEI-keyed identity so the company collapses across all
        its decisions; otherwise the identity is name-based.

        Parameters
        ----------
        name:
            The extracted company string.
        session:
            Active session used for the (read-only) lookups.

        Returns
        -------
        CompanyResolution
            The resolved natural key, canonical name, optional LEI, and the name
            of the tier that supplied it.
        """
        cached = self._company_cache.get(name)
        if cached is not None:
            return cached

        alias = self.canonicalize_alias(name, session)
        canonical_name = unicodedata.normalize(
            "NFKC", alias if alias else name
        ).strip()

        lei_id: str | None = None
        source = "unresolved"
        if len(canonical_name) < self.lei_min_name_length:
            source = "below_floor"
        else:
            lei_id = self._registry_lei(canonical_name, session)
            if lei_id is not None:
                source = "registry"
            else:
                lei_id = self._trigram_lei(canonical_name, session)
                if lei_id is not None:
                    source = "trigram"

        if lei_id is not None:
            resolution = CompanyResolution(
                natural_key=lei_id,
                canonical_name=canonical_name,
                lei_id=lei_id,
                source=source,
            )
        else:
            resolution = CompanyResolution(
                natural_key=f"name:{self._normalize_name(canonical_name)}",
                canonical_name=canonical_name,
                lei_id=None,
                source=source,
            )

        self._company_cache[name] = resolution
        return resolution

    def _registry_lei(self, canonical_name: str, session: Session) -> str | None:
        """Return the roster-resolved LEI for a company name, or ``None``.

        Reads ``celex_facts.company_registry_master_vw`` restricted to
        ``source = 'lei'``.  The constant qualifier prunes the view's other
        three ``UNION ALL`` branches at plan time (``One-Time Filter: false``),
        leaving two index scans — the roster's unique index on ``company_name``
        and the ``corp_lei`` primary key.  It never trigram-scans anything.

        The lookup is deliberately not keyed on ``celex_id``: ``fk_lei`` is a
        pure function of ``company_name``, which is what keeps the name-keyed
        ``_company_cache`` correct.  ``ORDER BY registry_id`` makes the pick
        reproducible; measured 20260909, no ``company_name`` on this branch
        carries two distinct ``registry_id`` values, so the ordering is a
        guarantee rather than an active tiebreak.

        Never read any other ``source`` here.  The ``llm`` branch is an
        unverified model assertion and keying a node on it would silently merge
        distinct companies; ``opencorporates`` and ``handelsregister`` carry
        register numbers, not LEIs.

        Parameters
        ----------
        canonical_name:
            The alias-canonical, NFKC-folded mention.
        session:
            Active session used for the read-only lookup.

        Returns
        -------
        str | None
            The LEI, or ``None`` when the roster holds none for this name.
        """
        sql = text(
            "SELECT registry_id FROM celex_facts.company_registry_master_vw "
            "WHERE source = 'lei' AND company_name = :name "
            "ORDER BY registry_id LIMIT 1"
        )
        row = session.execute(sql, {"name": canonical_name}).fetchone()
        return str(row[0]) if row is not None and row[0] else None

    def _trigram_lei(self, canonical_name: str, session: Session) -> str | None:
        """Fuzzy-match a company name against ``corp_lei``, or return ``None``.

        The fallback for off-roster mentions.  Rides the pg_trgm gist index on
        ``corp_lei.legal_name``, so the column must stay unwrapped; a match is
        accepted only at or above ``self.lei_match_threshold``.

        Parameters
        ----------
        canonical_name:
            The alias-canonical, NFKC-folded mention.
        session:
            Active session used for the read-only lookup.

        Returns
        -------
        str | None
            The matched LEI, or ``None`` when nothing clears the threshold.
        """
        sql = text(
            "SELECT lei, "
            "celex_facts.similarity(legal_name, :name) AS sim "
            "FROM celex_facts.corp_lei "
            "WHERE legal_name % :name "
            "ORDER BY legal_name <-> :name "
            "LIMIT 1"
        )
        row = session.execute(sql, {"name": canonical_name}).fetchone()
        if row is None or row[1] is None:
            return None
        if float(row[1]) < self.lei_match_threshold:
            return None
        return str(row[0])

    def canonicalize_alias(self, name: str, session: Session) -> str | None:
        """Return the canonical company name for an alias mention, or ``None``.

        Public because the temporal pass in ``Main_materialize_entity_graph``
        uses it as its alias-matching tier: it is a keyed table lookup, unlike
        the trigram probe in :meth:`resolve_company`, so it is safe to run
        against free-text HeidelTime actor strings.

        Parameters
        ----------
        name:
            The mention to canonicalize.
        session:
            Active session used for the read-only lookup.

        Returns
        -------
        str | None
            The canonical name, or ``None`` when the mention is not a known alias.
        """
        key = name.strip()
        if key in self._alias_cache:
            return self._alias_cache[key]
        sql = text(
            "SELECT company_canonical FROM celex_facts.company_canonical2alias "
            "WHERE lower(company_alias) = lower(:name) LIMIT 1"
        )
        row = session.execute(sql, {"name": key}).fetchone()
        canonical = str(row[0]) if row is not None and row[0] else None
        self._alias_cache[key] = canonical
        return canonical

    # ------------------------------------------------------------------
    # Upserts
    # ------------------------------------------------------------------

    def upsert_entity(
        self,
        session: Session,
        kind: EntityKind,
        natural_key: str,
        canonical_name: str,
        lei_id: str | None = None,
        nace_code: str | None = None,
        celex_id: str | None = None,
        article_number: int | None = None,
        attrs_json: str | None = None,
    ) -> UUID:
        """Upsert an entity on ``(kind, natural_key)`` and return its ``entity_id``.

        Idempotent: on conflict the descriptive columns are refreshed and the
        existing id returned, so the same company across decisions maps to one row.
        """
        stmt = (
            pg_insert(Entity)
            .values(
                entity_id=uuid.uuid4(),
                kind=kind.value,
                canonical_name=canonical_name,
                natural_key=natural_key,
                lei_id=lei_id,
                nace_code=nace_code,
                celex_id=celex_id,
                article_number=article_number,
                attrs_json=attrs_json,
            )
            .on_conflict_do_update(
                index_elements=["kind", "natural_key"],
                set_={
                    "canonical_name": canonical_name,
                    "lei_id": lei_id,
                    "nace_code": nace_code,
                    "celex_id": celex_id,
                    "article_number": article_number,
                    "attrs_json": attrs_json,
                },
            )
            .returning(Entity.entity_id)
        )
        return session.execute(stmt).scalar_one()

    def upsert_edge(
        self,
        session: Session,
        src_entity_id: UUID,
        dst_entity_id: UUID,
        relation: EdgeRelation,
        celex_id: str,
        source_fact_id: UUID,
        value_json: str | None = None,
    ) -> None:
        """Upsert an edge on ``(src, dst, relation, source_fact_id)``.

        The payload (``value_json``) is refreshed in place on re-materialization;
        ``source_fact_id`` keeps the edge resolvable via ``fact_provenance()``.
        """
        stmt = (
            pg_insert(EntityEdge)
            .values(
                edge_id=uuid.uuid4(),
                src_entity_id=src_entity_id,
                dst_entity_id=dst_entity_id,
                relation=relation.value,
                celex_id=celex_id,
                value_json=value_json,
                source_fact_id=source_fact_id,
            )
            .on_conflict_do_update(
                index_elements=[
                    "src_entity_id",
                    "dst_entity_id",
                    "relation",
                    "source_fact_id",
                ],
                set_={"value_json": value_json, "celex_id": celex_id},
            )
        )
        session.execute(stmt)

    def delete_edges_for_celex(self, celex_id: str, session: Session) -> int:
        """Delete all edges for one document (per-CELEX rebuild). Returns count."""
        result = session.execute(
            text("DELETE FROM celex_facts.entity_edges WHERE celex_id = :celex_id"),
            {"celex_id": celex_id},
        )
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # Cross-case queries
    # ------------------------------------------------------------------

    def relations_for_facts(
        self,
        fact_ids: Sequence[str | UUID],
        limit: int | None = None,
        session: Session | None = None,
    ) -> tuple[ValidatedRelation, ...]:
        """Return the validated edges that were materialized from *fact_ids*.

        The reverse of the lineage direction every other read walks: instead of
        asking which evidence backs an edge, this asks which edges a set of
        facts produced.  ``entity_edges.source_fact_id`` is ``NOT NULL`` and
        indexed (``ix_edges_source_fact``), so the lookup is a single indexed
        scan plus two endpoint joins.

        Used by the knowledge-ingestion pipeline to recover the relational
        structure behind the facts an agent cited, which the numbered fact
        catalog flattens away.

        Args:
            fact_ids: Fact UUIDs, as strings or ``UUID`` objects. An empty
                sequence returns an empty tuple without touching the database.
            limit: Maximum rows to return; defaults to
                ``settings.KNOWLEDGE_GRAPH_EVIDENCE_MAX_RELATIONS``.  Edges per
                fact are heavily skewed - a participation family emits one edge
                per party, and the largest single fact carries 196 - so an
                unbounded read on a wide trace is a real risk.
            session: Optional session to compose into a caller's transaction.

        Returns:
            The matching relations, ordered by source name, relation and
            decision so the rendered block is stable across runs.  A fact that
            produced no edge simply contributes nothing.
        """
        if not fact_ids:
            return ()
        row_limit = (
            limit
            if limit is not None
            else settings.KNOWLEDGE_GRAPH_EVIDENCE_MAX_RELATIONS
        )
        sql = text(
            """
            SELECT s.canonical_name AS src_name,
                   CAST(ed.relation AS text) AS relation,
                   d.canonical_name AS dst_name,
                   ed.celex_id,
                   ed.value_json,
                   CAST(ed.source_fact_id AS text) AS source_fact_id
            FROM celex_facts.entity_edges ed
            JOIN celex_facts.entities s ON s.entity_id = ed.src_entity_id
            JOIN celex_facts.entities d ON d.entity_id = ed.dst_entity_id
            WHERE ed.source_fact_id = ANY(CAST(:fact_ids AS uuid[]))
            ORDER BY s.canonical_name, ed.relation, ed.celex_id
            LIMIT :limit
            """
        )
        with self._session_scope(session) as active:
            rows = active.execute(
                sql,
                {
                    "fact_ids": [str(fact_id) for fact_id in fact_ids],
                    "limit": row_limit,
                },
            ).fetchall()

        relations: list[ValidatedRelation] = []
        for row in rows:
            mapping = dict(row._mapping)
            relations.append(
                ValidatedRelation(
                    src_name=mapping["src_name"],
                    relation=EdgeRelation(mapping["relation"]),
                    dst_name=mapping["dst_name"],
                    celex_id=mapping["celex_id"],
                    source_fact_id=mapping["source_fact_id"],
                    amount=_edge_amount(mapping["value_json"]),
                )
            )
        return tuple(relations)

    def companies_in_multiple_cartels(
        self, min_cartels: int = 2, session: Session | None = None
    ) -> list[dict[str, Any]]:
        """Companies that participated in more than one case, most-frequent first."""
        sql = text(
            """
            SELECT e.entity_id, e.canonical_name, e.lei_id,
                   count(DISTINCT ed.dst_entity_id) AS cartel_count
            FROM celex_facts.entities e
            JOIN celex_facts.entity_edges ed
              ON ed.src_entity_id = e.entity_id
             AND ed.relation = 'participated_in'
            WHERE e.kind = 'company'
            GROUP BY e.entity_id, e.canonical_name, e.lei_id
            HAVING count(DISTINCT ed.dst_entity_id) >= :min_cartels
            ORDER BY cartel_count DESC, e.canonical_name
            """
        )
        with self._session_scope(session) as active:
            rows = active.execute(sql, {"min_cartels": min_cartels}).fetchall()
        return [dict(row._mapping) for row in rows]

    def fines_for_company(
        self, entity_id: UUID, session: Session | None = None
    ) -> list[dict[str, Any]]:
        """All fine edges for a company, one row per case, with the amount payload."""
        sql = text(
            """
            SELECT c.celex_id, c.canonical_name AS case_name, ed.value_json
            FROM celex_facts.entity_edges ed
            JOIN celex_facts.entities c ON c.entity_id = ed.dst_entity_id
            WHERE ed.src_entity_id = :entity_id AND ed.relation = 'fined'
            ORDER BY c.celex_id
            """
        )
        with self._session_scope(session) as active:
            rows = active.execute(sql, {"entity_id": entity_id}).fetchall()
        return [dict(row._mapping) for row in rows]

    def co_participation_network(
        self,
        root_company_id: UUID,
        max_depth: int = 3,
        session: Session | None = None,
    ) -> list[dict[str, Any]]:
        """Companies reachable from a root via shared cases (company<->case<->company).

        A recursive CTE mirroring ``fact_provenance()``: a ``visited`` uuid array
        guards cycles and ``max_depth`` bounds the walk.  Returns each peer with
        its minimum hop distance from the root.
        """
        sql = text(
            """
            WITH RECURSIVE cohort AS (
                SELECT CAST(:root AS uuid) AS company_id, 0 AS depth,
                       ARRAY[CAST(:root AS uuid)] AS visited
                UNION ALL
                SELECT peer.src_entity_id, c.depth + 1,
                       c.visited || peer.src_entity_id
                FROM cohort c
                JOIN celex_facts.entity_edges mine
                  ON mine.src_entity_id = c.company_id
                 AND mine.relation = 'participated_in'
                JOIN celex_facts.entity_edges peer
                  ON peer.dst_entity_id = mine.dst_entity_id
                 AND peer.relation = 'participated_in'
                 AND peer.src_entity_id <> c.company_id
                WHERE c.depth < :max_depth
                  AND NOT peer.src_entity_id = ANY(c.visited)
            )
            SELECT e.entity_id, e.canonical_name, min(cohort.depth) AS hops
            FROM cohort
            JOIN celex_facts.entities e ON e.entity_id = cohort.company_id
            WHERE cohort.depth > 0
            GROUP BY e.entity_id, e.canonical_name
            ORDER BY hops, e.canonical_name
            """
        )
        with self._session_scope(session) as active:
            rows = active.execute(
                sql, {"root": str(root_company_id), "max_depth": max_depth}
            ).fetchall()
        return [dict(row._mapping) for row in rows]

    def co_participants_by_name(
        self,
        name_stem: str,
        relation: EdgeRelation = EdgeRelation.PARTICIPATED_IN,
        max_hops: int | None = None,
        max_seeds: int | None = None,
        max_peers: int | None = None,
        session: Session | None = None,
    ) -> GraphNeighbourhood:
        """Expand the company cohort reachable from a name stem via shared cases.

        The traversal the flat Q&A views cannot express: seed on every company
        node whose ``canonical_name`` carries *name_stem* as a whole word, then
        walk ``company -> case -> company`` over *relation* up to *max_hops*
        deep, returning each peer with its minimum hop distance, the decisions
        that connect it, and the edge lineage anchors behind those links.

        Two properties are load-bearing and deliberate:

        * Matching is **word-boundary**, not substring.  ``ILIKE '%bayer%'``
          also matches ``Bayerische Hypo- und Vereinsbank AG`` and
          ``Bayerischer Rundfunk`` - unrelated companies sharing a Bavarian
          prefix - while ``~* '\\mbayer\\M'`` returns only the genuine ones.
        * The whole **seed set** is excluded from the peers, not just the node
          a branch started from.  Company aliases split across several nodes
          (a minority are LEI-keyed), so a sibling alias would otherwise be
          reported as its own peer.

        Seeds carrying no edge of *relation* are skipped, which also drops the
        edge-less company nodes left over from retired fact families.

        Args:
            name_stem: Short company stem to seed on, e.g. ``"bayer"``. Regex
                metacharacters are stripped before the pattern is built.
            relation: Edge relation that defines co-membership.
            max_hops: Traversal depth bound; defaults to
                ``settings.GRAPH_SEARCH_MAX_HOPS``. Clamped to at least 1.
            max_seeds: Cap on seed nodes; defaults to
                ``settings.GRAPH_SEARCH_MAX_SEEDS``.
            max_peers: Cap on returned peers; defaults to
                ``settings.GRAPH_SEARCH_MAX_PEERS``.
            session: Active session to reuse; one is opened when omitted.

        Returns:
            A :class:`GraphNeighbourhood`. An unmatched or empty stem yields
            empty ``seeds`` and ``peers`` rather than raising.
        """
        hops = max(1, max_hops if max_hops is not None else settings.GRAPH_SEARCH_MAX_HOPS)
        seed_cap = max_seeds if max_seeds is not None else settings.GRAPH_SEARCH_MAX_SEEDS
        peer_cap = max_peers if max_peers is not None else settings.GRAPH_SEARCH_MAX_PEERS
        stem = _sanitize_stem(name_stem)
        empty = GraphNeighbourhood(
            seed_stem=name_stem or "", relation=relation, max_hops=hops
        )
        if not stem:
            logger.debug("graph traversal skipped: empty stem from %r", name_stem)
            return empty

        seed_params = {
            "stem": stem,
            "relation": relation.value,
            "max_seeds": seed_cap,
        }
        # One row past the cap, so a full page is distinguishable from a
        # truncated one without a second COUNT query.
        peer_params = {**seed_params, "max_hops": hops, "max_peers": peer_cap + 1}
        with self._session_scope(session) as active:
            seed_rows = active.execute(text(_SEEDS_SQL), seed_params).fetchall()
            if not seed_rows:
                return empty
            peer_rows = active.execute(
                text(_CO_PARTICIPANTS_SQL), peer_params
            ).fetchall()

        truncated = len(peer_rows) > peer_cap
        peer_rows = peer_rows[:peer_cap]
        return GraphNeighbourhood(
            seed_stem=name_stem,
            relation=relation,
            max_hops=hops,
            seeds=tuple(
                GraphSeed(
                    entity_id=row.entity_id,
                    canonical_name=row.canonical_name,
                    lei_id=row.lei_id,
                    edge_count=row.edge_count,
                )
                for row in seed_rows
            ),
            peers=tuple(
                GraphPeer(
                    entity_id=row.entity_id,
                    canonical_name=row.canonical_name,
                    lei_id=row.lei_id,
                    hops=row.hops,
                    shared_celex_ids=tuple(row.shared_celex_ids or ()),
                    source_fact_ids=tuple(
                        str(fid) for fid in (row.source_fact_ids or ())
                    ),
                )
                for row in peer_rows
            ),
            truncated=truncated,
        )
