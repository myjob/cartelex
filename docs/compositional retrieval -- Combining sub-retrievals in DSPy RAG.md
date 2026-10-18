My retrieval module in DSPy with open web UI delivers good results by searching my RAG, extracting pre-ingested facts and knowledge items. How could i build on top a retriever who combines sub retrievals such as: collect all legal cases affecting banks & provide their period of cartels as a histogram over years
-->

The useful reframe here is that "all bank cases + histogram of cartel periods over years" isn't a retrieval query at all — it's a small analytical _query plan_ with three phases that have very different reliability characteristics. Your existing `kb_sql`, `case_facts`, `rag_search`, and `rag_search_in_case` become the _primitives_; the new layer on top is a planner that decomposes the question, an executor that runs the sub-retrievals, and a reducer that aggregates deterministically. It slots in either as a new "compositional/analytical" route in your dispatcher, or as a thin layer above the router that calls it. This is the plan-and-execute-over-typed-primitives pattern, analytics-flavored rather than the multi-hop-text-QA flavor.

The first phase is **population resolution** — the set. "Cases affecting banks" is the denominator for everything downstream and the part most likely to silently corrupt the histogram. If "bank" is a structured field (sector, NACE code, defendant classification in your registry), this is a single SQL `WHERE` through `kb_sql` and it's exact. If it's only inferable from text, you fall back to `rag_search` for candidates plus a per-candidate entailment pass — and this is exactly where the principle you already adopted applies: keep the similarity ranking decoupled from the membership decision. A case being semantically near "bank cartel" is not the same proposition as "this case's infringement involved banks." A false positive adds a phantom bar; a false negative drops a real one. Instrument and audit this phase the hardest.

The second phase is **attribute projection** — the map. For each case in the population, fetch the cartel period. Since you've already normalized these via HeidelTime/TIMEX3 into `fact_registry`, resist looping `case_facts` N times through the LLM. Collapse it into one set-based query joining the population CTE to `fact_registry` on `fact_type = 'cartel_period'`. The per-case LLM path is only a fallback for cases where the period isn't yet a structured fact.

The third phase is **aggregation** — the reduce. The LLM must not do the arithmetic; binning and counting are deterministic SQL/pandas. Carry provenance: emit not just `year → count` but `year → [celex_ids]` so each bar is drillable back through `fact_provenance`. Someone will ask "which cases are in the 2001 bar?" and a legal artifact needs to answer that.

Before any of this, the planner has to resolve one genuine ambiguity that changes the answer's shape entirely: what does "cartel periods over years" _count_? Take Case A (1998–2002) and Case B (2001–2003). A _start-year_ histogram gives 1998→1, 2001→1. An _active-years_ histogram gives 1999→1, 2000→1, 2001→2, 2002→2, 2003→1. Completely different bars. You almost certainly want active-years (interval coverage), but the plan must commit to one and the rendered answer must state which. In Postgres that's an interval-to-years expansion:

```sql
WITH bank_cases AS (
  SELECT celex_id FROM case_registry
  WHERE defendant_sector = 'banking'        -- or NACE filter, or entailment-resolved id list
),
periods AS (
  SELECT fr.celex_id,
         lower(fr.cartel_period)::date AS start_d,
         upper(fr.cartel_period)::date AS end_d
  FROM fact_registry fr JOIN bank_cases USING (celex_id)
  WHERE fr.fact_type = 'cartel_period'
),
years AS (
  SELECT celex_id,
         generate_series(date_part('year', start_d)::int,
                         date_part('year', end_d)::int) AS yr
  FROM periods
)
SELECT yr, count(*) AS n_cartels, array_agg(celex_id) AS cases
FROM years GROUP BY yr ORDER BY yr;
```

The `array_agg` is what makes each bar auditable. For start-year semantics you drop the `generate_series` and bin on `date_part('year', start_d)`.

On the DSPy side, the planner produces a typed Pydantic plan and the executor is plain deterministic code:

```python
class AnalyticalPlan(BaseModel):
    population: PopulationSpec                                  # structured filter OR semantic+entailment
    attribute: Literal["cartel_period"]
    aggregation: Literal["histogram_by_year"]
    interval_semantics: Literal["active_years", "start_year", "end_year"]

class PlanAnalytical(dspy.Signature):
    """Decompose an analytical question into a typed, executable plan."""
    question: str = dspy.InputField()
    schema_hints: str = dspy.InputField(desc="structured fields, fact types, sector vocab")
    plan: AnalyticalPlan = dspy.OutputField()

class CompositionalRetriever(dspy.Module):
    def __init__(self, tools, db):
        self.plan   = dspy.Predict(PlanAnalytical)
        self.entail = dspy.ChainOfThought(MembershipEntailment)   # only for fuzzy populations
        self.tools, self.db = tools, db

    def forward(self, question):
        plan = self.plan(question=question, schema_hints=self.db.schema_hints).plan
        pop  = self._resolve_population(plan.population)   # exact SQL, or rag_search + entailment
        rows = self._project(pop, plan.attribute)          # one set-based SQL, not N LLM calls
        return self._aggregate(rows, plan)                 # deterministic; returns counts + provenance
```

