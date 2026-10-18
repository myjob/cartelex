"""
Persistence layer for knowledge-ingestion pipeline.

Writes knowledge items, question-variant embeddings, and graph edges in a
single database transaction.  The deferred ``trg_knowledge_grounding`` trigger
fires at ``COMMIT`` and rejects any knowledge item with zero outgoing edges,
guaranteeing nothing lands ungrounded.

Follows the **Provisioner pattern**: ``KnowledgeRepository`` and the embedding
callable are injected at ``__init__``.

``ingest()`` applies three gates in increasing cost: the deterministic
entity-graph contradiction check (no inference), the LLM-as-judge quality gate
(one inference), then the fact-grounding validation and the deferred trigger
(one transaction).

Exports:
    KnowledgeIngestor           — the canonical persistence service
    KnowledgeGraphContradiction — raised when a candidate denies a graph relation
    KnowledgeJudgeRejected      — raised when the LLM judge declines a candidate
"""

import datetime
import logging
import uuid
from collections.abc import Sequence
from typing import Any, Callable, List

import dspy
from sqlalchemy import text

from pipelines.agent_dspy.knowledge_graph_evidence import (
    find_graph_contradiction,
    render_validated_relations,
)
from pipelines.agent_dspy.knowledge_signatures import KnowHowJudgeSignature
from pipelines.graph.entity_graph_models import ValidatedRelation
from pipelines.rag.pgvector.embeddings.qwen3_embedding import retrieve_qwen3_embedding
from pipelines.rag.pgvector.knowledge_repository import KnowledgeRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository
from pipelines.rag.pgvector.models import (
    KnowledgeEdge,
    KnowledgeIngestAuditTrail,
    KnowledgeItem,
)

logger = logging.getLogger(__name__)


# Minimum accept score gate for the LLM judge. Any per-criterion score
# below this is treated as a structural reject even when the judge sets
# ``accept=True`` (defence against the model rubber-stamping rubbish).
_JUDGE_MIN_CRITERION_SCORE: float = 0.5

# Source-type discriminator for knowledge edges: a predecessor-fact source
# expresses derivation (``derived_from``), every other verified source type
# expresses supporting evidence (``supports``).
_FACT_SOURCE_TYPE: str = "fact"


class KnowledgeGraphContradiction(Exception):
    """Raised when a candidate conflicts with a validated entity-graph relation.

    The entity graph is a verified projection of the fact store, so a nugget
    denying one of its relations is wrong by construction.  This is decided
    deterministically and *before* the LLM judge runs, so the rejection costs
    no inference.  Carries the structured conflict for the audit trail.
    """

    def __init__(self, contradiction: Any) -> None:
        self.contradiction = contradiction
        super().__init__(
            f"Knowledge candidate contradicts the entity graph: "
            f"{contradiction.describe()}"
        )


class KnowledgeJudgeRejected(Exception):
    """Raised internally when the LLM judge rejects a knowledge candidate.

    Carries the structured verdict so the caller can log it / write it to
    the audit trail without re-running the LLM.
    """

    def __init__(self, verdict: Any) -> None:
        self.verdict = verdict
        super().__init__(
            f"LLM judge rejected knowledge candidate "
            f"(accept={getattr(verdict, 'accept', False)}, "
            f"rationale={getattr(verdict, 'rationale', '')!r})"
        )


