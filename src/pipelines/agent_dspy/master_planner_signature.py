"""Declarative signatures and structured models for the Master Planner Agent.

The Master Planner is a *fallback* over the existing Plan -> Execute ->
Synthesize QA pipeline.  It activates only when the primary pipeline returns
insufficient evidence on a compositional query (multi-entity filtering or
count / group-by aggregation).  This module holds the purely declarative parts
(Rule 1): the DSPy ``Signature`` classes and the ``pydantic`` models they
reference (Rule 3).  All business logic lives in
``master_planner_module.MasterPlannerModule`` (Rule 2).

Design note - no frozen fact-name enums
---------------------------------------
``celex_facts.facts`` holds 70+ distinct fact ``name`` values and the set
grows over time.  Fields that *name a fact* (``candidate_fact_names``,
``SubTask.group_by`` when it targets a fact, the ``name = '...'`` filters the
planner writes into ``kb_sql``) are therefore plain ``str`` and validated at
runtime against the live distinct-name catalog
(``FactRepository.select_distinct_fact_names``).  Closed ``Literal`` types are
used ONLY for small, stable *semantic* categories (kind of answer, where a
value was found, sub-task kind) - never for fact names, entity surfaces, or
roles.
"""

from typing import Literal

import dspy
from pydantic import BaseModel, Field

# Semantic bucket for an entity surface.  Deliberately broad with an ``other``
# catch-all so the set never needs to be exhaustive; it is NOT a fact name.
EntityType = Literal[
    "company",
    "person",
    "country",
    "court",
    "sector",
    "product",
    "law",
    "date",
    "other",
]

# Where a referenced value can be found in the knowledge base.
AvailableIn = Literal["fact", "knowledge", "chunk"]

# What kind of composed answer the query asks for.
TargetType = Literal["case_list", "count_group", "compare", "unknown"]

# Aggregation requested by the query.
Aggregation = Literal["none", "count", "group_count"]

# The kind of work a single sub-task performs.
SubTaskKind = Literal["entity_resolve", "retrieve", "filter", "join", "aggregate"]

# Edge relation that defines co-membership for a graph_search traversal.
# Mirrors celex_facts.edge_relation_enum: small, stable and semantic, which is
# what a closed Literal is for. It is NOT a fact name.
GraphRelation = Literal[
    "participated_in",
    "fined",
    "leniency_applied",
    "leniency_granted",
    "leniency_rejected",
]


# ---------------------------------------------------------------------------
# Stage A - Analysis
# ---------------------------------------------------------------------------


class ResolvedEntity(BaseModel):
    """One entity referenced by the query, with where it can be retrieved."""

    surface: str = Field(
        description="The entity exactly as written in the question, e.g. 'Bayer AG'."
    )
    entity_type: EntityType = Field(
        description=(
            "Broad semantic bucket for the entity (company, country, sector, "
            "etc.). Use 'other' when none fits. This is NOT a fact name."
        )
    )
    available_in: list[AvailableIn] = Field(
        default_factory=list,
        description=(
            "Stores where this entity is retrievable: 'fact' (structured), "
            "'knowledge' (distilled nugget), 'chunk' (raw document text)."
        ),
    )
    candidate_fact_names: list[str] = Field(
        default_factory=list,
        description=(
            "Fact names from the provided catalog that could carry this "
            "entity's value. Use names from the catalog ONLY; leave empty when "
            "the entity lives only in free-text chunks."
        ),
    )


class QueryAnalysis(BaseModel):
    """Structured analysis of a compositional query (Stage A output)."""

    entities: list[ResolvedEntity] = Field(
        default_factory=list,
        description="Entities the query references, each with its retrieval surface.",
    )
    target_type: TargetType = Field(
        description=(
            "Kind of answer: 'case_list' (filtered list of cases), "
            "'count_group' (counts grouped by a dimension), 'compare' (two "
            "groups), or 'unknown'."
        )
    )
    aggregation: Aggregation = Field(
        default="none",
        description="Aggregation needed: 'none', 'count', or 'group_count'.",
    )
    requires_statistics: bool = Field(
        description=(
            "True when the query needs statistical / causal / trend inference "
            "(e.g. 'did X increase after Y'). Such queries are out of scope and "
            "must be answered NA."
        )
    )
    rationale: str = Field(
        description="One or two sentences explaining the classification."
    )


