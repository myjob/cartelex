"""
DSPy Module for knowledge ingestion from ReAct agent traces.

Extracts reusable know-how from a (question, answer, evidence) triple,
generates question-variant embeddings, and persists everything into the
knowledge-graph repository via :class:`KnowledgeIngestor`.

All orchestration logic lives in ``forward()`` (Rule 2).  The two DSPy
predictors use declarative signatures (Rule 1) with Pydantic structured
outputs (Rule 3).

Exports:
    KnowledgeIngestModule  — dspy.Module for knowledge ingestion
"""

import logging
import time
import uuid
from typing import List, Optional

import dspy

from config.settings import settings
from pipelines.agent_dspy.knowledge_graph_evidence import (
    relations_for_fact_ids,
    render_validated_relations,
)
from pipelines.agent_dspy.knowledge_signatures import (
    KnowHowExtractionSignature,
    QuestionVariantSignature,
)
from pipelines.graph.entity_graph_models import ValidatedRelation
from pipelines.rag.pgvector.entity_graph_repository import EntityGraphRepository
from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.knowledge_ingestor import (
    KnowledgeGraphContradiction,
    KnowledgeIngestor,
    KnowledgeJudgeRejected,
)

logger = logging.getLogger(__name__)

# Max characters of each fact's value rendered into the numbered catalog the
# extractor cites against.  Long enough to disambiguate a fact, short enough to
# keep the prompt within budget.
_FACT_VALUE_SNIPPET_LEN = 240


