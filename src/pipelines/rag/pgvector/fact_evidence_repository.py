"""
Persistence layer for the evidence-verification ledger.

Writes the ``celex_facts.fact_evidence`` rows produced by
:class:`pipelines.agent_dspy.evidence_verifier_module.EvidenceVerifierModule`
and maintains the denormalized ``facts.verification_status`` verdict.

Two row shapes share the table (see ``migrate_fact_evidence.sql``):
  * **chunk rows** — ``chunk_id`` set; one per supporting chunk, upserted on
    the partial unique index ``(fact_id, chunk_id) WHERE chunk_id IS NOT NULL``.
  * **summary rows** — ``chunk_id`` NULL; exactly one per fact (the
    complete-ledger guarantee for human / derived / no-provenance facts),
    maintained by delete-then-insert.

Follows the **Provisioner pattern**: the engine may be injected to share a
connection pool with sibling repositories.

Exports:
    FactEvidenceRepository — canonical persistence service for fact_evidence.
"""

import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)


class FactEvidenceRepository:
    """DAO for ``celex_facts.fact_evidence`` and the fact verification verdict.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Ignored when ``engine`` is supplied.
    engine:
        Pre-built SQLAlchemy engine to share a connection pool (Provisioner
        pattern).  When provided, ``db_url`` is unused.
    """

    def __init__(self, db_url: str = DATABASE_URL, engine: Engine | None = None) -> None:
        self.engine = engine if engine is not None else make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)

    # ------------------------------------------------------------------
    # Ledger writes
    # ------------------------------------------------------------------

    def upsert_evidence(
        self,
        fact_id: UUID,
        chunk_id: str | None,
        support_label: str | None,
        final_score: float | None,
        sub_scores: dict[str, float] | None = None,
        evidence_quote: str | None = None,
        missing_elements: list[str] | None = None,
    ) -> None:
        """Replace a fact's authoritative verdict row in one transaction.

        Each verification produces exactly one verdict (a best supporting chunk
        or a chunk-less summary). This deletes any prior rows for the fact and
        inserts the current one, so the ledger holds exactly one row per fact
        and re-running is idempotent even when the fact's verification strategy
        changes between runs (e.g. LLM → derived) — the stale row from the old
        strategy is removed rather than left behind.

        Parameters
        ----------
        fact_id:
            UUID of the ``celex_facts.facts`` row.
        chunk_id:
            Stable ``pg_embedding_qwen.id`` of the supporting chunk, or ``None``
            for a chunk-less summary verdict (human / derived / no-provenance).
        support_label:
            Verifier label (e.g. ``supports``, ``insufficient``).
        final_score:
            Combined score for the chosen chunk (``None`` for summary rows).
        sub_scores:
            Optional per-signal scores keyed by ``entity``, ``keyword``,
            ``semantic``, ``temporal``, ``provenance``.
        evidence_quote:
            Exact supporting quote, or ``None``.
        missing_elements:
            List of required elements the evidence did not cover.
        """
        scores = sub_scores or {}
        params: dict[str, Any] = {
            "fact_id": str(fact_id),
            "chunk_id": chunk_id,
            "support_label": support_label,
            "final_score": final_score,
            "entity_score": scores.get("entity"),
            "keyword_score": scores.get("keyword"),
            "semantic_score": scores.get("semantic"),
            "temporal_score": scores.get("temporal"),
            "provenance_score": scores.get("provenance"),
            "evidence_quote": evidence_quote,
            "missing_elements": (
                json.dumps(missing_elements) if missing_elements is not None else None
            ),
        }

        columns = (
            "fact_id, chunk_id, support_label, final_score, entity_score, "
            "keyword_score, semantic_score, temporal_score, provenance_score, "
            "evidence_quote, missing_elements"
        )
        values = (
            ":fact_id, :chunk_id, :support_label, :final_score, :entity_score, "
            ":keyword_score, :semantic_score, :temporal_score, :provenance_score, "
            ":evidence_quote, CAST(:missing_elements AS jsonb)"
        )

        with self.Session() as session:
            session.execute(
                text(
                    "DELETE FROM celex_facts.fact_evidence WHERE fact_id = :fact_id"
                ),
                {"fact_id": str(fact_id)},
            )
            session.execute(
                text(
                    f"INSERT INTO celex_facts.fact_evidence ({columns}) "
                    f"VALUES ({values})"
                ),
                params,
            )
            session.commit()

    def set_verification_status(
        self, fact_id: UUID, status: str, score: float | None = None
    ) -> None:
        """Update the denormalized verdict on ``celex_facts.facts``.

        Parameters
        ----------
        fact_id:
            UUID of the fact row.
        status:
            One of the ``verification_status_enum`` values.
        score:
            Optional numeric confidence (the best candidate's ``final_score``
            or ``support_score``).
        """
        with self.Session() as session:
            session.execute(
                text(
                    "UPDATE celex_facts.facts "
                    "SET verification_status = CAST(:status AS "
                    "    celex_facts.verification_status_enum), "
                    "    verification_score = :score, "
                    "    verification_updated_at = now() "
                    "WHERE id = :fact_id"
                ),
                {"status": status, "score": score, "fact_id": str(fact_id)},
            )
            session.commit()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_predecessor_facts(self, fact_id: UUID) -> list[dict[str, Any]]:
        """Return the predecessor facts a derived fact was computed from.

        Reads the authoritative lineage join (``fact_source_link`` ⋈
        ``fact_source_registry`` where ``source_type='fact'``) so transitive
        verification does not depend on YAML ``predecessors`` or
        ``references_fact_json``.

        Parameters
        ----------
        fact_id:
            UUID of the derived fact.

        Returns
        -------
        list[dict]
            One ``{"fact_id", "name", "verification_status"}`` per predecessor
            fact; empty when the fact has no ``source_type='fact'`` lineage.
        """
        sql = text(
            "SELECT p.id AS fact_id, p.name AS name, "
            "       p.verification_status::text AS verification_status "
            "FROM celex_facts.fact_source_link fsl "
            "JOIN celex_facts.fact_source_registry sr ON sr.source_id = fsl.source_id "
            "JOIN celex_facts.facts p ON p.id = sr.source_fact_id "
            "WHERE fsl.fact_id = :fact_id AND sr.source_type = 'fact'"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"fact_id": str(fact_id)}).fetchall()
        return [
            {
                "fact_id": str(row.fact_id),
                "name": row.name,
                "verification_status": row.verification_status,
            }
            for row in rows
        ]

    def get_source_type_counts(self, fact_id: UUID) -> dict[str, int]:
        """Return the count of lineage sources per ``source_type`` for a fact.

        Lets the verifier decide a fact's provenance shape in a single query:
        whether it has chunk evidence to judge against versus only predecessor
        facts (a purely derived fact).

        Parameters
        ----------
        fact_id:
            UUID of the fact row.

        Returns
        -------
        dict[str, int]
            ``{source_type: count}`` over ``fact_source_link`` ⋈
            ``fact_source_registry``; empty when the fact has no lineage.
        """
        sql = text(
            "SELECT sr.source_type::text AS source_type, count(*) AS n "
            "FROM celex_facts.fact_source_link fsl "
            "JOIN celex_facts.fact_source_registry sr ON sr.source_id = fsl.source_id "
            "WHERE fsl.fact_id = :fact_id "
            "GROUP BY sr.source_type"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"fact_id": str(fact_id)}).fetchall()
        return {row.source_type: int(row.n) for row in rows}

    def get_evidence_for_fact(self, fact_id: UUID) -> list[dict[str, Any]]:
        """Return all ledger rows for a fact, summary row first.

        Parameters
        ----------
        fact_id:
            UUID of the fact row.

        Returns
        -------
        list[dict]
            Ledger rows ordered with the chunk-less summary row first, then
            supporting-chunk rows by descending ``final_score``.
        """
        sql = text(
            "SELECT id, chunk_id, support_label, final_score, evidence_quote, "
            "       missing_elements, verified_at "
            "FROM celex_facts.fact_evidence "
            "WHERE fact_id = :fact_id "
            "ORDER BY (chunk_id IS NOT NULL), final_score DESC NULLS LAST"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"fact_id": str(fact_id)}).fetchall()
        return [dict(row._mapping) for row in rows]

    def list_unverified(self, celex_id: str | None = None) -> list[dict[str, Any]]:
        """List facts whose verdict is not a trusted/verified state.

        Parameters
        ----------
        celex_id:
            Optional CELEX scope; omit to scan the whole corpus.

        Returns
        -------
        list[dict]
            ``{"fact_id", "celex_id", "name", "verification_status",
            "verification_score"}`` for each non-verified fact.
        """
        where = (
            "verification_status NOT IN ('verified_supported', 'manual_approved')"
        )
        params: dict[str, Any] = {}
        if celex_id:
            where += " AND celex_id = :celex_id"
            params["celex_id"] = celex_id
        sql = text(
            "SELECT id AS fact_id, celex_id, name, "
            "       verification_status::text AS verification_status, "
            "       verification_score "
            f"FROM celex_facts.facts WHERE {where} "
            "ORDER BY verification_updated_at DESC NULLS LAST"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row._mapping) for row in rows]
