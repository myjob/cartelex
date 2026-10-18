  - [x] feature 20260619: Master Plan Agent
  - [ ] feature 20260619: write integration test cases for class MasterPlannerModule - List all cases where Bayer AG was part of a cartel - "List all cases where company **Bayer AG** was the cartel leader" - "Which countries have a lot of banking cartels and which have a low number?" - "Did the number of banking cartel cases increase after the leniency law?"
  - [ ] feature 20260619: add progress of "Verifying contributing facts" such as 1 of 10 ..
  - [ ] feature 20260619: add a time tag rather than the [master]
  - [x] issue: master step "Verifying contributing facts" for  "List all cases where Bayer AG was part of a cartel" seems to hanging in  a loop
  - [x]  Added `TestAnheuserBuschMembershipHedgedSynthesizer` to [test_master_planner_integration.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/tests/test_master_planner_integration.py), faithfully reproducing the real scenario: two `kb_sql` legs joined on `celex_id`, a synthesizer that returns `confident=False`, and assertions that the result is `master_planner` (not NA) and lists both `40134_2872_5` and `CELEX-32000D0146` while excluding the companies-only case dropped by the join intersection. - Real pipeline: now returns the two cases.- 39 master-planner tests pass (2 perf benchmarks skipped), 31 router tests pass, ruff clean. One note worth surfacing: this fix makes the agent robust to a hedging synthesizer, but the synthesizer _itself_ is still returning `confident=False` on clean evidence (markdown-table company values may be throwing it off). That's a separate prompt/quality issue — the deterministic fallback now masks it rather than curing it. Want me to look at the `MasterSynthesizerSignature` prompt separately?

celex-dspy-agent

🔍 Validating case reference …  
📋 Building retrieval plan …  
→ 2 step(s): `knowledge_search`, `doc_search`  
⚙️ Executing retrieval …  
✨ Synthesizing answer …

🧭 Primary retrieval thin — escalating to the Master Planner …  
[master] Analyzing query & entities ...  
[master] Decomposing into sub-tasks ...  
[master] Executing 1 sub-task(s) over the tools ...  
[master] - sub-task 1 (kb_sql): SELECT DISTINCT celex_id FROM celex_facts.facts WHERE name = 'companies' AND val  
[master] -> 15 case(s), 0 chunk(s), 0 fact(s)  
[master] Joining & aggregating results ...  
[master] Verifying contributing facts ...

A good **Master Plan Agent** for RAG is less like “one smart agent” and more like a **query planner + retrieval orchestrator + join engine + verifier**.

# Master Planner Agent — Specification & Implementation Plan

## Context

The system answers questions about EU cartel decisions through a streaming `RetrievalRouterModule` → `QARetrievalPipeline` (Plan → Execute → Synthesize) over three retrieval targets: structured **facts**, distilled **knowledge items**, and raw **document chunks**. The existing `QAPlannerModule` emits a single flat `RetrievalPlan` of tool calls. This works for direct lookups and simple cross-case retrieval, but **fails on compositional queries** that need entity resolution, multi-step retrieval, and a join/aggregate before an answer can be formed. Examples that currently degrade to thin or empty answers:

- "List all cases where company **Bayer AG** was the cartel leader"
- "Which countries have a lot of banking cartels and which have a low number?"
- "Did the number of banking cartel cases increase after the leniency law?"

We will add a **Master Planner Agent**: a fallback DSPy module that activates **only when the existing QA pipeline returns insufficient evidence**. It analyses the query, identifies entities, decomposes the work into typed sub-tasks, runs the _existing_ six retrieval tools per sub-task, joins/aggregates the results, verifies the contributing rows with the _existing_ `EvidenceVerifierModule`, and synthesizes a grounded answer. If the query needs statistical/causal inference (e.g. the leniency-law trend question) or otherwise exceeds its capability envelope, it returns a clean **NA** with a short explanation and a suggested sub-agent extraction path — never a hallucination.

**Confirmed design decisions (from user):**

1. **Activation:** Fallback only — never on the hot path for simple queries.
2. **NA boundary:** Attempt retrieval + filtering + count/group-by aggregation. Return NA for trend / causal / statistical-inference queries.
3. **Decompose engine:** Static decompose-then-execute DAG (typed plan), not a ReAct loop — deterministic and mock-testable, consistent with the existing Plan → Execute → Synthesize shape.
4. **Verification:** Reuse `EvidenceVerifierModule` per contributing claim/row; drop unverified rows before aggregation.

---

# PART 1 — SPECIFICATION

## 1. Scope & capability envelope

**In scope (ATTEMPT):**

- _Entity-filtered enumeration_ across cases — e.g. "cases where Bayer AG was the cartel leader" (resolve entity → retrieve per-case → filter by role).
- _Count / group-by aggregation_ — e.g. "banking cartels per country" (retrieve candidate set → group → count → rank high/low).
- _Simple comparison_ between two retrieved groups.

**Out of scope (NA):**

- Trend / time-series / before-after causal inference — e.g. "did banking cartel cases increase after the leniency law" (requires temporal cohorting + statistical claim). Return NA + suggest the sub-agent data-extraction path.
- Significance tests, correlation, regression, forecasting.
- Any plan the planner itself marks `unsupported` (see §6).

## 2. Activation — fallback seam

The agent is a **fallback inside `RetrievalRouterModule.run_events`** (`src/pipelines/agent_dspy/retrieval_router.py`). Today the QA path delegates to `self.qa_pipeline.run_events(...)` and forwards every event. We change that loop to:

1. Forward QA `status` events as they stream (no UX regression).
2. Capture the QA terminal `result` prediction instead of yielding it immediately.
3. Judge sufficiency via a new `is_insufficient_answer(prediction)` helper (reuses the existing `_is_empty_evidence` markers in `src/pipelines/agent_dspy/retrieval_text.py` plus an empty/short-answer and "no … found" check).
4. **If sufficient** → yield the QA result unchanged (current behaviour).
5. **If insufficient AND the query looks compositional** (multi-entity / aggregation cue — see §6 classifier) → run the Master Planner, stream its `status` events, and yield _its_ terminal result (tagged `routing_decision="master_planner"` or `"master_planner_na"`).
6. **If insufficient but not compositional** → yield the QA result unchanged (the Master Planner adds no value to a simple miss).

This keeps the Master Planner entirely off the hot path: simple queries never construct or invoke it.

## 3. Module architecture

New module `MasterPlannerModule(dspy.Module)` in `src/pipelines/agent_dspy/master_planner_module.py`, following the project's Provisioner + Forward-Dispatch patterns. It is composed of small DSPy sub-modules and **reuses the existing executor and verifier** rather than re-implementing retrieval.

```
MasterPlannerModule.run_events(question, validation) -> Iterator[RetrievalEvent]
  stage A  AnalyzeModule        -> QueryAnalysis (entities, intent, target_type)
  stage B  DecomposeModule      -> MasterPlan (typed sub-tasks DAG)  | unsupported -> NA
  stage C  SubTaskExecutor      -> per sub-task evidence (reuses _ToolDispatcher)
  stage D  JoinAggregateModule  -> JoinedResult (deterministic join/group/count)
  stage E  VerifyGate           -> keep only EvidenceVerifier-supported rows
  stage F  MasterSynthesizer    -> grounded answer  | low-confidence -> NA
```

Each stage emits a `RetrievalEvent("status", …)` so the UI shows progress, and the module ends with exactly one terminal `RetrievalEvent("result", …)` whose `dspy.Prediction` matches the existing field contract (`answer`, `plan`, `evidence`, `celex_validation`, `references`, `fact_ids`, `chunk_ids`, `fact_names`, `routing_decision`). This makes it a drop-in for `stream_generator` in `oi_dspy.py` (which only reads `event.kind` and `event.prediction.answer`).

## 4. Pydantic models & DSPy signatures (Rules 1–3)

All in new file `src/pipelines/agent_dspy/master_planner_signature.py` (mirroring `dspy_signature.py`). Every field carries `desc=`/`description=`; extraction outputs reference Pydantic models.

**Stage A — Analysis**