class KnowledgeIngestModule(dspy.Module):
    """Extracts know-how from a ReAct agent trace and ingests it into the
    knowledge graph.

    Provisioner pattern: :class:`KnowledgeIngestor` is injected at
    ``__init__``.  All business logic (extraction → variant generation →
    persistence → audit) resides in ``forward()``.

    Parameters
    ----------
    ingestor:
        The persistence layer that writes to ``knowledge_items``,
        ``knowledge_question_embeddings``, and ``knowledge_edges``.
    fact_repo:
        DAO used to resolve supporting fact UUIDs into ``(name, value)`` pairs
        for the citable catalog the extractor attributes nuggets against.
        Defaults to a fresh :class:`FactRepository` on the shared engine.
    entity_graph_repo:
        DAO used to recover the validated entity-graph relations the cited
        facts produced.  Defaults to an :class:`EntityGraphRepository` sharing
        the fact repository's connection pool.  Pass ``None`` with
        ``settings.KNOWLEDGE_GRAPH_EVIDENCE_ENABLED`` false to run the
        prose-only pipeline.
    """

    def __init__(
        self,
        ingestor: KnowledgeIngestor,
        fact_repo: FactRepository | None = None,
        entity_graph_repo: EntityGraphRepository | None = None,
    ) -> None:
        super().__init__()
        self.ingestor = ingestor
        self.fact_repo = fact_repo or FactRepository()
        if entity_graph_repo is not None:
            self.entity_graph_repo: EntityGraphRepository | None = entity_graph_repo
        elif settings.KNOWLEDGE_GRAPH_EVIDENCE_ENABLED:
            self.entity_graph_repo = EntityGraphRepository(
                engine=self.fact_repo.engine
            )
        else:
            self.entity_graph_repo = None
        self.extractor = dspy.Predict(KnowHowExtractionSignature)
        self.variant_gen = dspy.Predict(QuestionVariantSignature)

    def _resolve_validated_relations(
        self, supporting_fact_ids: List[uuid.UUID]
    ) -> tuple[ValidatedRelation, ...]:
        """Recover the entity-graph relations the cited facts produced.

        One query for the whole supporting-fact set; each nugget is later
        handed only the subset anchored on the facts it cited.

        A graph failure is logged and degraded to "no relations" rather than
        raised: the graph is an *additional* trust signal, so losing it must
        weaken the gate, never fail the ingestion run.

        Parameters
        ----------
        supporting_fact_ids:
            Every fact UUID the agent cited for this trace.

        Returns
        -------
        tuple[ValidatedRelation, ...]
            The validated relations, empty when the graph is disabled,
            unreachable, or simply holds no edge for these facts.
        """
        if self.entity_graph_repo is None or not supporting_fact_ids:
            return ()
        try:
            return self.entity_graph_repo.relations_for_facts(supporting_fact_ids)
        except Exception as exc:
            logger.warning(
                "Entity-graph lookup failed for %d fact(s); continuing without "
                "graph evidence: %s",
                len(supporting_fact_ids),
                exc,
                exc_info=True,
            )
            return ()

    def _build_fact_catalog(
        self, supporting_fact_ids: List[uuid.UUID]
    ) -> str:
        """Render supporting facts as a numbered catalog for citation.

        Index ``i`` in the catalog corresponds to ``supporting_fact_ids[i]``,
        so the extractor's per-nugget ``source_fact_refs`` map back to the exact
        fact UUIDs without any re-parsing of rendered prose.

        Parameters
        ----------
        supporting_fact_ids:
            Fact UUIDs the agent used, in stable order.

        Returns
        -------
        str
            One ``[index] name: value`` line per fact (value truncated).  Facts
            missing from the database render with a placeholder so the index
            space stays aligned with ``supporting_fact_ids``.
        """
        fact_map = self.fact_repo.select_facts_by_ids(
            [str(fact_id) for fact_id in supporting_fact_ids]
        )
        lines: list[str] = []
        for index, fact_id in enumerate(supporting_fact_ids):
            name, value = fact_map.get(str(fact_id), ("<unknown>", ""))
            snippet = value[:_FACT_VALUE_SNIPPET_LEN].replace("\n", " ")
            lines.append(f"[{index}] {name}: {snippet}")
        return "\n".join(lines)

    @staticmethod
    def _resolve_fact_refs(
        source_fact_refs: List[int],
        supporting_fact_ids: List[uuid.UUID],
    ) -> List[uuid.UUID]:
        """Map a nugget's cited catalog indices back to fact UUIDs.

        Out-of-range and duplicate indices are dropped so a malformed citation
        cannot widen the grounding beyond the catalog the extractor was shown.

        Parameters
        ----------
        source_fact_refs:
            Catalog indices the extractor cited for one nugget.
        supporting_fact_ids:
            The fact UUIDs the catalog was built from, in catalog order.

        Returns
        -------
        List[uuid.UUID]
            Deduplicated fact UUIDs the nugget is directly derived from.
        """
        resolved: list[uuid.UUID] = []
        seen: set[int] = set()
        for ref in source_fact_refs or []:
            if not isinstance(ref, int):
                continue
            if 0 <= ref < len(supporting_fact_ids) and ref not in seen:
                seen.add(ref)
                resolved.append(supporting_fact_ids[ref])
        return resolved

    def forward(
        self,
        user_prompt: str,
        agent_answer: str,
        supporting_facts: str,
        supporting_fact_ids: Optional[List[uuid.UUID]] = None,
        supporting_chunk_ids: Optional[List[str]] = None,
        queue_item_id: Optional[uuid.UUID] = None,
    ) -> dspy.Prediction:
        """Extract know-how, generate question variants, persist, and audit.

        Pipeline stages:
          0. **Graph evidence** — the cited facts are resolved to the
             ``entity_edges`` relations they materialized.  The graph is a
             validated projection of the fact store, so these relations are
             ground truth: they are rendered into the extractor and the judge,
             and they gate each nugget deterministically inside ``ingest()``
             before any judge call.
          1. **Extract** — ``KnowHowExtractionSignature`` distils knowledge
             nuggets from the agent trace and cites, per nugget, the facts
             (by index into the numbered catalog) that ground it.
          2. **Variant generation** — ``QuestionVariantSignature`` produces
             2–3 alternative question phrasings per nugget.
          3. **Persist** — ``KnowledgeIngestor.ingest()`` writes item +
             embeddings + edges in one transaction (deferred trigger
             guarantees grounding).  Only the facts the nugget cited become
             ``derived_from`` edges; ``supports`` edges inherit those cited
             facts' verified provenance; the retrieved ``supporting_chunk_ids``
             are not used for grounding.  A nugget that cites no fact is
             skipped (audited as ungrounded) rather than blanket-linked.
          4. **Audit** — one audit row per nugget.

        Parameters
        ----------
        user_prompt:
            The user's original question.
        agent_answer:
            The agent's synthesized answer.
        supporting_facts:
            Serialised evidence string the agent used.
        supporting_fact_ids:
            UUIDs of the candidate facts the agent used.  These form the
            numbered catalog the extractor cites against; only the per-nugget
            cited subset becomes ``derived_from`` edges (and the seed for the
            verified ``supports`` edges built inside the ingestor).
        supporting_chunk_ids:
            Varchar IDs of the chunks the retriever surfaced.  Recorded in
            the audit trail for traceability only — ``supports`` edges are
            grounded on the facts' verified sources, not on these.
        queue_item_id:
            FK to the ``knowledge_extraction_queue`` row (for audit).

        Returns
        -------
        dspy.Prediction
            With ``ingested_items: list[KnowledgeItem]``,
            ``errors: list[str]``, ``rejected_count: int`` (nuggets the LLM
            judge dropped, if a judge was wired into the ingestor) and
            ``contradicted_count: int`` (nuggets the entity graph refuted
            before any judge call).
        """
        supporting_fact_ids = supporting_fact_ids or []
        supporting_chunk_ids = supporting_chunk_ids or []
        dspy_module_name = self.__class__.__name__

        # Render the supporting facts as a numbered catalog so the extractor can
        # attribute each nugget to the specific facts that ground it, instead of
        # the whole evidence bundle being fanned onto every nugget.
        numbered_facts = self._build_fact_catalog(supporting_fact_ids)

        # The entity graph is a validated projection of these same facts, so
        # the relations behind them are ground truth the extractor should not
        # contradict.  Resolved once here; narrowed per nugget below.
        validated_relations = self._resolve_validated_relations(supporting_fact_ids)
        if validated_relations:
            logger.info(
                "Resolved %d validated entity-graph relation(s) for %d fact(s).",
                len(validated_relations),
                len(supporting_fact_ids),
            )

        # --- Stage 1: Extract know-how ---
        extraction_pred = self.extractor(
            user_prompt=user_prompt,
            agent_answer=agent_answer,
            supporting_facts=supporting_facts,
            numbered_facts=numbered_facts,
            validated_relations=render_validated_relations(validated_relations),
        )
        know_how_items = extraction_pred.output.items

        if not know_how_items:
            logger.info("No novel know-how extracted from trace.")
            return dspy.Prediction(ingested_items=[], errors=[])

        logger.info(
            "Extracted %d knowledge nugget(s) from trace.", len(know_how_items)
        )

        # --- Stage 2 + 3 + 4: Per-nugget variant gen → persist → audit ---
        ingested_items = []
        errors = []
        rejected_count = 0
        contradicted_count = 0

        for item in know_how_items:
            start_time = time.time()
            knowledge_id = None

            # Attribute this nugget to only the facts it cited.  An empty
            # resolution means the extractor found nothing in the catalog that
            # grounds it: persisting it would either fail the grounding trigger
            # or (the old bug) blanket-link every fact, so skip it instead.
            grounded_fact_ids = self._resolve_fact_refs(
                item.source_fact_refs, supporting_fact_ids
            )
            if not grounded_fact_ids:
                logger.info(
                    "Skipping ungrounded nugget (no cited facts): %r",
                    item.knowledge_text[:80],
                )
                self.ingestor.audit_run(
                    queue_item_id=queue_item_id,
                    knowledge_id=None,
                    user_prompt=user_prompt,
                    agent_answer=agent_answer,
                    dspy_module=dspy_module_name,
                    success=False,
                    duration_sec=int(time.time() - start_time),
                    errors="ungrounded: extractor cited no supporting facts",
                    predecessors={
                        "fact_ids": [],
                        "chunk_ids": supporting_chunk_ids,
                    },
                )
                continue

            try:
                # Stage 2: Generate question variants
                variant_pred = self.variant_gen(
                    knowledge_text=item.knowledge_text,
                    seed_question=user_prompt,
                )
                variants = variant_pred.output.variants or []

                # Include the original question as the first variant
                all_variants = [user_prompt] + variants

                # Stage 3: Persist (single transaction with deferred trigger).
                #   supports edges are derived from the facts' verified
                #   provenance inside ingest(); retrieved chunk_ids are kept
                #   only as audit metadata below.
                persisted_item = self.ingestor.ingest(
                    knowledge_text=item.knowledge_text,
                    knowledge_type=item.knowledge_type,
                    confidence=item.confidence,
                    question_variants=all_variants,
                    supporting_fact_ids=grounded_fact_ids,
                    user_prompt=user_prompt,
                    supporting_facts_text=supporting_facts,
                    validated_relations=relations_for_fact_ids(
                        validated_relations, grounded_fact_ids
                    ),
                )
                knowledge_id = persisted_item.id
                ingested_items.append(persisted_item)

                # Stage 4: Audit — success
                duration_sec = int(time.time() - start_time)
                self.ingestor.audit_run(
                    queue_item_id=queue_item_id,
                    knowledge_id=knowledge_id,
                    user_prompt=user_prompt,
                    agent_answer=agent_answer,
                    dspy_module=dspy_module_name,
                    success=True,
                    duration_sec=duration_sec,
                    predecessors={
                        "fact_ids": [str(fid) for fid in grounded_fact_ids],
                        "chunk_ids": supporting_chunk_ids,
                    },
                )

            except KnowledgeGraphContradiction as exc:
                # The graph is verified, so this nugget is wrong -- not merely
                # low quality.  Recorded distinctly from a judge rejection
                # because it is a deterministic finding, and because a cluster
                # of them points at a faulty extractor rather than a strict
                # judge.
                contradicted_count += 1
                duration_sec = int(time.time() - start_time)
                logger.info(
                    "Entity graph contradicts candidate %r: %s",
                    item.knowledge_text[:80],
                    exc.contradiction.describe(),
                )
                self.ingestor.audit_run(
                    queue_item_id=queue_item_id,
                    knowledge_id=None,
                    user_prompt=user_prompt,
                    agent_answer=agent_answer,
                    dspy_module=dspy_module_name,
                    success=False,
                    duration_sec=duration_sec,
                    errors=f"graph_contradiction: {exc}",
                    predecessors={
                        "fact_ids": [str(fid) for fid in grounded_fact_ids],
                        "chunk_ids": supporting_chunk_ids,
                        "graph_contradiction": exc.contradiction.model_dump(
                            mode="json"
                        ),
                    },
                )

            except KnowledgeJudgeRejected as exc:
                # Judge rejection is a deliberate filtering outcome, not a
                # technical failure.  Skip persistence silently, log the
                # verdict, and record an audit row tagged accordingly.
                rejected_count += 1
                duration_sec = int(time.time() - start_time)
                logger.info(
                    "Judge rejected candidate %r: %s",
                    item.knowledge_text[:80],
                    exc.verdict,
                )
                self.ingestor.audit_run(
                    queue_item_id=queue_item_id,
                    knowledge_id=None,
                    user_prompt=user_prompt,
                    agent_answer=agent_answer,
                    dspy_module=dspy_module_name,
                    success=False,
                    duration_sec=duration_sec,
                    errors=f"judge_rejected: {exc}",
                    predecessors={
                        "fact_ids": [str(fid) for fid in grounded_fact_ids],
                        "chunk_ids": supporting_chunk_ids,
                        "judge_verdict": exc.verdict.model_dump()
                        if hasattr(exc.verdict, "model_dump")
                        else None,
                    },
                )

            except Exception as exc:
                duration_sec = int(time.time() - start_time)
                error_msg = f"Failed to ingest nugget: {exc}"
                logger.error(error_msg, exc_info=True)
                errors.append(error_msg)

                # Audit — failure
                self.ingestor.audit_run(
                    queue_item_id=queue_item_id,
                    knowledge_id=knowledge_id,
                    user_prompt=user_prompt,
                    agent_answer=agent_answer,
                    dspy_module=dspy_module_name,
                    success=False,
                    duration_sec=duration_sec,
                    errors=str(exc),
                )

        logger.info(
            "Knowledge ingestion complete: %d ingested, %d contradicted by the "
            "entity graph, %d rejected by judge, %d errors.",
            len(ingested_items),
            contradicted_count,
            rejected_count,
            len(errors),
        )
        return dspy.Prediction(
            ingested_items=ingested_items,
            errors=errors,
            rejected_count=rejected_count,
            contradicted_count=contradicted_count,
        )
