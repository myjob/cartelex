# v0.2.1 — Re-release of v0.2.0

The v0.2.0 tag was created on a commit that predated the release merge: its
source archives carried version 0.1.0 and none of the v0.2.0 changes. v0.2.1
publishes the intended v0.2.0 content from `main`. It also removes the re-added
`.cursorrules` and a stray `celex-facts/` spec-kit scaffold from the repository.
The v0.2.0 notes below apply unchanged.

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