```python
class ResolvedEntity(BaseModel):
    surface: str        # as written, e.g. "Bayer AG"
    entity_type: Literal["company", "country", "court", "commissioner",
                          "product", "sector", "other"]
    available_in: list[Literal["fact", "knowledge", "chunk"]]  # where found

class QueryAnalysis(BaseModel):
    entities: list[ResolvedEntity]
    target_type: Literal["case_list", "count_group", "compare", "unknown"]
    aggregation: Literal["none", "count", "group_count"] = "none"
    requires_statistics: bool   # True -> downstream NA
    rationale: str

class AnalyzeSignature(dspy.Signature):
    """Identify the entities a query references and classify what kind of
    composed answer it needs. Flag queries needing statistical/causal
    inference."""
    question: str = dspy.InputField(desc=...)
    kb_schema: str = dspy.InputField(desc=...)
    analysis: QueryAnalysis = dspy.OutputField(desc=...)
```

**Stage B — Decomposition** (extends, does not duplicate, `RetrievalStep`)

```python
class SubTask(BaseModel):
    task_id: int
    kind: Literal["entity_resolve", "retrieve", "filter", "join",
                  "aggregate"]
    tool: str = ""          # one of the 6 existing tools for 'retrieve'
    query: str = ""
    case_id: str = ""
    depends_on: list[int] = []
    filter_role: str = ""   # e.g. "cartel leader / ringleader / initiator"
    group_by: str = ""      # e.g. "country" for count_group
    rationale: str

class MasterPlan(BaseModel):
    supported: bool         # False -> NA before any execution
    na_reason: str = ""
    suggested_path: str = ""  # sub-agent extraction hint when unsupported
    target_type: Literal["case_list", "count_group", "compare"]
    subtasks: list[SubTask]

class DecomposeSignature(dspy.Signature):
    """Decompose a compositional query into an ordered, dependency-aware list
    of retrieval/join/aggregate sub-tasks reusing the available tools. Set
    supported=False with na_reason for statistical/causal/trend queries."""
    question: str = dspy.InputField()
    analysis: QueryAnalysis = dspy.InputField()
    kb_schema: str = dspy.InputField()
    plan: MasterPlan = dspy.OutputField()
```

**Stage F — Synthesis**

```python
class MasterAnswer(BaseModel):
    answer_md: str
    confident: bool         # False -> caller emits NA
    cited_celex_ids: list[str]

class MasterSynthesizerSignature(dspy.Signature):
    """Compose a grounded markdown answer from joined+verified rows. Cite every
    celex_id. If the verified evidence does not support a confident answer, set
    confident=False."""
    question: str = dspy.InputField()
    joined_evidence: str = dspy.InputField()
    target_type: str = dspy.InputField()
    result: MasterAnswer = dspy.OutputField()
```

## 5. Stage logic (logic lives in `forward`/`run_events`, Rule 2)

- **A. Analyze:** `dspy.ChainOfThought(AnalyzeSignature)`. To populate `available_in`, reuse cheap deterministic probes before the LLM call where possible: company surfaces via `keyword_search_chunks_all_cases` (one hit ⇒ chunk-available) and `knowledge_searcher.search_knowledge_any_terms`.
- **B. Decompose:** `dspy.ChainOfThought(DecomposeSignature)`. If `analysis.requires_statistics` is True or `plan.supported` is False → short- circuit to the NA terminal (§6).
- **C. SubTaskExecutor:** new lightweight executor that **reuses the existing `_ToolDispatcher`** (`src/pipelines/agent_dspy/retrieval_dispatch.py`) by mapping each `retrieve` sub-task to a `RetrievalStep` and calling `dispatch(step)`. `entity_resolve` sub-tasks use `keyword_search_chunks_all_cases` / `semantic_search_facts`. Results are kept **structured per sub-task** (lists of `{celex_id, fact_name, value, chunk_id, text}`) rather than flattened to a string, so the join stage can operate on rows. Honour `depends_on` ordering; cap fan-out with a new `MASTER_PLANNER_MAX_SUBTASKS` / `MASTER_PLANNER_MAX_CASES` budget (§7).
- **D. JoinAggregate:** **deterministic Python (polars)** — no LLM. `join` intersects/links sub-task row sets on `celex_id`; `filter` applies `filter_role` against retrieved per-case role facts (e.g. `companies`, `leniency_applicants`, cartel-leader/ringleader text in chunks); `aggregate` does `group_by(group_by).count()` and sorts for high/low framing. Use polars per CLAUDE.md ("ALWAYS use polars instead of pandas").
- **E. VerifyGate:** for each row that will appear in the answer, call the existing `EvidenceVerifierModule.forward(celex_id, fact_name)` (`src/pipelines/agent_dspy/evidence_verifier_module.py`). Keep rows whose verdict is `verified_supported` or `weak_support`; drop the rest. Thresholds come from `settings.EVIDENCE_SUPPORT_MIN_SCORE` / `EVIDENCE_WEAK_MIN_SCORE`. For chunk-only entity matches (no stored fact), fall back to a single grounding check rather than the full per-fact verifier.
- **F. Synthesize:** `dspy.ChainOfThought(MasterSynthesizerSignature)` over the verified rows. If `confident=False`, or zero rows survive verification, emit NA.

## 6. Complexity & NA gate

Two gates:

1. **Cheap pre-gate (router seam):** `looks_compositional(question)` — a pure heuristic (multiple capitalised entities, or "how many … per/by …", "which countries/companies …", "compare", role words like "leader/ ringleader"). Only compositional misses reach the Master Planner.
2. **Plan-time NA (authoritative):** `analysis.requires_statistics` or `plan.supported == False`. NA terminal returns: `"NA — this query needs <na_reason>. <suggested_path>"`, e.g. suggest running the offline sub-agent extraction over the cartel/leniency facts.

NA is a first-class terminal `result` event with `routing_decision= "master_planner_na"`, so the UI renders it as a normal (short) answer.

## 7. Settings (no magic numbers — add to `src/config/settings.py`)

```python
MASTER_PLANNER_ENABLED       = bool(os.getenv("MASTER_PLANNER_ENABLED", "true"))
MASTER_PLANNER_MAX_SUBTASKS  = int(os.getenv("MASTER_PLANNER_MAX_SUBTASKS", "8"))
MASTER_PLANNER_MAX_CASES     = int(os.getenv("MASTER_PLANNER_MAX_CASES", "60"))
MASTER_PLANNER_VERIFY_ROWS   = bool(os.getenv("MASTER_PLANNER_VERIFY_ROWS", "true"))
```

Reuse existing `LLM_MODEL_ID`, `EVIDENCE_SUPPORT_MIN_SCORE`, `EVIDENCE_WEAK_MIN_SCORE`, `SEMANTIC_SEARCH_HITS`.

## 8. Worked examples

- **"cases where Bayer AG was the cartel leader"** → Analyze: entity=Bayer AG (company, in chunk+fact). Decompose: `entity_resolve(Bayer AG)` → `retrieve(doc_search, "Bayer AG ringleader instigator cartel leader")` + `retrieve(kb_sql, companies/leniency facts)` → `filter_role="cartel leader"` per case → VerifyGate → list of `celex_id — title`. **ATTEMPT.**
- **"banking cartels per country, high vs low"** → Analyze: target=count_group, group_by=country. Decompose: `retrieve(doc_search/knowledge_search, "banking cartel")` cross-case → resolve each case's country fact (`case_facts` / `kb_sql`) → `aggregate(group_by=country, count)` → rank → VerifyGate on contributing country facts. **ATTEMPT.**
- **"did banking cartel cases increase after the leniency law"** → Analyze: `requires_statistics=True` (temporal causal trend). Decompose short-circuits → **NA** with suggestion to run the sub-agent temporal extraction over `decision_date` + `leniency_applicants`.

---

# PART 2 — IMPLEMENTATION PLAN

## New files

|File|Purpose|
|---|---|
|`src/pipelines/agent_dspy/master_planner_signature.py`|Pydantic models + DSPy signatures (§4).|
|`src/pipelines/agent_dspy/master_planner_module.py`|`MasterPlannerModule` + `SubTaskExecutor` + `JoinAggregate` + NA logic (§3,§5).|
|`src/pipelines/agent_dspy/master_planner_text.py`|`looks_compositional()`, `is_insufficient_answer()`, role-word tables (kept logic-free of DSPy).|
|`src/pipelines/agent_dspy/tests/test_master_planner_module.py`|unittest suite (mocks at boundaries).|
|`src/pipelines/agent_dspy/tests/test_master_planner_router_fallback.py`|router-seam fallback tests.|

## Modified files

