# Data Model — Companies

Data lineage of `celex_facts.company_investigated2lei2corpidx2hr_vw`, from raw
LLM-extracted facts through to the enrichment view.


The central table is ```opencorp_index_companies``` which holds all data received from the opencorporates API. The FK relations betwee oc index companies and facts in the n:m table is build by company name applying a fuzzy logic in a few cases. see:  [Design Corp Index](<docs/Design Corp Index.md>) and implementation [Main_ingest_company_investigated2celex](src/batch/Main_ingest_company_investigated2celex.py)

![](assets/Data%20Model%20Companies/file-20260802162456268.png)


Table oc index query documents the query of the REST call and its return errors. The table main purpose of oc api daily call is to avoid a quota issue (100'000 p. day, 500k p.M)

## View definition

```sql
CREATE VIEW celex_facts.company_investigated2lei2corpidx2hr_vw AS
SELECT DISTINCT company_name, address, iso_country_code,
                fk_lei, fk_opencorp_idx, fk_handelsregister, jurisdication_code
FROM celex_facts.company_investigated2celex_id;
```

Source: `src/pipelines/rag/pgvector/pg_celex-ddl.sql:1223-1231`. Seven columns —
`address` (renamed from `adress`) and `jurisdication_code` included. It is a plain
`DISTINCT` pass-through over `celex_facts.company_investigated2celex_id` — no
joins, no transformation. All lineage lives in how that table gets populated.

**Three competing copies of this definition exist**, and replaying a stale one has
broken the view once already (dropping `jurisdication_code`, which fails
`Main_ingest_opencorp_index_companies.py` with `UndefinedColumn`):
`pg_celex-ddl.sql:1223-1231` is the maintained one;
`migrate_company_investigated_jurisdiction.sql:39-49` is **stale** and still uses
the pre-rename `adress`; `migrate_opencorp_relaxation_retry.sql:45-55` is the
repair. Note this view resolves **nothing** — it projects the `fk_*` marker
columns as they stand. For resolved register attributes see
[The registry master view](#the-registry-master-view) below.

## Lineage overview

```
FACT_EXTRACTOR.yaml (companies_investigated / companies_address / companies_country)
        |  DSPy extraction (universal / fact_context / detail_retriever)
        v
celex_facts.facts   (raw fact landing table, one row per celex_id + fact name)
        |  Main_ingest_company_investigated2celex.py  (roster flatten)
        v
celex_facts.company_investigated2celex_id   <---- fk_lei -------- celex_facts.corp_lei (GLEIF LEI dump)
        |                                    <---- fk_handelsregister -- celex_facts.handelsregister_companies (DE register)
        |                                    <---- fk_opencorp_idx ---- celex_facts.opencorp_index_companies (OpenCorporates API)
        v
        |
        +--> celex_facts.company_investigated2lei2corpidx2hr_vw
        |        markers only -- also the resume cursor for the OpenCorporates batch
        |
        +--> celex_facts.company_registry_master_vw
                 resolved register attributes, tall over all four sources
                 (+ celex_facts.llm_index_companies, joined by exact name)
```

## Stage 0 — Raw fact extraction (DSPy)

Company facts are declared, not hard-coded, in `config_main/FACT_EXTRACTOR.yaml`:

| Fact | YAML location | Pipeline | Extracts |
|---|---|---|---|
| `companies_investigated` | `FACT_EXTRACTOR.yaml:401` | `universal` (hybrid-search) | Party/addressee names, verbatim, from decision text |
| `companies_address` | `FACT_EXTRACTOR.yaml:287` | `detail_retriever`, `section_scope: [operative]` | Registered address, per-entity targeted retrieval |
| `companies_country` | `FACT_EXTRACTOR.yaml:269` | `fact_context`, `normalize_iso_country: true` | ISO 3166-1 alpha-2 country code, deterministically canonicalized |

`companies_address` and `companies_country` both declare `predecessors:
["companies_investigated"]` — they enrich the roster that `companies_investigated`
defines, rather than re-deriving companies themselves.

Extraction is implemented in `src/pipelines/agent_dspy/universal_modules.py`:
- `UniversalFactModule.forward` (`:189`) — `companies_investigated`.
- `FactContextModule` (`:901`) — `companies_country`.
- `FactDetailRetriever` (`:964`) — `companies_address` (one search + one
  extraction call per company, so no company is silently dropped by a single
  retrieval miss).

Driver: `src/batch/Main_ingest_generic_facts.py` (`FactIngestionOrchestrator`,
`PIPELINE_MODULE_MAP`). Output lands via `FactRepository.upsert_fact`
(`src/pipelines/rag/pgvector/fact_repository.py:475`) into `celex_facts.facts`
(`name`, `value_json`, `value_md`, keyed by `celex_id`) — this is the landing
table for every raw fact, not just company facts.

Historical note: the older fact names `companies` / `companies_all` /
`companies_canoncial` were normalized to `companies_investigated` (commit
`2546446`), and the superseded `company_canonical2celex_id` migration path was
deleted (commit `3728795`).

Their rows are being deleted from the database one name at a time by scoped
scripts rather than in one pass. Applied 2026-08-29:
`migrate_delete_companies_fact.sql` (`companies`, 878 rows) and
`migrate_delete_companies_all_fact.sql` (`companies_all`, 744).
`migrate_delete_companies_canoncial_fact.sql` (`companies_canoncial`, 732) is
written and dry-run verified but **not yet applied**. None of them is the bundled
`migrate_retire_obsolete_company_facts.sql`, which deletes all four names in one
transaction and is unsafe: it omits the `knowledge_edges.dst_source_id ->
fact_source_registry` constraint and so aborts partway. `company_country` (744
rows) is still present and cleared for the same treatment.

Two consequences worth carrying forward. `companies_canoncial` was the **sole
source of all 4,286 `participated_in` edges** in `entity_edges`; deleting it
empties the entity graph's company layer, and rematerializing from
`companies_investigated` does not refill it until `entity_graph_mapping` reads
both row keys (see the caveat below and `ISSUES.md` 20260829). And
`qa_company_delta_vw`, which diffed the two names, is dropped by that same
script — it is the only database object whose definition names the retired fact.

**Known caveat (tracked in `ISSUES.md`):** `companies_investigated` is
currently `run_state_active: false` even though six other facts, including
`companies_country`, declare it as a predecessor. Until it is reactivated and
re-run corpus-wide, the roster table below can be stale or incomplete for
CELEXes ingested after the question wording changed.

## Stage 1 — Roster flattening into `company_investigated2celex_id`

`src/batch/Main_ingest_company_investigated2celex.py` is pure ETL (no LLM
calls) that turns the three raw facts above into one roster row per
`(company_name, celex_id)`:

- `extract_company_names` (`:42`) — parses `companies_investigated.value_json`
  into distinct names -> `company_name`, `celex_id`.
- `extract_enrichment_map` (`:90`) + `CompanyInvestigatedIngestor._build_roster`
  (`:213`) — joins `companies_address` / `companies_country` facts onto the
  roster -> `address`, `iso_country_code`. The join is *not* by verbatim name:
  the two facts are produced by different pipelines that disagree on how a
  company is spelled, so `match_enrichment` walks a four-rung cascade —
  verbatim key; folded key (`fold_match_key`, case/accent/punctuation
  insensitive) against an index that also carries each key with its trailing
  parenthetical alias stripped, so `fact_context`'s
  `Toyoda Gosei Co., Ltd. (TOYODA GOSEI)` is reachable by the bare name;
  folded prefix, where the enrichment key is a leading whole-word run of the
  roster name (`Praoil` -> `Praoil, Strada 2, ... Italy`); and folded reverse
  prefix, where the roster name is a leading run of the enrichment key
  (`Pirelli S.p.A.` -> `Pirelli S.p.A., Milan`). Both prefix rungs resolve only
  when every matching key agrees on one value, so an ambiguity yields NULL
  rather than a guess.
- Persisted through `company_investigated_repository.py`:
  `insert_companies` (INSERT), `update_missing_enrichment` (COALESCE backfill
  UPDATE — never overwrites a value already set) and `delete_companies`
  (DELETE).

`process_celex` **reconciles** against the current fact rather than skipping a
CELEX that already has rows, so a corrected or re-extracted
`companies_investigated` takes effect. Stored names are diffed against the
cleaned names the fact yields: a surviving name keeps its row — and therefore
its `id`, every enrichment column already resolved and every
`company_investigated_celex_id2opencorp_index_company` link hanging off it — a
new name is inserted, and a dropped name is deleted, cascading its links away.
A fact that yields no names at all is treated as `empty` and leaves the stored
rows untouched, so a failed extraction cannot empty a roster. Over an unchanged
fact the whole pass is a no-op, so re-runs remain idempotent.

Table: `celex_facts.company_investigated2celex_id`
(`src/pipelines/rag/pgvector/pg_celex-ddl.sql:1181-1193`), unique on
`(company_name, celex_id)`. `fk_lei`, `fk_opencorp_idx`, `fk_handelsregister`
are left `NULL` at this stage — they are plain `text` columns, not real
foreign keys, and are populated only by the downstream enrichment branches
below.

## Stage 2 — Parallel enrichment branches

All three branches run as post-stages of the same `main()` in
`Main_ingest_company_investigated2celex.py` (plus a separate script for
OpenCorporates) and independently `UPDATE` one `fk_*` column each. Each writes
`'NA'` when no match is found, so the flat columns carry three states:
`NULL` (not attempted), `'NA'` (attempted, no match), or a real value/marker.

### 2a. `fk_handelsregister` — German commercial register

- `CompanyInvestigatedIngestor.enrich_handelsregister` (`:252`), scoped to
  `iso_country_code = 'DE'`.
- `company_investigated_repository.py:411` `resolve_handelsregister_numbers`
  — fuzzy match via `_handelsregister_match_key` (`:56`), a SQL expression
  that lowercases, expands German umlauts/ligatures, folds remaining Latin
  accents, and strips all non-`[a-z0-9]` characters. The same expression
  shape backs the `ix_hr_matchkey` functional index
  (`pg_celex-ddl.sql:1218-1221`) on the source table, so the match rides an
  index rather than a full scan.
- `set_handelsregister` (`:524`) writes the resolved number or `'NA'`.
- Source table `celex_facts.handelsregister_companies` is loaded independently
  of the fact pipeline by `HandelsregisterLoader.load`
  (`src/pipelines/rag/pgvector/handelsregister_loader.py:364`), which ingests
  FTM JSONL from the `de_offeneregister` open-data dataset into
  `handelsregister_companies` / `handelsregister_previous_names` /
  `handelsregister_officers`.

### 2b. `fk_lei` — GLEIF Legal Entity Identifier

- `CompanyInvestigatedIngestor.enrich_lei` (`:324`), global (not
  country-scoped).
- `company_investigated_repository.py:465` `resolve_lei_codes` — matches each
  roster name as an uppercase substring of `celex_facts.corp_lei.legal_name`
  (`LIKE '%NAME%'` on a trigram index), preferring an exact match, then the
  shortest containing `legal_name`. Names shorter than
  `MIN_LEI_NEEDLE_LENGTH = 4` chars are skipped as `'NA'` (too many false
  matches below the trigram floor).
- `set_lei` (`:541`) writes the resolved LEI or `'NA'`.
- Source table `celex_facts.corp_lei` is loaded by `CorpLEIIngestor`
  (`src/pipelines/rag/pgvector/corp_lei_index_ingestor.py:46`) from GLEIF's
  concatenated LEI XML dump (e.g.
  `celex_docs/20260421-gleif-concatenated-file-lei2.xml...`), independent of
  the fact-extraction pipeline.

### 2c. `fk_opencorp_idx` — OpenCorporates index

- Standalone script `src/batch/Main_ingest_opencorp_index_companies.py`
  (not a `BatchJobRunner` job; iterates companies directly), e.g.:
  ```
  PYTHONPATH=src .venv/bin/python \
      src/batch/Main_ingest_opencorp_index_companies.py \
      --iso-country-code GB --limit 20
  ```
- Its target list comes from `select_opencorp_targets`
  (`company_investigated_repository.py:295`), which queries **the view
  itself** — `company_investigated2lei2corpidx2hr_vw` — for names where
  `fk_lei = 'NA' OR fk_handelsregister = 'NA'` and `fk_opencorp_idx IS NULL`.
  The view therefore doubles as this job's resume cursor: once a name is
  marked, it drops out of the target set on the next run.
- Matches are found via `OpenCorpClient.search_companies`
  (`src/pipelines/corp_index/opencorp_client.py`) against the OpenCorporates
  API, persisted to `celex_facts.opencorp_index_companies` through
  `OpenCorpIndexRepository` (`src/pipelines/corp_index/opencorp_repository.py`).
- Links are written to the N:M table
  `celex_facts.company_investigated_celex_id2opencorp_index_company`
  (`pg_celex-ddl.sql:1245-1257`) via `roster_ids_for_company` (`:328`) +
  `insert_opencorp_links` (`:365`); the roster column itself is then set by
  `set_opencorp_idx` (`:557`) to `'LINKED'` or `'NA'` — a marker, not the
  OpenCorporates ID. The real per-company OpenCorporates match lives only in
  the N:M link table.

**N:M table usage note:** `company_investigated_celex_id2opencorp_index_company`
is a **candidate pool, not a resolved link** — 83,423 rows over 4,097 roster
rows, averaging 20.4 candidates each and 2,436 at worst. Only 5,578 of them were
ever adjudicated, and `match_score IS NOT NULL` is exactly equivalent to
`resolution_rung IS NOT NULL`; the other ~77,800 rows are raw search dumps.
Retrieving *which* OpenCorporates company matched a roster row means querying
this table directly — never the `fk_opencorp_idx` column, which is only ever the
marker `'LINKED'` / `'NA'` / `'AMBIGUOUS'`.

It was write-only until 2026-08-30: `celex_facts.company_registry_master_vw` is
its **first reader**, and restricts to scored rows then takes the best by
`match_score` and rung precedence. Any other consumer joining it without both
steps will multiply its row count by roughly thirteen.

## Column-by-column summary

| View column | Base table column | Written by | Upstream source |
|---|---|---|---|
| `company_name` | `company_name` | Stage 1, `insert_companies` | `companies_investigated` fact (LLM, `universal` pipeline) |
| `address` | `address` | Stage 1, `insert_companies` / `update_missing_enrichment` | `companies_address` fact (LLM, `detail_retriever` pipeline) |
| `iso_country_code` | `iso_country_code` | Stage 1, `insert_companies` / `update_missing_enrichment` | `companies_country` fact (LLM, `fact_context` pipeline, deterministic ISO normalization) |
| `fk_lei` | `fk_lei` | Stage 2b, `set_lei` | `celex_facts.corp_lei` (GLEIF XML dump, non-LLM substring match) |
| `fk_handelsregister` | `fk_handelsregister` | Stage 2a, `set_handelsregister` | `celex_facts.handelsregister_companies` (`de_offeneregister` FTM JSONL, non-LLM fuzzy-key match) |
| `fk_opencorp_idx` | `fk_opencorp_idx` | Stage 2c, `set_opencorp_idx` | OpenCorporates API search, via `celex_facts.opencorp_index_companies` (marker only; real link in the N:M table) |

`celex_id` (on the base table but not exposed by the view) is a trivial
passthrough from Stage 1 — the `celex_id` the `companies_investigated` fact
row belonged to.

## `company_investigated_repository.py`

`src/pipelines/rag/pgvector/company_investigated_repository.py` is the single
DAO fronting every read/write described above: roster inserts, enrichment
UPDATEs (Stage 1), the three `fk_*` resolution + set methods (Stage 2), and
the `select_opencorp_targets` view read that feeds Stage 2c back in. All
queries go through the shared engine from `session.py`.


## The registry master view

> Full reference, measurements and verification queries:
> [Data Quest company_registry_master_vw](<Data Quest company_registry_master_vw.md>).
> The summary below is the lineage view of the same object.

`celex_facts.company_registry_master_vw` (added 2026-08-30,
`src/pipelines/rag/pgvector/migrate_registry_master_view.sql` — the **sole** home
of its definition) is the resolved counterpart to the marker view above. Where
`company_investigated2lei2corpidx2hr_vw` says *whether* a source matched, this one
says *what that source holds*.

**Grain: one row per `(roster row, source)`** — tall, not a wide pivot. A pivot
would need a new column family per source and still could not say which source a
value came from. The anchor is `company_investigated2celex_id.id`, the only key
common to all four sources, so this is a roster-anchored view rather than a global
union over the 8.8M register records.

### The four branches

| Source | Rank | Joined by | Rows |
|---|---|---|---|
| `lei` | 1 | `roster.fk_lei` -> `corp_lei.lei`, primary key | 829 |
| `handelsregister` | 2 | `roster.fk_handelsregister` -> `handelsregister_companies.company_number`, primary key | 295 |
| `opencorporates` | 3 | best scored candidate from the N:M pool, `LATERAL ... LIMIT 1` | 2,175 |
| `llm` | 4 | `llm_index_companies.company_name`, exact | 6,371 |

Total 9,670 rows (measured 2026-08-30). `opencorp_index_company_detail` is **not**
a fifth branch: it is keyed on exactly the identity of the OpenCorporates search
row, so it is a `COALESCE` overlay flagged by `has_detail`. A separate branch would
emit two rows that cannot meaningfully disagree, and would start doubling the
OpenCorporates branch as the API budget fills that table.

### The trust ladder

`source_rank` encodes **issued identifier > primary register > aggregator with a
fuzzy link > model assertion**. The consumer contract is:

```sql
SELECT DISTINCT ON (company_key) *
FROM celex_facts.company_registry_master_vw
ORDER BY company_key, source_rank, link_confidence DESC NULLS LAST;
```

`llm` is ranked last because every value in it is a model assertion no registry
confirmed; it must never supply an identity key (see
`migrate_llm_index_companies.sql`), only `attrs_json`-grade enrichment. `link_method`
and `link_confidence` are exposed so a consumer can re-rank without editing the view.

### The OpenCorporates pick

Ordered `match_score DESC`, then rung precedence (`verbatim`, `verbatim@any`,
`split`, `no-paren`, `no-role`, `core`, `core@country`, `core@any` — from
`opencorp_resolution_ladder.py`), then the candidate uuid. That last term is not
decoration: without it the pick is non-deterministic across plans, and a view whose
answer changes between runs is unusable as an identity source. `array_position`
returns NULL for an unknown rung, which sorts last, so a new rung degrades to
least-preferred instead of crashing.

### Traps the view absorbs

1. `fk_lei` / `fk_handelsregister` hold the literal `'NA'` for "looked up, no
   match" — 5,554 of 6,383 rows on `fk_lei` alone.
2. `fk_opencorp_idx` is never an id.
3. `llm_index_companies` writes `'NA'` rather than NULL on every identifier column.

**`'NA'` is also the real ISO 3166-1 code for Namibia.** The view strips it from
identifier, name, address and status columns, but never from `iso_country_code` or
`jurisdiction_code`, where `corp_lei` holds 107 genuinely Namibian rows. Any code
folding this sentinel must make the same distinction.

### What it does not do

It surfaces what was already resolved; it resolves nothing new. Only **3,008 of
6,383** roster rows (47%) reach a registry-grade row at rank <= 3 — the remaining
3,364 fall through to `llm`. The view makes that shortfall countable for the first
time; it does not shrink it. It also does not reconcile sources (`source_rank` is a
preference order, not a merge rule), does not dedup a company across decisions, and
carries no aliases, previous names or ownership structure.
