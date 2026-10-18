
> **Reranker:** the `HybridSearch` reranker (`FlashRanker`) is now pluggable — see **[Design BgeRanker](Design%20BgeRanker.md)** (`settings.RERANKER_BACKEND`).

Using DSPy, I want to build a user Q & A which plans and execute complex retrieval from 1) a knowledge base storing facts about ~800 legal cases, and 2) a RAG which offers hybrid search over all the documents.

User questions may target one legal case or complex questions about all the cases with a **macroscopic aggregation**  such as

"what did the number of leniency cases accepeted by the EU commission increased over time?"


--->  Claude 
Here's how I'd structure this in DSPy. The core insight: your two retrieval surfaces have different shapes (structured facts vs. unstructured text), so the planner's main job is choosing the right surface for each sub-question, and aggregation questions almost always want to hit the structured KB via generated queries.

## Architecture

```
question
  └─ Planner ──► decomposed steps
       └─ Agent (ReAct loop) ──► tools
              ├─ kb_sql(query)              # structured aggregation
              ├─ case_facts(case_id)        # single-row lookup
              ├─ rag_search(query, filters) # hybrid search over docs
              └─ rag_search_in_case(case_id, query)
       └─ Synthesizer ──► grounded answer + citations
```

The KB should be exposed as a SQL-queryable surface (DuckDB/SQLite/Postgres) — without that, "did leniency cases accepted by the Commission increase over time" devolves into manually fetching 800 records and counting. With SQL, it's one tool call and one `GROUP BY year`.

## DSPy modules

```python
import dspy

# --- Signatures ---

class PlanRetrieval(dspy.Signature):
    """Decompose a legal question into retrieval steps.
    Classify the question as: single_case | aggregate | semantic | hybrid.
    - single_case: known case → case_facts + optional rag_search_in_case
    - aggregate: counts/trends/distributions across cases → kb_sql
    - semantic: reasoning/text-level question → rag_search
    - hybrid: aggregate over a filtered set, then read text for examples
    """
    question: str = dspy.InputField()
    kb_schema: str = dspy.InputField(desc="Tables, columns, enums, value samples")
    question_type: str = dspy.OutputField()
    steps: list[str] = dspy.OutputField(desc="Ordered tool-call sketches")

class GenerateKBSQL(dspy.Signature):
    """Translate a question into a single read-only SQL query over the cases KB.
    Use only columns and enum values present in the schema.
    """
    question: str = dspy.InputField()
    schema: str = dspy.InputField()
    sql: str = dspy.OutputField()

class Synthesize(dspy.Signature):
    """Answer the question grounded strictly in evidence. Cite case_ids and doc spans."""
    question: str = dspy.InputField()
    evidence: list[dict] = dspy.InputField()
    answer: str = dspy.OutputField()
    citations: list[str] = dspy.OutputField()


# --- Tools ---

def kb_sql(sql: str) -> list[dict]:
    """Run read-only SQL against the cases KB. Returns rows or an error message."""
    if not is_safe_select(sql):                      # validate: SELECT-only, no DDL
        return [{"error": "only read-only SELECT allowed"}]
    return db.execute(sql).fetchall()

def case_facts(case_id: str) -> dict: ...
def rag_search(query: str, filters: dict | None = None, k: int = 8) -> list[dict]: ...
def rag_search_in_case(case_id: str, query: str, k: int = 8) -> list[dict]: ...


# --- Composition ---

class LegalQA(dspy.Module):
    def __init__(self, schema_summary: str):
        super().__init__()
        self.schema = schema_summary
        self.plan = dspy.ChainOfThought(PlanRetrieval)
        self.agent = dspy.ReAct(
            signature="question, plan -> evidence: list[dict]",
            tools=[kb_sql, case_facts, rag_search, rag_search_in_case],
            max_iters=8,
        )
        self.synth = dspy.ChainOfThought(Synthesize)

    def forward(self, question: str):
        p = self.plan(question=question, kb_schema=self.schema)
        e = self.agent(question=question, plan="\n".join(p.steps))
        return self.synth(question=question, evidence=e.evidence)
```

## Why two stages instead of pure ReAct

A standalone `dspy.ReAct` will work, but separating _Plan_ from _Execute_ gives you:

- a place to validate the plan ("aggregate question — yet no kb_sql call planned" → reject and re-plan),
- a chance to show the user the plan before spending tokens on retrieval,
- cleaner traces for evaluation and DSPy compilation.

