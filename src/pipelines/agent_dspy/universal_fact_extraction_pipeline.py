import asyncio
import logging
import time
from typing import Any, Optional

import dspy

from config.llm_factory import batch_profile, create_lm
from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository
from tools.PipelineFactIngestor import FactIngestor
from extract.rag.hybrid_search import HybridSearch
from extract.rag.ranker.ranker_factory import create_ranker
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from tools.markdown_utils import format_fact_to_markdown
from tools.country_normalizer import normalize_country_fields


from pipelines.rule_engine.condition_checker import ConditionValidator
from pipelines.agent_dspy.universal_modules import UniversalFactModule

from pipelines.rag.pgvector.embeddings.fact_embedding_loader_qwen import (
    FactEmbeddingLoaderQwen,
)
from pipelines.rag.pgvector.session import Session

logger = logging.getLogger(__name__)

# --- Pipeline Controller ---


class FactRagPipeline:
    """Orchestrates fact extraction for a single target_fact_name.

    Parameters
    ----------
    target_fact_name:
        The fact name this pipeline is responsible for.
    ingestor:
        ``FactIngestor`` used to persist and audit extracted facts.
    fact_repo:
        Optional injected ``FactRepository``.  When omitted a new instance
        is created using default settings.  Passing a shared instance avoids
        opening duplicate connections in batch scenarios.
    """

    def __init__(
        self,
        target_fact_name: str,
        ingestor,
        fact_embedding_loader: FactEmbeddingLoaderQwen,
        fact_repo: Optional[FactRepository] = None,
    ):
        self.target_fact_name = target_fact_name
        self.ingestor = ingestor
        self.fact_embedding_loader = fact_embedding_loader
        self.fact_repo = fact_repo if fact_repo is not None else FactRepository()
        # self.predicates = PREDICATE_REGISTRY

    async def process(
        self,
        params: FactExtractorParamVO,
        dspy_agent: dspy.Module,
        context_data: dict[str, Any] | None = None,
    ):
        if context_data is None:
            context_data = {}

        if params.condition:
            fact_context = self.fact_repo.select_facts4celex(params.celex_id)
            # Expose celex_id so concept conditions can fall back to the year
            # embedded in the id when eu_commisssion_decision_date is absent.
            fact_context.setdefault("celex_id", params.celex_id)
            rule = ConditionValidator()
            is_match = rule.evaluate(params.condition, fact_context)
            logger.info(f"condition | {params.condition} | is {is_match}")

            if not is_match:
                #!! TODO: should this return 'NA' rather than no fact?
                return {"status": "condition_not_met"}
        else:
            logger.info("No condition defined .. processing")

        # 2. High-speed Fact Existence Check
        if not params.force_override and self.ingestor.exists(params.celex_id):
            logger.info(
                f"Skipping pipeline for target fact {params.target_fact_name} of {params.celex_id}: Fact exists."
            )
            return {"status": "skipped", "celex_id": params.celex_id}

        start_time = time.time()
        dspy_module_name = dspy_agent.__class__.__name__
        try:
            # Execute Brain (via __call__)
            prediction = await asyncio.to_thread(dspy_agent, params=params)

            # Deterministically canonicalize country values to ISO 3166-1
            # alpha-2 codes for facts that opt in (e.g. companies_country).
            # Pipeline-agnostic: applied here so it holds regardless of which
            # extraction module produced the rows. Unresolvable values are
            # left untouched, so this never drops data.
            if params.normalize_iso_country:
                prediction.output.items = normalize_country_fields(
                    prediction.output.items
                )

            md_value = format_fact_to_markdown(prediction.output.items)
            logger.info(
                f"Retrieved MD value handing over to SQL ingestion for '{params.target_fact_name}':\n>> {md_value} <<"
            )

            # Ingest. When a module emits explicit lineage_sources (e.g. the
            # case_title module records the real first-page chunk), they are
            # persisted directly; otherwise the ingestor derives chunk lineage
            # from references_doc. Predecessors are passed through so a derived
            # fact records its derived_from / gated_by edges in the same write.
            fact_id = self.ingestor.save_fact(
                params.celex_id,
                prediction.output.items,
                prediction.output.references_doc,
                lineage_sources=getattr(prediction.output, "lineage_sources", None),
                pipeline=params.pipeline,
                dspy_module=dspy_module_name,
                predecessors=params.predecessors,
                condition=params.condition,
            )
            if fact_id is None:
                logger.warning(
                    "save_fact returned None for %s; audit trail will have no fact_id",
                    params.celex_id,
                )

            duration_sec = int(time.time() - start_time)
            self.ingestor.audit_run(
                params, fact_id, dspy_module_name, True, duration_sec
            )

            # Add fact embeddings         FactEmbeddingLoaderQwen(session_factory=session_factory)
            upserted = self.fact_embedding_loader.ingest_for_celex_and_fact(
                params.celex_id, params.target_fact_name
            )
            logger.info(f"fact embedding upserted {upserted}")

            return {
                "status": "success",
                "data": prediction.output.items,
                "fact_id": fact_id,
            }
        except Exception as e:
            duration_sec = int(time.time() - start_time)
            self.ingestor.audit_run(
                params, None, dspy_module_name, False, duration_sec, str(e)
            )
            raise e


# --- Execution ---
# Typicall call from batch.Main_ingest_generic_facts.py rather than here
async def main():
    # Setup global LM
    logger.debug("Initializing LM")
    # Cached: the demo re-asks the same question on every run.
    dspy.configure(lm=create_lm(batch_profile(cache=True)))

    # Initialize HybridSearch Orchestrator
    from tools.celex_tools import CelexDatabaseToolkit  # local import: demo only

    tools = CelexDatabaseToolkit()
    ranker = create_ranker()
    hybrid_search = HybridSearch(
        repo=tools.chunk_repo, ranker=ranker, embed_query=tools.embed_query
    )
    # celex_id = 'CELEX-31975D0358'
    celex_id = "CELEX-32003D0570"

    # Shared repository — single connection pool for agents and pipelines
    shared_repo = FactRepository()
    # Shared lineage registry on the same pool so provenance is recorded.
    lineage_repo = LineageRepository(engine=shared_repo.engine)

    # Single "Brain" for everything
    agent = UniversalFactModule(sql_store=shared_repo, searcher=hybrid_search)

    # Pipeline for Fines
    fines_pipe = FactRagPipeline(
        target_fact_name="fines",
        ingestor=FactIngestor("fines", fact_table=shared_repo, lineage_repo=lineage_repo),
        fact_embedding_loader=FactEmbeddingLoaderQwen(session_factory=Session),
        fact_repo=shared_repo,
    )

    # Run Complex Table (Fines)
    fines_params = FactExtractorParamVO(
        celex_id=celex_id,
        question="List all fines",
        target_fact_name="fines",
        output_schema="company, amount",
        verify_fact_name="companies_investigated",
        force_override=True,
    )
    await fines_pipe.process(fines_params, dspy_agent=agent)

    # Run Single Value (Commissioner)
    comm_pipe = FactRagPipeline(
        target_fact_name="commissioner",
        ingestor=FactIngestor("commissioner", fact_table=shared_repo, lineage_repo=lineage_repo),
        fact_embedding_loader=FactEmbeddingLoaderQwen(session_factory=Session),
    )

    comm_params = FactExtractorParamVO(
        celex_id=celex_id,
        question="Who was the commissioner?",
        target_fact_name="commissioner",
        output_schema="",
        force_override=True,
    )
    await comm_pipe.process(comm_params, dspy_agent=agent)


if __name__ == "__main__":
    asyncio.run(main())