class KnowledgeIngestor:
    """Persists knowledge items, question-variant embeddings, and graph edges
    in one transaction.

    Provisioner pattern: ``KnowledgeRepository`` and ``embed_fn`` are injected
    at ``__init__``.  The ingestor mirrors the shape of
    :class:`tools.PipelineFactIngestor.FactIngestor`.

    Parameters
    ----------
    knowledge_repo:
        DAO for the knowledge-graph tables.
    embed_fn:
        A callable ``(str) -> list[float]`` that returns an embedding vector.
        Defaults to :func:`retrieve_qwen3_embedding`.
    judge:
        Optional LLM-as-judge predictor used as a stage-5 quality gate.
        Anything truthy is invoked with the
        :class:`pipelines.agent_dspy.knowledge_signatures.KnowHowJudgeSignature`
        inputs and is expected to return an object with an ``output``
        attribute that is a ``KnowHowJudgeResult``.  Pass ``None``
        (default) to disable the judge — useful for unit tests and the
        legacy code path.  When ``True`` is passed the ingestor
        provisions ``dspy.Predict(KnowHowJudgeSignature)`` itself.
    lineage_repo:
        DAO for the data-lineage registry.  Used to read the supporting
        facts' verified ``fact_source_link`` provenance so knowledge
        ``supports`` edges reuse the same evidence sources the facts were
        confirmed on.  Defaults to a ``LineageRepository`` sharing the
        knowledge engine's connection pool.
    """

    def __init__(
        self,
        knowledge_repo: KnowledgeRepository,
        embed_fn: Callable[[str], list[float]] | None = None,
        judge: Any | None = None,
        lineage_repo: LineageRepository | None = None,
    ) -> None:
        self.knowledge_repo = knowledge_repo
        self.embed_fn = embed_fn or retrieve_qwen3_embedding
        self.Session = knowledge_repo.Session
        self.lineage_repo = lineage_repo or LineageRepository(
            engine=knowledge_repo.engine
        )
        if judge is True:
            self.judge: Any | None = dspy.Predict(KnowHowJudgeSignature)
        else:
            self.judge = judge

    # ------------------------------------------------------------------
    # Core ingestion — single transaction
    # ------------------------------------------------------------------

    def ingest(
        self,
        knowledge_text: str,
        knowledge_type: str,
        confidence: float,
        question_variants: List[str],
        supporting_fact_ids: List[uuid.UUID] | None = None,
        created_by: str = "knowledge_ingest_pipeline",
        agent_run_id: uuid.UUID | None = None,
        user_prompt: str = "",
        supporting_facts_text: str = "",
        validated_relations: Sequence[ValidatedRelation] = (),
    ) -> KnowledgeItem:
        """Write a knowledge item, its question-variant embeddings, and
        derived_from / supports edges in **one transaction**.

        The deferred ``trg_knowledge_grounding`` trigger fires at ``COMMIT``
        and will raise if no edges were inserted for this item.

        Source edges inherit the supporting facts' **verified** provenance:
        for each fact we reuse the source rows it is already linked to in
        ``fact_source_link`` / ``fact_source_registry`` rather than
        re-registering raw retrieved chunks.  Sources are differentiated by
        type — a predecessor-fact source (``fact``) yields a ``derived_from``
        edge, every other evidence type yields a ``supports`` edge.  A fact
        with no verified sources yet contributes only its direct
        ``derived_from`` edge to the fact, which already satisfies the
        grounding trigger.

        Parameters
        ----------
        knowledge_text:
            The distilled knowledge nugget text.
        knowledge_type:
            Classification label (e.g. ``legal_principle``).
        confidence:
            Score 0.0–1.0.
        question_variants:
            Question phrasings (incl. original) to embed.
        supporting_fact_ids:
            Fact UUIDs → ``derived_from`` edges, and the seed for the
            verified ``supports`` edges.
        created_by:
            Agent run ID string or pipeline name.
        agent_run_id:
            Optional UUID for traceability.
        validated_relations:
            Entity-graph relations materialized from the cited facts.  Treated
            as ground truth: they gate the candidate deterministically before
            the judge runs, and are rendered into the judge as evidence that
            outranks the retrieved prose.  Empty disables both.

        Returns
        -------
        KnowledgeItem
            The persisted item with server-generated ``id`` and timestamps.

        Raises
        ------
        ValueError
            When ``supporting_fact_ids`` is empty — there is no edge source.
        sqlalchemy.exc.IntegrityError
            When the deferred trigger rejects an ungrounded item (no edges).
        KnowledgeGraphContradiction
            When the candidate denies a relation the entity graph holds.
            Decided deterministically before any LLM call.
        KnowledgeJudgeRejected
            When the optional LLM-as-judge predictor was provisioned and
            scored the candidate below the acceptance threshold.  The
            caller is expected to catch this exception, skip persistence,
            and write a ``success=False`` audit row.
        """
        # Gate order is cheapest-and-most-authoritative first.  The entity
        # graph is deterministic and verified, so a conflict with it settles
        # the candidate without spending an inference call; only survivors
        # reach the LLM judge, and only its survivors touch the database.
        contradiction = find_graph_contradiction(knowledge_text, validated_relations)
        if contradiction is not None:
            logger.info(
                "Entity graph contradicts knowledge candidate: %s",
                contradiction.describe(),
            )
            raise KnowledgeGraphContradiction(contradiction)

        if self.judge is not None:
            self._judge_or_reject(
                knowledge_text=knowledge_text,
                knowledge_type=knowledge_type,
                user_prompt=user_prompt,
                supporting_facts_text=supporting_facts_text,
                validated_relations=validated_relations,
            )

        supporting_fact_ids = supporting_fact_ids or []

        if not supporting_fact_ids:
            raise ValueError(
                "ingest() requires at least one supporting fact id to ground "
                "the knowledge item"
            )

        # Collect the verified sources the supporting facts are already linked
        # to and classify each by source type.  Reads happen before the write
        # transaction; these fact_source_registry rows are committed, so the
        # edge FK is satisfied.  Dedupe across facts while preserving
        # trust-rank order (dict keeps insertion order).
        source_edge_relations: dict[uuid.UUID, str] = {}
        for fact_id in supporting_fact_ids:
            for link in self.lineage_repo.get_sources_for_fact(fact_id):
                if link.source_id in source_edge_relations:
                    continue
                source = self.lineage_repo.get_source(link.source_id)
                source_type = source.source_type if source is not None else None
                source_edge_relations[link.source_id] = (
                    "derived_from"
                    if source_type == _FACT_SOURCE_TYPE
                    else "supports"
                )

        with self.Session() as session:
            # 1. Insert knowledge item
            item = KnowledgeItem(
                knowledge_text=knowledge_text,
                knowledge_type=knowledge_type,
                confidence=confidence,
                created_by=created_by,
                agent_run_id=agent_run_id,
            )
            session.add(item)
            session.flush()  # → item.id is now available

            # 2. Insert question-variant embeddings
            for variant_text in question_variants:
                try:
                    embedding = self.embed_fn(variant_text)
                    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
                    session.execute(
                        text(
                            "INSERT INTO celex_facts.knowledge_question_embeddings "
                            "(knowledge_id, question_variant, embedding) "
                            "VALUES (:kid, :qv, CAST(:emb AS vector))"
                        ),
                        {
                            "kid": item.id,
                            "qv": variant_text,
                            "emb": embedding_str,
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to embed question variant %r for item %s: %s",
                        variant_text,
                        item.id,
                        exc,
                    )

            # 3. Insert derived_from edges (knowledge → fact)
            for fact_id in supporting_fact_ids:
                edge = KnowledgeEdge(
                    src_knowledge_id=item.id,
                    dst_fact_id=fact_id,
                    relation_type="derived_from",
                    confidence=confidence,
                    created_by=created_by,
                    agent_run_id=agent_run_id,
                )
                session.add(edge)

            # 4. Insert source edges (knowledge → verified source),
            #    inherited from the supporting facts' fact_source_link
            #    provenance and differentiated by source type: 'fact' sources
            #    express derivation, all other types express support.
            for source_id, relation_type in source_edge_relations.items():
                edge = KnowledgeEdge(
                    src_knowledge_id=item.id,
                    dst_source_id=source_id,
                    relation_type=relation_type,
                    confidence=confidence,
                    created_by=created_by,
                    agent_run_id=agent_run_id,
                )
                session.add(edge)

            # 5. Commit — deferred trigger fires here
            session.commit()
            session.refresh(item)
            session.expunge(item)

        logger.info(
            "Ingested knowledge item %s (%d variants, %d edges)",
            item.id,
            len(question_variants),
            len(supporting_fact_ids) + len(source_edge_relations),
        )
        return item

    # ------------------------------------------------------------------
    # LLM-as-judge quality gate
    # ------------------------------------------------------------------

    def _judge_or_reject(
        self,
        knowledge_text: str,
        knowledge_type: str,
        user_prompt: str,
        supporting_facts_text: str,
        validated_relations: Sequence[ValidatedRelation] = (),
    ) -> None:
        """Invoke the injected judge predictor; raise on rejection.

        The judge scores four criteria — specificity, generality,
        correctness, non_triviality — and sets an overall ``accept`` flag.
        A candidate passes when both the flag is set AND every
        per-criterion score is at or above
        :data:`_JUDGE_MIN_CRITERION_SCORE`; otherwise
        :class:`KnowledgeJudgeRejected` is raised with the full verdict so
        the caller can record it in the audit trail.

        Parameters
        ----------
        knowledge_text:
            The candidate nugget under review.
        knowledge_type:
            The extractor's declared label.
        user_prompt:
            The seed question that surfaced the nugget.
        supporting_facts_text:
            The evidence used by the extractor (free-form string).
        validated_relations:
            Entity-graph relations behind the cited facts, rendered into the
            judge as ground truth that outranks the retrieved prose.

        Raises
        ------
        KnowledgeJudgeRejected
            When the judge declines the candidate.
        """
        try:
            pred = self.judge(
                knowledge_text=knowledge_text,
                knowledge_type=knowledge_type,
                user_prompt=user_prompt or "",
                supporting_facts=supporting_facts_text or "",
                validated_relations=render_validated_relations(validated_relations),
            )
        except Exception as exc:
            # A judge failure must not block ingestion silently — we log,
            # but err on the side of letting the item through (the
            # deferred grounding trigger still catches the worst case).
            logger.warning(
                "LLM judge raised %s — defaulting to accept.", exc,
                exc_info=True,
            )
            return

        verdict = getattr(pred, "output", pred)
        scores = (
            getattr(verdict, "specificity", 0.0),
            getattr(verdict, "generality", 0.0),
            getattr(verdict, "correctness", 0.0),
            getattr(verdict, "non_triviality", 0.0),
        )
        accept_flag = bool(getattr(verdict, "accept", False))
        min_score = min(scores) if scores else 0.0

        if not accept_flag or min_score < _JUDGE_MIN_CRITERION_SCORE:
            logger.info(
                "Judge rejected knowledge candidate: accept=%s, scores=%s, "
                "rationale=%r",
                accept_flag,
                scores,
                getattr(verdict, "rationale", ""),
            )
            raise KnowledgeJudgeRejected(verdict)

        logger.debug(
            "Judge accepted candidate: scores=%s, rationale=%r",
            scores,
            getattr(verdict, "rationale", ""),
        )

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    def audit_run(
        self,
        queue_item_id: uuid.UUID | None,
        knowledge_id: uuid.UUID | None,
        user_prompt: str,
        agent_answer: str,
        dspy_module: str,
        success: bool,
        duration_sec: int,
        errors: str = "",
        predecessors: dict | None = None,
    ) -> None:
        """Write one row to ``knowledge_ingest_audit_trail``.

        Parameters
        ----------
        queue_item_id:
            FK to the queue row that triggered this run.
        knowledge_id:
            FK to the resulting knowledge item (``None`` on failure).
        user_prompt:
            The original user question.
        agent_answer:
            The agent's synthesized answer.
        dspy_module:
            Class name of the DSPy module that ran.
        success:
            Whether the pipeline succeeded.
        duration_sec:
            Wall-clock seconds.
        errors:
            Error message string (empty on success).
        predecessors:
            Optional JSONB metadata about predecessor facts/chunks.
        """
        with self.Session() as session:
            try:
                audit = KnowledgeIngestAuditTrail(
                    queue_item_id=queue_item_id,
                    knowledge_id=knowledge_id,
                    user_prompt=user_prompt,
                    agent_answer=agent_answer,
                    pipeline="knowledge_ingest",
                    dspy_module=dspy_module,
                    predecessors=predecessors,
                    success=success,
                    errors=errors,
                    duration=duration_sec,
                    created_at=datetime.datetime.now(datetime.timezone.utc),
                )
                session.add(audit)
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.error(
                    "Failed to save knowledge audit trail (queue=%r, knowledge=%r): %s",
                    queue_item_id,
                    knowledge_id,
                    exc,
                )
