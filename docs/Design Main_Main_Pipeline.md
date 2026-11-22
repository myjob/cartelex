#pipeline #orchestration
# Design — Main_Main_Pipeline (end-to-end orchestrator)

Status: **implemented 20261108** (branch `feat/main-main-pipeline`); first live
`--dry-run` passed all prerequisites, see `ISSUES.md` for the operator decisions
it raised. Decision record and the
design history: [docs/claude/plans/20261108-main-main-pipeline-design.md](claude/plans/20261108-main-main-pipeline-design.md).

`src/batch/Main_Main_Pipeline.py` is the single entry point of the pipeline,
from download to report. It finds what changed, works out everything that
depends on it, asks the operator when the run is long, runs the existing
`Main_*` steps in dependency order for only the affected cases, shows progress
throughout, and restarts from the last sane state after a failure.

Related: [Design Downloader](Design%20Downloader.md),
[Design Mineru PDF 2 MD](Design%20Mineru%20PDF%202%20MD.md),
[Design Source Decision](Design%20Source%20Decision.md),
[Design Translator](Design%20Translator.md),
[Design fact completeness checker](Design%20fact%20completeness%20checker.md),
[Design Pipeline Universal](Design%20Pipeline%20Universal.md).

---

## 1. Why

After a change the operator used to work out by hand which steps, facts and
cases to re-run. Reference case (20261108): `products` and the NACE facts were
recomputed for 787 decisions; that took a forced copy of
`FACT_EXTRACTOR.yaml`, a hand-picked `--filter-fact`, and chasing the
successors (`OECD_sector`, `case_summary`, entity graph, reports) manually.

The orchestrator handles three kinds of small change:

| Change | Effect |
|--------|--------|
| A document is added to a loader file | every chapter runs for that case |
| A bug in a step is fixed | the affected facts are recomputed |
| A fact is refreshed | its successor facts and downstream chapters are recomputed |

## 2. Principles

* **Recompute only what is stale.** Above all `case_summary` (compute
  intensive) and the entity graph (~2.5 h full run).
* **Reuse, do not re-implement.** Every chapter calls existing `Main_*`
  scripts as subprocesses (own CLI and logging; GPU memory released between
  chapters; `stdin=/dev/null`).
* **Stop on the first failure**, never report a half-updated corpus.
* **All prerequisites must pass** before anything is computed.
* **The operator always sees progress**, and decides on long runs.

## 3. Chapters

```
0 Prerequisites ──> 1 Downloader ──> 2 Document preparation ──> 3 Ingest facts
                                                                    │
                    6 Report <── 5 Fact summaries <── 4 Entity graph <┘
```

Each chapter receives a set of cases, works only on them, and passes the cases
it changed to the next. Within a chapter every step runs only where needed.

| # | Chapter | Steps | Runs for |
|---|---------|-------|----------|
| 0 | Prerequisites | section 5 | always |
| 1 | Downloader | `Main_download_documents` | added loader rows; rows whose download URL changed |
| 2 | Document preparation | `Main_convert_pdf_markdown` (MinerU) -> `Main_decide_source_document` -> `Main_translate_markdown` (non-EN only) -> `Main_ingest_case_markdown2pgvector` -> `Main_classify_sections` -> `Main_enrich_chunk_context` | documents new from chapter 1 |
| 3 | Ingest facts | `Main_ingest_generic_facts` (all facts except `case_summary`) | stale or missing facts |
| 4 | Entity graph | `Main_materialize_entity_graph --celex-id` | cases whose graph input facts changed |
| 5 | Fact summaries | `case_summary` | cases where a `context_facts` value changed |
| 6 | Report | section 8 | only when an earlier chapter changed something |

## 4. Staleness: what enters the plan

The plan (chapters x steps x facts x cases) is the union of:

| Source | Enters at | Default |
|--------|-----------|---------|
| Added loader row, or a row whose download URL changed | chapter 1 | on |
| Chunks inserted for a newly added document | chapter 3, all facts of the case | on |
| Chunks newer than the facts built from them (splitter change) | chapter 3 | off (setting) |
| Fact older than one of its predecessors (audit timestamps) | chapter 3, the fact and its successors | on |
| Fact missing although it should exist (`fact_completeness_checker`) | chapter 3, that fact only | on |
| Operator request, `--refresh-fact <name>` | chapter 3, the fact and its successors | - |

Rules:

* **Chunks** count as a change only when a new document brings them in; chunk
  age is an opt-in trigger for re-chunking.
* **Fact staleness** compares stored-fact / audit timestamps
  (`celex_facts.fact_ingestion_audit_trail_vw.inserted_update_at`); successors
  come from walking the `predecessors` graph (`graphlib`, starting from
  `src/batch/fact_dependency_manager.py`).
* **`case_summary`** is rebuilt only when the *value* of one of its
  `context_facts` actually changed. A re-extraction that stores the same value
  does not trigger it. The values a summary was built from are recorded with
  it (e.g. a hash) for this comparison.
* **Exact forcing.** Stale facts are refreshed with a per-fact force
  (`Main_ingest_generic_facts --force-facts a,b`), never with `--overwrite`,
  which also forces every predecessor.

## 5. Prerequisites (chapter 0)

All must pass; a failure stops the run and names the check.