class AnalyzeSignature(dspy.Signature):
    """Analyse a compositional question about EU competition-law cases.

    Identify the entities it references, classify the kind of composed answer
    it needs, and map each entity to candidate fact names drawn ONLY from the
    provided fact-name catalog.  Set ``requires_statistics`` to true for any
    query that needs trend, before/after, correlation, or causal inference -
    those are out of scope and will be answered NA downstream.
    """

    question: str = dspy.InputField(desc="The user's natural-language question.")
    kb_schema: str = dspy.InputField(
        desc="Description of the facts / knowledge / chunk schema and tools."
    )
    fact_name_catalog: str = dspy.InputField(
        desc=(
            "Newline-separated list of the live distinct fact names. Map "
            "entities to candidate fact names from THIS list only."
        )
    )
    analysis: QueryAnalysis = dspy.OutputField(
        desc="Structured analysis: entities, target_type, aggregation, NA flag."
    )


# ---------------------------------------------------------------------------
# Stage B - Decomposition
# ---------------------------------------------------------------------------


class SubTask(BaseModel):
    """A single node in the Master Planner's sub-task DAG."""

    task_id: int = Field(description="1-based identifier of this sub-task.")
    kind: SubTaskKind = Field(
        description=(
            "What this sub-task does: 'entity_resolve' (find cases mentioning "
            "an entity), 'retrieve' (run one retrieval tool), 'filter' (keep "
            "rows matching a role), 'join' (link sub-task results on celex_id), "
            "'aggregate' (group_by + count)."
        )
    )
    tool: str = Field(
        default="",
        description=(
            "For kind='retrieve' only: one of 'kb_sql', 'case_facts', "
            "'rag_search', 'rag_search_in_case', 'doc_search', "
            "'knowledge_search', 'graph_search'. Use 'graph_search' for a "
            "relationship between two companies (who shared cases with whom) - "
            "it is the only tool that walks company-to-company adjacency. "
            "Empty for non-retrieve sub-tasks."
        ),
    )
    query: str = Field(
        default="",
        description="Query string or SQL fragment for the tool / resolution.",
    )
    case_id: str = Field(
        default="",
        description="celex_id to scope the sub-task; required by case_facts / rag_search_in_case.",
    )
    depends_on: list[int] = Field(
        default_factory=list,
        description="task_id values that must run before this sub-task.",
    )
    filter_role: str = Field(
        default="",
        description=(
            "For kind='filter': the role to keep, e.g. "
            "'ringleader / instigator / cartel leader'."
        ),
    )
    group_by: str = Field(
        default="",
        description=(
            "For kind='aggregate': the dimension to group by, e.g. 'country' "
            "or a fact name from the catalog."
        ),
    )
    graph_seed: str = Field(
        default="",
        description=(
            "For tool='graph_search' only: SHORT company name stem to seed the "
            "traversal, e.g. 'bayer'. Never a full legal name — company aliases "
            "are split across separate graph nodes, so a stem seeds all of them "
            "while a full name seeds one and under-reports. Empty otherwise."
        ),
    )
    graph_relation: GraphRelation = Field(
        default="participated_in",
        description=(
            "For tool='graph_search' only: the edge type that defines "
            "co-membership between two companies."
        ),
    )
    graph_hops: int = Field(
        default=1,
        description=(
            "For tool='graph_search' only: traversal depth. 1 = direct "
            "co-participants (the normal case), 2 = peers of peers. Never above 2."
        ),
    )
    rationale: str = Field(description="Why this sub-task is needed.")


