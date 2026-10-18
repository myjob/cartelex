"""
Declarative DSPy signatures for the knowledge-ingestion pipeline.

Covers two stages:
  1. **KnowHowExtractionSignature** — distil reusable knowledge nuggets from
     a ReAct agent's QA trace (question + answer + supporting evidence).
  2. **QuestionVariantSignature** — generate alternative question phrasings
     for a knowledge nugget so the embedding space covers paraphrase variants.

Rules enforced:
  - Rule 1: Declarative signatures only (no f-string prompts).
  - Rule 2: Logic-free — all orchestration lives in the Module's ``forward()``.
  - Rule 3: Structured outputs via Pydantic BaseModels.
"""

from typing import List

import dspy
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Know-how extraction (stage 1)
# ---------------------------------------------------------------------------


class KnowHowItem(BaseModel):
    """A single distilled knowledge nugget extracted from an agent trace."""

    knowledge_text: str = Field(
        description="The distilled, self-contained knowledge statement."
    )
    knowledge_type: str = Field(
        description=(
            "Classification label.  Must be one of: "
            "legal_principle, procedure, precedent, domain_fact."
        )
    )
    confidence: float = Field(
        description="Confidence score between 0.0 and 1.0 that the statement is correct and grounded."
    )
    source_fact_refs: List[int] = Field(
        default_factory=list,
        description=(
            "Indices (from the numbered fact catalog in 'numbered_facts') of "
            "the facts that DIRECTLY ground this specific statement.  Cite only "
            "the facts whose content substantively supports this nugget -- do "
            "NOT list every fact in the catalog.  Use an empty list when no "
            "catalog fact grounds the statement."
        ),
    )


class KnowHowExtractionResult(BaseModel):
    """Collection of knowledge nuggets extracted from one agent trace."""

    items: List[KnowHowItem] = Field(
        description=(
            "Extracted knowledge nuggets.  May be empty if the trace "
            "contains no novel, self-contained, grounded know-how."
        )
    )


class KnowHowExtractionSignature(dspy.Signature):
    """Extract reusable knowledge nuggets from a Q&A agent trace.

    Only extract statements that are:
      - **Novel**: not a trivial restatement of the question.
      - **Self-contained**: understandable without the surrounding conversation.
      - **Grounded**: explicitly supported by the provided evidence/facts.

    Do NOT extract generic filler or speculative conclusions.

    For every nugget, populate ``source_fact_refs`` with the indices of the
    facts from ``numbered_facts`` that directly ground it.  The catalog lists
    every candidate fact; most nuggets are grounded by only a few of them, so
    cite precisely rather than listing the whole catalog.

    ``validated_relations`` is authoritative: it comes from the entity graph,
    where every relation was materialized from a verified fact.  Never state
    anything that conflicts with it, and prefer its spelling of a company or
    case name over the prose.
    """

    user_prompt: str = dspy.InputField(
        desc="The user's original question."
    )
    agent_answer: str = dspy.InputField(
        desc="The agent's synthesized answer to the question."
    )
    supporting_facts: str = dspy.InputField(
        desc="Evidence and facts the agent used to build the answer."
    )
    numbered_facts: str = dspy.InputField(
        desc=(
            "Numbered catalog of the candidate facts, one per line as "
            "'[index] name: value'.  Each nugget's 'source_fact_refs' must "
            "reference these indices."
        )
    )
    validated_relations: str = dspy.InputField(
        desc=(
            "Verified entity-graph relations behind those facts, one per line "
            "as '- source --relation--> target [celex_id]'. Treat them as "
            "ground truth. May be empty when the facts produced no graph edge."
        )
    )
    output: KnowHowExtractionResult = dspy.OutputField(
        desc="Extracted knowledge nuggets (possibly empty)."
    )


# ---------------------------------------------------------------------------
# Question-variant generation (stage 2)
# ---------------------------------------------------------------------------


class QuestionVariantResult(BaseModel):
    """Alternative question phrasings for embedding coverage."""

    variants: List[str] = Field(
        description="2 to 3 semantically equivalent reformulations of the seed question."
    )


class QuestionVariantSignature(dspy.Signature):
    """Generate alternative phrasings for a knowledge-retrieval question.

    Preserve the core meaning of the seed question while varying:
      - Vocabulary (synonyms, domain jargon vs. plain language)
      - Sentence structure (active ↔ passive, interrogative ↔ declarative)
      - Specificity (broader or narrower framing)
    """

    knowledge_text: str = dspy.InputField(
        desc="The knowledge statement to generate question variants for."
    )
    seed_question: str = dspy.InputField(
        desc="The original user question that surfaced this knowledge."
    )
    output: QuestionVariantResult = dspy.OutputField(
        desc="Alternative question variants (2–3 reformulations)."
    )


# ---------------------------------------------------------------------------
# Quality scoring (stage 5 — LLM-as-judge)
# ---------------------------------------------------------------------------


class KnowHowJudgeResult(BaseModel):
    """Quality verdict for a single extracted knowledge nugget."""

    accept: bool = Field(
        description=(
            "True only when the nugget is reusable, general, specific, "
            "grounded in the evidence, and non-trivial."
        )
    )
    specificity: float = Field(
        description=(
            "0.0–1.0 score for how concrete the statement is "
            "(vague generalities score low)."
        )
    )
    generality: float = Field(
        description=(
            "0.0–1.0 score for reusability across cases (per-case facts "
            "and volatile corpus aggregates score low)."
        )
    )
    correctness: float = Field(
        description=(
            "0.0–1.0 score for how well the evidence supports the statement."
        )
    )
    non_triviality: float = Field(
        description=(
            "0.0–1.0 score for novelty (paraphrases of the question "
            "and tautologies score low)."
        )
    )
    rationale: str = Field(
        description="One-sentence justification for the verdict.",
        default="",
    )


class KnowHowJudgeSignature(dspy.Signature):
    """Decide whether a knowledge nugget belongs in ``knowledge_items``.

    Reject when the candidate is:
      - **Per-case fact** — belongs in ``facts``, not in general knowledge.
      - **Corpus aggregate** — volatile count/statistic, not doctrine.
      - **Restatement** — paraphrase of the user question with no new content.
      - **Ungrounded** — not entailed by the supplied evidence.
      - **Trivial** — true but useless ("EU has member states").

    Accept only when the nugget states a reusable doctrine, procedure, or
    pattern that another agent could meaningfully retrieve later.

    ``validated_relations`` carries verified entity-graph relations and
    outranks every other input: it is derived deterministically from confirmed
    facts, while ``supporting_facts`` is retrieved prose.  Score
    ``correctness`` low when the nugget conflicts with a relation listed there,
    and score ``non_triviality`` low when the nugget merely restates one --
    such a claim is already answerable exactly from the graph.
    """

    knowledge_text: str = dspy.InputField(
        desc="The candidate knowledge nugget under review."
    )
    knowledge_type: str = dspy.InputField(
        desc="The candidate's declared classification label."
    )
    user_prompt: str = dspy.InputField(
        desc="The user question that surfaced this nugget."
    )
    supporting_facts: str = dspy.InputField(
        desc="The evidence the extractor used to produce the nugget."
    )
    validated_relations: str = dspy.InputField(
        desc=(
            "Verified entity-graph relations behind the cited facts, one per "
            "line as '- source --relation--> target [celex_id]'. Ground "
            "truth; outranks supporting_facts. May be empty."
        )
    )
    output: KnowHowJudgeResult = dspy.OutputField(
        desc="Structured verdict with per-criterion scores and an accept flag."
    )