| # | Check |
|---|-------|
| 1 | PostgreSQL reachable (`settings.DATABASE_URL`) |
| 2 | Document folder configured and present |
| 3 | DGX serving: a real minimal call to `settings.LLM_MODEL_ID` (after a hard boot the host is up before Ollama serves) |
| 4 | No other `Main_*` batch running (shared DB, DGX, GPU) |
| 5 | Local GPU free memory covers the planned GPU chapters (MinerU, BGE); re-checked right before each GPU chapter |
| 6 | `FACT_EXTRACTOR.yaml` parses |
| 7 | Every entry loads through `FactExtractorParamVO.from_yaml_struct` |
| 8 | Dependency declarations complete: unique names; every `predecessors`, `verify`, `context_facts`, `na_fallback_fact` name resolves; no cycles. Shared with `test_fact_extractor_yaml_integrity.py` |
| 9 | `fact_completeness_checker` runs; its gaps feed the plan, they do not fail the check |

Checks 6-8 exist because manual YAML edits have crashed the pipeline mid-run.

## 6. Operator gate, state and restart

* **Estimate**: mean audit `duration` per fact x cases for chapter 3/5; per-case
  constants from settings for the other chapters.
* **Gate**: estimate below `PIPELINE_ASK_THRESHOLD_MIN` (default 30) starts
  directly; above it the plan is shown and the operator decides.
* **Not now**: the plan is saved as *pending* and kept until started; the next
  invocation offers it instead of recomputing.
* **Failure**: stop at once; no further cases, no later chapters.
* **Restart** (`--resume`, e.g. after a DGX hard boot): re-run the
  prerequisites, then continue the failed step with its unfinished cases.
* **State**: one JSON file per run under a settings path (plan, chapter and
  step status, done cases). No DB migration.

## 7. Progress

Always presented to the operator, via `logging` on the console:

* plan summary before the gate;
* chapter start / finish lines with duration and cases changed;
* a running line per step, e.g.
  `chapter 3 ingest facts | NACE_class_code | 412/787 cases | 38m elapsed | ETA 31m`;
* steps run in case batches (`PIPELINE_BATCH_SIZE`), so progress and restart
  state advance per batch; each step's subprocess log path is printed when it
  starts;
* `--status` prints the same view from another terminal or after a crash;
* on stop: chapter, step, last finished case, reason, log path, and the exact
  resume command;
* on success: per-chapter summary, report paths, zip name.

## 8. Report (chapter 6)

1. `reporting.report_celex_facts` -> `celex_docs/reports/celex_fact_report_<ts>.xlsx`
2. `reporting.report_eddy_celex_facts --facts-report <workbook of step 1>`
3. `reporting.retrieve_case_summary_md2disk` -> `celex_docs/reports/case_summary/`
4. zip `celex_docs/reports/case_summary` -> `celex_docs/reports/case_summary-<YYYYMMDDHHMMSS>.zip`

## 9. CLI

Run from the repo root:

```
.venv/bin/python -m batch.Main_Main_Pipeline                 # detect, plan, gate, run
.venv/bin/python -m batch.Main_Main_Pipeline --dry-run       # prerequisites + plan only
.venv/bin/python -m batch.Main_Main_Pipeline --resume        # continue a pending or failed run
.venv/bin/python -m batch.Main_Main_Pipeline --status        # progress of the current run
.venv/bin/python -m batch.Main_Main_Pipeline --refresh-fact products
.venv/bin/python -m batch.Main_Main_Pipeline --only report
.venv/bin/python -m batch.Main_Main_Pipeline --yes           # skip the gate
```

## 10. Module layout

| Module | Role |
|--------|------|
| `src/batch/Main_Main_Pipeline.py` | CLI flow: lock, prerequisites, open-plan offer, plan, gate, run, exit codes (0 ok / 1 step failed / 2 usage or out-of-date plan / 3 prerequisite failed or locked) |
| `src/batch/main_pipeline/models.py` | `Chapter`, `StepPlan`, `RunPlan` |
| `src/batch/main_pipeline/state_store.py` | `run-<run_id>.json` (atomic writes), PID-checked `main_pipeline.lock` |
| `src/batch/main_pipeline/prerequisites.py` | the checks of section 5, `gpu_ok` |
| `src/batch/main_pipeline/planner.py` | `PlannerRepository` (staleness SQL), `build_plan`, `fillable_gaps`, `loader_urls` |
| `src/batch/main_pipeline/runner.py` | `StepRunner` (batches, progress, stop on failure), `step_argvs`, `zip_case_summaries` |
| `src/batch/fact_dependency_manager.py` | `load_structs`, `check_catalogue`, `dependents`, `execution_order` |

Supporting changes: `Main_ingest_generic_facts --force-facts`,
`Main_download_documents --ids`, and `FactRepository.upsert_fact` keeping
`created_at` when the value is unchanged.

Implementation notes (decided while building):

* A stale or refreshed fact is forced together with its catalogue dependents
  in one ingest call (`case_summary` excepted); chapter 5 then runs
  `case_summary` unforced and `FactSummaryModule` rebuilds only on an input
  value change.
* A decision whose download URL changed runs convert, translate and embed with
  `--mode force` and gets every fact except `case_summary` re-extracted.
* Completeness gaps enter the plan only when the missing fact's predecessors
  are stored and non-NA; otherwise the ingest would skip them on every run.
* `--resume` and "y" at the open-plan offer count as the operator's go; the
  start question is asked only for a freshly built plan. "r" replaces an open
  plan (it is marked `superseded`).
* Settings: `PIPELINE_LOADER_FILE`, `PIPELINE_ASK_THRESHOLD_MIN`,
  `PIPELINE_BATCH_SIZE`, `PIPELINE_STATE_DIR`, `PIPELINE_LOG_DIR`,
  `PIPELINE_GPU_MIN_FREE_MB_MINERU`, `PIPELINE_GPU_MIN_FREE_MB_BGE`,
  `PIPELINE_CHUNK_AGE_TRIGGER`, `PIPELINE_STEP_SECONDS_PER_CASE`.
