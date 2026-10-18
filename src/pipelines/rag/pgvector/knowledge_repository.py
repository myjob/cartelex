"""
Unified DAO (Data Access Object) for all celex_facts knowledge-graph persistence.

Covers three tables:
  - ``knowledge_items``               — distilled knowledge nuggets
  - ``knowledge_question_embeddings`` — question-variant vectors
  - ``knowledge_edges``               — typed edges (supports / derived_from / contradicts)

plus a read-only duplicate lookup on ``knowledge_extraction_queue``.

All queries go through the shared engine from ``session.py`` (single connection pool).
The class follows the Provisioner pattern: database client injected at ``__init__``.

Exports:
    KnowledgeRepository  — the canonical persistence service for the knowledge graph
"""

import logging
import uuid as _uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from pipelines.rag.pgvector.models import (
    KnowledgeEdge,
    KnowledgeItem,
    KnowledgeQuestionEmbedding,
)
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)


class KnowledgeRepository:
    """Unified DAO for the celex_facts knowledge-graph tables.

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

    # ------------------------------------------------------------------
    # KnowledgeItem — CRUD
    # ------------------------------------------------------------------

    def insert_knowledge_item(
        self,
        knowledge_text: str,
        knowledge_type: str,
        created_by: str,
        *,
        scope: str = "global",
        jurisdiction: Optional[str] = None,
        status: str = "draft",
        confidence: Optional[float] = None,
        agent_run_id: Optional[_uuid.UUID] = None,
    ) -> KnowledgeItem:
        """Insert a new knowledge item and return the persisted ORM instance.

        Parameters
        ----------
        knowledge_text:
            The knowledge nugget text (feeds the generated tsvector column).
        knowledge_type:
            Classification label (e.g. ``"legal_principle"``, ``"procedure"``).
        created_by:
            Agent run ID string or ``"human"``.
        scope:
            One of ``global``, ``jurisdiction``, ``topic``.  Defaults to ``"global"``.
        jurisdiction:
            Optional jurisdiction code (e.g. ``"EU"``).
        status:
            One of ``draft``, ``validated``, ``deprecated``.  Defaults to ``"draft"``.
        confidence:
            Optional confidence score (0.0 – 1.0).
        agent_run_id:
            Optional UUID linking to the originating agent run.

        Returns
        -------
        KnowledgeItem
            The newly created item with server-generated ``id`` and timestamps.
        """
        item = KnowledgeItem(
            knowledge_text=knowledge_text,
            knowledge_type=knowledge_type,
            created_by=created_by,
            scope=scope,
            jurisdiction=jurisdiction,
            status=status,
            confidence=confidence,
            agent_run_id=agent_run_id,
        )
        with self.Session() as session:
            session.add(item)
            session.commit()
            session.refresh(item)
            # Expunge so the object is usable outside the session.
            session.expunge(item)
        return item

    def select_knowledge_item(self, item_id: _uuid.UUID) -> Optional[KnowledgeItem]:
        """Return a single ``KnowledgeItem`` by primary key, or ``None``.

        Parameters
        ----------
        item_id:
            The UUID primary key.
        """
        with self.Session() as session:
            return session.get(KnowledgeItem, item_id)

    def list_knowledge_items(
        self,
        *,
        status: Optional[str] = None,
        scope: Optional[str] = None,
        knowledge_type: Optional[str] = None,
        jurisdiction: Optional[str] = None,
    ) -> List[KnowledgeItem]:
        """Return knowledge items matching the given optional filters.

        Parameters
        ----------
        status:
            Filter by status (e.g. ``"draft"``, ``"validated"``).
        scope:
            Filter by scope (e.g. ``"global"``, ``"jurisdiction"``).
        knowledge_type:
            Filter by knowledge type.
        jurisdiction:
            Filter by jurisdiction code.

        Returns
        -------
        List[KnowledgeItem]
            Ordered by ``created_at`` descending.
        """
        with self.Session() as session:
            q = session.query(KnowledgeItem)
            if status is not None:
                q = q.filter(KnowledgeItem.status == status)
            if scope is not None:
                q = q.filter(KnowledgeItem.scope == scope)
            if knowledge_type is not None:
                q = q.filter(KnowledgeItem.knowledge_type == knowledge_type)
            if jurisdiction is not None:
                q = q.filter(KnowledgeItem.jurisdiction == jurisdiction)
            return q.order_by(KnowledgeItem.created_at.desc()).all()

    def update_knowledge_item(
        self, item_id: _uuid.UUID, **fields
    ) -> Optional[KnowledgeItem]:
        """Partially update a knowledge item and return the refreshed instance.

        Automatically bumps ``updated_at`` to the current UTC time.

        Parameters
        ----------
        item_id:
            The UUID primary key of the item to update.
        **fields:
            Column names and their new values.  Only the provided fields are
            modified; all others remain unchanged.

        Returns
        -------
        Optional[KnowledgeItem]
            The updated item, or ``None`` if the id was not found.

        Raises
        ------
        ValueError
            If no fields are provided or an unknown column name is given.
        """
        if not fields:
            raise ValueError("At least one field must be provided for update.")

        allowed = {
            "knowledge_text",
            "knowledge_type",
            "scope",
            "jurisdiction",
            "status",
            "confidence",
            "created_by",
            "agent_run_id",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unknown fields: {bad}")

        with self.Session() as session:
            item = session.get(KnowledgeItem, item_id)
            if item is None:
                return None
            for col, val in fields.items():
                setattr(item, col, val)
            item.updated_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(item)
            session.expunge(item)
        return item

    def delete_knowledge_item(self, item_id: _uuid.UUID) -> bool:
        """Hard-delete a knowledge item (cascades to embeddings and edges).

        Parameters
        ----------
        item_id:
            The UUID primary key.

        Returns
        -------
        bool
            ``True`` if a row was deleted, ``False`` if not found.
        """
        with self.Session() as session:
            item = session.get(KnowledgeItem, item_id)
            if item is None:
                return False
            session.delete(item)
            session.commit()
        return True

    def search_knowledge_fulltext(
        self, query: str, *, limit: int = 20
    ) -> List[KnowledgeItem]:
        """Full-text search over ``knowledge_items.knowledge_tsv``.

        Uses ``plainto_tsquery('english', ...)`` for safe, injection-free
        matching against the server-generated tsvector column.

        Parameters
        ----------
        query:
            Natural-language search string.
        limit:
            Maximum number of results.  Defaults to 20.

        Returns
        -------
        List[KnowledgeItem]
            Matching items ordered by relevance (``ts_rank``).
        """
        sql = text(
            "SELECT id FROM celex_facts.knowledge_items "
            "WHERE knowledge_tsv @@ plainto_tsquery('english', :query) "
            "ORDER BY ts_rank(knowledge_tsv, plainto_tsquery('english', :query)) DESC "
            "LIMIT :limit"
        )
        with self.Session() as session:
            rows = session.execute(sql, {"query": query, "limit": limit}).fetchall()
            if not rows:
                return []
            ids = [row[0] for row in rows]
            return (
                session.query(KnowledgeItem)
                .filter(KnowledgeItem.id.in_(ids))
                .all()
            )

    def search_knowledge_any_terms(
        self, query: str, *, limit: int = 20
    ) -> List[KnowledgeItem]:
        """FTS using OR across all meaningful terms in *query*.

        Uses ``websearch_to_tsquery`` with an explicit ``OR`` between each
        token so that a natural-language question like "which corporations
        were granted an exemption" matches knowledge items containing any of
        the content words — not all of them at once.

        Stop words and short tokens (< 3 chars) are stripped before building
        the OR query, so generic structural words do not dilute the match.

        Parameters
        ----------
        query:
            Natural-language question or keyword string.
        limit:
            Maximum number of results.  Defaults to 20.

        Returns
        -------
        List[KnowledgeItem]
            Matching items ordered by relevance (``ts_rank``), most relevant first.
        """
        import re

        _STOP = frozenset({
            "the", "a", "an", "of", "in", "for", "to", "and", "or", "by",
            "at", "on", "with", "from", "which", "who", "what", "were",
            "was", "is", "are", "be", "been", "has", "have", "had", "did",
            "do", "does", "that", "this", "it", "its", "about", "into",
            "between", "how", "when", "where", "why",
        })
        tokens = [
            t for t in re.findall(r"\b[a-zA-Z]{3,}\b", query)
            if t.lower() not in _STOP
        ]
        if not tokens:
            return []

        # websearch_to_tsquery supports the keyword OR between terms.
        websearch_q = " OR ".join(tokens)
        sql = text(
            "SELECT id FROM celex_facts.knowledge_items "
            "WHERE knowledge_tsv @@ websearch_to_tsquery('english', :query) "
            "ORDER BY ts_rank(knowledge_tsv, websearch_to_tsquery('english', :query)) DESC "
            "LIMIT :limit"
        )
        with self.Session() as session:
            rows = session.execute(sql, {"query": websearch_q, "limit": limit}).fetchall()
            if not rows:
                return []
            ids = [row[0] for row in rows]
            return (
                session.query(KnowledgeItem)
                .filter(KnowledgeItem.id.in_(ids))
                .all()
            )

    def find_queued_duplicate(
        self,
        agent_answer: str,
        prompt_key: str,
        normalize: Callable[[str], str],
    ) -> Optional[_uuid.UUID]:
        """Return the id of a queue row that already holds this Q&A pair.

        Candidates are narrowed in SQL by exact answer text; the prompt is
        compared in Python through *normalize* so the dedup key has a single
        definition (typographic quotes, context suffix, case, whitespace).

        Parameters
        ----------
        agent_answer:
            The synthesised answer about to be enqueued.
        prompt_key:
            ``normalize(user_prompt)`` of the prompt about to be enqueued.
        normalize:
            The prompt normaliser used to build *prompt_key*.

        Returns
        -------
        Optional[uuid.UUID]
            The first matching queue row id, or ``None`` when the pair is new.
        """
        sql = text(
            "SELECT id, user_prompt FROM celex_facts.knowledge_extraction_queue "
            "WHERE agent_answer = :answer"
        )
        with self.Session() as session:
            rows = session.execute(sql, {"answer": agent_answer}).fetchall()
        for row_id, user_prompt in rows:
            if normalize(user_prompt) == prompt_key:
                return row_id
        return None

    def get_knowledge_sources(self, item_id: _uuid.UUID) -> list[dict]:
        """Return the supporting fact/chunk sources for a knowledge item via its edges.

        Joins ``knowledge_edges`` with ``fact_source_registry`` (for
        ``dst_source_id`` edges) and with ``facts`` (for ``dst_fact_id`` edges)
        to surface the original evidence that grounded the knowledge nugget.

        Parameters
        ----------
        item_id:
            UUID primary key of the knowledge item to trace.

        Returns
        -------
        list[dict]
            Each dict has:
            ``source_kind`` — ``'chunk'`` (registry-based) or ``'fact'``;
            ``celex_id``    — case identifier (str or None);
            ``chunk_id``    — pg_embedding_qwen UUID string for chunk edges, None otherwise;
            ``doc_pos``     — first positional index from ``chunk_seq``, or None;
            ``fact_name``   — ``facts.name`` for fact edges, None otherwise.
        """
        sql = text(
            """
            SELECT
                'chunk'           AS source_kind,
                sr.celex_id,
                sr.chunk_id::text,
                CASE WHEN array_length(sr.chunk_seq, 1) > 0
                     THEN sr.chunk_seq[1] ELSE NULL END AS doc_pos,
                NULL::text        AS fact_name
            FROM celex_facts.knowledge_edges e
            JOIN celex_facts.fact_source_registry sr
              ON sr.source_id = e.dst_source_id
            WHERE e.src_knowledge_id = :item_id
              AND e.dst_source_id IS NOT NULL

            UNION ALL

            SELECT
                'fact'            AS source_kind,
                f.celex_id,
                NULL::text        AS chunk_id,
                NULL::int         AS doc_pos,
                f.name            AS fact_name
            FROM celex_facts.knowledge_edges e
            JOIN celex_facts.facts f
              ON f.id = e.dst_fact_id
            WHERE e.src_knowledge_id = :item_id
              AND e.dst_fact_id IS NOT NULL
            """
        )
        with self.Session() as session:
            rows = session.execute(sql, {"item_id": item_id}).fetchall()
        return [
            {
                "source_kind": row[0],
                "celex_id": row[1],
                "chunk_id": row[2],
                "doc_pos": row[3],
                "fact_name": row[4],
            }
            for row in rows
        ]

    def exists(self, item_id: _uuid.UUID) -> bool:
        """Return ``True`` when a knowledge item with the given id exists.

        Parameters
        ----------
        item_id:
            The UUID primary key.
        """
        with self.Session() as session:
            return session.get(KnowledgeItem, item_id) is not None

    # ------------------------------------------------------------------
    # KnowledgeQuestionEmbedding — CRUD
    # ------------------------------------------------------------------

    def insert_question_embedding(
        self,
        knowledge_id: _uuid.UUID,
        question_variant: str,
        embedding: list,
    ) -> KnowledgeQuestionEmbedding:
        """Insert a question-variant embedding for a knowledge item.

        Parameters
        ----------
        knowledge_id:
            FK to ``knowledge_items.id``.
        question_variant:
            The question text this embedding represents.
        embedding:
            The embedding vector as a Python list of floats.

        Returns
        -------
        KnowledgeQuestionEmbedding
            The persisted row with server-generated ``id``.
        """
        # Store the embedding as a pgvector literal string, e.g. "[0.1,0.2,...]"
        embedding_str = str(embedding)
        with self.Session() as session:
            session.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_question_embeddings "
                    "(knowledge_id, question_variant, embedding) "
                    "VALUES (:kid, :qv, CAST(:emb AS vector))"
                ),
                {
                    "kid": knowledge_id,
                    "qv": question_variant,
                    "emb": embedding_str,
                },
            )
            session.commit()

        # Re-read via ORM so caller gets a proper model instance.
        with self.Session() as session:
            return (
                session.query(KnowledgeQuestionEmbedding)
                .filter(
                    KnowledgeQuestionEmbedding.knowledge_id == knowledge_id,
                    KnowledgeQuestionEmbedding.question_variant == question_variant,
                )
                .order_by(KnowledgeQuestionEmbedding.created_at.desc())
                .first()
            )

    def list_question_embeddings(
        self, knowledge_id: _uuid.UUID
    ) -> List[KnowledgeQuestionEmbedding]:
        """Return all question-variant embeddings for a knowledge item.

        Parameters
        ----------
        knowledge_id:
            FK to ``knowledge_items.id``.

        Returns
        -------
        List[KnowledgeQuestionEmbedding]
        """
        with self.Session() as session:
            return (
                session.query(KnowledgeQuestionEmbedding)
                .filter(KnowledgeQuestionEmbedding.knowledge_id == knowledge_id)
                .order_by(KnowledgeQuestionEmbedding.created_at.asc())
                .all()
            )

    def delete_question_embeddings(self, knowledge_id: _uuid.UUID) -> int:
        """Bulk-delete all question embeddings for a knowledge item.

        Parameters
        ----------
        knowledge_id:
            FK to ``knowledge_items.id``.

        Returns
        -------
        int
            Number of rows deleted.
        """
        with self.Session() as session:
            count = (
                session.query(KnowledgeQuestionEmbedding)
                .filter(KnowledgeQuestionEmbedding.knowledge_id == knowledge_id)
                .delete()
            )
            session.commit()
        return count

    def search_similar_questions(
        self,
        embedding: list,
        *,
        limit: int = 10,
        knowledge_id: object | None = None,
    ) -> List[Dict]:
        """Cosine-similarity search over question-variant embeddings.

        Parameters
        ----------
        embedding:
            The query embedding vector as a Python list of floats.
        limit:
            Maximum number of results.  Defaults to 10.
        knowledge_id:
            When provided, restricts the search to rows belonging to that
            knowledge item (useful when all rows share the same embedding
            dimension, e.g. in test fixtures).

        Returns
        -------
        List[Dict]
            Each dict has keys ``id``, ``knowledge_id``, ``question_variant``,
            ``distance`` (cosine distance, lower = more similar).
        """
        embedding_str = str(embedding)
        where = (
            "WHERE knowledge_id = :knowledge_id " if knowledge_id is not None else ""
        )
        sql = text(
            "SELECT id, knowledge_id, question_variant, "
            "       embedding <=> CAST(:emb AS vector) AS distance "
            "FROM celex_facts.knowledge_question_embeddings "
            f"{where}"
            "ORDER BY embedding <=> CAST(:emb AS vector) "
            "LIMIT :limit"
        )
        params: dict = {"emb": embedding_str, "limit": limit}
        if knowledge_id is not None:
            params["knowledge_id"] = str(knowledge_id)
        with self.Session() as session:
            rows = session.execute(sql, params).fetchall()
            return [
                {
                    "id": row[0],
                    "knowledge_id": row[1],
                    "question_variant": row[2],
                    "distance": row[3],
                }
                for row in rows
            ]

    def search_knowledge_by_question(
        self,
        embedding: list,
        *,
        max_distance: float,
        limit: int,
    ) -> List[Dict]:
        """Knowledge items whose stored question variants lie near *embedding*.

        Keeps each item's closest variant only, so an item with four
        near-identical phrasings occupies one slot of *limit*.

        Parameters
        ----------
        embedding:
            Query embedding, same model as the stored variants.
        max_distance:
            Cosine-distance ceiling; farther variants are ignored.
        limit:
            Maximum number of items returned.

        Returns
        -------
        List[Dict]
            Closest first; each dict has ``item`` (:class:`KnowledgeItem`),
            ``question_variant`` (the matched phrasing) and ``distance``.
        """
        sql = text(
            "SELECT knowledge_id, question_variant, distance FROM ("
            "  SELECT DISTINCT ON (knowledge_id) knowledge_id, question_variant,"
            "         embedding <=> CAST(:emb AS vector) AS distance"
            "  FROM celex_facts.knowledge_question_embeddings"
            "  ORDER BY knowledge_id, distance"
            ") best "
            "WHERE distance <= :max_distance "
            "ORDER BY distance "
            "LIMIT :limit"
        )
        params = {"emb": str(embedding), "max_distance": max_distance, "limit": limit}
        with self.Session() as session:
            rows = session.execute(sql, params).fetchall()
            if not rows:
                return []
            items = {
                item.id: item
                for item in session.query(KnowledgeItem)
                .filter(KnowledgeItem.id.in_([row[0] for row in rows]))
                .all()
            }
            return [
                {"item": items[row[0]], "question_variant": row[1], "distance": row[2]}
                for row in rows
                if row[0] in items
            ]

    # ------------------------------------------------------------------
    # KnowledgeEdge — CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_edge_destinations(
        dst_source_id: Optional[_uuid.UUID],
        dst_fact_id: Optional[_uuid.UUID],
        dst_knowledge_id: Optional[_uuid.UUID],
    ) -> None:
        """Raise ``ValueError`` unless exactly one destination is set."""
        count = sum(
            x is not None for x in (dst_source_id, dst_fact_id, dst_knowledge_id)
        )
        if count != 1:
            raise ValueError(
                f"Exactly one destination must be set, got {count} non-null "
                f"(dst_source_id={dst_source_id!r}, dst_fact_id={dst_fact_id!r}, "
                f"dst_knowledge_id={dst_knowledge_id!r})."
            )

    def insert_edge(
        self,
        src_knowledge_id: _uuid.UUID,
        relation_type: str,
        *,
        dst_source_id: Optional[_uuid.UUID] = None,
        dst_fact_id: Optional[_uuid.UUID] = None,
        dst_knowledge_id: Optional[_uuid.UUID] = None,
        confidence: Optional[float] = None,
        created_by: Optional[str] = None,
        agent_run_id: Optional[_uuid.UUID] = None,
    ) -> KnowledgeEdge:
        """Insert a typed edge in the knowledge graph.

        Validates that exactly one destination field is set before touching
        the database, providing a clear ``ValueError`` instead of a raw
        ``IntegrityError`` from the CHECK constraint.

        Parameters
        ----------
        src_knowledge_id:
            The source knowledge item.
        relation_type:
            One of ``supports``, ``derived_from``, ``contradicts``.
        dst_source_id:
            FK to ``fact_source_registry.source_id`` (uuid) — raw evidence
            grounding (chunk, csv, book, lei, heideltime).
        dst_fact_id:
            FK to ``facts.id`` (uuid).
        dst_knowledge_id:
            FK to ``knowledge_items.id`` (uuid).
        confidence:
            Optional confidence score.
        created_by:
            Agent run ID string or ``"human"``.
        agent_run_id:
            Optional UUID linking to the originating agent run.

        Returns
        -------
        KnowledgeEdge
            The persisted edge with server-generated ``id``.
        """
        self._validate_edge_destinations(dst_source_id, dst_fact_id, dst_knowledge_id)

        edge = KnowledgeEdge(
            src_knowledge_id=src_knowledge_id,
            dst_source_id=dst_source_id,
            dst_fact_id=dst_fact_id,
            dst_knowledge_id=dst_knowledge_id,
            relation_type=relation_type,
            confidence=confidence,
            created_by=created_by,
            agent_run_id=agent_run_id,
        )
        with self.Session() as session:
            session.add(edge)
            session.commit()
            session.refresh(edge)
            session.expunge(edge)
        return edge

    def list_edges_for_knowledge(
        self, knowledge_id: _uuid.UUID
    ) -> List[KnowledgeEdge]:
        """Return all outgoing edges from a knowledge item.

        Parameters
        ----------
        knowledge_id:
            The ``src_knowledge_id`` to filter on.

        Returns
        -------
        List[KnowledgeEdge]
        """
        with self.Session() as session:
            return (
                session.query(KnowledgeEdge)
                .filter(KnowledgeEdge.src_knowledge_id == knowledge_id)
                .order_by(KnowledgeEdge.created_at.asc())
                .all()
            )

    def delete_edge(self, edge_id: _uuid.UUID) -> bool:
        """Delete a single edge by primary key.

        Parameters
        ----------
        edge_id:
            The UUID of the edge to delete.

        Returns
        -------
        bool
            ``True`` if a row was deleted, ``False`` if not found.
        """
        with self.Session() as session:
            edge = session.get(KnowledgeEdge, edge_id)
            if edge is None:
                return False
            session.delete(edge)
            session.commit()
        return True

    def list_edges_by_relation_type(
        self, relation_type: str
    ) -> List[KnowledgeEdge]:
        """Return all edges with the given relation type.

        Parameters
        ----------
        relation_type:
            E.g. ``"supports"``, ``"derived_from"``, ``"contradicts"``.

        Returns
        -------
        List[KnowledgeEdge]
        """
        with self.Session() as session:
            return (
                session.query(KnowledgeEdge)
                .filter(KnowledgeEdge.relation_type == relation_type)
                .order_by(KnowledgeEdge.created_at.asc())
                .all()
            )