- **`src/pipelines/agent_dspy/retrieval_router.py`** — inject optional `master_planner: MasterPlannerModule | None` into `RetrievalRouterModule.__init__`. In the QA branch of `run_events` (the `for event in self.qa_pipeline.run_events(...)` loop, lines ~520-527): forward `status` events, capture the terminal `result`, then apply the §2 sufficiency/compositional gate and optionally run the Master Planner instead. Default `None` keeps every existing test green.
- **`src/config/settings.py`** — add the four `MASTER_PLANNER_*` settings (§7).
- **`src/ui/backend_server/oi_dspy.py`** — build a `MasterPlannerModule` (reusing the already-provisioned `fact_repo`, `toolkit`, `aggregator`, `hybrid_search`, `knowledge_repo`, `lineage_repo`, `chunk_repo`, and an `EvidenceVerifierModule`) and pass it into `RetrievalRouterModule(...)` at lines ~343-349. Gate on `settings.MASTER_PLANNER_ENABLED`.

## Reused components (do not reimplement)

- `_ToolDispatcher.dispatch(step)` + `RetrievalStep` — all six retrieval tools (`retrieval_dispatch.py`, `dspy_signature.py`).
- `EvidenceVerifierModule.forward(celex_id, fact_name)` + `FactVerificationResult` (`evidence_verifier_module.py`).
- `_is_empty_evidence` / empty-evidence markers (`retrieval_text.py`).
- `RetrievalEvent` dataclass + the `dspy.Prediction` field contract (`retrieval_modules.py`).
- `attach_references` on `QARetrievalPipeline` for provenance on single-case rows; `FactProvenanceRow` for cross-case chunk rows.
- Entity probes: `keyword_search_chunks_all_cases`, `keyword_count_chunks_all_cases`, `semantic_search_facts` (`CelexDatabaseToolkit`), `search_knowledge_any_terms` (`KnowledgeRepository`).
- `FactRepository.select_facts4celex_with_ids` for per-case role/country facts.

## DSPy / config conventions

- LM is already configured globally via `dspy.configure(lm=...)` in `oi_dspy.py`; new `ChainOfThought` modules pick it up automatically.
- All sub-modules take dependencies via `__init__` (Provisioner); no client is constructed inside `forward`.

## Testing (unittest, per CLAUDE.md)

Place tests in `src/pipelines/agent_dspy/tests/` (each dir already has `__init__.py`). Inject mocks only at boundaries (DB repos, dispatcher, verifier, LLM sub-modules), following `test_retrieval_modules.py` / `_FakeCelexChecker` and `test_evidence_verifier_module.py` patterns.

Cases to cover:

1. `looks_compositional` / `is_insufficient_answer` truth tables (pure funcs).
2. Router seam: sufficient QA result → Master Planner **not** invoked; empty QA result + compositional → invoked; empty + non-compositional → not invoked.
3. Decompose NA short-circuit when `requires_statistics=True` (leniency-law example) → terminal `routing_decision="master_planner_na"`.
4. SubTaskExecutor maps `retrieve` sub-tasks to `RetrievalStep` and calls a mocked dispatcher with the right tool/query/case_id.
5. JoinAggregate (polars) — group_by count + high/low ordering on fixed rows (no LLM, fully deterministic).
6. VerifyGate drops rows whose mocked verdict is `insufficient_evidence`.
7. Bayer-AG case-list happy path end-to-end with all boundaries mocked → asserts the celex_ids that survive verification appear in the answer.

Run: `.venv/bin/python -m unittest discover -s src/pipelines/agent_dspy/tests -p "test_master_planner*.py" -v`

## End-to-end verification

1. `ruff check src/pipelines/agent_dspy/master_planner_module.py src/pipelines/agent_dspy/master_planner_signature.py src/pipelines/agent_dspy/master_planner_text.py` — clean.
2. Full unit run: `.venv/bin/python -m unittest` — no regressions.
3. Live smoke (requires DB + Ollama + backend per `oi_dspy.py`): start the backend, POST to `/v1/chat/completions` with each of the three example queries; assert (a) Bayer query returns a celex_id list, (b) banking-per- country returns a grouped count, (c) leniency-law-trend returns an `NA — …` message. The existing Playwright harness (`src/ui/backend_server/tests/test_webui_celex_context.py`) is the template for a UI-level check if desired.

## Suggested phasing

1. Models + signatures + pure text helpers (+ their unit tests).
2. `MasterPlannerModule` stages C–F reusing dispatcher/verifier (+ tests).
3. Router-seam fallback wiring (+ fallback tests).
4. `settings.py` + `oi_dspy.py` provisioning; live smoke of the 3 queries.


## What was built

**New files**

- [master_planner_signature.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/master_planner_signature.py) — declarative DSPy signatures (`AnalyzeSignature`, `DecomposeSignature`, `MasterSynthesizerSignature`) + Pydantic models. No frozen fact-name enums: fields naming facts are plain `str` validated against the live catalog; `Literal`s only for stable semantic categories.
- [master_planner_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/master_planner_module.py) — `MasterPlannerModule` with stages A–F: Analyze → Decompose (NA short-circuit for statistics/causal) → execute sub-tasks via the existing `_ToolDispatcher` → deterministic **polars** join/filter/group-count → `EvidenceVerifierModule` row gate → synthesize. Catalog grounding drops unknown fact names from `kb_sql` before execution.
- [master_planner_text.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/master_planner_text.py) — pure helpers: `is_insufficient_answer`, `looks_compositional`, the catalog-grounded `ROLE_FACT_MAP` (real fact names), catalog gating.
- 3 unittest files — 30 tests, all passing.

**Modified**

- [fact_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/fact_repository.py) — `select_distinct_fact_names()` (excludes `test_fact_%`).
- [retrieval_router.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/retrieval_router.py) — fallback seam: holds the QA terminal result, escalates only when `is_insufficient_answer` **and** `looks_compositional`; default `None` keeps existing behavior.
- [settings.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/config/settings.py) — four `MASTER_PLANNER_*` settings.
- [oi_dspy.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/ui/backend_server/oi_dspy.py) — provisions an `EvidenceVerifierModule` + `MasterPlannerModule` (reusing existing repos), gated on `MASTER_PLANNER_ENABLED`.

## Verification status

- `ruff check` — clean on all changed files.
- New tests: **30/30 pass** (NA short-circuit, sub-task→step mapping, catalog drop of unknown `kb_sql` names, polars group-count, verify gate, Bayer happy path, all four router-seam gate cases).
- Full `agent_dspy` suite: **394/395 pass**. The one failure (`test_e2e_aggregation_brewery_cartel_period`) is a **live-Ollama+Postgres** test calling `QARetrievalPipeline` directly — a path I did not modify (`retrieval_modules.py` shows zero diff) — and it fails because the real LLM returned "No relevant evidence found" rather than a CELEX list. Not a regression from this work.

## Remaining manual step

The plan's live smoke test (POST the 3 example queries to `/v1/chat/completions`) needs the DB + Ollama + backend running, which I can't exercise deterministically here. The unit-level equivalents are covered by the Bayer happy-path and NA tests.

One thing worth flagging: the verify stage calls `EvidenceVerifierModule.forward`, which **persists** a `fact_evidence` ledger row as a side effect. That matches normal verification semantics, but if you'd prefer the planner's gate to be read-only, that's a small follow-up — let me know.


## The hang, reproduced with real data (no mocks)

I wired the real `FactRepository`, `ChunkRepository`, and `EvidenceVerifierModule` against the live `celex_facts` corpus and the real Ollama LLM, and ran the exact query that stalled. Measured numbers for _"List all cases where Bayer AG was part of a cartel"_:

| | Before (current `case_list`) | After (scoped) |
|---|---|---|
| Candidate cases | 29 | 29 |
| Rows sent to verifier | **1,543** | **114** |
| Rows hitting the **LLM judge** | **296** | **0** |
| Verify-stage time | **~22 min** (296 × 4.5 s LLM + ~34 s routing) | **28 s** (all deterministic) |

**Root cause:** for a `case_list` answer, [_join_aggregate](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/master_planner_module.py) handed the verify stage _every fact of every candidate case_ (~53 facts × 29 cases). ~300 of those (cartel/leniency/infringement/boolean facts) route to the per-candidate LLM judge in [evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py), each ~4.5 s and sequential — that's the 10-minute "Verifying contributing facts" stall. `count_group` already scoped its rows to the group fact-names before verifying; `case_list`/`compare` never did.

## Fix

