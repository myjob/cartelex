# v0.2.2 — End-to-end pipeline orchestrator, decision dates and provenance

**Period:** 2026-11-07 (v0.2.0) to 2026-11-22
**Scope:** 41 commits, 161 files changed (+33k / -1k lines)

v0.2.0 added the steps that bring decisions into the corpus. Each step still
had to be started by hand, in the right order. This release adds one entry
point, `batch.Main_Main_Pipeline`, that plans and runs the whole chain from
loader file to report. It stops on a failure and resumes from the last
finished batch. The release also repairs `eu_commission_decision_date`, which
was wrong for a large share of decisions. It reconciles the facts against
the hand-maintained case index and makes NACE classification much more
accurate. Case summaries and reports now carry verification status and
source links.

---

## Highlights

- **`Main_Main_Pipeline` orchestrator**: prerequisite checks, a staleness
  planner, batched subprocess steps with live progress, and a resumable run state
- **`eu_commission_decision_date` repair** (#369, #379): the date comes from the
  decision's own "Done at Brussels" line. It now matches the date in the
  `DEC-AT` id for 36 of 39 decisions (was 15) and the human index year for
  517 of 529 (was 438).
- **NACE shortlist-then-pick**: the reranker builds a shortlist and the LLM
  picks from it. Against human labels, class accuracy rose from 26% to 63%.
- **Human index reconciliation** (`batch.Main_recon_human_index`): seeds the gold set and
  lists conflicts for review
- **Provenance in summaries and reports** (#355, code done): every summary
  chapter names its source facts and their verification status
- **BGE reranker on the RTX 5070** in fp16: 3.5x the throughput at half the VRAM

---

## New features

### Pipeline orchestration

| Component | What it does | Design doc |
|---|---|---|
| `Main_Main_Pipeline` | One CLI from download to report. It runs the prerequisites, offers an open plan or builds a new one, asks the operator when the estimate exceeds `PIPELINE_ASK_THRESHOLD_MIN`, and on the first failure stops and prints the resume command | [Design Main_Main_Pipeline](docs/Design%20Main_Main_Pipeline.md) |
| Prerequisites | Checks the DB, the document folder, the DGX (a health gate plus one real completion), other running batches, free local GPU memory and the fact catalogue. Every check must pass | |
| Staleness planner | Turns loader changes, new documents, changed fact inputs, completeness gaps and an opt-in chunk-age trigger into an ordered run plan with estimates | |
| Batch runner | Runs steps as subprocesses in batches of `PIPELINE_BATCH_SIZE` cases and writes the restart state after each batch. Plans are JSON files written atomically, and a PID-checked lock keeps a second orchestrator out | |

- A live progress bar for each step, with rate and ETA, follows the step's
  subprocess log. The plan is shown as a width-aware table.
- `--resume --skip-case ID` drops a decision that cannot succeed (for example,
  one with a dead download URL) from the open plan
- When every failure in a batch was an LLM timeout and at least one case
  succeeded, the batch exits with code 3. The run continues and the failed
  decisions are re-planned. A batch where every case times out still stops
  the run once it has at least `LLM_HUNG_SERVER_MIN_BATCH` cases.
- Decisions whose download URL changed are re-prepared, and all their facts
  except `case_summary` are re-extracted

### Facts

- **`--force-facts`** (`Main_ingest_generic_facts`) forces only the named facts.
  Their predecessors are read from the store, not re-extracted.
- **`--select-facts`** runs a fact closure without forcing it. The planner
  names the facts of every plain step, so new decisions and gaps get facts
  even when the YAML has `run_state_active: false`.
- **`facts.created_at` moves only when the value changes**, so staleness checks
  mean "changed after", not "rewritten after". Every attempt is still recorded
  in `fact_ingestion_audit_trail`.
- **`fact_dependency_manager`**: catalogue checks, a dependency graph and the
  execution order. The orchestrator refuses to start on a broken
  `FACT_EXTRACTOR.yaml`.
- **Title fallback** (`na_fallback_fact` YAML key): when an extraction returns
  NA, `TitleFallbackModule` infers the value from `case_title` and records a
  `derived_from` lineage edge. It is enabled for `products`, which now covers
  goods and services. The `services` fact is retired.
- **NACE shortlist-then-pick** (`NaceModule(level=group|class)`): BGE ranks all
  NACE 2.1 classes and the LLM picks one of the top `NACE_SHORTLIST_TOP_K` (25).
  On 139 labelled decisions: division 61% to 76%, group 45% to 63%, class 26%
  to 63%.
- **`DecisionDateModule`** (pipeline `decision_date`,
  `pipelines/legal/decision_date.py`): reads the date line, then the title
  date. It falls back to the LLM over `heideltime_event_agg` only when the
  text states no date. A value that disagrees with the `DEC-*` id date or the
  human index is downgraded to `weak_support` / `reference_conflict`.
- **`tools.decision_date_report`**: read-only baseline and dry-run comparison
  against the reference dates

### Gold set and QA

- **`batch.Main_recon_human_index`**: deterministic reconciliation of four
  columns of the hand-maintained case index against the extracted facts, with
  no LLM. Results go to the new table `celex_facts.human_index_recon`, and a
  `verdict` column records the human review
  ([human_index2facts_recon](docs/human_index2facts_recon.md))
- **Case summary layout** (`agent_dspy/case_summary_layout.py`, #355): the 10
  copy-only chapters are rendered from the stored values instead of LLM text,
  so the LLM writes 16 chapters instead of 25. Each chapter ends with its
  sources and their weakest verification status. A `References` chapter lists
  the best supporting chunk for each source fact. The summary is recorded as
  `derived_from` lineage, and the timestamp is no longer part of its value.
- **XLSX report**: a new `Facts (status)` sheet with verification status,
  score, update time and chunk links. The QA tool `case_facts` prints
  `name [status]: value`.
- Fabel 5.2 architecture and DB reviews:
  [ARCHITECTURE_REVIEW-Fabel5.2](docs/qa/ARCHITECTURE_REVIEW-Fabel5.2.md),
  [Fabel5.2-Review](docs/qa/Fabel5.2-Review.md)

### Ranker and LLM

- The GPU extra and both GPU lock files move to `torch==2.13.0+cu130`
  (sm_75 to sm_120). The `+cu126` build has no RTX 5070 kernels, so the
  reranker had been falling back to CPU without any error.
- `RERANKER_DTYPE` (default `auto`: float16 on CUDA, float32 on CPU). In an A/B
  over 100 decisions, fp16 returned the same chunk set in 6146 of 6146
  retrievals and the same NACE class in 716 of 716 decisions.
- `RERANKER_BATCH_SIZE` 32 to 16: the card is compute-bound, so a larger batch
  only uses more VRAM
- Local LLM evaluation ([Local LLM Eval](docs/Local%20LLM%20Eval.md)): `qwen3-coder:30b` stays the batch
  model. The 27% repeat churn came from Ollama's KV prefix cache, not from
  the model.

---

## Fixes

### Pipeline planner
- Loader rows are linked to the download audit by candidate URL, not by mapped
  id. In the old matching, all 187 rows shown as "added" had in fact been
  downloaded.
- A failed re-extraction no longer counts as checked. Stale facts behind a
  missing or NA predecessor are left out of the plan.
- Naive local timestamps are converted through the machine's IANA time zone
  before they are compared with `timestamptz` columns
- The "downloaded since" baseline is the finish of the last full run

### Decision date (#369, #379)
- Re-extracted 168 decisions, then re-ran the dependent facts, graph and
  summaries. The full re-verify over 782 decisions gave 759
  `verified_supported`, 19 `weak_support` / `reference_conflict`
  and 3 `unverified`.
- The three decisions that still disagree with the date in their `DEC-AT` id
  are registry errors: their text has the right date

### Other
- The industry chapter of `case_summary` no longer refers to the retired
  `services` fact
- Windows clones: removed `src/pipelines/nace/NACE_Tree.py`, which on a
  case-insensitive file system collided with `nace_tree.py`
- Test-suite fixes and a guard for the unittest suite runner

---

## Infrastructure

- The Postgres container name and bind mount are aligned in
  `scripts/pg_*.sh` and
  [INSTALL celex-facts](docs/infra/INSTALL%20celex-facts.md).
  `restore_pgsql.sh` is renamed to `pg_restore.sh`, and there is a new
  `pg_restore_app_role.sh`.
- `.claude/settings.json` and the `gpu-burn` clone are no longer tracked
- Spec-kit templates are added under `.specify/`

---

## Upgrade notes

1. **Reinstall the GPU extra** from the `cu130` index (driver 580 / CUDA 13.0
   or newer). Do not move to `cu132` without a driver that supports
   CUDA 13.2.
2. **Apply `migrate_human_index_recon.sql`**, then `schema_comments.sql`.
3. **New settings** (see `src/config/settings.py`): `PIPELINE_*` (loader file,
   ask threshold, batch size, state and log folders, minimum free GPU memory),
   `LLM_HUNG_SERVER_MIN_BATCH`, `RERANKER_DTYPE`, `NACE_SHORTLIST_TOP_K`,
   `HUMAN_INDEX_XLSX_PATH`, `SIZE_BAND_*_FROM_TOKENS`.
4. **Re-extract `products` and both NACE facts** to get the title fallback and
   the shortlist pick. The stored output format of the NACE facts is unchanged.
5. **Regenerate summaries (#355 run book)**:
   `.venv/bin/python -m batch.Main_ingest_generic_facts --filter-fact case_summary --overwrite`,
   then `batch.Main_verify_facts_backfill --fact-name case_summary`, then
   `reporting.report_celex_facts`.

## Known limitations

- #355 is not closed: the summary regeneration run book above still has to
  be run
- #381: 17 decisions state no adoption date in their text, and the LLM fallback
  has not been run for them yet
- Other open follow-ups are tracked in `ISSUES.md`

---

# v0.2.0 — Document acquisition pipeline, translation and deterministic facts

**Period:** 2026-10-18 (v0.1.0) to 2026-11-07
**Scope:** 165 commits, 294 files changed (+63k / -2k lines)

v0.1.0 assumed the decision corpus was already on disk as Markdown. This release
adds the steps before that: getting the decisions in. An ad-hoc list of EU
competition decisions can now go from a loader file to English, chunked,
fact-extracted Markdown, and every step leaves an audit trail in Postgres. The
release also adds two deterministic facts and hardens the HeidelTime cartel-period
pipeline, which now replaces the legacy LLM `cartel_period` fact.

---

## Highlights

- **HTTP document downloader** (`batch.Main_download_documents`) with a Postgres
  document registry, an atomic master descriptor JSON and an append-only audit
- **Registry back-fill and document lineage**: chunks and facts record the
  source document they came from
- **Master identity matching and registry de-duplication**
- **MinerU PDF to Markdown conversion** (`batch.Main_convert_pdf_markdown`)
  through the asynchronous MinerU Tasks API
- **Markdown translator** (`batch.Main_translate_markdown`): DSPy chunk
  translation into an English master document
- **Source document decision** (`batch.Main_decide_source_document`): a rule
  ladder that picks the valid Commission decision among undecided candidates
- **New deterministic facts**: `recitals_max` and `number_undertaking`
- **HeidelTime `cartel_period`** takes over from the legacy LLM fact
- **Fact completeness checker** (`tools.fact_completeness_checker`)

---

## New features

### Document acquisition

| Batch | What it does | Design doc |
|---|---|---|
| `Main_download_documents` | Reads a loader file (csv, xlsx or a plain URL list), picks one PDF per decision with an English-first candidate selector, downloads it and registers every candidate | [Design Downloader](docs/Design%20Downloader.md) |
| `Main_convert_pdf_markdown` | Converts registered PDFs to Markdown on a MinerU server, detects the language from the Markdown and keeps the registry and master JSON in sync | [Design Mineru PDF 2 MD](docs/Design%20Mineru%20PDF%202%20MD.md) |
| `Main_decide_source_document` | Resolves `undecided` decisions from MinerU Markdown evidence: title-page "COMMISSION DECISION of", rejects summaries, letters and superseded combined decisions, merges documents the corpus already holds | [Design Source Decision](docs/Design%20Source%20Decision.md) |
| `Main_translate_markdown` | Translates a non-English source Markdown into `<stem>_EN.md`, atomically per decision, and makes it the master document; the original stays referenced | [Design Translator](docs/Design%20Translator.md) |

**Downloader details**

- Validated HTTPS PDF fetcher: never raises, rejects insecure redirects, retries
  with `tenacity`, and allows plain HTTP only when the payload is a valid PDF
- EUR-Lex PDFs are fetched from the Cellar REST API; EUR-Lex bot checks are
  recorded as an `access_challenge` failure
- Identifier rules enforce real CELEX syntax and handle collisions in every
  decision-id branch, with provisional ids for non-PDF candidates
- A failures view lists every failed candidate, so "what is still missing" is one
  query
- New schema: `document_registry`, `decision_registry`, `celex_number`, `is_source`,
  lineage foreign keys and conversion, translation and download audit tables

**Lineage and identity**

- Chunks and facts carry a `document_id`. A back-fill
  (`--limit N` for test runs) fills it in for existing rows.
- Master back-fill mapper with offline mode; existing master entries only fill
  blank standard keys
- Master identity matching by URL (date-guarded), case number tie-break and a
  content-hash safety net
- Registry de-duplication batch: re-points duplicates to the master key and
  deletes only on hard evidence; it can be re-run safely

**Conversion and translation**

- MinerU client with queue-aware deadlines, a submission limit, an upload-abort
  audit and atomic result placement (stem-named sidecars, shared images folder)
- The converter only consumes paths: Markdown paths are set at registration
  (`mineru/<pdf stem>.md`), never built by the converter
- DSPy translator with a per-chunk language check that rejects only
  source-language echoes; it uses a plain-text output field so bracketed Markdown
  is not truncated
- `TokenTargetMarkdownSplitter` gains optional sentence-aware separators for its
  fallback split
- RAG ingest gains a **lazy mode** that skips decisions that already have chunks

### Facts

- **`recitals_max`**: a deterministic recital counter that scores the longest
  recital chain and counts only recitals before the operative part. A corpus
  validation report is included in `docs/`.
- **`number_undertaking`**: a deterministic count of distinct legal units from
  `companies_investigated`, de-duplicated on the company stem
- **HeidelTime `cartel_period`** is gated on `case_commission_infringement` and
  replaces the retired LLM `cartel_period` fact under the same name
- **`tools.fact_completeness_checker`** separates real fact gaps from expected
  absences and from facts that are stored but empty
  ([design doc](docs/Design%20fact%20completeness%20checker.md))
- **`tools.heideltime_acceptance_report`** compares facts before and after a
  re-run

---

## Fixes

### HeidelTime
- One shared partial-date parser validates every event date
- Cartel periods with partial precision (year or month only) are kept downstream
- Bounded `event_agg` and `cartel_period` prompts; conduct selection gets its own
  output schema and is capped to the earliest and latest event per batch
- A null cartel-period envelope is treated as "no period" instead of an error

### Database
- `facts_cartel_legal_period_vw` compared the cartel period with itself; it now
  projects the legal period correctly
- `question2fact2embeddings.fk_fact_id` gets an `ON DELETE CASCADE` foreign key
  to `facts`; 316 orphan rows were removed after a backup
- Registry migrations use lock timeouts, `NOT VALID` foreign keys and views that
  can be re-created

### Source decision and translation
- Corpus identity is matched by case and date, using the document's own title
  date; a same-language match also needs the same subject
- A downloader re-run keeps a decided source selection
- An undeterminable language is audited as `skipped`, not `failed`

---

## Security and dependencies

- Dependency bumps for known vulnerabilities: `requests>=2.33`, `mistune>=3.3`,
  `weasyprint>=70`, applied to `pyproject.toml` and all lock files
  (`requirements*.txt`)
- New direct dependencies: `tenacity` (fetcher retries) and `openpyxl` (xlsx loaders)
- The README now cites the Zenodo DOI
  [10.5281/zenodo.23481580](https://zenodo.org/uploads/23481580)

---

## Upgrade notes

1. **Apply the migrations** in `src/pipelines/rag/pgvector/` for the document
   registry, lineage, conversion and translation audits, and the decision merge,
   then apply `schema_comments.sql`.
2. **Run the lineage back-fill** so existing chunks and facts get a `document_id`.
3. **Re-run facts** that depend on `cartel_period`. The fact is now produced by the
   `heideltime_cartel_period` pipeline; `FACT_EXTRACTOR.yaml` has been updated.
4. **New settings**: MinerU server and deadlines, and download review limits
   (for example `DOWNLOAD_REVIEW_MAX_CASE_PDFS`). See `src/config/settings.py`.
5. The ingestion run book (step order: back-fill, download, convert, decide,
   translate, ingest) is in section 10 of
   [Design Downloader](docs/Design%20Downloader.md). Run all commands from the
   repository root, for example `.venv/bin/python -m batch.Main_download_documents`.

## Known limitations

Open follow-ups are tracked in `ISSUES.md`. They include the downloader and
registry back-fill residuals from the final reviews, re-chunking of decisions whose
Markdown changed since the April splitter change, and translation back-fill
follow-ups.
