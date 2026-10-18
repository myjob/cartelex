

# Integrate Intent-Based Routing into QARetrievalPipeline

## Background

The existing [QueryRouterModule](file://~/a/celex-facts/src/pipelines/agent_dspy/query_router.py) dispatches fact-extraction calls to either `EnumerationFactModule` or `UniversalFactModule` based on heuristic intent classification. This works well for the **per-case fact-extraction** pipeline (driven by YAML targets, operating on a single `celex_id`).

The **QA retrieval pipeline** ([QARetrievalPipeline](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_modules.py#L1284-L1504)) serves a different role — it answers ad-hoc user questions using Plan→Execute→Synthesize over the entire knowledge base. It currently lacks intent-based routing, so a question like *"list all courts in case CELEX-31991D0562"* goes through the full Plan→Execute→Synthesize pipeline when it would be better served by the `EnumerationFactModule`'s map-then-aggregate approach (higher recall for entity enumeration).

The goal is to add the **same routing mechanism** at the entry of `QARetrievalPipeline`:

| Intent                            | Example                                                       | Route                                                    |
| --------------------------------- | ------------------------------------------------------------- | -------------------------------------------------------- |
| **enumeration** (single case)     | "list all courts in case CELEX-31991D0562"                    | `EnumerationFactModule`                                  |
| **qa** (cross-case or analytical) | "all cases with Leniency", "Did leniency increase over time?" | Existing `QARetrievalPipeline` (Plan→Execute→Synthesize) |

## User Review Required

> [!IMPORTANT]
> **Classifier choice: heuristic v1, LLM-upgradeable later.**
> Per your guidance, the router will use a **heuristic classifier** (regex patterns) for v1 — cheap, transparent, testable. The function is designed as a **swappable pure function** so an LLM classifier can replace it later without touching the module wiring. The chosen path is always logged via a `routing_decision` attribute on the prediction.

> [!IMPORTANT]
> **Routing scope — enumeration only fires when a celex_id is present.**
> `EnumerationFactModule` requires a `celex_id` + `question` to search within a single case's document chunks. Cross-case enumeration queries like "list all cases with leniency" have no single celex_id, so they must stay on the QA pipeline (which uses `kb_sql` / `doc_search` across all cases). The classifier therefore has a **two-axis decision**:
> 1. Does the question have enumeration intent? (regex patterns)
> 2. Does the question reference a specific celex_id? (reuses `CelexSelectionModule`)
>
> Only when **both** conditions hold does the router dispatch to `EnumerationFactModule`.

## Open Questions

> [!NOTE]
> **Schema inference for enumeration:** When the `QueryRouterModule` dispatches to `EnumerationFactModule` in the per-case pipeline, it receives a `FactExtractorParamVO` with a pre-configured `output_schema` from YAML. In the QA retrieval pipeline, the user's free-form question has no pre-set schema. The plan proposes **inferring the schema from the question** using a small heuristic table (e.g., "courts" → `"court, country, jurisdiction"`, "fines" → `"company, amount"`). Should this cover only the known entity types, or would you prefer an LLM-based schema derivation as a future extension?

## Proposed Changes

### Retrieval Router Module

#### [NEW] [retrieval_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_router.py)

A new thin module analogous to [query_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/query_router.py) but operating at the retrieval-pipeline level.

**Components:**

1. **`classify_retrieval_intent(question: str) → Literal["enumeration", "qa"]`**
   - Pure function, no I/O, no LLM.
   - Reuses the same `_ENUMERATION_PATTERNS` regex table from `query_router.py` (imported, not duplicated).
   - Returns `"enumeration"` when enumeration patterns match **and** the question appears to target a single case (contains a celex_id-like token, checked via `_CELEX_CANDIDATE_RE` from `retrieval_modules.py`).
   - Returns `"qa"` otherwise.
   - Observability: the function is pure and separately testable.

2. **`_infer_enumeration_schema(question: str) → tuple[str, str]`**
   - Returns `(target_fact_name, output_schema)` for the `FactExtractorParamVO`.
   - Maps question keywords to known entity schemas:
     ```
     "court"   → ("court_list_raw", "court, country, jurisdiction")
     "fine"    → ("fines", "company, amount")
     "party"   → ("companies_all", "company")
     "compan"  → ("companies", "company")
     "applicant" → ("leniency_applicants", "applicant")
     "remedy"  → ("decision_remedies", "remedy")
     ```
   - Falls back to a generic `("entities", "entity")` when no keyword matches.

3. **`RetrievalRouterModule(dspy.Module)`**
   - Injected dependencies (Provisioner pattern):
     - `qa_pipeline: QARetrievalPipeline` — the existing full pipeline.
     - `enumeration: EnumerationFactModule` — the map-then-aggregate module.
     - `celex_checker: CelexValidator` — for resolving celex_ids from free-form text.
   - `forward(self, question: str) → dspy.Prediction`:
     1. Classify intent via `classify_retrieval_intent(question)`.
     2. If `"qa"` → delegate to `self.qa_pipeline(question=question)`, tag `routing_decision="qa"`.
     3. If `"enumeration"`:
        a. Run `CelexSelectionModule` to extract and validate the celex_id.
        b. If no valid celex_id → fall back to QA pipeline (log the fallback).
        c. If valid celex_id → build a `FactExtractorParamVO` from the inferred schema, dispatch to `self.enumeration(params=params)`.
        d. Wrap the `EnumerationFactModule` result in a `dspy.Prediction` with the same output shape as `QARetrievalPipeline` (`answer`, `plan=None`, `evidence`, `celex_validation`, `routing_decision="enumeration"`).

**Output contract alignment:** The enumeration path must produce a `dspy.Prediction` with the same attributes (`answer`, `plan`, `evidence`, `celex_validation`) as the QA path so callers don't need branching logic. The enumeration result's `FactOutput.items` will be formatted into a markdown table/list as the `answer` string.

---

### Modifications to Existing Files

#### [MODIFY] [retrieval_modules.py](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_modules.py)

- Export `_CELEX_CANDIDATE_RE` as `CELEX_CANDIDATE_RE` (rename from private to module-public) so `retrieval_router.py` can import it without accessing a private name. Alternatively, the retrieval_router can import the existing `classify_intent` and `_CELEX_CANDIDATE_RE` — the underscore-prefix is a convention, not a hard barrier in the same package.

No other changes needed — `QARetrievalPipeline` is used as-is by the router.

---

### Tests

#### [NEW] [test_retrieval_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/tests/test_retrieval_router.py)

Mirrors the structure of [test_query_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/tests/test_query_router.py):

1. **`TestClassifyRetrievalIntent`** — pure function tests:
   - Enumeration + celex_id → `"enumeration"` (e.g. "list all courts in CELEX-31991D0562")
   - Enumeration without celex_id → `"qa"` (e.g. "list all courts across all cases")
   - QA phrasings → `"qa"` (e.g. "How many cases had leniency?")
   - Empty question → `"qa"`
   - Case insensitivity

2. **`TestInferEnumerationSchema`** — schema inference:
   - "courts" → `("court_list_raw", "court, country, jurisdiction")`
   - "fines" → `("fines", "company, amount")`
   - unknown entity → fallback schema

3. **`TestRetrievalRouterModuleDispatch`** — module dispatch with stubs:
   - Enumeration question + valid celex → enumeration stub called, QA stub not called, `routing_decision="enumeration"`
   - Enumeration question + invalid celex → QA fallback, `routing_decision="qa_fallback"`
   - QA question → QA stub called, enumeration stub not called, `routing_decision="qa"`
   - Output shape has `answer`, `plan`, `evidence`, `celex_validation`, `routing_decision`

## Verification Plan

### Automated Tests

```bash
cd ~/a/celex-facts
PYTHONPATH=src .venv/bin/python -m pytest src/pipelines/agent_dspy/tests/test_retrieval_router.py -v
```

Existing tests must still pass:

```bash
PYTHONPATH=src .venv/bin/python -m pytest src/pipelines/agent_dspy/tests/test_query_router.py -v
PYTHONPATH=src .venv/bin/python -m pytest src/pipelines/agent_dspy/tests/test_retrieval_modules.py -v
```

### Manual Verification

After implementation, run the integration test suite against the live DB to verify the end-to-end routing:

```bash
PYTHONPATH=src .venv/bin/python src/pipelines/agent_dspy/tests/test_qa_retrieval_integration.py
```

Specifically verify that:
- "list all courts in case CELEX-31991D0562" is routed to enumeration (check logs for `RetrievalRouter routed ... to enumeration`)
- "all cases with Leniency" is routed to QA (check logs for `RetrievalRouter routed ... to qa`)
- "How many cases had leniency?" stays on QA
- The `routing_decision` attribute is present on every prediction output



**Ready for 2 review**

Select text to add comments on the plan

# Plan Review: Intent-Based Routing into QARetrievalPipeline

## Context

The original plan proposed adding a `RetrievalRouterModule` analogous to `QueryRouterModule` to dispatch ad-hoc questions between `EnumerationFactModule` (single-case entity recall) and `QARetrievalPipeline` (cross-case Plan→Execute→Synthesize). The motivating example: _"list all courts in case CELEX-31991D0562"_ should not go through the full QA pipeline when the map-then-aggregate enumeration module would give higher recall.

**Review finding — the router module is already implemented.** [retrieval_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_router.py) exists at 362 lines and matches the original plan's component breakdown: `classify_retrieval_intent`, `infer_enumeration_schema`, `_format_enumeration_answer`, `RetrievalRouterModule`. The plan was largely accurate, but three concrete gaps remain and one design choice in the plan is incompatible with project conventions:

1. **Bug:** the router imports `CELEX_CANDIDATE_RE` from [retrieval_modules.py:46](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_router.py#L46) but only the private `_CELEX_CANDIDATE_RE` is defined at [retrieval_modules.py:912](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_modules.py#L912). `import retrieval_router` raises `ImportError`.
2. **Dead code:** the only `QARetrievalPipeline` consumer outside tests is [oi_dspy.py:90](file://~/a/celex-facts/src/ui/agno-ui/oi_dspy.py#L90); it has not been migrated to the router, so the routing is unreachable in production.
3. **Tests missing:** no `test_retrieval_router.py` exists.
4. **Plan convention drift:** original plan specified `pytest`, but CLAUDE.md mandates `unittest`. [test_query_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/tests/test_query_router.py) uses unittest with lightweight `_StubAgent` classes — the new test must follow that pattern.

This plan corrects the original to address the outstanding work precisely.

## Outstanding Changes

### 1. [FIX] Export `CELEX_CANDIDATE_RE` from retrieval_modules

[src/pipelines/agent_dspy/retrieval_modules.py:912](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_modules.py#L912)

Rename the module-level constant from `_CELEX_CANDIDATE_RE` to `CELEX_CANDIDATE_RE` (drop the leading underscore). One internal call site at [retrieval_modules.py:1013](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_modules.py#L1013) must be updated in the same edit. The regex is a stable, intentionally shared boundary type (already used by `CelexSelectionModule`), so promoting it to a public name is the cleaner fix than aliasing.

> [!NOTE] Per [feedback_rename_includes_tests](file://~/.claude/projects/-home-job-a-celex-facts/memory/feedback_rename_includes_tests.md), this rename must grep all call sites including tests. Current call sites: 2 in `retrieval_modules.py`. No test references to the private name were found.

### 2. [NEW] [test_retrieval_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/tests/test_retrieval_router.py)

Mirror the unittest + `_StubAgent` style of [test_query_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/tests/test_query_router.py). No `pytest`, no `unittest.mock` heavy machinery — just inline stubs that record calls and return tagged `dspy.Prediction` objects.

**Test classes:**

- **`TestClassifyRetrievalIntent`** — pure-function tests, no stubs:
    
    - Enumeration + celex_id → `"enumeration"` (e.g. _"list all courts in CELEX-31991D0562"_).
    - Enumeration without celex_id → `"qa"` (e.g. _"list all courts across all cases"_).
    - Pure QA phrasing → `"qa"` (e.g. _"How many cases had leniency?"_).
    - Empty string → `"qa"`.
    - Case-insensitive enumeration trigger.
- **`TestInferEnumerationSchema`** — pure-function tests:
    
    - `"courts"` → `("court_list_raw", "court, country, jurisdiction")`.
    - `"fines"` → `("fines", "company, amount")`.
    - `"penalties"` → `("fines", "company, amount")` (penalt substring).
    - `"applicants"` → `("leniency_applicants", "applicant")`.
    - Unknown noun (e.g. _"list all subsidiaries"_) → generic fallback `("subsidiary", "subsidiary")` via `_singularise`.
- **`TestRetrievalRouterModuleDispatch`** — stub-driven dispatch:
    
    - Build stubs that capture the kwargs passed and return a sentinel `dspy.Prediction`:
        - `_StubQAPipeline.__call__(question=...)` → records, returns prediction with `answer="qa-stub"`, `plan=None`, `evidence=""`, `celex_validation=CelexValidationResult(status="no_celex")`.
        - `_StubEnumeration.__call__(params=...)` → records `params`, returns `dspy.Prediction(output=FactOutput(items=[...], source="enumeration"))`.
        - `_StubCelexChecker` implementing the `CelexValidator` protocol — return `(status="valid", canonical_celex_id="CELEX-31991D0562")` or `("invalid", "")` based on input.
    - **Cases:**
        - Enumeration intent + valid celex → enumeration stub called once with a `FactExtractorParamVO`, QA stub not called, returned prediction has `routing_decision="enumeration"`, `answer` is a markdown table/list, `celex_validation.status == "valid"`.
        - Enumeration intent + invalid celex → QA stub called, enumeration stub not called, `routing_decision="qa_fallback"`.
        - QA intent → QA stub called, enumeration stub not called, `routing_decision="qa"`.
        - Output-shape contract: every returned prediction has `answer`, `plan`, `evidence`, `celex_validation`, `routing_decision`.

**Files to reuse (do not duplicate):**

- `FactExtractorParamVO` from [fact_extractor_param.py](file://~/a/celex-facts/src/pipelines/agent_dspy/fact_extractor_param.py).
- `FactOutput` from [dspy_protocols.py:136](file://~/a/celex-facts/src/pipelines/agent_dspy/dspy_protocols.py#L136).
- `CelexValidationResult` from [dspy_signature.py:9](file://~/a/celex-facts/src/pipelines/agent_dspy/dspy_signature.py#L9).

### 3. [MODIFY] Wire the router into [oi_dspy.py](file://~/a/celex-facts/src/ui/agno-ui/oi_dspy.py)

Without this step, the router has zero call sites and the routing is unreachable in production.

[src/ui/agno-ui/oi_dspy.py:31-39, 89-97](file://~/a/celex-facts/src/ui/agno-ui/oi_dspy.py#L31-L97)

- Add imports:
    
    ```python
    from pipelines.agent_dspy.enumeration_fact_module import EnumerationFactModule
    from pipelines.agent_dspy.retrieval_router import RetrievalRouterModule
    ```
    
- After constructing `pipeline = QARetrievalPipeline(...)` (line 90), construct the enumeration module with the same shared dependencies it needs (review `EnumerationFactModule.__init__` to confirm exact wiring — likely `chunk_repo`/`hybrid_search`/`toolkit`).
- Wrap with the router:
    
    ```python
    router = RetrievalRouterModule(
        qa_pipeline=pipeline,
        enumeration=enumeration_module,
        celex_checker=toolkit,  # CelexDatabaseToolkit already satisfies the protocol
    )
    ```
    
- Replace the in-process call site (search the file for `pipeline(` invocations) so the HTTP handler dispatches to `router(question=...)` instead. Keep the variable name `pipeline` rebinding minimal — readability over churn.

The `celex_checker=toolkit` arg removes the lazy `from tools.celex_checker import CelexCheckerToolkit` fallback inside `RetrievalRouterModule.__init__`. Consider tightening that signature to require `celex_checker` (drop the `None` default) in a follow-up — out of scope here.

## Files Modified

|File|Change|
|---|---|
|[src/pipelines/agent_dspy/retrieval_modules.py](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_modules.py)|Rename `_CELEX_CANDIDATE_RE` → `CELEX_CANDIDATE_RE`.|
|[src/pipelines/agent_dspy/tests/test_retrieval_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/tests/test_retrieval_router.py)|New — unittest-based tests for the three components.|
|[src/ui/agno-ui/oi_dspy.py](file://~/a/celex-facts/src/ui/agno-ui/oi_dspy.py)|Build `EnumerationFactModule`, wrap pipeline with `RetrievalRouterModule`.|

No changes needed to [retrieval_router.py](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_router.py) itself — its import will work once step 1 lands.

## Verification

### Automated (project standard: unittest)

```bash
cd ~/a/celex-facts
PYTHONPATH=src .venv/bin/python -m unittest src.pipelines.agent_dspy.tests.test_retrieval_router -v
```

Regression — neighbouring tests must still pass after the rename:

```bash
PYTHONPATH=src .venv/bin/python -m unittest \
    src.pipelines.agent_dspy.tests.test_query_router \
    src.pipelines.agent_dspy.tests.test_retrieval_modules \
    src.pipelines.agent_dspy.tests.test_celex_case_id_module -v
```

Lint:

```bash
ruff check src/pipelines/agent_dspy/retrieval_router.py \
           src/pipelines/agent_dspy/retrieval_modules.py \
           src/pipelines/agent_dspy/tests/test_retrieval_router.py \
           src/ui/agno-ui/oi_dspy.py
```

### Manual (DB-bound)

Start the API server and exercise the routing decisions through the chat interface, then grep server logs for the `RetrievalRouter` log lines emitted from [retrieval_router.py:299-337](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_router.py#L299-L337):

| Question                                    | Expected log line                                                                                                         |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `list all courts in case CELEX-31991D0562`  | `RetrievalRouter detected enumeration intent` → `RetrievalRouter enumeration: celex=CELEX-31991D0562 fact=court_list_raw` |
| `all cases with Leniency`                   | `RetrievalRouter routed question to qa strategy` (no celex_id → enum gate fails)                                          |
| `How many cases had leniency?`              | `RetrievalRouter routed question to qa strategy` (non-enumeration intent)                                                 |
| `list all subsidiaries in CELEX-31991D0562` | `enumeration: celex=... fact=subsidiary schema='subsidiary'` (generic fallback path)                                      |
|                                             |                                                                                                                           |

Also confirm `routing_decision` is present on the response payload returned to the OpenAI-compatible endpoint (or at least visible in server-side logs of the prediction).

## Items Not in Scope (Flagged for Follow-Up)

- **`celex_checker` should not be Optional.** [retrieval_router.py:268-278](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_router.py#L268-L278) lazily imports `CelexCheckerToolkit` when `celex_checker=None`. This breaks the Provisioner pattern (dependencies injected, not constructed). Once `oi_dspy.py` passes the toolkit explicitly, the default has no remaining caller and can be removed.
- **`force_override=True` in the enumeration param** ([retrieval_router.py:345](file://~/a/celex-facts/src/pipelines/agent_dspy/retrieval_router.py#L345)). Worth verifying behavior matches intent — the existing comment says "QA is always live, never cached"; the `EnumerationFactModule` cache semantics should be re-read to confirm.





27-May, All four planned changes are in and verified. Summary of what shipped:

- **[retrieval_modules.py:912](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/index.html?id=866254ce-3d86-4df4-8dfd-6c43ada3fefd&parentId=1&origin=4df5623e-5fba-4f09-9d72-554d1ec5e3ed&swVersion=5&extensionId=Anthropic.claude-code&platform=electron&vscode-resource-base-authority=vscode-resource.vscode-cdn.net&parentOrigin=vscode-file%3A%2F%2Fvscode-app&purpose=webviewView&session=9b4791a9-2655-4cf9-bd0a-9a5fe8f06fc7)** — renamed `_CELEX_CANDIDATE_RE` → `CELEX_CANDIDATE_RE` (fixed the broken import in `retrieval_router.py`). Updated the one internal call site at line 1013 and a docstring reference in [test_retrieval_modules.py:836](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/index.html?id=866254ce-3d86-4df4-8dfd-6c43ada3fefd&parentId=1&origin=4df5623e-5fba-4f09-9d72-554d1ec5e3ed&swVersion=5&extensionId=Anthropic.claude-code&platform=electron&vscode-resource-base-authority=vscode-resource.vscode-cdn.net&parentOrigin=vscode-file%3A%2F%2Fvscode-app&purpose=webviewView&session=9b4791a9-2655-4cf9-bd0a-9a5fe8f06fc7).
- **[retrieval_router.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/index.html?id=866254ce-3d86-4df4-8dfd-6c43ada3fefd&parentId=1&origin=4df5623e-5fba-4f09-9d72-554d1ec5e3ed&swVersion=5&extensionId=Anthropic.claude-code&platform=electron&vscode-resource-base-authority=vscode-resource.vscode-cdn.net&parentOrigin=vscode-file%3A%2F%2Fvscode-app&purpose=webviewView&session=9b4791a9-2655-4cf9-bd0a-9a5fe8f06fc7)** — promoted `_format_enumeration_answer` → `format_enumeration_answer` so it can be reused from `oi_dspy.py`.
- **[tests/test_retrieval_router.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/index.html?id=866254ce-3d86-4df4-8dfd-6c43ada3fefd&parentId=1&origin=4df5623e-5fba-4f09-9d72-554d1ec5e3ed&swVersion=5&extensionId=Anthropic.claude-code&platform=electron&vscode-resource-base-authority=vscode-resource.vscode-cdn.net&parentOrigin=vscode-file%3A%2F%2Fvscode-app&purpose=webviewView&session=9b4791a9-2655-4cf9-bd0a-9a5fe8f06fc7)** — new, 18 unittest cases across four classes (classifier, schema inference, formatter, dispatch), mirroring `test_query_router.py`'s stub style.
- **[oi_dspy.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/index.html?id=866254ce-3d86-4df4-8dfd-6c43ada3fefd&parentId=1&origin=4df5623e-5fba-4f09-9d72-554d1ec5e3ed&swVersion=5&extensionId=Anthropic.claude-code&platform=electron&vscode-resource-base-authority=vscode-resource.vscode-cdn.net&parentOrigin=vscode-file%3A%2F%2Fvscode-app&purpose=webviewView&session=9b4791a9-2655-4cf9-bd0a-9a5fe8f06fc7)** — built `EnumerationFactModule` at startup; added a streaming-friendly enumeration branch in `stream_generator` before the passthrough check, using `classify_retrieval_intent` + `infer_enumeration_schema` + `format_enumeration_answer`. Also removed two pre-existing dead imports the ruff gate flagged.

Verification:

- `ruff check` on all four modified files — clean.
- `oi_dspy.py` compiles.
- 79 targeted unit tests pass; 19 pre-existing failures elsewhere are integration tests requiring Postgres on port 5433 (unrelated).
- Manual verification still requires running the FastAPI server against the live DB to confirm log lines — covered in the plan's "Manual" section.