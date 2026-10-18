"""Enumeration fact-extraction strategy — sibling to :class:`UniversalFactModule`.

Implements the recall-oriented half of the router/strategy split. The
distinguishing pattern is **map-then-aggregate**: instead of asking one
LLM call to find every X in a concatenated top-K context, we ask a
small LLM call *per candidate chunk* (so a court named once in chunk #4
is not buried by the volume of chunk #1's content), then aggregate the
per-chunk results into a deduplicated, canonical entity list.

Emits the same ``dspy.Prediction(output=FactOutput(...))`` shape as
``UniversalFactModule`` so ``FactRagPipeline.process`` requires no
changes.
"""

import logging
import time
from typing import Any, Dict, List

import dspy
from pydantic import BaseModel, Field

from extract.rag.enumeration_search import EnumerationSearch
from extract.rag.models.chunk_vo import RankedChunkVo
from pipelines.agent_dspy.dspy_protocols import FactOutput, SQLFactStore
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import (
    _check_context_token_limit,
    _check_fact_exists,
    _log_dspy_history,
    _resolve_schema_description,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signatures (Rule 1 — declarative, no business logic)
# ---------------------------------------------------------------------------


class EntityExtractionResult(BaseModel):
    entities: List[Dict[str, Any]] = Field(
        description=(
            "Entities found in THIS chunk that match the requested schema. "
            "Use STRICTLY the requested keys. Return an empty list if the "
            "chunk contains no matching entity."
        )
    )


class EntityAggregationResult(BaseModel):
    items: List[Dict[str, Any]] = Field(
        description=(
            "Deduplicated, canonicalized list of distinct entities. "
            "Merge surface-form variants of the same real-world entity into "
            "a single row. Use STRICTLY the requested keys."
        )
    )


class EntityExtractionSignature(dspy.Signature):
    """Extract entities matching the requested schema from a SINGLE chunk.

    Scope is strictly the provided chunk text — do not invent entities,
    do not infer from outside knowledge, and return an empty list when
    the chunk contains nothing matching.
    """

    question = dspy.InputField(
        desc="The natural-language enumeration question (e.g. 'list all courts')."
    )
    schema_description = dspy.InputField(
        desc=(
            "STRICTLY use these exact keys for each entity dict "
            "(e.g. 'court, country, jurisdiction')."
        )
    )
    chunk_text = dspy.InputField(
        desc="The single chunk of document text to scan."
    )
    output: EntityExtractionResult = dspy.OutputField(
        desc="Entities extracted strictly from this chunk."
    )


class EntityAggregationSignature(dspy.Signature):
    """Deduplicate and canonicalize per-chunk entity candidates.

    Inputs are the union of per-chunk extractions, which typically
    contains the same real-world entity under multiple surface forms
    (e.g. 'Bundesgerichtshof' / 'German Federal Court of Justice' /
    'BGH'). Merge variants into one canonical row using the formal
    name; consolidate column values when minor variations refer to the
    same thing.
    """

    question = dspy.InputField(
        desc="The original enumeration question, including any normalization rules."
    )
    schema_description = dspy.InputField(
        desc="STRICTLY use these exact keys for each output dict."
    )
    candidate_entities = dspy.InputField(
        desc=(
            "Concatenated per-chunk extraction results, one block per "
            "chunk. Many entities will be duplicates under different "
            "surface forms — merge them."
        )
    )
    output: EntityAggregationResult = dspy.OutputField(
        desc="Final deduplicated, canonicalized entity list."
    )


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class EnumerationFactModule(dspy.Module):
    """Map-extract-aggregate pipeline for "list all X" fact targets.

    Parameters
    ----------
    sql_store:
        ``FactRepository`` used for the existence pre-check (mirrors
        ``UniversalFactModule``).
    searcher:
        ``EnumerationSearch`` providing the dense+BM25 candidate set.
    """

    def __init__(self, sql_store: SQLFactStore, searcher: EnumerationSearch):
        super().__init__()
        self.sql_store = sql_store
        self.searcher = searcher
        self.extract = dspy.Predict(EntityExtractionSignature)
        self.aggregate = dspy.ChainOfThought(EntityAggregationSignature)

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        prediction, _facts = _check_fact_exists(self.sql_store, params)
        if prediction:
            return prediction

        t0 = time.perf_counter()
        ranked_chunks: List[RankedChunkVo] = self.searcher.search(
            celex_id=params.celex_id,
            query=params.question,
            section_types=params.section_scope,
        )
        logger.debug(
            "EnumerationFactModule retrieval took %.4fs (%d chunks)",
            time.perf_counter() - t0,
            len(ranked_chunks),
        )

        if not ranked_chunks:
            return dspy.Prediction(output=FactOutput(items=[], source="enumeration"))

        schema = _resolve_schema_description(params)

        # ---- Map step: one extraction call per candidate chunk ----------
        per_chunk_blocks: list[str] = []
        per_chunk_doc_pos: list[int] = []
        for chunk in ranked_chunks:
            try:
                pred = self.extract(
                    question=params.question,
                    schema_description=schema,
                    chunk_text=chunk.chunk,
                )
                entities = pred.output.entities or []
            except Exception as exc:
                logger.warning(
                    "EnumerationFactModule extract failed on chunk doc_pos=%d: %s",
                    chunk.doc_pos,
                    exc,
                )
                continue

            if not entities:
                continue
            per_chunk_blocks.append(
                f"[doc_pos: {chunk.doc_pos}] {entities}"
            )
            per_chunk_doc_pos.append(chunk.doc_pos)

        if not per_chunk_blocks:
            logger.info(
                "EnumerationFactModule found no entities across %d chunks for '%s'",
                len(ranked_chunks),
                params.target_fact_name,
            )
            return dspy.Prediction(output=FactOutput(items=[], source="enumeration"))

        candidate_entities = "\n---\n".join(per_chunk_blocks)
        _check_context_token_limit(candidate_entities, params.target_fact_name)

        # ---- Aggregate step: merge + canonicalize -----------------------
        agg = self.aggregate(
            question=params.question,
            schema_description=schema,
            candidate_entities=candidate_entities,
        )
        _log_dspy_history("Enumeration aggregation", params.target_fact_name)

        items = agg.output.items or []
        references_doc = per_chunk_doc_pos[:3] if items else None

        return dspy.Prediction(
            output=FactOutput(
                items=items,
                source="enumeration",
                references_doc=references_doc,
            )
        )