Inside the executor, ReAct is still the right pattern because retrieval needs to be adaptive: if a SQL query returns zero rows you want the agent to widen filters, not give up.

## The aggregation case in practice

For _"did the number of leniency cases accepted by the EU Commission increase over time?"_ the agent should converge on something like:

```sql
SELECT date_trunc('year', decision_date) AS yr,
       COUNT(*) AS n
FROM cases
WHERE jurisdiction = 'EU_COMMISSION'
  AND case_type    = 'LENIENCY'
  AND outcome      = 'ACCEPTED'
GROUP BY yr ORDER BY yr;
```

For this to work reliably the LLM needs to know your **controlled vocabulary**. Pass a schema summary that includes distinct values (or top-k) of low-cardinality fields:

```
cases(case_id PK, decision_date DATE,
      jurisdiction ENUM['EU_COMMISSION','DG_COMP','NCA_DE',...],
      case_type    ENUM['CARTEL','MERGER','LENIENCY','ABUSE_DOMINANCE',...],
      outcome      ENUM['ACCEPTED','REJECTED','SETTLED','PENDING',...],
      total_fine_eur NUMERIC, ...)
```

Without enums exposed, the model will hallucinate `case_type='leniency application'` and the query returns nothing.

## Things worth getting right

The **schema-as-prompt** matters more than the model choice. Keep a `schema.md` that lists tables, columns, enum values, and 2–3 example queries; load it into the planner's `kb_schema` field. Refresh it whenever the KB changes.

**SQL safety**: gate `kb_sql` to read-only with a parser (`sqlglot` works), block multi-statement, and cap row count. Return errors as tool output so ReAct can self-correct rather than crash.

**Hybrid retrieval needs a per-case filter**. Many user questions are "in case X, what did the court say about Y" — your dense+BM25 index should accept a `case_id` filter, otherwise top-k will be polluted by similar passages from neighboring cases.

**Compile with examples**. Build ~50 labeled examples spanning all four question types (single_case / aggregate / semantic / hybrid) with the expected SQL or retrieval calls and the gold answer. Then:

```python
from dspy.teleprompt import MIPROv2
optimizer = MIPROv2(metric=legal_qa_metric, auto="medium")
compiled = optimizer.compile(LegalQA(schema), trainset=train, valset=val)
```

Your metric should reward (a) correct routing — did it call `kb_sql` for aggregates? — and (b) factual correctness against gold, not just answer overlap. Routing failures dominate errors in this kind of system, so make them visible in the metric.

**Hybrid questions** ("show me cartel cases above €500M and quote what the Commission said about deterrence") are where the architecture earns its keep: agent calls `kb_sql` to get the list, then loops `rag_search_in_case` for each. Set `max_iters` high enough (8–12) and consider a soft budget on tool calls so it doesn't fan out across all 800 cases.

If you want, I can sketch the schema introspection helper or the eval harness next.


** Implementation **
Built the Q&A Retrieval Pipeline using the **Plan → Execute → Synthesize** architecture. All **18 tests pass**.

### What was created/modified:

|Component|Description|
|---|---|
|**Protocols**|`KBFactSearcher`, `CrossCaseAggregator` in `dspy_protocols.py`|
|**Signatures**|`RetrievalStep`, `RetrievalPlan`, `QAPlannerSignature`, `QASynthesizerSignature` — all declarative, no f-strings|
|**Modules**|`QAPlannerModule` → `QAExecutorModule` → `QASynthesizerModule`, composed by `QARetrievalPipeline`|
|**Tools**|`kb_sql` (aggregation), `case_facts` (single-case), `rag_search` (cross-case semantic), `rag_search_in_case` (hybrid per-case)|
|**Safety**|SQL gate blocking write operations, multi-statement, auto-LIMIT|
|**Tests**|18 tests covering both your test cases + SQL safety + tool routing|

### Key decision:

The `CelexDatabaseToolkit` already satisfies `KBFactSearcher` (via `semantic_search_facts`). The one missing piece is a concrete `CrossCaseAggregator` — a thin wrapper executing read-only SQL against `FactRepository.engine`. See the walkthrough artifact for wiring details.