The meta-principle tying it together: keep a _small_ surface of LLM judgment (decompose the question, resolve fuzzy predicates into a structured filter or an entailment list, choose the aggregation semantics) and a _large_ surface of deterministic execution. That split also maps cleanly onto your tooling — the `PlanAnalytical` signature and the `MembershipEntailment` judge are the two things worth compiling/optimizing with MIPROv2, while `_project` and `_aggregate` stay as ordinary, testable code that never varies.

Two things I'd pin down before building: whether your defendant/sector classification is structured enough to make population resolution exact (which decides whether you even need the entailment branch), and whether the front end in Open WebUI expects a chart-ready `{year, count, cases}` payload or you want the reducer to hand back something else. If it's useful I can write out the full executor — the `_resolve_population` fork, the entailment signature, and the chart payload shape — against your actual `fact_registry` / `case_registry` columns.

### can this compositional retrieval be abstracted in a way that I can use for other queries such as how many banking cases per EU country or so?
-->
Yes — and the cleanest way to see it is that "banking cases per country" and "banking cartel periods per year" are the _same plan shape_ with one slot swapped. Both are: resolve a population, derive a grouping key from each case, then count. The only thing that differs is how you turn a case into its grouping key(s). For the year query that key comes from expanding a temporal interval; for the country query it comes from a categorical field. So the abstraction isn't a new retriever per question — it's a fixed executor plus a small registry of _dimensions_ you can extend.

The unifying observation that makes this work: both of those dimensions are _multi-valued_. A cartel active 1998–2002 expands to five year-keys; a cartel affecting France, Germany, and Italy expands to three country-keys. If you define the dimension contract to return a _list_ of keys per case, the executor handles year, country, and any future categorical the same way — it always lands on a "one row per (case, key)" intermediate, and everything downstream is identical. Compare the two CTEs:

```sql
-- year query
years AS (
  SELECT celex_id, generate_series(start_yr, end_yr) AS key FROM periods
)
-- country query
country_keys AS (
  SELECT fr.celex_id, fr.value AS key
  FROM fact_registry fr JOIN bank_cases USING (celex_id)
  WHERE fr.fact_type = 'affected_member_state'
)
```

Structurally interchangeable. So the generalized plan becomes a typed selection over closed vocabularies rather than freeform:

```python
class AnalyticalPlan(BaseModel):
    population: PopulationSpec
    dimension: str                                   # must name a registered dimension
    measure: Literal["count", "sum", "avg"] = "count"
    count_mode: Literal["cases", "pairs"] = "cases"  # distinct cases vs case-key pairs
    measure_field: Optional[str] = None              # for sum/avg, e.g. fine amount

class Dimension(Protocol):
    name: str
    def keys_for(self, case_row) -> list[Key]: ...   # 1:1 or 1:many
    def to_sql(self, population_cte: str) -> str: ... # emits the (case_id, key) CTE
    def render_label(self, key) -> str: ...
```

You register `YearOfCartelInterval`, `AffectedMemberState`, `DefendantSector`, `DecisionYear`, `FineBucket`, and each new analytical question type is a new registration — the executor, the reducer, and the provenance handling never change. The planner's job shrinks to picking a registered dimension and measure, which is both more reliable (you can validate the plan and reject an unknown dimension before touching the DB) and exactly the kind of constrained mapping MIPROv2 optimizes well. This is essentially a tiny semantic/metrics layer over your registry — the dimensions-and-measures pattern from dbt/Cube/LookML — so there's good prior art if you want to borrow conventions.

The country query then falls out with zero new executor code:

```sql
WITH bank_cases AS (
  SELECT celex_id FROM case_registry WHERE defendant_sector = 'banking'
),
country_keys AS (
  SELECT fr.celex_id, fr.value AS country
  FROM fact_registry fr JOIN bank_cases USING (celex_id)
  WHERE fr.fact_type = 'affected_member_state'
)
SELECT country,
       count(DISTINCT celex_id) AS n_cases,       -- count_mode = 'cases'
       array_agg(DISTINCT celex_id) AS cases
FROM country_keys GROUP BY country ORDER BY n_cases DESC;
```