1. **Scope `case_list`/`compare` verification to the query-relevant fact names** (mirrors what `count_group` already does). New `_scope_to_relevant_facts` / `_relevant_fact_names` derive the entity/role facts from the planner's `candidate_fact_names`, the entity surface/type, and the filter role — all entity-strategy facts (no LLM). Falls back to the full frame on a catalog miss so a case is never dropped.
2. **Runaway cap** `MASTER_PLANNER_MAX_VERIFY_ROWS` (default 120) in `_verify_rows`: rows past the cap pass through _unverified_ (logged) rather than dropped, so no query can ever issue hundreds of LLM-judge calls even if scoping resolves nothing.

## Tests

- [test_master_planner_verify_perf.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/tests/test_master_planner_verify_perf.py) — the real, no-mock benchmark. Opt-in via `MASTER_PLANNER_PERF=1` (needs corpus + LLM; auto-skips otherwise, like the other e2e tests). Asserts scoping collapses the fan-out >4× to entity-only facts under the cap, and that the scoped verify finishes well under the stall. **Passes against live data.**
- A always-on regression test (`TestCaseListVerifyScoping`) proving only membership facts reach the verifier.
- Full master-planner suite: 38 tests green (2 perf skipped); router tests green; ruff clean.

## Further opportunities (not done — flagging)

- **Parallelize `_verify_rows`** — rows are independent; a small thread pool would cut even the deterministic 28 s, but needs care around DB-pool/DSPy thread-safety.
- **Dedupe routing DB calls** — `_route_strategy` does a `select_fact4celex` + `get_source_type_counts` per row (~34 s for 1,543); `_build_case_table` already loaded those facts and could pass them through.
- **`_build_case_table`** could load only the relevant fact names instead of all ~60 per case.

# Want me to take any of those on, or parallelize the verify loop?

...



Added `TestAnheuserBuschMembershipHedgedSynthesizer` to [test_master_planner_integration.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/tests/test_master_planner_integration.py), faithfully reproducing the real scenario: two `kb_sql` legs joined on `celex_id`, a synthesizer that returns `confident=False`, and assertions that the result is `master_planner` (not NA) and lists both `40134_2872_5` and `CELEX-32000D0146` while excluding the companies-only case dropped by the join intersection. - Real pipeline: now returns the two cases.- 39 master-planner tests pass (2 perf benchmarks skipped), 31 router tests pass, ruff clean. One note worth surfacing: this fix makes the agent robust to a hedging synthesizer, but the synthesizer _itself_ is still returning `confident=False` on clean evidence (markdown-table company values may be throwing it off). That's a separate prompt/quality issue — the deterministic fallback now masks it rather than curing it. Want me to look at the `MasterSynthesizerSignature` prompt separately?

## The scenario being fixed

The Master Planner is a **fallback agent**. When the primary QA pipeline can't answer a _compositional_ query (one that filters or counts across multiple entities), this agent kicks in. Its pipeline is: Analyze → Decompose → Execute → **Join** → **Verify** → **Synthesize** (see [master_planner_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/master_planner_module.py)).

The specific query was something like _"which cases involve Anheuser-Busch as a member?"_ — answered by:

- Two `kb_sql` retrieval legs (e.g. one pulling cases, one pulling companies),
- A **join on `celex_id`** that intersects them (rows that don't appear in both legs get dropped — that's the "companies-only case dropped by the join intersection"),
- A **synthesizer** that writes the final markdown answer and sets a `confident` flag.

## The bug