class MasterPlan(BaseModel):
    """Structured output of the Decompose stage (Stage B)."""

    supported: bool = Field(
        description=(
            "False when the query is out of scope (statistics / causal / trend). "
            "When False, no sub-tasks run and the agent answers NA."
        )
    )
    na_reason: str = Field(
        default="",
        description="When supported=False, a short reason shown in the NA answer.",
    )
    suggested_path: str = Field(
        default="",
        description="When supported=False, a suggested offline / sub-agent extraction path.",
    )
    target_type: TargetType = Field(
        default="unknown",
        description="Mirror of the analysis target_type for the executor.",
    )
    subtasks: list[SubTask] = Field(
        default_factory=list,
        description="Ordered, dependency-aware sub-tasks; empty when supported=False.",
    )


class DecomposeSignature(dspy.Signature):
    """Decompose a compositional query into an ordered sub-task DAG.

    Reuse the available retrieval tools for ``retrieve`` sub-tasks and reference
    fact names from the provided catalog only.  A question about a relationship
    BETWEEN TWO COMPANIES (who was in cartels with whom, who is connected to
    whom through shared cases) needs a ``graph_search`` sub-task with a short
    ``graph_seed`` stem - no SQL over the facts table or the relation views can
    answer it, because neither holds a company-to-company edge.  Set
    ``supported=False`` with a
    ``na_reason`` and a ``suggested_path`` for any query that needs statistical,
    causal, trend, or before/after inference - do NOT emit sub-tasks for those.
    """

    question: str = dspy.InputField(desc="The user's natural-language question.")
    analysis: QueryAnalysis = dspy.InputField(
        desc="The Stage A analysis: entities, target_type, aggregation, NA flag."
    )
    kb_schema: str = dspy.InputField(
        desc="Description of the facts / knowledge / chunk schema and tools."
    )
    fact_name_catalog: str = dspy.InputField(
        desc="Newline-separated list of the live distinct fact names."
    )
    plan: MasterPlan = dspy.OutputField(
        desc="Typed sub-task DAG, or supported=False for out-of-scope queries."
    )


# ---------------------------------------------------------------------------
# Stage F - Synthesis
# ---------------------------------------------------------------------------


class MasterAnswer(BaseModel):
    """Structured final answer composed from joined + verified rows."""

    answer_md: str = Field(
        description="The grounded answer in markdown, citing every celex_id."
    )
    confident: bool = Field(
        description=(
            "True whenever the evidence contains any verified rows that bear on "
            "the question - that is the normal case. Set False ONLY for genuine "
            "failure: the evidence is empty, the rows contradict each other, or "
            "they cannot address the question's dimension at all. Markdown or "
            "formatting noise and partial values are NOT grounds for False."
        )
    )
    cited_celex_ids: list[str] = Field(
        default_factory=list,
        description="Every celex_id cited in the answer.",
    )


class MasterSynthesizerSignature(dspy.Signature):
    """Compose a grounded markdown answer from joined and verified rows.

    The ``joined_evidence`` rows have ALREADY been deterministically joined and
    verified against the corpus before reaching you.  Each
    ``[celex_id] name = value`` line is authoritative ground truth - your job is
    to PRESENT these rows, not to re-adjudicate or second-guess them.

    Cite every celex_id that appears in the evidence.  For a 'case_list' answer
    format each entry as ``celex_id - case title``.  For a 'count_group' answer
    present grouped counts and call out the high / low extremes the question
    asked for.

    Be confident by default: any verified row that bears on the question
    supports an answer.  Reserve ``confident=False`` for true failure only -
    empty evidence, mutually contradictory rows, or rows that cannot address the
    question's dimension.  Condensed or partially formatted values are still
    valid evidence and are NOT a reason to hedge.
    """

    question: str = dspy.InputField(desc="The original user question.")
    joined_evidence: str = dspy.InputField(
        desc=(
            "Rendered joined + pre-verified rows produced by the executor. Fact "
            "values are normalized and may appear as condensed comma-separated "
            "lists."
        )
    )
    target_type: str = dspy.InputField(
        desc="The plan target_type: 'case_list', 'count_group', or 'compare'."
    )
    result: MasterAnswer = dspy.OutputField(
        desc="Final grounded answer with confidence flag and cited celex_ids."
    )