Two wrinkles specific to "per country," both of which the abstraction already absorbs if you anticipate them. First, the double-counting question is the direct analog of the interval-semantics ambiguity from before: a multi-country cartel counts once per country (`count_mode="pairs"`, so column totals exceed your case count) or you collapse to a primary jurisdiction (`count_mode="cases"` with a single-valued dimension). The planner has to commit and the answer has to say which — `count(DISTINCT celex_id)` versus `count(*)` is that switch. Second, "EU country" needs a controlled vocabulary: a member-state list, plus a decision on how to handle EU-wide Commission decisions, third countries, and historical membership changes. That normalization belongs _inside_ the `AffectedMemberState` dimension handler, not in the executor — which is the general principle here: each dimension owns its own quirks, so the rest of the system stays generic.

The population resolution fork (structured filter vs. `rag_search` + entailment) is shared across every query, so "banking" gets resolved once the same way whether you're asking about years or countries. If you want, the executor skeleton I offered last time can be the generic version directly — the `to_sql` dimension contract plus a `YearOfCartelInterval` and `AffectedMemberState` handler — so you'd have both queries working off one codebase rather than two.

I'll search for current resources on this.Here's a curated set, ordered roughly from foundational to most directly applicable to the decompose-then-combine pattern you're building. I've noted what each actually gives you, since the "compositional/sub-retrieval" framing maps onto a few different DSPy idioms.

Official DSPy docs (most authoritative, kept current):

- **Multi-Hop Retrieval** — https://dspy.ai/tutorials/multihop_search/ — the canonical "compose sub-retrievals" example. It builds a module with two sub-modules, generate_query and append_notes, that takes a claim and iteratively produces a list of titles. This is the closest official analog to chaining your `rag_search` calls, though it's iterative-evidence-gathering rather than the map-then-aggregate shape you want.
    
- **Building RAG as Agent** — https://dspy.ai/tutorials/agents/ — sets up a dspy.ReAct agent with search and lookup tools and then optimizes it for advanced multi-hop browsing. This is the pattern if you let a ReAct loop select among your four tools dynamically rather than running a fixed plan.
    
- **Customizing DSPy Modules** — https://dspy.ai/tutorials/custom_module/ — walks through a multi-stage RAG built from query generation, context retrieval, and answer generation, and notes that inside forward() you can call arbitrary Python — database handlers, MCP tools, and so on, not just other DSPy modules. That last point is the key license for the deterministic SQL executor we discussed: the aggregation stays plain Python, only the planning is a module.
    
- **RAG (foundational)** — https://dspy.ai/tutorials/rag/ — and the **tutorials index** — https://dspy.ai/tutorials/ — which also lists "Advanced Tool Use," "RL for Multi-Hop Research," and the GEPA optimizer tutorials worth a look given your MIPROv2 work.
    

Conceptual grounding:

- **Demonstrate-Search-Predict (DSP)** — arXiv 2212.14024 — the paper DSPy grew out of, on composing retrieval and language models for knowledge-intensive NLP. This is the academic root of "compositional retrieval" and worth reading for the vocabulary.

Community deep-dives (closest to your actual goal):

- **Building a Multi-Agent RAG System with DSPy** (Mark Kiehl) — https://medium.com/@markwkiehl/building-a-multi-agent-rag-system-with-dspy-d4b497475b83 — the most relevant practical one. It uses a MasterPlanAgent that decomposes a complex query into a sequence of targeted steps, plus a search agent combining BM25 and vector retrieval via Reciprocal Rank Fusion — i.e. plan-decomposition over sub-retrievals with the RRF hybrid you already use.
    
- **Multi-Agent RAG with DSPy + GEPA** (Kargar) — https://kargarisaac.medium.com/building-and-optimizing-multi-agent-rag-systems-with-dspy-and-gepa-2b88b5838ce2 — builds two domain subagents each with their own vector-search tool, then a lead agent that orchestrates them, optimizing each ReAct subagent's prompts. Good for the subagent-composition angle and the judge-module/metric design.
    
- **Improving RAG Answer Quality Through Complex Reasoning** (Towards Data Science) — https://towardsdatascience.com/improving-rag-answer-quality-through-complex-reasoning-2608ec6c2a65/ — a worked multi-hop walkthrough with a concrete example of why single-shot retrieval fails on compound questions.
    

One honest caveat: almost all of this literature is multi-hop _text QA_ — chaining retrievals to accumulate evidence for one answer. None of it directly covers the decompose → map-over-a-population → deterministic-aggregate shape behind your histogram/per-country queries, which is closer to text-to-analytics with a semantic layer. So take the _planner/decomposition_ and _tool-composition_ mechanics from the multi-hop and RAG-as-agent tutorials, take the _query decomposition into typed steps_ idea from Kiehl's MasterPlanAgent, and supply the aggregation layer yourself (the dimension registry + SQL we sketched). If it helps, I can sanity-check your planner signature against the multi-hop tutorial's structure once you've drafted it.


https://github.com/markwkiehl/medium_dspy_multi_agent_pipeline_search_docs_web