The synthesizer ([MasterSynthesizerSignature](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/master_planner_signature.py#L273), the line you have selected) is an LLM step. On this query it returned `confident=False` _even though the deterministic join + verify stages had already produced solid, grounded rows_. The likely cause: the company values live in a markdown table in the evidence, and the LLM second-guessed itself reading them.

Originally, `confident=False` meant the agent threw away the good rows and emitted **NA** (no answer). So a clean, verifiable result was being discarded because the LLM got cold feet.

## The fix

In [master_planner_module.py:301-321](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/master_planner_module.py#L301-L321):

```python
if synth.confident and answer_md:
    answer = synth.answer_md
else:
    # don't let a hedging synthesizer discard a grounded answer
    answer = self._fallback_answer(plan, rows_df, group_df)
```

When the synthesizer hedges, instead of returning NA, the agent now **builds the answer deterministically** straight from the already-verified rows (`_fallback_answer`). The reasoning is sound: the join and verify stages had _already_ confirmed these rows are real and supported — an empty result would have returned NA earlier — so a nervous LLM shouldn't get to veto them.

The new test `TestAnheuserBuschMembershipHedgedSynthesizer` reproduces exactly this: two `kb_sql` legs, a synthesizer stubbed to return `confident=False`, and it asserts the routing decision is `master_planner` (a real answer) **not** NA, and that both expected celex ids appear while the join-excluded one does not.

## The caveat I flagged

This fix makes the **agent robust to** a hedging synthesizer — but it doesn't fix **why** the synthesizer hedges. The LLM is still returning `confident=False` on clean evidence. The deterministic fallback now **masks** that symptom rather than curing it. The root cause is a prompt/quality issue in the `MasterSynthesizerSignature` itself (probably the markdown-table values confusing it).

So there are two separate concerns:

1. **Robustness** (done) — agent no longer drops good answers.
2. **Synthesizer quality** (open) — why is `confident` wrong, and should the prompt be improved so it's reliable on its own?

My closing question was offering to investigate #2 — tightening the synthesizer's prompt/signature so it stops hedging on valid evidence, rather than relying on the deterministic safety net to cover for it.

Want me to dig into that synthesizer prompt issue?

----------------------------------------------------------------------
---------------------------------------------------------

====

For your examples, the hard part is not retrieval itself. The hard part is turning a messy question into a **retrieval plan**, enforcing **typed outputs** from sub-agents, joining evidence across sources, and deciding when the result is strong enough.

---

**1. Core pattern: Master Plan Agent as query planner**

Think of the Master Plan Agent as producing a plan like this:

User question

  ↓

Intent + decomposition

  ↓

Sub-agent selection

  ↓

Parallel / sequential retrieval

  ↓

Normalize outputs into typed tables

  ↓

Join / aggregate / compare

  ↓

Validate evidence

  ↓

Synthesize answer with citations and uncertainty

The Master Plan Agent should not directly “search everything.” It should decide:

1. **What entities are needed?**  
    Companies, countries, laws, court cases, sectors, dates.
2. **What facts are needed?**  
    Company location, sector classification, legal event date, case filing date, case outcome.
3. **Which retrieval agents are best suited?**  
    Web search agent, internal KB agent, legal corpus agent, company registry agent, statistics agent, vector search agent.
4. **What intermediate data structure is required?**  
    Usually a table, graph, or timeline.
5. **What operation is needed after retrieval?**  
    Deduplicate, join, count, group by, compare before/after, detect trend, test hypothesis.

---

**2. Use typed retrieval contracts**

Each sub-agent should return a predictable schema, not free-form text.

For example:

{

  "agent": "company_retriever",

  "task": "find_brewery_companies_in_eu",

  "output_schema": {

    "company_name": "string",

    "country": "string | null",

    "country_confidence": "float",

    "source_url": "string",

    "evidence_quote": "string",

    "confidence": "float"

  }

}

For legal cases:

{

  "agent": "legal_case_retriever",

  "task": "find_eu_banking_cartel_cases",

  "output_schema": {

    "case_name": "string",

    "sector": "string",

    "jurisdiction": "EU | member_state",

    "case_date": "date",

    "decision_date": "date | null",

    "case_type": "cartel | antitrust | other",

    "source": "string",

    "confidence": "float"

  }

}

This is essential because the Master Plan Agent can only reliably join results if every sub-agent emits **structured, typed records**.

---

**3. Architecture pattern**

A practical design:

Master Plan Agent

├── Query Analyzer

├── Task Decomposer

├── Retrieval Router

├── Sub-Agent Orchestrator

├── Entity Resolver

├── Join/Aggregation Engine

├── Evidence Verifier

└── Answer Synthesizer

**A. Query Analyzer**

Classifies the question:

{

  "question_type": "aggregation",

  "entities": ["brewery companies", "EU", "countries"],

  "operations": ["retrieve", "deduplicate", "group_by_country", "count"],

  "output": "table + explanation"

}

or:

{

  "question_type": "temporal_causal_comparison",

  "entities": ["cartel cases", "banking", "EU leniency law"],

  "operations": ["retrieve_policy_date", "retrieve_cases", "classify_sector", "bucket_before_after", "compare_trend"],

  "output": "trend assessment with caveats"

}

**B. Task Decomposer**

Transforms the user question into a DAG, not a flat list.

Example 1:

Find brewery companies in EU

  ├── Retrieve candidate brewery companies

  ├── Resolve company country

  ├── Deduplicate companies

  ├── Validate EU membership of country

  └── Count companies per country

Example 2:

Did banking cartel cases increase after EU leniency law?

  ├── Retrieve introduction date of relevant leniency law/policy

  ├── Retrieve cartel cases involving banking/financial services

  ├── Extract dates and jurisdictions

  ├── Filter to EU-relevant cases

  ├── Bucket cases before/after policy introduction

  ├── Normalize by time period length

  └── Compare counts and explain limitations

**C. Retrieval Router**

Maps tasks to sub-agents:

{

  "retrieve_companies": ["web_search_agent", "company_registry_agent", "industry_directory_agent"],

  "resolve_country": ["company_profile_agent", "knowledge_base_agent"],

  "retrieve_cartel_cases": ["legal_corpus_agent", "competition_authority_agent"],

  "retrieve_policy_date": ["legal_policy_agent"],

  "time_series_analysis": ["analytics_agent"]

}

**D. Entity Resolver**

This is critical.

Different agents may return:

AB InBev

Anheuser-Busch InBev

Anheuser Busch InBev SA/NV

AB InBev NV

The Master Plan Agent needs an entity resolution layer:

{

  "canonical_entity": "Anheuser-Busch InBev",

  "aliases": [

    "AB InBev",

    "Anheuser Busch InBev SA/NV"

  ],

  "country": "Belgium",

  "confidence": 0.93

}

For legal cases, similarly:

"EURIBOR cartel"

"Euro interest rate derivatives cartel"

"Interest rate derivatives case"

may need to resolve to the same case family.

---

**4. Pattern 1: Retrieval-to-table-to-aggregation**

Your first example is a classic **retrieve → normalize → aggregate** workflow.

**User question**

Retrieve brewery companies in EU, retrieve companies country and provide number per country.

**Master Plan**

Goal: Count EU brewery companies by country.

Step 1: Find candidate brewery companies in Europe.

Step 2: For each company, retrieve country of headquarters or registration.

Step 3: Filter to EU countries.

Step 4: Deduplicate companies.

Step 5: Group by country.

Step 6: Return table with source-backed evidence.

**Internal working table**

company_id | company_name | country | evidence_source | confidence

-----------|--------------|---------|-----------------|-----------

c001       | Company A    | Germany | source 1        | 0.91

c002       | Company B    | Belgium | source 2        | 0.87

c003       | Company C    | Germany | source 3        | 0.82

**Aggregation**

country  | count

---------|------

Germany  | 2

Belgium  | 1

**Recommended implementation style**

Have the Master Agent call sub-agents with typed tasks:

[

  {

    "task_id": "T1",

    "agent": "company_retrieval_agent",

    "input": {

      "industry": "brewery",

      "region": "EU"

    },

    "expected_output": "company_records[]"

  },

  {

    "task_id": "T2",

    "agent": "company_country_agent",

    "input": {

      "companies_from": "T1"

    },

    "expected_output": "company_country_records[]"

  },

  {

    "task_id": "T3",

    "agent": "aggregation_agent",

    "input": {

      "join": ["T1.company_name", "T2.company_name"],

      "group_by": "country",

      "metric": "count_distinct_company"

    }

  }

]

The key is that the aggregation step should operate on structured records, not text snippets.

---

**5. Pattern 2: Retrieval-to-timeline-to-comparison**

Your second example is more complex:

Did the number of cartel cases against banking in EU increase after introduction of leniency law?

This is not just retrieval. It involves:

1. Legal definition lookup.
2. Date extraction.
3. Case retrieval.
4. Sector classification.
5. Before/after comparison.
6. Possibly causal caveats.

**Master Plan**

Goal: Compare the number of EU banking cartel cases before and after the relevant leniency law or policy.

Step 1: Identify the relevant EU leniency regime and its introduction date.

Step 2: Retrieve EU cartel cases involving banks or financial markets.

Step 3: Extract case dates consistently.

Step 4: Classify whether each case belongs to banking/financial services.

Step 5: Deduplicate related cases.

Step 6: Bucket cases into pre-law and post-law periods.

Step 7: Normalize by years in each period.

Step 8: Compare counts and provide cautious interpretation.

**Example intermediate table**

case_id | case_name | sector  | case_date  | period     | source | confidence

--------|-----------|---------|------------|------------|--------|-----------

k001    | Case A    | banking | 1998-05-01 | pre-law    | ...    | 0.88

k002    | Case B    | banking | 2003-09-12 | post-law   | ...    | 0.91

k003    | Case C    | banking | 2007-11-20 | post-law   | ...    | 0.84

**Important design choice: define the date field**

For legal cases, “case date” is ambiguous. It could mean:

- date of infringement

- date of investigation opening

- date of statement of objections

- date of Commission decision

- date of court judgment

The Master Plan Agent should explicitly choose or ask the retrieval agent to retrieve all relevant dates.

For this question, a good default is usually:

Use decision date for counting formal cartel cases, unless the user asks about investigations.

But the system should record this assumption.

**Example comparison logic**

pre_period_count = count cases before leniency_date

post_period_count = count cases after leniency_date

pre_rate = pre_period_count / number_of_pre_years

post_rate = post_period_count / number_of_post_years

increase = post_rate > pre_rate

Do not only compare raw counts if the periods have unequal lengths.

Better:

cases per year before vs. cases per year after

Even better:

cases per year, with sensitivity analysis around policy introduction date

---

**6. Use a query graph, not just prompt chaining**

For multi-retriever RAG, represent the plan as a graph.

Example:

{

  "nodes": [

    {

      "id": "N1",

      "type": "retrieve",

      "agent": "legal_policy_agent",

      "output": "leniency_policy"

    },

    {

      "id": "N2",

      "type": "retrieve",

      "agent": "legal_case_agent",

      "output": "cartel_cases"

    },

    {

      "id": "N3",

      "type": "classify",

      "input": "N2",

      "output": "banking_cartel_cases"

    },

    {

      "id": "N4",

      "type": "temporal_bucket",

      "input": ["N1", "N3"],

      "output": "cases_before_after"

    },

    {

      "id": "N5",

      "type": "aggregate",

      "input": "N4",

      "output": "trend_result"

    }

  ]

}

This lets you inspect, retry, cache, and debug each stage.

---

**7. Recommended sub-agent types**

For your setup, I would use these sub-agent roles:

|**Sub-agent**|**Responsibility**|
|---|---|
|**Planner Agent**|Decomposes the user question into a DAG|
|**Router Agent**|Chooses retrieval sources and sub-agents|
|**Entity Search Agent**|Finds candidate entities|
|**Fact Retrieval Agent**|Retrieves specific attributes for known entities|
|**Legal/Regulatory Agent**|Handles statutes, cases, enforcement actions|
|**Temporal Agent**|Extracts dates, periods, timelines|
|**Entity Resolution Agent**|Deduplicates names and aliases|
|**Schema Normalizer**|Converts text snippets into structured rows|
|**Join Agent**|Joins records across agents|
|**Aggregation Agent**|Counts, groups, compares|
|**Verifier Agent**|Checks sources, conflicts, missing evidence|
|**Answer Agent**|Produces final response with caveats|

In practice, not every one needs to be an LLM agent. Some should be deterministic services.

For example:

LLM agent:

- query decomposition

- source selection

- entity classification

- evidence synthesis

Deterministic code:

- joins

- counts

- deduplication rules

- date bucketing

- group-by aggregation

This makes the system more reliable.

---

**8. Join patterns**

**A. Entity join**

Used when different agents return facts about the same entity.

company_retriever output:

company_name

country_retriever output:

company_name, country

join on:

canonical_company_id

Important: do not join only on raw strings. Use canonical IDs where possible.

---

**B. Attribute enrichment join**

Used when one agent finds entities and another adds properties.

companies

  ↓ enrich with country

  ↓ enrich with industry

  ↓ enrich with EU membership

  ↓ aggregate

---

**C. Temporal join**

Used for before/after questions.

policy_event.date

  joined with

case_event.date

Then:

case_event.date < policy_event.date  → before

case_event.date >= policy_event.date → after

---

**D. Evidence join**

Every final row should retain evidence.

entity | fact | source | quote | confidence

Without this, your final answer becomes hard to audit.

---

**9. Confidence and conflict handling**

The Master Plan Agent should not simply accept every sub-agent result.

Use a confidence model:

{

  "fact": "Company X is headquartered in Germany",

  "support": [

    {

      "source": "company registry",

      "confidence": 0.95

    },

    {

      "source": "company website",

      "confidence": 0.88

    }

  ],

  "conflicts": [

    {

      "source": "old industry directory",

      "value": "Austria",

      "confidence": 0.41

    }

  ],

  "final_value": "Germany",

  "final_confidence": 0.91

}

For legal/statistical questions, the final answer should distinguish:

- strong evidence

- weak evidence

- missing data

- ambiguous definitions

- correlation vs causation

For the cartel example, the agent should avoid overclaiming causality. It may answer:

The number of recorded banking cartel cases increased after the leniency regime under this counting method, but this does not prove the leniency law caused the increase. Other factors may include enforcement priorities, market structure, reporting incentives, and data availability.

---

**10. A useful Master Plan Agent prompt template**

You can structure the master prompt like this:

You are the Master Plan Agent for a multi-retriever RAG system.

Your job is to:

1. Understand the user's analytical intent.

2. Decompose the question into atomic retrieval and reasoning tasks.

3. Select the appropriate sub-agents.

4. Define typed output schemas for each sub-agent.

5. Specify joins, filters, aggregations, and validation checks.

6. Identify assumptions and ambiguities.

7. Produce an execution plan as JSON.

8. Do not answer from memory unless the required facts are already retrieved.

For each task, return:

- task_id

- task_type

- assigned_agent

- input

- output_schema

- dependencies

- validation_rules

- retry_strategy

After retrieval, produce:

- normalized records

- joins performed

- aggregation logic

- evidence summary

- final answer

- uncertainty notes

---

**11. Example Master Plan output for brewery question**

{

  "question_type": "entity_aggregation",

  "goal": "Count EU brewery companies by country",

  "tasks": [

    {

      "task_id": "T1",

      "task_type": "retrieve_entities",

      "assigned_agent": "company_search_agent",

      "input": {

        "entity_type": "company",

        "industry": "brewery",

        "region": "European Union"

      },

      "output_schema": {

        "company_name": "string",

        "source": "string",

        "evidence": "string",

        "confidence": "float"

      }

    },

    {

      "task_id": "T2",

      "task_type": "enrich_entities",

      "assigned_agent": "company_profile_agent",

      "dependencies": ["T1"],

      "input": {

        "attributes": ["headquarters_country", "registration_country"]

      },

      "output_schema": {

        "company_name": "string",

        "canonical_company_id": "string",

        "country": "string",

        "country_type": "headquarters | registration",

        "source": "string",

        "confidence": "float"

      }

    },

    {

      "task_id": "T3",

      "task_type": "filter",

      "assigned_agent": "deterministic_filter",

      "dependencies": ["T2"],

      "input": {

        "filter": "country is EU member"

      }

    },

    {

      "task_id": "T4",

      "task_type": "aggregate",

      "assigned_agent": "aggregation_engine",

      "dependencies": ["T3"],

      "input": {

        "group_by": "country",

        "metric": "count_distinct(canonical_company_id)"

      }

    }

  ],

  "final_output": {

    "format": "table",

    "columns": ["country", "number_of_brewery_companies", "evidence_coverage"]

  }

}

---

**12. Example Master Plan output for cartel/leniency question**

{

  "question_type": "temporal_comparison",

  "goal": "Assess whether EU banking cartel cases increased after introduction of leniency law",

  "tasks": [

    {

      "task_id": "T1",

      "task_type": "retrieve_policy_event",

      "assigned_agent": "legal_policy_agent",

      "input": {

        "policy": "EU leniency law or leniency notice",

        "jurisdiction": "European Union"

      },

      "output_schema": {

        "policy_name": "string",

        "introduction_date": "date",

        "effective_date": "date",

        "source": "string",

        "confidence": "float"

      }

    },

    {

      "task_id": "T2",

      "task_type": "retrieve_cases",

      "assigned_agent": "competition_law_case_agent",

      "input": {

        "case_type": "cartel",

        "sector": ["banking", "financial services", "financial markets"],

        "jurisdiction": "European Union"

      },

      "output_schema": {

        "case_name": "string",

        "case_id": "string",

        "sector": "string",

        "decision_date": "date",

        "investigation_start_date": "date | null",

        "jurisdiction": "string",

        "source": "string",

        "confidence": "float"

      }

    },

    {

      "task_id": "T3",

      "task_type": "classify_sector",

      "assigned_agent": "sector_classifier_agent",

      "dependencies": ["T2"],

      "input": {

        "target_sector": "banking or financial services"

      }

    },

    {

      "task_id": "T4",

      "task_type": "deduplicate",

      "assigned_agent": "entity_resolution_agent",

      "dependencies": ["T2", "T3"],

      "input": {

        "dedupe_key": ["case_name", "case_id", "decision_date"]

      }

    },

    {

      "task_id": "T5",

      "task_type": "temporal_bucket",

      "assigned_agent": "temporal_analysis_agent",

      "dependencies": ["T1", "T4"],

      "input": {

        "date_field": "decision_date",

        "split_date": "T1.effective_date"

      }

    },

    {

      "task_id": "T6",

      "task_type": "aggregate_compare",

      "assigned_agent": "statistics_agent",

      "dependencies": ["T5"],

      "input": {

        "metrics": [

          "count_before",

          "count_after",

          "cases_per_year_before",

          "cases_per_year_after"

        ]

      }

    }

  ],

  "final_output": {

    "format": "analytical_answer",

    "include": [

      "method",

      "before_after_counts",

      "rate_comparison",

      "evidence",

      "limitations",

      "causality_warning"

    ]

  }

}

---

**13. Practical implementation pattern**

A robust flow in code could look like this:

def answer_question(user_question):

    plan = master_planner.create_plan(user_question)

    task_results = {}

    for task in topological_sort(plan.tasks):

        inputs = resolve_dependencies(task, task_results)

        if task.task_type in ["retrieve_entities", "retrieve_cases", "retrieve_policy_event"]:

            result = retrieval_router.run(task.assigned_agent, inputs)

        elif task.task_type == "deduplicate":

            result = entity_resolver.run(inputs)

        elif task.task_type == "aggregate":

            result = aggregation_engine.run(inputs)

        elif task.task_type == "temporal_bucket":

            result = temporal_engine.run(inputs)

        else:

            result = generic_agent.run(task, inputs)

        validated = verifier.validate(task, result)

        task_results[task.task_id] = validated

    final_answer = answer_synthesizer.compose(

        question=user_question,

        plan=plan,

        results=task_results

    )

    return final_answer

The important part: the Master Plan Agent plans; deterministic components execute joins and calculations.

---

**14. Anti-patterns to avoid**

Avoid these:

|**Anti-pattern**|**Why it fails**|
|---|---|
|One agent searches everything|Poor controllability and weak auditability|
|Free-form sub-agent answers|Hard to join and aggregate|
|Joining on raw strings only|Entity duplication and false joins|
|No date semantics|Bad before/after analysis|
|No confidence model|Contradictory sources pollute final answer|
|No evidence propagation|Final answer cannot be audited|
|Treating correlation as causation|Especially dangerous in legal/economic questions|
|Asking every sub-agent the full user question|Leads to redundant, inconsistent outputs|

The sub-agents should receive **narrow tasks**, not the original broad question.

---

**15. Best overall pattern**

For your use case, I would use this design:

Master Plan Agent

  produces a typed task DAG

Sub-agents

  retrieve narrow, source-backed facts

Normalizer

  converts facts into canonical tables

Entity Resolver

  deduplicates entities and cases

Join Engine

  joins records by canonical IDs and dates

Aggregation Engine

  performs counts, rates, trends

Verifier

  checks source coverage and contradictions

Answer Synthesizer

  explains result, evidence, and uncertainty

The mental model is:

LLM for planning and interpretation.

Retrievers for evidence.

Code for joins and aggregation.

LLM again for explanation.

That split will give you much better reliability than trying to let one agent retrieve, reason, join, count, and explain all at once.

You can implement the **Master Plan Agent** with **DSPy as the runtime/planning/evaluation layer** and **Beads as the engineering memory / dependency graph**, not as the online query executor.

I found Beads as bd, a CLI-backed “persistent, structured memory” and dependency-aware graph for coding agents, with commands like bd ready, bd create, bd dep add, bd show, and bd remember. It is useful for tracking implementation tasks, experiments, failures, and reusable design decisions, but I would not put it directly in the request path of your RAG service. ([GitHub](https://github.com/gastownhall/beads "GitHub - gastownhall/beads: Beads - A memory upgrade for your coding agent · GitHub"))

DSPy is a good fit for the actual agent logic because it gives you **Signatures**, **Modules**, **ReAct tool use**, and **optimizers**. DSPy’s current docs describe Signatures as typed inputs/outputs, Modules as reusable execution strategies, ReAct as a tool loop, and optimizers such as GEPA/MIPRO-style compilation against metrics. ([dspy.ai](https://dspy.ai/ "DSPy"))

---

**Recommended split**

Use:

DSPy

  Runtime Master Plan Agent

  Sub-agent routing

  Structured extraction

  Answer synthesis

  Evaluation / optimization

Python code

  Joins

  Deduplication

  Counts

  Temporal bucketing

  Confidence scoring

Beads

  Backlog of retrieval capabilities

  Dependency graph of sub-agents

  Persistent design decisions

  Evaluation findings

  Known failure modes

So the runtime architecture is:

User question

  ↓

DSPy Planner

  ↓

Typed task plan

  ↓

Retrieval sub-agents / tools

  ↓

Normalizer

  ↓

Deterministic join + aggregation

  ↓

Verifier

  ↓

DSPy Answer Synthesizer

Beads tracks the implementation of this system:

bd issue: implement cartel case retriever

bd issue: add company-country resolver

bd issue: add entity deduplication

bd issue: add before/after trend metric

bd memory: EU leniency questions require normalized case-rate comparison

---

**1. Define your data contracts first**

Do this outside DSPy. This is the most important part.

from pydantic import BaseModel, Field

from typing import Literal, Optional, Any

class Evidence(BaseModel):

    source_id: str

    source_title: str

    url: Optional[str] = None

    quote: Optional[str] = None

    confidence: float = Field(ge=0.0, le=1.0)

class CompanyRecord(BaseModel):

    company_id: str

    company_name: str

    canonical_name: str

    industry: str

    country: Optional[str]

    country_type: Literal["headquarters", "registration", "unknown"]

    evidence: list[Evidence]

    confidence: float = Field(ge=0.0, le=1.0)

class LegalCaseRecord(BaseModel):

    case_id: str

    case_name: str

    sector: str

    jurisdiction: str

    case_type: Literal["cartel", "antitrust", "other"]

    decision_date: Optional[str] = None

    investigation_start_date: Optional[str] = None

    evidence: list[Evidence]

    confidence: float = Field(ge=0.0, le=1.0)

class PolicyEvent(BaseModel):

    policy_name: str

    jurisdiction: str

    introduction_date: Optional[str]

    effective_date: Optional[str]

    evidence: list[Evidence]

    confidence: float = Field(ge=0.0, le=1.0)

Your sub-agents should return these records, not prose.

---

**2. Implement retrieval sub-agents as plain Python tools**

DSPy tools are plain Python callables that can be passed into dspy.ReAct; DSPy introspects callable signatures, type hints, and docstrings to build tool schemas. ([dspy.ai](https://dspy.ai/diving-deeper/tools-react-and-mcp/ "Tools, ReAct, and MCP - DSPy"))

Example stubs:

def retrieve_brewery_companies(region: str, limit: int = 100) -> list[dict]:

    """Retrieve candidate brewery companies in a region."""

    # Call vector DB, SQL, web index, company registry, etc.

    return []

def resolve_company_country(company_name: str) -> dict:

    """Resolve a company's headquarters or registration country."""

    return {}

def retrieve_eu_leniency_policy() -> dict:

    """Retrieve the EU leniency policy introduction and effective dates."""

    return {}

def retrieve_cartel_cases(

    jurisdiction: str,

    sector_hint: str,

    start_year: int = 1980,

    end_year: int = 2026,

) -> list[dict]:

    """Retrieve competition/cartel cases for a jurisdiction and sector."""

    return []

Keep these functions narrow. The Master Agent should not ask a company retriever to answer the whole question.

---

**3. Define DSPy signatures**

DSPy Signatures let you specify the task as typed input/output instead of hand-writing prompts. ([dspy.ai](https://dspy.ai/ "DSPy"))

import dspy

class PlanRetrieval(dspy.Signature):

    """Create a retrieval and analysis plan for a user question."""

    question: str = dspy.InputField()

    question_type: str = dspy.OutputField(

        desc="One of: entity_aggregation, temporal_comparison, multi_hop_fact, generic"

    )

    tasks_json: str = dspy.OutputField(

        desc="JSON list of tasks with ids, dependencies, agent/tool names, and expected schemas."

    )

    assumptions: str = dspy.OutputField(

        desc="Important assumptions, ambiguities, and default date semantics."

    )

class NormalizeCompany(dspy.Signature):

    """Normalize raw company retrieval results into canonical company records."""

    raw_records_json: str = dspy.InputField()

    normalized_json: str = dspy.OutputField(

        desc="JSON list of CompanyRecord-compatible objects."

    )

class NormalizeLegalCases(dspy.Signature):

    """Normalize raw legal case retrieval results into canonical legal case records."""

    raw_records_json: str = dspy.InputField()

    normalized_json: str = dspy.OutputField(

        desc="JSON list of LegalCaseRecord-compatible objects."

    )

class SynthesizeAnswer(dspy.Signature):

    """Answer the user using structured tables, computed metrics, and evidence."""

    question: str = dspy.InputField()

    plan_json: str = dspy.InputField()

    computed_results_json: str = dspy.InputField()

    evidence_json: str = dspy.InputField()

    assumptions: str = dspy.InputField()

    answer: str = dspy.OutputField()

    caveats: str = dspy.OutputField()

---

**4. Build the Master Plan Agent as a DSPy module**

DSPy modules are normal Python classes with submodules in __init__ and execution logic in forward; the docs explicitly recommend decomposing programs this way for reuse, routing, governance, inspection, and optimization. ([dspy.ai](https://dspy.ai/getting-started/composing-modules/ "Composing modules - DSPy"))

import json

from collections import Counter

from datetime import date

import dspy

EU_COUNTRIES = {

    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czechia",

    "Denmark", "Estonia", "Finland", "France", "Germany", "Greece",

    "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg",

    "Malta", "Netherlands", "Poland", "Portugal", "Romania", "Slovakia",

    "Slovenia", "Spain", "Sweden"

}

class MasterPlanRAG(dspy.Module):

    def __init__(self):

        super().__init__()

        self.planner = dspy.ChainOfThought(PlanRetrieval)

        self.normalize_companies = dspy.ChainOfThought(NormalizeCompany)

        self.normalize_cases = dspy.ChainOfThought(NormalizeLegalCases)

        self.synthesizer = dspy.ChainOfThought(SynthesizeAnswer)

    def forward(self, question: str) -> dspy.Prediction:

        plan = self.planner(question=question)

        if plan.question_type == "entity_aggregation":

            result = self._run_company_aggregation(question, plan)

        elif plan.question_type == "temporal_comparison":

            result = self._run_temporal_legal_comparison(question, plan)

        else:

            result = {

                "status": "unsupported_or_generic",

                "message": "No specialized workflow matched."

            }

        answer = self.synthesizer(

            question=question,

            plan_json=plan.tasks_json,

            computed_results_json=json.dumps(result, default=str),

            evidence_json=json.dumps(result.get("evidence", []), default=str),

            assumptions=plan.assumptions,

        )

        return dspy.Prediction(

            answer=answer.answer,

            caveats=answer.caveats,

            plan=plan.tasks_json,

            assumptions=plan.assumptions,

            computed=result,

        )

    def _run_company_aggregation(self, question: str, plan) -> dict:

        raw_companies = retrieve_brewery_companies(region="EU", limit=500)

        normalized = self.normalize_companies(

            raw_records_json=json.dumps(raw_companies)

        )

        company_dicts = json.loads(normalized.normalized_json)

        companies = [CompanyRecord.model_validate(x) for x in company_dicts]

        # Filter EU and dedupe by canonical company id/name.

        seen = set()

        filtered = []

        for c in companies:

            if c.country not in EU_COUNTRIES:

                continue

            key = c.company_id or c.canonical_name.lower()

            if key in seen:

                continue

            seen.add(key)

            filtered.append(c)

        counts = Counter(c.country for c in filtered)

        return {

            "workflow": "company_country_aggregation",

            "table": [

                {"country": country, "company_count": count}

                for country, count in counts.most_common()

            ],

            "records_used": [c.model_dump() for c in filtered],

            "evidence": [

                ev.model_dump()

                for c in filtered

                for ev in c.evidence

            ],

        }

    def _run_temporal_legal_comparison(self, question: str, plan) -> dict:

        raw_policy = retrieve_eu_leniency_policy()

        policy = PolicyEvent.model_validate(raw_policy)

        raw_cases = retrieve_cartel_cases(

            jurisdiction="EU",

            sector_hint="banking OR financial services OR financial markets",

            start_year=1980,

            end_year=date.today().year,

        )

        normalized = self.normalize_cases(

            raw_records_json=json.dumps(raw_cases)

        )

        case_dicts = json.loads(normalized.normalized_json)

        cases = [LegalCaseRecord.model_validate(x) for x in case_dicts]

        split_date = policy.effective_date or policy.introduction_date

        if not split_date:

            return {

                "workflow": "temporal_legal_comparison",

                "error": "Could not determine split date for leniency policy.",

                "evidence": [ev.model_dump() for ev in policy.evidence],

            }

        before = []

        after = []

        for c in cases:

            if c.case_type != "cartel":

                continue

            # Default: decision_date is the counting date.

            if not c.decision_date:

                continue

            if c.decision_date < split_date:

                before.append(c)

            else:

                after.append(c)

        before_years = max(1, int(split_date[:4]) - 1980)

        after_years = max(1, date.today().year - int(split_date[:4]) + 1)

        before_rate = len(before) / before_years

        after_rate = len(after) / after_years

        return {

            "workflow": "temporal_legal_comparison",

            "policy": policy.model_dump(),

            "date_semantics": "Cases counted by decision_date unless unavailable.",

            "split_date": split_date,

            "before_count": len(before),

            "after_count": len(after),

            "before_cases_per_year": before_rate,

            "after_cases_per_year": after_rate,

            "direction": (

                "increase" if after_rate > before_rate

                else "decrease_or_no_increase"

            ),

            "before_cases": [c.model_dump() for c in before],

            "after_cases": [c.model_dump() for c in after],

            "evidence": [

                *[ev.model_dump() for ev in policy.evidence],

                *[

                    ev.model_dump()

                    for c in before + after

                    for ev in c.evidence

                ],

            ],

        }

This pattern keeps the LLM responsible for planning, normalization, and synthesis, while Python performs the sensitive operations: filtering, joining, deduplication, counting, and rate comparison.

---

**5. Use ReAct only where the agent needs tool choice**

You do **not** need dspy.ReAct for every step. Use it for open-ended retrieval routing, not for deterministic joins.

For example, a legal research sub-agent can be ReAct:

legal_research_agent = dspy.ReAct(

    "question -> raw_findings_json: str",

    tools=[

        retrieve_eu_leniency_policy,

        retrieve_cartel_cases,

    ],

    max_iters=8,

)

DSPy’s ReAct loop lets the model pick a named tool, supply arguments, observe the result, and continue until it selects the auto-added finish tool or reaches max_iters. The final typed outputs are produced by a separate extraction module. ([dspy.ai](https://dspy.ai/diving-deeper/tools-react-and-mcp/ "Tools, ReAct, and MCP - DSPy"))

But for your Master Plan Agent, I would usually prefer this:

DSPy planner decides what to run.

Python orchestrator runs it.

DSPy normalizer structures messy results.

Python joins and aggregates.

DSPy synthesizer explains.

That is more stable than giving one ReAct agent all tools and hoping it joins correctly.

---

**6. Add an explicit router**

For production, avoid relying only on free-form question_type. Add a controlled router.

class RouteQuestion(dspy.Signature):

    """Route a question to the correct analytical workflow."""

    question: str = dspy.InputField()

    route: str = dspy.OutputField(

        desc="One of: company_count_by_country, legal_before_after, generic_rag"

    )

    reason: str = dspy.OutputField()

class Router(dspy.Module):

    def __init__(self):

        super().__init__()

        self.route = dspy.ChainOfThought(RouteQuestion)

    def forward(self, question: str):

        pred = self.route(question=question)

        allowed = {

            "company_count_by_country",

            "legal_before_after",

            "generic_rag",

        }

        route = pred.route

        if route not in allowed:

            route = "generic_rag"

        return dspy.Prediction(route=route, reason=pred.reason)

Then the Master Agent dispatches by route.

---

**7. Implement joins as deterministic functions**

Do not ask DSPy to count unless the count itself requires interpretation.

def dedupe_companies(records: list[CompanyRecord]) -> list[CompanyRecord]:

    seen = {}

    for r in records:

        key = r.company_id or r.canonical_name.lower().strip()

        if key not in seen or r.confidence > seen[key].confidence:

            seen[key] = r

    return list(seen.values())

def count_by_country(records: list[CompanyRecord]) -> list[dict]:

    counts = Counter(r.country for r in records if r.country)

    return [

        {"country": country, "count": count}

        for country, count in counts.most_common()

    ]

def bucket_cases_by_policy_date(

    cases: list[LegalCaseRecord],

    split_date: str,

    date_field: str = "decision_date",

) -> dict:

    before, after, skipped = [], [], []

    for case in cases:

        d = getattr(case, date_field)

        if not d:

            skipped.append(case)

        elif d < split_date:

            before.append(case)

        else:

            after.append(case)

    return {

        "before": before,

        "after": after,

        "skipped": skipped,

    }

This is where most RAG systems become unreliable if they leave the operation to the LLM.

---

**8. Add evaluation sets**

DSPy becomes much more valuable once you compile/optimize against examples. DSPy optimizers tune prompts against a metric and can save optimized programs. ([dspy.ai](https://dspy.ai/ "DSPy"))

Create examples like:

trainset = [

    dspy.Example(

        question="How many brewery companies are in the EU per country?",

        expected_route="company_count_by_country",

    ).with_inputs("question"),

    dspy.Example(

        question="Did EU banking cartel cases increase after the leniency notice?",

        expected_route="legal_before_after",

    ).with_inputs("question"),

]

Router metric:

def route_accuracy(example, pred, trace=None):

    return pred.route == example.expected_route

Compile:

optimizer = dspy.MIPROv2(

    metric=route_accuracy,

    auto="medium",

)

compiled_router = optimizer.compile(

    Router(),

    trainset=trainset,

)

For the full Master Agent, use task-level metrics:

def aggregation_metric(example, pred, trace=None):

    computed = pred.computed

    if example.expected_route == "company_count_by_country":

        return "table" in computed and len(computed["table"]) > 0

    if example.expected_route == "legal_before_after":

        return (

            "before_cases_per_year" in computed

            and "after_cases_per_year" in computed

            and "direction" in computed

        )

    return bool(pred.answer)

---

**9. Use Beads to manage the build**

Beads is useful for building the system because it gives you project-level memory and a dependency-aware issue graph. Its README describes bd init, bd ready, bd create, bd dep add, bd show, and bd remember, plus JSON output and dependency tracking for agent workflows. ([GitHub](https://github.com/gastownhall/beads "GitHub - gastownhall/beads: Beads - A memory upgrade for your coding agent · GitHub"))

Example setup:

bd init

bd setup codex

Create implementation beads:

bd create "Implement DSPy MasterPlanRAG module" -p 0

bd create "Implement company retrieval adapter" -p 1

bd create "Implement company country resolver" -p 1

bd create "Implement legal case retriever" -p 1

bd create "Implement EU leniency policy retriever" -p 1

bd create "Implement deterministic join and aggregation engine" -p 0

bd create "Add DSPy evaluation set for routing and aggregation" -p 1

Link dependencies:

bd dep add "Implement DSPy MasterPlanRAG module" "Implement deterministic join and aggregation engine"

bd dep add "Implement DSPy MasterPlanRAG module" "Implement company retrieval adapter"

bd dep add "Implement DSPy MasterPlanRAG module" "Implement legal case retriever"

Store design decisions:

bd remember "MasterPlanRAG should use DSPy for planning/normalization/synthesis, but Python for joins, counts, dedupe, and temporal bucketing."

bd remember "Legal cartel trend questions should compare cases-per-year before vs after the policy date, not only raw before/after counts."

bd remember "Company aggregation questions require canonical company ids; never join only on display name."

Then your coding agent can use:

bd ready

bd show <id>

bd update <id> --claim

bd close <id>

---

**10. Suggested repo structure**

rag-master-agent/

  app/

    master_agent.py

    signatures.py

    schemas.py

    routing.py

  retrievers/

    company_retriever.py

    company_country_resolver.py

    legal_case_retriever.py

    policy_retriever.py

    vector_search.py

    web_search.py

  transforms/

    entity_resolution.py

    joins.py

    aggregations.py

    temporal.py

    confide