[ [qa_retrieval_pipeline.md](obsidian://open?vault=celex-facts&file=docs%2Fqa_retrieval_pipeline)](Qa%20Retrieval%20Pipeline%20)
obsidian://open?vault=celex-facts&file=docs%2Fqa_retrieval_pipeline


how each existing component maps to the four tools:

**Used directly:**

- `rag_search_in_case` → injects `HybridSearcher` protocol → satisfied by the existing `HybridSearch` class from `hybrid_search.py` (keyword + semantic + FlashRanker)
- `case_facts` → injects `SQLFactStore` protocol → satisfied by `FactRepository`
- `rag_search` → injects `KBFactSearcher` protocol → satisfied by `CelexDatabaseToolkit.semantic_search_facts()` (Qwen3 embeddings over `question2fact2embeddings`)

**Needs new implementation:**

- `kb_sql` → injects `CrossCaseAggregator` protocol → **no existing impl yet** (needs a thin wrapper around `FactRepository.engine`)

So 3 out of 4 tools reuse your existing infrastructure directly. The `rag_search` tool uses semantic fact search rather than raw document chunk search — which is the right choice for cross-case queries since fact embeddings are pre-distilled knowledge (one embedding per question-answer pair), whereas raw chunks would return noisy passages from 800 documents.

One thing **not** used is the cross-case raw chunk search (`CelexDatabaseToolkit.search_document_chunk`). If you want that as a 5th tool for exploratory questions where facts haven't been extracted yet, I can add a `doc_search` tool. Want me to add it, or is the current set of four tools sufficient?

Tool	What it searches	Best for
kb_sql	Structured facts via SQL	Aggregation, counting, trends across ~800 cases
case_facts	All facts for one celex_id	Single-case deep dive
rag_search	Fact embeddings (question2fact2embeddings)	Cross-case semantic fact lookup
rag_search_in_case	Document chunks via HybridSearch (keyword+semantic+FlashRanker)	Scoped hybrid search within one case
doc_search (new)	Raw document chunks (pg_embedding_qwen) across all cases	Commissioner names, countries, towns — entity-specific lookups
The doc_search tool is satisfied by CelexDatabaseToolkit.semantic_search_chunks() which already exists.


---
---> Gemini


Building a multi-source, reasoning-heavy system is exactly where DSPy shines. Unlike traditional chained architectures (like LangChain) where you hand-craft the prompt for every step, DSPy allows you to define the **flow of information** and then computationally optimize the prompts for your specific legal dataset.

Your use case presents a classic challenge: balancing **needle-in-a-haystack retrieval** (specific case facts) with **macroscopic aggregation** (trend analysis across 800 cases).

Here is how you should architect and build this using DSPy agents.

---

### 1. The Architecture Strategy

For this to work effectively, a standard similarity-search RAG will fail on your complex question ("how did the number of leniency cases increase over time?"). Standard RAG will just retrieve the top $K$ chunks mentioning "leniency cases" and miss the big picture.

You need a **ReAct (Reason + Act)** agent equipped with purpose-built tools:

1. **The KB Tool (Structured Data):** Instead of semantic search, this tool should accept query parameters (or SQL) to filter, group, and count metadata (e.g., dates, verdicts, case types).
    
2. **The Hybrid RAG Tool (Unstructured Data):** This executes dense + sparse retrieval (e.g., BM25 + Vector) to find specific arguments, clauses, or nuances inside the case documents.
    

### 2. Step-by-Step Implementation

#### Step 2.1: Define Your Tools

In DSPy, an agent's tools are simply Python functions. The docstrings are critical because DSPy's ReAct module uses them to understand _when_ and _how_ to use the tool.

Python

```
def search_legal_kb(query: str) -> str:
    """
    Use this tool to find aggregated statistics, dates, and hard facts across the 800 legal cases.
    It translates natural language into structured queries (like SQL) against the knowledge base.
    Example: "count of leniency cases by year" -> returns statistical summaries.
    """
    # Implementation: Use an LLM to map query to SQL/API call, or rely on an existing API.
    # Return a stringified JSON or summary table.
    return "{'2018': 12, '2019': 15, '2020': 22, '2021': 35}"

def search_case_documents(query: str) -> str:
    """
    Use this tool to do a hybrid search over the raw text of the legal documents.
    Best for finding specific clauses, legal arguments, or nuanced definitions within cases.
    """
    # Implementation: Call your Vector DB / Elasticsearch hybrid search endpoint.
    # Return concatenated text chunks.
    return "Chunk 1: ... \n Chunk 2: ..."
```

#### Step 2.2: Define the DSPy Signatures

Signatures define the input/output behavior of your modules. For a complex Q&A system, it is highly recommended to split the task: first **Plan**, then **Execute**.

Python

```
import dspy

class LegalPlannerSignature(dspy.Signature):
    """Plan a step-by-step strategy to answer complex legal questions involving multiple sources."""
    question = dspy.InputField(desc="The user's legal question.")
    plan = dspy.OutputField(desc="A step-by-step plan on how to answer the question using a structured KB and a document RAG.")

class LegalSynthesizerSignature(dspy.Signature):
    """Synthesize the final answer based on the original question and the accumulated context."""
    question = dspy.InputField()
    context = dspy.InputField(desc="Information retrieved from the KB and Document RAG.")
    answer = dspy.OutputField(desc="A detailed, comprehensive answer with citations if applicable.")
```

#### Step 2.3: Build the Agent Module

Now, we wire the components together. We will use `dspy.ChainOfThought` for the planner, and `dspy.ReAct` as the execution engine that wields your tools.

Python

```
class LegalResearchAgent(dspy.Module):
    def __init__(self):
        super().__init__()
        # 1. The Planner
        self.planner = dspy.ChainOfThought(LegalPlannerSignature)
        
        # 2. The ReAct Executor (The Agent)
        # We pass a simple question -> answer signature, but equip it with tools.
        self.executor = dspy.ReAct("question -> context", tools=[search_legal_kb, search_case_documents], max_iters=4)
        
        # 3. The Synthesizer
        self.synthesizer = dspy.ChainOfThought(LegalSynthesizerSignature)

    def forward(self, question):
        # Step 1: Generate a plan
        plan_result = self.planner(question=question)
        
        # Step 2: Agent executes the plan to gather context
        # We prompt the executor to fulfill the plan
        execution_prompt = f"Question: {question}\nExecution Plan: {plan_result.plan}"
        execution_result = self.executor(question=execution_prompt)
        
        # Step 3: Synthesize the final answer
        final_result = self.synthesizer(question=question, context=execution_result.context)
        
        return dspy.Prediction(
            plan=plan_result.plan,
            context=execution_result.context,
            answer=final_result.answer
        )

# Initialize the agent
agent = LegalResearchAgent()
```

### 3. The DSPy "Magic": Compilation (Optimization)

This is the most crucial step. Out of the box, the agent might struggle to choose the right tool or formulate the right queries. DSPy optimizes your agent by rewriting its internal prompts based on a dataset of examples.

You need to create a small dataset (20-50 examples) of varied questions and their correct answers.

Python

```
from dspy.teleprompt import BootstrapFewShotWithRandomSearch

# 1. Create a training set
trainset = [
    dspy.Example(
        question="How did the number of leniency cases accepted by the EU commission increase over time?", 
        answer="The number of leniency cases increased steadily from 12 in 2018 to 35 in 2021..."
    ).with_inputs("question"),
    dspy.Example(
        question="What was the primary argument in the Microsoft antitrust case?", 
        answer="The primary argument centered on the bundling of Internet Explorer..."
    ).with_inputs("question"),
    # Add more diverse examples here...
]

# 2. Define a metric to evaluate the agent (e.g., exact match, or an LLM-as-a-judge checking for factual accuracy)
def legal_eval_metric(example, pred, trace=None):
    # For a real system, use an LLM (dspy.Evaluate) to check if `pred.answer` matches the nuance of `example.answer`
    return dspy.evaluate.answer_exact_match(example, pred)

# 3. Compile the agent
optimizer = BootstrapFewShotWithRandomSearch(
    metric=legal_eval_metric,
    max_bootstrapped_demos=3,
    max_labeled_demos=3,
    num_candidate_programs=5,
    num_threads=4
)

# This process will run the agent, look at the traces, and rewrite the prompt instructions 
# for the Planner, Executor, and Synthesizer to make them highly effective.
optimized_legal_agent = optimizer.compile(LegalResearchAgent(), trainset=trainset)

# 4. Use the optimized agent
response = optimized_legal_agent(question="How did the number of leniency cases accepted by the EU commission increase over time?")
print(response.answer)
```

### Key Takeaways for Your Use Case

1. **Tool Segregation is vital:** Do not force standard RAG to do math or timeline aggregation. Ensure your `search_legal_kb` tool can handle metadata filtering (like "GROUP BY year").
    
2. **Context Limits:** The ReAct agent's `max_iters` should be carefully tuned (usually 3 to 5). If it loops too many times fetching legal text, it will blow out the context window.
    
3. **Optimize for the Planner:** The reason we added an explicit `planner` module before the `ReAct` executor is that LLMs often rush into searching without thinking about _how_ to aggregate data. The `plan` step forces the model to realize: _"To answer this, I first need to hit the structured KB to get the timeline data, rather than reading 800 individual documents."_
---------------------------------
---> Copilot
how to build this with DSPy agents?Build it as a **DSPy program**, not just as a bigger prompt. The pattern I recommend is:

user question

   -> planner: decompose into subquestions

   -> executor: answer each subquestion with RAG / KB tools

   -> synthesizer: write final answer from collected evidence only

   -> verifier: check coverage, support, contradictions

   -> repair loop: retrieve missing evidence or revise answer

DSPy is a good fit for this because signatures define typed input/output contracts, modules can be composed into larger programs, and dspy.ReAct can manage iterative tool use over callable tools when you want the model to decide which retrieval function to call next. DSPy’s docs describe signatures as declarative input/output specs, modules as composable building blocks, and dspy.ReAct as a tool-using module that reasons, selects tools, receives observations, and continues until it can finish. ([DSPy](https://dspy.ai/learn/programming/signatures/ "Signatures - DSPy"))

**1. Wrap your existing RAG and KB modules as narrow tools**

Do **not** expose a huge “answer everything” tool. Expose small, auditable tools that return evidence.

import json

import dspy

# Example wrappers around your existing modules / clients.

# Adapt the internals to your actual RAG and KB APIs.

def rag_search(query: str) -> str:

    """Search unstructured documents. Use for text passages, policies, manuals, reports, emails, etc.

    Return JSON with passage_id, title, text, source_url, and score.

    """

    # Example:

    # result = rag_module(query=query, k=5)

    # return json.dumps(result.passages, ensure_ascii=False)

    raise NotImplementedError

def kb_lookup(query: str) -> str:

    """Query the structured knowledge base. Use for entities, attributes, relations, IDs, dates, ownership,

    dependencies, and canonical facts. Return JSON records with record_id, entity, relation, value, source.

    """

    # Example:

    # result = kb_module(question=query)

    # return json.dumps(result.records, ensure_ascii=False)

    raise NotImplementedError

The important part is that tools return **evidence**, not just final prose. A verifier cannot reliably check an answer if the retrieval tools hide provenance.

**2. Define typed planning, synthesis, and verification signatures**

Use structured outputs for the plan and verification result. This gives you programmatic control over stopping, retries, logging, and evaluation.

from __future__ import annotations

import json

from typing import Literal

from pydantic import BaseModel, Field

import dspy

class SubTask(BaseModel):

    id: int

    subquestion: str

    source: Literal["rag", "kb", "both"]

    depends_on: list[int] = Field(default_factory=list)

    success_criterion: str

class EvidenceItem(BaseModel):

    step_id: int

    subquestion: str

    source: Literal["rag", "kb", "react"]

    query: str

    answer: str

    citations: list[str] = Field(default_factory=list)

    confidence: float = 0.0

class VerificationResult(BaseModel):

    passed: bool

    score: float = Field(ge=0.0, le=1.0)

    missing_evidence: list[str] = Field(default_factory=list)

    unsupported_claims: list[str] = Field(default_factory=list)

    contradictions: list[str] = Field(default_factory=list)

    followup_questions: list[str] = Field(default_factory=list)

class PlanQuestion(dspy.Signature):

    """Decompose a complex user question into the minimal set of factual subquestions.

    Prefer fewer steps. Each step must be answerable by RAG, KB, or both.

    """

    question: str = dspy.InputField()

    steps: list[SubTask] = dspy.OutputField(

        desc="Ordered plan of subquestions needed to answer the original question."

    )

class ExecuteStep(dspy.Signature):

    """Answer one subquestion using the provided tools. Use tool evidence, not prior knowledge.

    Return citations or record IDs whenever possible.

    """

    original_question: str = dspy.InputField()

    subquestion: str = dspy.InputField()

    prior_evidence_json: str = dspy.InputField(

        desc="JSON array of evidence already collected from earlier steps."

    )

    step_answer: str = dspy.OutputField()

    citations: list[str] = dspy.OutputField()

    confidence: float = dspy.OutputField(desc="0.0 to 1.0 confidence based on evidence quality.")

class SynthesizeAnswer(dspy.Signature):

   """Answer the original question using only the supplied evidence.

    Cite evidence IDs inline. Say what is unknown when evidence is missing.

    """

    question: str = dspy.InputField()

    evidence_json: str = dspy.InputField()

    answer: str = dspy.OutputField()

    claims: list[str] = dspy.OutputField(

        desc="Atomic factual claims made in the answer."

    )

class VerifyAnswer(dspy.Signature):

    """Verify whether the answer fully answers the question and whether every factual claim is supported.

    Check for missing subquestions, unsupported claims, and contradictions.

    """

    question: str = dspy.InputField()

    evidence_json: str = dspy.InputField()

    answer: str = dspy.InputField()

    claims: list[str] = dspy.InputField()

    verdict: VerificationResult = dspy.OutputField()

class RepairPlan(dspy.Signature):

    """Given a failed verification, propose only the additional subquestions needed to repair the answer."""

    question: str = dspy.InputField()

    evidence_json: str = dspy.InputField()

    verification_json: str = dspy.InputField()

    new_steps: list[SubTask] = dspy.OutputField()

**3. Compose the full planner-executor-verifier module**

Here is a practical skeleton. It uses dspy.ReAct only for **step execution**, where dynamic tool choice is useful. The outer control flow remains deterministic and inspectable.

class ComplexQAAgent(dspy.Module):

    def __init__(

        self,

        rag_tool,

        kb_tool,

        max_initial_steps: int = 6,

        max_repair_rounds: int = 2,

        max_repair_steps: int = 4,

    ):

        super().__init__()

        self.max_initial_steps = max_initial_steps

        self.max_repair_rounds = max_repair_rounds

        self.max_repair_steps = max_repair_steps

        self.plan = dspy.ChainOfThought(PlanQuestion)

        # ReAct is useful here: the model can decide RAG vs KB vs both per subquestion.

        self.execute_step = dspy.ReAct(

            signature=ExecuteStep,

            tools=[rag_tool, kb_tool],

            max_iters=5,

        )

        self.synthesize = dspy.ChainOfThought(SynthesizeAnswer)

        self.verify = dspy.ChainOfThought(VerifyAnswer)

        self.repair = dspy.ChainOfThought(RepairPlan)

    def _json(self, obj) -> str:

        def encode(x):

            if hasattr(x, "model_dump"):

                return x.model_dump()

            if hasattr(x, "__dict__"):

                return x.__dict__

            return str(x)

        return json.dumps(obj, default=encode, ensure_ascii=False, indent=2)

    def _execute_steps(

        self,

        question: str,

        steps: list[SubTask],

        evidence: list[EvidenceItem],

    ) -> list[EvidenceItem]:

        for step in steps:

            pred = self.execute_step(

                original_question=question,

                subquestion=step.subquestion,

                prior_evidence_json=self._json(evidence),

            )

            evidence.append(

                EvidenceItem(

                    step_id=[step.id](http://step.id/),

                    subquestion=step.subquestion,

                    source="react",

                    query=step.subquestion,

                    answer=pred.step_answer,

                    citations=pred.citations,

                    confidence=float(pred.confidence),

                )

            )

        return evidence

    def forward(self, question: str):

        # 1. Plan

        planned = self.plan(question=question)

        steps = planned.steps[: self.max_initial_steps]

        evidence: list[EvidenceItem] = []

        # 2. Execute initial plan

        evidence = self._execute_steps(question, steps, evidence)

        # 3. Synthesize

        draft = self.synthesize(

            question=question,

            evidence_json=self._json(evidence),

        )

        # 4. Verify

        verification = self.verify(

            question=question,

            evidence_json=self._json(evidence),

            answer=draft.answer,

            claims=draft.claims,

        ).verdict

        # 5. Repair loop

        repair_round = 0

        while not verification.passed and repair_round < self.max_repair_rounds:

            repair_round += 1

            repair_pred = self.repair(

                question=question,

                evidence_json=self._json(evidence),

                verification_json=self._json(verification),

            )

            new_steps = repair_pred.new_steps[: self.max_repair_steps]

            if not new_steps:

                break

            evidence = self._execute_steps(question, new_steps, evidence)

            draft = self.synthesize(

                question=question,

                evidence_json=self._json(evidence),

            )

            verification = self.verify(

                question=question,

                evidence_json=self._json(evidence),

                answer=draft.answer,

                claims=draft.claims,

            ).verdict

        return dspy.Prediction(

            answer=draft.answer,

            plan=steps,

            evidence=evidence,

            verification=verification,

            repair_rounds=repair_round,

        )

Usage:

agent = ComplexQAAgent(

    rag_tool=rag_search,

    kb_tool=kb_lookup,

)

result = agent(question="Which suppliers affected by policy X also have open incidents in region Y?")

print(result.answer)

print(result.verification)

print(result.evidence)

dspy.ReAct accepts callables or dspy.Tool instances as tools, and the docs show that it keeps a trajectory of reasoning/tool calls, which is useful for debugging multi-call behavior. ([DSPy](https://dspy.ai/api/modules/ReAct/ "ReAct - DSPy"))

**4. Add a reward function for automatic refinement**

Once the basic module works, wrap it in dspy.Refine or dspy.BestOfN to get multiple attempts and select the best verified result. DSPy’s current output-refinement docs describe BestOfN and Refine as modules that run multiple attempts and use a reward_fn plus threshold to stop or select the best prediction. ([DSPy](https://dspy.ai/tutorials/output_refinement/best-of-n-and-refine/ "Output Refinement - DSPy"))

def verification_reward(args, pred: dspy.Prediction) -> float:

    v = pred.verification

    score = float(v.score)

    # Penalize unsupported claims heavily.

    if v.unsupported_claims:

        score -= 0.25

    # Penalize missing evidence.

    if v.missing_evidence:

        score -= 0.20

    # Penalize contradictions.

    if v.contradictions:

        score -= 0.30

    # Require verifier pass for high score.

    if not v.passed:

        score = min(score, 0.49)

    return max(0.0, min(1.0, score))

verified_agent = dspy.Refine(

    module=agent,

    N=3,

    reward_fn=verification_reward,

    threshold=0.85,

)

**5. Optimize it with examples, not prompt tweaking**

Create a small train/dev set of complex questions with expected answers and, ideally, expected evidence IDs. Then compile the whole program or selected submodules. DSPy optimizers can tune few-shot examples and instructions; the docs describe MIPROv2 as optimizing instructions and few-shot demonstrations jointly using bootstrapping, instruction proposals, and Bayesian search. ([DSPy](https://dspy.ai/learn/optimization/optimizers/ "Optimizers - DSPy"))

def complex_qa_metric(example, pred, trace=None) -> float:

    # Minimal example metric.

    # In production, combine:

    # - answer correctness

    # - citation/evidence support

    # - no unsupported claims

    # - no contradictions

    # - tool budget compliance

    v = pred.verification

    if not v.passed:

        return 0.0

    if v.unsupported_claims or v.contradictions:

        return 0.0

    return float(v.score)

optimizer = dspy.MIPROv2(

    metric=complex_qa_metric,

    auto="medium",

    max_bootstrapped_demos=4,

    max_labeled_demos=4,

)

compiled_agent = optimizer.compile(

    student=agent,

    trainset=trainset,

    valset=devset,

)

**6. Practical rules that matter in production**

Keep the **planner** constrained. Tell it to produce the _minimal_ number of subquestions and cap max_initial_steps.

Keep the **retrieval tools evidence-first**. Return snippets, triples, record IDs, timestamps, source names, and scores. Avoid returning only natural-language answers.

Keep the **verifier separate** from the synthesizer. The synthesizer wants to be helpful; the verifier should be skeptical.

Use deterministic checks before LM verification where possible. For example: “every final answer citation must appear in evidence,” “no answer if confidence below threshold,” “no claim without citation,” and “KB entity IDs must exist.”

Log plan, evidence, answer, claims, verification, and ReAct trajectory. This gives you debuggability when the agent answers incorrectly.

A good final production shape is:

ComplexQAAgent

  planner: ChainOfThought(PlanQuestion)

  executor: ReAct(ExecuteStep, tools=[rag_search, kb_lookup])

  synthesizer: ChainOfThought(SynthesizeAnswer)

  verifier: ChainOfThought(VerifyAnswer)

  repairer: ChainOfThought(RepairPlan)

  outer wrapper: Refine or BestOfN with verification_reward

  optimizer: MIPROv2 or BootstrapFewShot on real multi-hop examples

That gives you an agent that can decompose, retrieve repeatedly, verify, repair, and expose why it answered the way it did.