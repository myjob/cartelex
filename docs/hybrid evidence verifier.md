#design

# Plan: `evidence_verifier_module.py` — hybrid evidence verifier

  

## Context

  

Manual review of facts in the celex-facts system sometimes finds that a fact's

*cited chunk does not actually support the claim* — the chunk was merely

retrieved or linked during extraction, not verified. Today a fact is treated as

usable simply because it was extracted and has provenance. There is no step that

asks "does this chunk **directly support** this fact?".

  

This builds a **hybrid evidence verifier** (per the ChatGPT/Opus discussion):

deterministic candidate generation + hard gates + scoring, with the LLM used

only as a last-resort support judge for the fact classes that need it. Output is

persisted to a new `fact_evidence` table plus a `verification_status` on the fact.

  

## Definition of done

  

1. **Every (non-excluded) fact ends with at least one `fact_evidence` row** — including unverified, low-score, human, derived, and no-provenance facts — and a populated `facts.verification_status`. The table is a complete ledger, never sparse.

2. **Speedy; LLM only when no deterministic means is effective.** Deterministic SQL (entity surface match, heideltime `timex_value` match, transitive derivation) resolves the fact whenever possible; the LLM `EvidenceSupportSignature` judge runs only on the concept/relation branch and only when deterministic matching is insufficient.

3. **Hardwired exclude list** — fact names like `decision_chapter_tokens`, `decision_chapter_sentences` are skipped entirely (no evidence row, no status churn) via `settings.EVIDENCE_EXCLUDED_FACT_NAMES`.

4. **Derivation is read from lineage, not YAML.** Predecessor facts come from the authoritative join (`source_type='fact'`, `description='fact:<name>'`):

```sql

SELECT fsl.fact_id, f.name AS fact_name, fsl.source_id, sr.source_type,

sr.description, sr.source_fact_id

FROM celex_facts.fact_source_link fsl

JOIN celex_facts.fact_source_registry sr ON sr.source_id = fsl.source_id

JOIN celex_facts.facts f ON f.id = fsl.fact_id

WHERE sr.source_type = 'fact' AND fsl.fact_id = :fact_id;

```

  

Decisions confirmed: persistence = module + `fact_evidence` + status column + repository; routing = deterministic strategy router with LLM only on the branch that needs it; evidence source = reuse `heideltime_extractions` + `company_canonical2*` / FTS (no new normalized tables).

  

### Fact classes that must NOT be flagged as failures

- **Human-entered** (`name LIKE 'human_index_%'` / `created_by='human'`) → status `manual_approved`; one evidence row with `chunk_id NULL`, no retrieval, no LLM.

- **Derived** (lineage shows only `source_type='fact'` predecessors, no chunk sources) → verify **transitively** from predecessors' `verification_status`; one evidence row with `chunk_id NULL` referencing the weakest predecessor. No LLM.

- **No-provenance** (software-version etc.) → status `unverified`; one evidence row with `chunk_id NULL`. No LLM.

  

## Reused components (do NOT reinvent)

| Need                           | Reuse                                                                                                   | Path                                                                                                                                                                      |
| ------------------------------ | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| FTS / keyword candidates       | `ChunkRepository.fulltext_search_in_celex`, `keyword_search`                                            | [chunk_repository.py](src/pipelines/rag/pgvector/chunk_repository.py)                                                                                                     |
| Semantic candidates            | `ChunkRepository.semantic_search_in_celex`                                                              | same                                                                                                                                                                      |
| Resolve chunk text             | `ChunkRepository.get_chunk_by_id`                                                                       | same                                                                                                                                                                      |
| Provenance + predecessor facts | `LineageRepository.get_sources_for_fact` / `get_source` (`source_type`, `source_fact_id`, `trust_rank`) | [lineage_repository.py:505](src/pipelines/rag/pgvector/lineage_repository.py#L505)                                                                                        |
| Fact loading                   | `FactRepository.select_fact4celex` / `select_facts4celex`                                               | [fact_repository.py](src/pipelines/rag/pgvector/fact_repository.py)                                                                                                       |
| Temporal signal                | `heideltime_extractions` via `CelexHeidelTimeExtraction` (`timex_value`, `timex_text`, `fk_chunk_id`)   | [models.py:216](src/pipelines/rag/pgvector/models.py#L216)                                                                                                                |
| Entity aliases                 | `company_canonical2alias`, `company_canonical2celex_id`                                                 | DDL                                                                                                                                                                       |
| Embedding + cache              | `retrieve_qwen3_embedding`, `QueryEmbeddingRepository.get_or_cache`                                     | [qwen3_embedding.py](src/pipelines/rag/pgvector/embeddings/qwen3_embedding.py), [query_embedding_repository.py](src/pipelines/rag/pgvector/query_embedding_repository.py) |
| **LLM-judge gate template**    | `KnowledgeIngestor._judge_or_reject` (threshold + exception-safe default)                               | [knowledge_ingestor.py:308](src/pipelines/rag/pgvector/knowledge_ingestor.py#L308)                                                                                        |
| DSPy module shape              | `EnumerationFactModule` (Provisioner + forward dispatch)                                                | [enumeration_fact_module.py](src/pipelines/agent_dspy/enumeration_fact_module.py)                                                                                         |
| **Backfill batch pattern**     | `heideltime_source_register_fisher.py` (`--dry-run`/`--celex-id`, idempotent, stats)                    | [heideltime_source_register_fisher.py](src/pipelines/heideltime/heideltime_source_register_fisher.py)                                                                     |

  

## Files to create / modify

  

### 1. NEW `src/pipelines/agent_dspy/evidence_verifier_module.py`

VOs + one DSPy signature + `EvidenceVerifierModule` (mirrors `enumeration_fact_module.py`).

  

**Enums / VOs**

- `VerificationStrategy` (StrEnum): `excluded`, `trusted_human`, `derived`, `deterministic_entity`, `deterministic_temporal`, `llm_support`, `no_provenance`.

- `VerificationStatus` (StrEnum): `verified_supported`, `weak_support`, `insufficient_evidence`, `contradicted`, `missing_required_entity`, `missing_required_time`, `manual_approved`, `unverified`, `manual_rejected`.

- `CandidateEvidence` — `chunk_id`, `doc_pos`, `chunk_text`, per-signal scores, `final_score`, `candidate_source`.

- `FactVerificationResult` — `fact_id`, `celex_id`, `fact_name`, `strategy`, `status`, `best_chunk_id` (nullable), `final_score`, `support_label`, `evidence_quote`, `missing_elements`, `candidates`.

  

**Signature (used only by the `llm_support` branch)**

- `EvidenceSupportSignature` → `EvidenceSupportResult` (discussion §14). Inputs: `fact_statement`, `required_elements`, `chunk_text`. Output: `label` (`supports|contradicts|insufficient|partially_supports|ambiguous`), `support_score`, `evidence_quote` (exact substring or null), `supported_elements`, `missing_elements`, `reason`. **Hard rule: no `supports` without an exact quote.** Required elements come from the fact's `value_json` (entities) + a module concept dictionary (leniency/infringement synonyms) — no separate LLM slot classifier.

  

**`EvidenceVerifierModule(dspy.Module)`** — Provisioner; inject `chunk_repo`, `lineage_repo`, `fact_repo`, `evidence_repo`, `embed_fn`, optional `query_embedding_repo`. `forward(celex_id, fact_name)`:

  

1. **Exclude gate**: `fact_name in settings.EVIDENCE_EXCLUDED_FACT_NAMES` → return early, **no row written**, strategy `excluded`.

2. **Load** fact via `fact_repo.select_fact4celex`.

3. **Route strategy** — deterministic `_route_strategy(fact, sources)`:

- `human_` prefix / `created_by='human'` → `trusted_human`.

- name/`output_schema` ∈ concept/relation class (infringement*, leniency*, exemption*, dominance, gravity, remedies, recidivism) → `llm_support`.

- name/schema ∈ entity class (companies*, court_list*, market_geographical, products*, NACE*, authorities*, law_article*, companies_country) → `deterministic_entity`.

- name/schema ∈ temporal class (`*decision_date*`, `*period*`, `heideltime_*`, `timeline`) → `deterministic_temporal`.

- else only `source_type='fact'` predecessors present → `derived`.

- else → `no_provenance`.

4. **Dispatch** (each path ALWAYS produces one persisted evidence row + status — DoD #1):

- `_verify_trusted_human` → `manual_approved`, `chunk_id NULL`.

- `_verify_derived` → `lineage_repo`-driven predecessor query (the DoD #4 SQL via a new `get_predecessor_facts(fact_id)` helper); aggregate predecessor `verification_status` (weakest wins: any unverified/insufficient ⇒ `weak_support`/`insufficient_evidence`, all verified ⇒ `verified_supported`); `chunk_id NULL`, `missing_elements` lists weak predecessors. No LLM.

- `_verify_entity_deterministic` → for each extracted surface in `value_json` (alias-expanded), find a provenance/keyword chunk containing it (`keyword_search`/`fulltext_search_in_celex`). All present in one chunk → `verified_supported` (quote = snippet); partial → `weak_support`; none → `missing_required_entity` (still writes the best partial/empty row). No LLM.

- `_verify_temporal_deterministic` → join fact date(s) to `heideltime_extractions` on exact `timex_value` (+ overlap secondary) within `celex_id`; matched in a provenance chunk → `verified_supported` (quote = `timex_text` sentence); else `missing_required_time`. No LLM.

- `_verify_llm_support` → Slick Pipe: candidate gen (provenance + FTS/keyword + semantic-embed + temporal); a **deterministic concept-dictionary keyword gate runs first** — only candidates passing it (entity + concept term present) go to the judge; weighted scoring; `EvidenceSupportSignature` over top-N ranked candidates with the `_judge_or_reject` discipline (try/except → log + skip). Accept only `label=="supports"` + exact quote + `support_score ≥ settings.EVIDENCE_SUPPORT_MIN_SCORE`; below → `weak_support`; `contradicts` → `contradicted`; none → `insufficient_evidence`. Always writes the best candidate row even on failure.

- `_verify_no_provenance` → `unverified`, `chunk_id NULL`.

5. **Persist**: `evidence_repo.upsert_evidence(...)` + `set_verification_status(...)`. Return `FactVerificationResult`.

  

Module constants: weight profiles, concept dictionary, entity/temporal/concept name classifiers, top-N. Thresholds + exclude list in `settings.py`.

  

### 2. NEW `src/pipelines/rag/pgvector/fact_evidence_repository.py`

`FactEvidenceRepository` (Provisioner):

- `upsert_evidence(fact_id, chunk_id|None, support_label, final_score, sub_scores, evidence_quote, missing_elements)` — for `chunk_id` set: `INSERT … ON CONFLICT (fact_id, chunk_id) DO UPDATE`; for `chunk_id NULL` (summary row): delete-then-insert the fact's null-chunk row in one tx. Guarantees ≥1 row per fact (DoD #1).

- `set_verification_status(fact_id, status, score=None)` — updates `facts.verification_status` (+ score, timestamp).

- `get_predecessor_facts(fact_id)` — the DoD #4 lineage join, returns predecessor `(fact_id, name, verification_status)`.

- `get_evidence_for_fact(fact_id)`, `list_unverified(celex_id=None)` — monitoring.

  

### 3. NEW `src/pipelines/rag/pgvector/migrate_fact_evidence.sql`

- `CREATE TYPE celex_facts.verification_status_enum AS ENUM (...)`.

- `ALTER TABLE celex_facts.facts ADD COLUMN verification_status … DEFAULT 'unverified', ADD verification_score numeric, ADD verification_updated_at timestamptz`.

- `CREATE TABLE celex_facts.fact_evidence` — surrogate `id uuid PK DEFAULT gen_random_uuid()`, `fact_id uuid REFERENCES facts(id) ON DELETE CASCADE`, `chunk_id uuid NULL` (FK type matches `pg_embedding_qwen.id`; NULL for human/derived/no-provenance), `support_label text`, `final_score numeric`, five sub-score numerics, `evidence_quote text`, `missing_elements jsonb`, `verified_at timestamptz default now()`. Partial unique index `(fact_id, chunk_id) WHERE chunk_id IS NOT NULL`; partial unique index `(fact_id) WHERE chunk_id IS NULL` (one summary row per fact). Index on `fact_id`.

  

### 4. MODIFY `src/pipelines/rag/pgvector/models.py`

Add `FactEvidence(Base)` ORM + `verification_status` column on `Facts` ([models.py:47](src/pipelines/rag/pgvector/models.py#L47)).

  

### 5. MODIFY `src/config/settings.py`

Add `EVIDENCE_SUPPORT_MIN_SCORE`, `EVIDENCE_WEAK_MIN_SCORE`, `EVIDENCE_CANDIDATE_LIMIT`, `EVIDENCE_JUDGE_TOP_N`, and `EVIDENCE_EXCLUDED_FACT_NAMES` (default incl. `decision_chapter_tokens`, `decision_chapter_sentences`), env-overridable.

  

### 6. NEW `src/batch/Main_verify_facts_backfill.py`

Modeled on `heideltime_source_register_fisher.py`: iterate all facts (`--celex-id`, `--dry-run`), build `EvidenceVerifierModule`, call `forward` per fact, persist, log **per-strategy + per-status** stats. Idempotent. Skips excluded names. Guarantees every processed fact has an evidence row.

  

### 7. NEW tests

- `src/pipelines/agent_dspy/tests/test_evidence_verifier_module.py` — fake repos + fake judge (`SimpleNamespace(output=...)`). Cover: routing (`human_index_year`→trusted, `decision_chapter_tokens`→excluded/no-row, `leniency_granted`→llm, `court_list`→entity, `eu_commisssion_decision_date`→temporal, derived-only→transitive); **entity gate concludes without any LLM call**; **every non-excluded path writes exactly one summary or best-candidate row** (DoD #1); `supports`-without-quote downgrade; judge exception → run completes.

- `src/pipelines/rag/pgvector/tests/test_fact_evidence_repository.py` — chunk upsert idempotency, null-chunk single-summary-row guarantee, status update, predecessor query.

  

## Verification

1. `ruff check` on new modules + batch — clean.

2. `.venv/bin/python -m unittest src.pipelines.agent_dspy.tests.test_evidence_verifier_module src.pipelines.rag.pgvector.tests.test_fact_evidence_repository -v` (mocked boundaries).

3. Apply migration on scratch DB; confirm enum, columns, table, partial indexes.

4. `PYTHONPATH=src .venv/bin/python src/batch/Main_verify_facts_backfill.py --celex-id CELEX-31982D0123 --dry-run` then real run; assert: every fact for that CELEX has exactly one summary/best `fact_evidence` row + non-null status; `human_index_*`→`manual_approved` (no LLM); `decision_chapter_tokens` has no row; `court_list`→SQL entity match; `leniency_granted` quote is an exact substring of its chunk; derived `leniency_rejected` status aggregates its predecessors.

  

## Out of scope

`fact_verification_runs`, `fact_alerts`, manual-review queue (discussion §2/§8/§13). `FactVerificationResult.candidates` lets that layer be added later without API change.


## Definition-of-done coverage

1. **Complete ledger** — every non-excluded path produces a row; summary rows carry `chunk_id NULL`. ✅
2. **LLM only when needed** — only the concept branch reaches the judge, behind a deterministic pre-gate. ✅
3. **Hardwired excludes** — `settings.EVIDENCE_EXCLUDED_FACT_NAMES` short-circuits with no write. ✅
4. **Derivation from lineage** — `get_predecessor_facts` uses your exact `fact_source_link`+`fact_source_registry` join. ✅

## What I could not run

The two end-to-end verification steps from the plan need a live DB + Ollama, which I didn't execute: applying `migrate_fact_evidence.sql` to a database, and the real backfill run against `CELEX-31982D0123`. When you're ready, the migration is `psql -f src/pipelines/rag/pgvector/migrate_fact_evidence.sql` followed by `PYTHONPATH=src .venv/bin/python src/batch/Main_verify_facts_backfill.py --celex-id CELEX-31982D0123 --dry-run`.

One design note worth flagging: I kept entity matching to plain case-insensitive substring search (`keyword_search`) rather than wiring in `company_canonical2alias` expansion — it keeps the deterministic path fast and dependency-light, but a company cited only under an alias not present verbatim in the chunk will land as `weak_support` rather than `verified_supported`. Easy to add alias expansion later if the dry-run surfaces that.

===
ChatGPT v1

given pgvector with a table document chunks and their embeddings and a fact table with retrieved conclusions from a set of chunks, how to verify the fact with the most significant chunk? There are the following cases: 1) company, product names etc where the relevant chunk can be retrieved by key word search 2) concepts such as infringement, leniency where a semantic search could revalidate the fact 3) dates and periods: with heideltime extracted date, events and periods relate to a fact  
  
The best design is a **hybrid evidence verifier**:

fact → candidate chunks → score by mandatory elements → verify support → store best evidence

Do **not** choose the “most significant chunk” by embedding similarity alone. A chunk is significant only if it supports the fact’s required slots:

subject / entity

predicate / concept

object

date or period

source proximity / provenance

For your three cases, use different retrieval signals, then combine them.

---

**1. Suggested data model**

Keep your existing document_chunks and facts, but add structured evidence tables.

-- Original chunks

CREATE TABLE document_chunks (

    id              BIGSERIAL PRIMARY KEY,

    document_id     BIGINT NOT NULL,

    chunk_index     INT NOT NULL,

    page_start      INT,

    page_end        INT,

    content         TEXT NOT NULL,

    embedding       vector(1536),

    -- maintain these by generated column or trigger

    fts_simple      tsvector,

    fts_english     tsvector

);

-- Extracted facts / conclusions

CREATE TABLE facts (

    id              BIGSERIAL PRIMARY KEY,

    statement       TEXT NOT NULL,

    fact_type       TEXT,         -- entity_fact, concept_fact, temporal_fact, mixed

    embedding       vector(1536),

    extraction_confidence NUMERIC,

    created_at      TIMESTAMPTZ DEFAULT now()

);

-- Original provenance from extraction

CREATE TABLE fact_source_chunks (

    fact_id         BIGINT REFERENCES facts(id),

    chunk_id        BIGINT REFERENCES document_chunks(id),

    extraction_role TEXT,         -- source, context, neighbor, inferred

    PRIMARY KEY (fact_id, chunk_id)

);

-- Canonical entities linked to facts

CREATE TABLE fact_entities (

    fact_id         BIGINT REFERENCES facts(id),

    canonical_id    TEXT,

    surface_text    TEXT,

    entity_type     TEXT,         -- company, product, person, authority, etc.

    required        BOOLEAN DEFAULT true

);

-- Canonical entities detected in chunks

CREATE TABLE chunk_entities (

    chunk_id        BIGINT REFERENCES document_chunks(id),

    canonical_id    TEXT,

    surface_text    TEXT,

    entity_type     TEXT,

    char_start      INT,

    char_end        INT

);

-- Temporal expressions from chunks, normalized from HeidelTime/TIMEX3

CREATE TABLE chunk_timex (

    id              BIGSERIAL PRIMARY KEY,

    chunk_id        BIGINT REFERENCES document_chunks(id),

    timex_type      TEXT,         -- DATE, TIME, DURATION, SET

    timex_value     TEXT,         -- e.g. 2021-05-03, 2021, 2021-Q2, P3M

    date_range      daterange,    -- normalized searchable range

    surface_text    TEXT,

    char_start      INT,

    char_end        INT

);

-- Temporal expressions linked to facts

CREATE TABLE fact_timex (

    id              BIGSERIAL PRIMARY KEY,

    fact_id         BIGINT REFERENCES facts(id),

    role            TEXT,         -- event_date, effective_period, filing_date, infringement_period, etc.

    timex_value     TEXT,

    date_range      daterange

);

-- Final verification result

CREATE TABLE fact_evidence (

    fact_id         BIGINT REFERENCES facts(id),

    chunk_id        BIGINT REFERENCES document_chunks(id),

    support_label   TEXT,         -- supports, contradicts, insufficient

    final_score     NUMERIC,

    entity_score    NUMERIC,

    keyword_score   NUMERIC,

    semantic_score  NUMERIC,

    temporal_score  NUMERIC,

    provenance_score NUMERIC,

    evidence_quote  TEXT,

    missing_elements JSONB,

    verified_at     TIMESTAMPTZ DEFAULT now(),

    PRIMARY KEY (fact_id, chunk_id)

);

The important addition is fact_evidence. Your fact table should not merely say “this fact came from these chunks.” It should say:

This fact is supported by chunk X with score Y and evidence quote Z.

---

**2. Indexes you probably want**

Use separate search paths for names, concepts, and vectors.

CREATE INDEX chunks_embedding_hnsw_idx

ON document_chunks

USING hnsw (embedding vector_cosine_ops);

CREATE INDEX chunks_fts_simple_idx

ON document_chunks

USING gin (fts_simple);

CREATE INDEX chunks_fts_english_idx

ON document_chunks

USING gin (fts_english);

CREATE INDEX chunk_entities_canonical_idx

ON chunk_entities (canonical_id, chunk_id);

CREATE INDEX chunk_timex_range_idx

ON chunk_timex

USING gist (date_range);

pgvector supports cosine distance via <=>, and cosine similarity is commonly computed as 1 - cosine_distance; HNSW and IVFFlat are available for approximate nearest-neighbor search, while exact search gives perfect recall by default. For verification or audit jobs, prefer exact search or a higher-recall HNSW setting; for interactive retrieval, HNSW is usually fine. ([GitHub](https://github.com/pgvector/pgvector "GitHub - pgvector/pgvector: Open-source vector similarity search for Postgres · GitHub"))

PostgreSQL full-text search gives you websearch_to_tsquery, phraseto_tsquery, ts_rank_cd, and ts_headline; ts_rank_cd considers proximity of matching terms, and ts_headline can produce relevant snippets for display. ([PostgreSQL](https://www.postgresql.org/docs/current/textsearch-controls.html "PostgreSQL: Documentation: 18: 12.3. Controlling Text Search"))

For company/product name variation, PostgreSQL’s pg_trgm extension can help with fuzzy text similarity and index-backed approximate string matching. ([PostgreSQL](https://www.postgresql.org/docs/current/pgtrgm.html?utm_source=chatgpt.com "PostgreSQL: Documentation: 18: F.35. pg_trgm — support for similarity ..."))

---

**3. Define “most significant chunk”**

Use this definition:

The most significant chunk is the chunk with the highest probability of supporting the fact, after checking mandatory entity, concept, and temporal coverage.

So:

best_chunk = argmax P(chunk supports fact)

not:

best_chunk = nearest embedding

A good scoring formula:

final_score =

  0.25 * provenance_score

+ 0.25 * entity_score

+ 0.20 * semantic_score

+ 0.20 * temporal_score

+ 0.10 * keyword_score

But make the weights depend on the fact type:

entity-heavy fact:

  entity_score and keyword_score dominate

concept-heavy fact:

  semantic_score and verifier_score dominate

date/period fact:

  temporal_score + entity_score + event/predicate match dominate

For production, I would use database scoring to get the **top 10–20 candidates**, then run a final **support verifier** over those candidates.

---

**4. Candidate generation query**

This is the core pattern.

WITH f AS (

    SELECT *

    FROM facts

    WHERE id = :fact_id

),

required_entities AS (

    SELECT canonical_id

    FROM fact_entities

    WHERE fact_id = :fact_id

      AND required = true

),

entity_coverage AS (

    SELECT

        ce.chunk_id,

        COUNT(DISTINCT ce.canonical_id)::numeric

            / NULLIF((SELECT COUNT(*) FROM required_entities), 0) AS entity_score

    FROM chunk_entities ce

    JOIN required_entities re

      ON re.canonical_id = ce.canonical_id

    GROUP BY ce.chunk_id

),

provenance_candidates AS (

    SELECT

        chunk_id,

        1.0::numeric AS provenance_score

    FROM fact_source_chunks

    WHERE fact_id = :fact_id

),

keyword_candidates AS (

    SELECT

        [c.id](http://c.id/) AS chunk_id,

        ts_rank_cd(

            c.fts_simple,

            websearch_to_tsquery('simple', :entity_keyword_query),

            32

        ) AS keyword_score

    FROM document_chunks c

    WHERE c.fts_simple @@ websearch_to_tsquery('simple', :entity_keyword_query)

    ORDER BY keyword_score DESC

    LIMIT 100

),

semantic_candidates AS (

    SELECT

        [c.id](http://c.id/) AS chunk_id,

        1 - (c.embedding <=> f.embedding) AS semantic_score

    FROM document_chunks c

    CROSS JOIN f

    ORDER BY c.embedding <=> f.embedding

    LIMIT 100

),

temporal_candidates AS (

    SELECT

        ct.chunk_id,

        MAX(

            CASE

                WHEN ct.timex_value = ft.timex_value THEN 1.0

                WHEN ct.date_range && ft.date_range THEN 0.75

                ELSE 0.0

            END

        ) AS temporal_score

    FROM fact_timex ft

    JOIN chunk_timex ct

      ON ct.timex_value = ft.timex_value

      OR ct.date_range && ft.date_range

    WHERE ft.fact_id = :fact_id

    GROUP BY ct.chunk_id

),

candidate_ids AS (

    SELECT chunk_id FROM provenance_candidates

    UNION

    SELECT chunk_id FROM keyword_candidates

    UNION

    SELECT chunk_id FROM semantic_candidates

    UNION

    SELECT chunk_id FROM temporal_candidates

),

scored AS (

    SELECT

        [c.id](http://c.id/) AS chunk_id,

        c.content,

        COALESCE(ec.entity_score, 0) AS entity_score,

        COALESCE(kc.keyword_score, 0) AS keyword_score,

        COALESCE(sc.semantic_score, 0) AS semantic_score,

        COALESCE(tc.temporal_score, 0) AS temporal_score,

        COALESCE(pc.provenance_score, 0) AS provenance_score,

        (

            0.25 * COALESCE(pc.provenance_score, 0) +

            0.25 * COALESCE(ec.entity_score, 0) +

            0.20 * COALESCE(sc.semantic_score, 0) +

            0.20 * COALESCE(tc.temporal_score, 0) +

            0.10 * COALESCE(kc.keyword_score, 0)

        ) AS final_score

    FROM candidate_ids ci

    JOIN document_chunks c

      ON [c.id](http://c.id/) = ci.chunk_id

    LEFT JOIN entity_coverage ec

      ON ec.chunk_id = [c.id](http://c.id/)

    LEFT JOIN keyword_candidates kc

      ON kc.chunk_id = [c.id](http://c.id/)

    LEFT JOIN semantic_candidates sc

      ON sc.chunk_id = [c.id](http://c.id/)

    LEFT JOIN temporal_candidates tc

      ON tc.chunk_id = [c.id](http://c.id/)

    LEFT JOIN provenance_candidates pc

      ON pc.chunk_id = [c.id](http://c.id/)

)

SELECT *

FROM scored

WHERE

    -- hard gate: if the fact has required entities, chunk must contain all of them

    (

        (SELECT COUNT(*) FROM required_entities) = 0

        OR entity_score = 1.0

    )

ORDER BY final_score DESC

LIMIT 20;

This gives you candidate evidence. Then you still need a final verifier.

---

**5. Final verifier: do not skip this step**

After the SQL candidate step, run the top chunks through a verifier:

Input:

  fact.statement

  chunk.content

  extracted entities

  extracted dates/periods

Output:

  supports | contradicts | insufficient

  evidence_quote

  missing_elements

Example verifier instruction:

Determine whether the chunk directly supports the fact.

Fact:

"{fact_statement}"

Required entities:

{entities}

Required temporal scope:

{dates_or_periods}

Chunk:

"{chunk_text}"

Return JSON:

{

  "label": "supports | contradicts | insufficient",

  "evidence_quote": "...",

  "supported_elements": ["..."],

  "missing_elements": ["..."],

  "reason": "..."

}

Only show chunks where:

support_label = supports

entity coverage is complete

temporal coverage is complete when time is material

no stronger contradiction is found

This is the part that turns “retrieval” into “verification.”

---

**Case-specific handling**

**Case 1: company names, product names, authority names**

For company/product names, use **canonical entity matching first**, keyword search second, embeddings third.

Proper nouns are often poorly handled by pure embedding search. For example, two chunks about different companies in the same industry may be semantically similar, but only one contains the right company.

Use this hierarchy:

1. chunk_entities canonical_id match

2. exact phrase search / FTS

3. trigram fallback for OCR errors, aliases, spelling variants

4. semantic score only as secondary signal

Example:

SELECT

    [c.id](http://c.id/),

    ts_rank_cd(

        c.fts_simple,

        websearch_to_tsquery('simple', '"Acme GmbH" "Product X"'),

        32

    ) AS keyword_score,

    ts_headline(

        'simple',

        c.content,

        websearch_to_tsquery('simple', '"Acme GmbH" "Product X"'),

        'MaxFragments=2, MaxWords=30, MinWords=8'

    ) AS snippet

FROM document_chunks c

WHERE c.fts_simple @@ websearch_to_tsquery('simple', '"Acme GmbH" "Product X"')

ORDER BY keyword_score DESC

LIMIT 20;

For names, I would use simple text search configuration more often than english, because you usually do not want stemming or stop-word behavior to distort legal names, product names, IDs, or abbreviations.

Add hard gates:

company fact → require company entity

product fact → require product entity

company-product fact → require both

A chunk that does not contain the required entity should usually not be cited, even if its embedding score is high.

---

**Case 2: concepts such as infringement, leniency, liability, dominance**

For legal or regulatory concepts, use **semantic retrieval plus concept normalization**.

Example:

fact.statement:

"Company A received leniency for its participation in the infringement."

concepts:

- leniency

- infringement

- participation

- undertaking / company

Semantic search:

SELECT

    [c.id](http://c.id/),

    1 - (c.embedding <=> :fact_embedding::vector) AS semantic_score,

    c.content

FROM document_chunks c

ORDER BY c.embedding <=> :fact_embedding::vector

LIMIT 50;

But pure semantic search is still not enough. You should also maintain a concept dictionary or taxonomy:

infringement:

  infringement

  violation

  breach

  cartel conduct

  anti-competitive agreement

  prohibited conduct

leniency:

  leniency

  immunity

  reduction of fine

  cooperation notice

  leniency applicant

Then retrieve with both:

semantic query:

  "Company A received leniency for the infringement"

keyword/concept query:

  "Company A" AND ("leniency" OR "immunity" OR "reduction of fine")

For this case, the final verifier is essential. A chunk may mention “leniency” and “infringement” but still not say that the named company received leniency.

Good support requires:

entity match:

  Company A

concept match:

  leniency / immunity / fine reduction

relation match:

  Company A is the recipient, not merely another party

event match:

  same infringement / same proceeding

---

**Case 3: dates, events, and periods with HeidelTime**

For temporal facts, do not verify using embeddings alone.

HeidelTime extracts temporal expressions and normalizes them according to TIMEX3, which is exactly what you want for searchable date evidence. ([GitHub](https://github.com/HeidelTime/heideltime "GitHub - HeidelTime/heideltime: A multilingual, cross-domain temporal tagger developed at the Database Systems Research Group at Heidelberg University. · GitHub"))

Store each extracted temporal expression as both:

surface form:

  "in 2019", "between March and June 2020", "for three years"

normalized value:

  2019, 2020-03/2020-06, P3Y

searchable range:

  [2019-01-01, 2020-01-01)

  [2020-03-01, 2020-07-01)

Example normalization:

"2019"         → daterange('2019-01-01', '2020-01-01', '[)')

"Q2 2021"     → daterange('2021-04-01', '2021-07-01', '[)')

"May 5, 2020" → daterange('2020-05-05', '2020-05-06', '[)')

Temporal matching query:

SELECT

    ct.chunk_id,

    MAX(

        CASE

            WHEN ct.timex_value = ft.timex_value THEN 1.0

            WHEN ct.date_range = ft.date_range THEN 0.95

            WHEN ct.date_range && ft.date_range THEN 0.75

            WHEN ct.date_range @> lower(ft.date_range) THEN 0.60

            ELSE 0.0

        END

    ) AS temporal_score

FROM fact_timex ft

JOIN chunk_timex ct

  ON ct.timex_value = ft.timex_value

  OR ct.date_range && ft.date_range

WHERE ft.fact_id = :fact_id

GROUP BY ct.chunk_id

ORDER BY temporal_score DESC;

The key issue is that dates are ambiguous. A chunk may contain the right date but refer to the wrong event.

So store or infer an **event-time relation**:

entity: Company A

event: submitted leniency application

time: 2019-05-03

entity: Company A

event: participated in infringement

period: 2016-2019

A temporal fact should be verified by the triple:

entity + event/predicate + time/period

not by:

date alone

For example, this is weak:

Chunk contains: Company A, infringement, 2019

This is strong:

Chunk says: "Company A participated in the infringement from 2016 until 2019."

For date/period facts, your verifier should ask:

Does the chunk attach this date or period to the same event described in the fact?

---

**6. Recommended evidence decision logic**

Use this acceptance logic:

For each fact:

  1. Generate candidates from provenance, keyword, semantic, and temporal retrieval.

  2. Apply hard gates:

       required entities must match

       required temporal scope must match when relevant

  3. Rank candidates with weighted score.

  4. Run verifier on top N chunks.

  5. Select the highest-scoring chunk labelled "supports".

  6. If no chunk supports the fact, mark fact as unverified.

  7. If a chunk contradicts the fact, mark conflict.

Pseudo-code:

def verify_fact(fact_id):

    candidates = retrieve_candidates(fact_id)

    gated = [

        c for c in candidates

        if required_entities_covered(fact_id, c)

        and required_time_covered_if_material(fact_id, c)

    ]

    ranked = rank_candidates(fact_id, gated)

    for chunk in ranked[:20]:

        result = verifier(fact_id, [chunk.id](http://chunk.id/))

        if result.label == "supports":

            store_fact_evidence(

                fact_id=fact_id,

                chunk_id=[chunk.id](http://chunk.id/),

                support_label="supports",

                final_score=chunk.final_score,

               evidence_quote=result.evidence_quote,

                missing_elements=result.missing_elements

            )

            return chunk

    mark_fact_unverified(fact_id)

    return None

---

**7. Important caveat: sometimes one chunk is not enough**

Because your facts are “retrieved conclusions from a set of chunks,” some facts may be multi-hop.

Example:

Chunk 1: Company A is a subsidiary of Group B.

Chunk 2: Group B received leniency.

Fact: Company A benefited from leniency through Group B.

No single chunk fully supports the fact.

In that case, do not force one “most significant chunk.” Store evidence as:

primary evidence chunk

supporting context chunk

inference step

Schema extension:

CREATE TABLE fact_evidence_set (

    id              BIGSERIAL PRIMARY KEY,

    fact_id         BIGINT REFERENCES facts(id),

    support_label   TEXT,

    final_score     NUMERIC

);

CREATE TABLE fact_evidence_set_chunks (

    evidence_set_id BIGINT REFERENCES fact_evidence_set(id),

    chunk_id        BIGINT REFERENCES document_chunks(id),

    evidence_role   TEXT, -- primary, entity_support, temporal_support, inference_support

    evidence_quote  TEXT

);

Then your UI can show:

Primary source

Supporting source

instead of pretending one chunk proves everything.

---

**Practical recommendation**

For your setup, I would implement this order:

1. Keep fact → original source chunks.

2. Add fact_entities, chunk_entities, fact_timex, chunk_timex.

3. Generate candidate chunks from:

   - original provenance chunks

   - entity keyword search

   - semantic pgvector search

   - temporal range search

4. Score candidates by coverage.

5. Run a final support verifier on the top 10–20.

6. Store the result in fact_evidence.

7. Only show chunks where support_label = supports.

The most important rule:

A referenced chunk should be shown only if it directly supports the final answer claim, not merely because it was retrieved or linked during extraction.

should the case specific handling go by a LLM planner or  with kind of hardwiring fact types to verifier logic?  
  
===  
  
Use a **hybrid**, but make the **verifier logic mostly hardwired**.

The LLM should help classify or structure the fact; it should **not freely decide the verification strategy at runtime** unless the case is ambiguous. For evidence verification, you want repeatability, auditability, and debuggability.

The pattern I would use is:

LLM or extractor produces structured fact metadata

        ↓

deterministic router selects verifier modules

        ↓

SQL retrieval generates candidates

        ↓

hard gates check entities / dates / required slots

        ↓

reranker or LLM support judge decides: supports / contradicts / insufficient

        ↓

store verified evidence

This is closer to production practice than a fully agentic “planner decides what to do” approach. Frameworks such as Haystack expose explicit conditional routers that route data through different pipeline paths based on declared conditions, which is the same architectural idea: use routing logic as part of the pipeline, not as an uncontrolled reasoning step. ([Haystack Documentation](https://docs.haystack.deepset.ai/docs/conditionalrouter "ConditionalRouter | Haystack Documentation")) OpenAI’s Structured Outputs are also useful here because the LLM can produce schema-conformant classification or extraction results, rather than free-form plans. ([OpenAI Developers](https://developers.openai.com/api/docs/guides/structured-outputs "Structured model outputs | OpenAI API"))

**Recommended answer**

Use **hardwired verifier modules**, with an **LLM classifier/planner only as a constrained front-end**.

So not this:

LLM planner:

  "I think I should do semantic search, then maybe keyword search, then dates..."

But this:

LLM or extractor:

{

  "fact_type": "mixed",

  "required_entities": ["Acme GmbH", "Product X"],

  "required_concepts": ["leniency", "infringement"],

  "required_temporal_roles": [

    {"role": "decision_date", "value": "2019"}

  ]

}

Then your system deterministically does:

entity verifier

concept verifier

temporal verifier

support verifier

That gives you flexibility without sacrificing control.

---

**Why I would not use a free LLM planner**

A free LLM planner is attractive at first because it can say things like:

This fact is about a company, a concept, and a date, so I should combine keyword, semantic, and temporal search.

But in production, that becomes hard to trust. The planner may choose different strategies for similar facts, skip a required entity, fail to check a date, or over-rely on semantic similarity. That is especially dangerous for legal, compliance, patent, regulatory, or contract RAG.

For fact verification, the important questions are deterministic:

Does the chunk contain the required entity?

Does it contain the required product?

Does it express the required concept?

Does the date attach to the same event?

Does the sentence actually support the fact?

Those should be explicit checks.

The LLM is useful for the last step:

Given this fact and this candidate chunk, does the chunk support, contradict, or fail to support the fact?

But candidate generation and mandatory checks should be deterministic as much as possible.

---

**The architecture I would implement**

**Step 1: Normalize the fact into a structured schema**

Every fact should be represented as slots, not only as a sentence.

Example fact:

Acme GmbH received leniency in 2019 for its participation in the infringement concerning Product X.

Structured form:

{

  "fact_id": 123,

  "statement": "Acme GmbH received leniency in 2019 for its participation in the infringement concerning Product X.",

  "fact_type": "mixed",

  "entities": [

    {

      "type": "company",

      "canonical_id": "company:acme_gmbh",

      "surface": "Acme GmbH",

      "required": true

    },

    {

      "type": "product",

      "canonical_id": "product:x",

      "surface": "Product X",

      "required": true

    }

  ],

  "concepts": [

    {

      "name": "leniency",

      "required": true

    },

    {

      "name": "infringement",

      "required": true

    }

  ],

  "temporal_constraints": [

    {

      "role": "decision_date",

      "value": "2019",

      "required": true

    }

  ]

}

You can produce this with deterministic extraction, an LLM with structured output, or a combination. If you use an LLM, make it emit a fixed schema with enums such as:

entity_fact

concept_fact

temporal_fact

mixed_fact

multi_hop_fact

unclear

Structured output is much safer than asking the LLM to produce an open-ended plan. OpenAI’s docs distinguish structured schema output from function/tool calling and note that Structured Outputs are designed for schema adherence. ([OpenAI Developers](https://developers.openai.com/api/docs/guides/structured-outputs "Structured model outputs | OpenAI API"))

---

**Step 2: Deterministic router activates verifier modules**

Instead of one fact type choosing one route, allow multiple modules.

def route_fact_verifiers(fact):

    verifiers = []

    if fact.entities:

        verifiers.append("entity_verifier")

    if fact.concepts:

        verifiers.append("concept_verifier")

    if fact.temporal_constraints:

        verifiers.append("temporal_verifier")

    if fact.requires_multiple_sources:

        verifiers.append("multi_hop_verifier")

    verifiers.append("support_verifier")

    return verifiers

A fact can be:

entity-only

concept-only

temporal-only

entity + concept

entity + temporal

concept + temporal

entity + concept + temporal

multi-hop

So I would avoid a single rigid fact_type. Use fact_type as a hint, but route based on the actual required slots.

---

**Step 3: Case-specific logic should be hardwired**

**Case 1: company/product names**

Use deterministic logic first:

canonical entity match

exact phrase match

alias match

trigram fallback

then semantic similarity

Hard gate:

If company is required, candidate chunk must contain the canonical company or an accepted alias.

Do not let semantic search override this. A semantically similar chunk about another company should not verify the fact.

---

**Case 2: concepts like infringement, leniency, dominance**

Use semantic search and concept dictionaries.

concept: leniency

terms:

  leniency

  immunity

  reduction of fine

  leniency application

  cooperation notice

For this case, the verifier should check relation, not just word presence.

Bad support:

The chunk mentions "Acme", "leniency", and "infringement" somewhere.

Good support:

The chunk states that Acme received leniency or immunity for the relevant infringement.

So here I would use:

semantic retrieval → concept keyword expansion → LLM/NLI support check

The support check can be LLM-based, but the required concept list should be explicit.

---

**Case 3: dates and periods**

Temporal verification should be hardwired.

Do not rely on vector similarity for dates. Use your HeidelTime output to normalize dates and periods into searchable values/ranges, then require that the date is attached to the right event.

For example:

Fact:

Acme participated in the infringement from 2016 to 2019.

Weak chunk:

Acme was mentioned in 2016. The infringement decision was issued in 2019.

Strong chunk:

Acme participated in the infringement between 2016 and 2019.

The temporal verifier should check:

entity match

event/predicate match

date or period match

role match

The most important part is role match.

For example, 2019 could mean:

decision date

filing date

infringement end date

leniency application date

settlement date

publication date

A chunk containing the right year is not enough.

---

**Best design: hard gates plus soft scoring**

I would separate **mandatory gates** from **ranking scores**.

**Hard gates**

These decide whether a chunk is even eligible.

Required company missing → reject

Required product missing → reject

Required date missing when date is material → reject or penalize heavily

Wrong temporal role → reject

Contradiction found → reject or flag

**Soft scores**

These rank eligible chunks.

entity_score

keyword_score

semantic_score

temporal_score

provenance_score

support_score

Example:

def verify_candidate(fact, chunk):

    gates = {

        "entities_ok": check_required_entities(fact, chunk),

        "temporal_ok": check_required_temporal_roles(fact, chunk),

        "concepts_ok": check_required_concepts(fact, chunk),

    }

    if not gates["entities_ok"]:

        return reject("missing_required_entity")

    if fact.has_required_temporal_constraint and not gates["temporal_ok"]:

        return reject("missing_or_wrong_temporal_evidence")

    support = llm_or_nli_support_check(fact.statement, chunk.content)

    if support.label != "supports":

        return reject(support.label)

    score = weighted_score(

        entity_score=entity_score(fact, chunk),

        concept_score=concept_score(fact, chunk),

        temporal_score=temporal_score(fact, chunk),

        semantic_score=semantic_score(fact, chunk),

        provenance_score=provenance_score(fact, chunk),

        support_score=support.score,

    )

    return accept(score=score, quote=support.evidence_quote)

This gives you predictable behavior.

---

**Where the LLM planner is useful**

Use an LLM planner/classifier only for things like:

What are the required entities?

Is this date a decision date, infringement period, or filing date?

Is this fact single-hop or multi-hop?

Which concepts are material?

Does this fact require exact keyword evidence, semantic evidence, temporal evidence, or all of them?

But force it to answer with a schema.

Example:

{

  "verification_profile": "entity_concept_temporal",

  "retrieval_modes": ["provenance", "keyword", "semantic", "temporal"],

  "hard_gates": [

    "required_entities_present",

    "required_temporal_role_present",

    "same_event_relation"

  ],

  "support_question": "Does the chunk state that Acme GmbH received leniency in 2019 for the Product X infringement?"

}

Then your code executes the plan only if it matches known allowed values.

So the LLM does **classification**, not orchestration.

---

**What I would avoid**

I would avoid this pattern:

Ask LLM:

"How should I verify this fact?"

Then execute whatever it says.

That is too variable.

I would also avoid this pattern:

fact_type = "concept"

therefore only semantic search

That is too rigid.

Many facts are mixed. For example:

"Acme received leniency in 2019 for the Product X infringement."

This is simultaneously:

entity fact

concept fact

temporal fact

event-relation fact

So the verifier should be compositional.

---

**Practical recommendation**

Implement this:

1. LLM/extractor creates structured fact metadata.

2. Deterministic router activates verifier modules.

3. Each verifier module retrieves and scores candidates.

4. Hard gates remove invalid chunks.

5. LLM/NLI support verifier checks the top candidates.

6. Store the best supporting chunk or evidence set.

In short:

Use hardwired verifier logic for correctness.

Use an LLM planner only as a constrained classifier.

Use an LLM support judge only after deterministic candidate generation.

That gives you the best balance: robust enough for production, flexible enough for messy facts, and explainable enough to debug when citations look wrong.

  
===  
my manual check of referenced chunks sometimes question accuracy the fact retrieved.

How to detect and alert failing verification?

Yes. When your manual review questions the accuracy of the retrieved fact, you should treat that as a **verification failure signal**, not merely a citation-ranking issue.

The right pattern is:

fact is extracted

→ candidate evidence is retrieved

→ candidate evidence is checked against required slots

→ support/contradiction verifier runs

→ fact receives a verification status

→ only verified facts are eligible for user answers/citations

→ failures are queued or alerted

The most important change: **facts should not be considered usable just because they were extracted or semantically retrieved.** They should be usable only if they have at least one verified evidence item.

---

**1. Add explicit verification states**

Every fact should have a status like this:

unverified

verified_supported

weak_support

insufficient_evidence

contradicted

missing_required_entity

missing_required_time

ambiguous_temporal_role

multi_hop_required

stale_evidence

manual_rejected

manual_approved

For example:

ALTER TABLE facts

ADD COLUMN verification_status TEXT DEFAULT 'unverified',

ADD COLUMN verification_score NUMERIC,

ADD COLUMN verification_updated_at TIMESTAMPTZ,

ADD COLUMN last_verification_run_id BIGINT;

Then enforce this rule in your answer pipeline:

Only facts with verification_status = 'verified_supported'

are allowed to support final answers.

For debugging, you can still inspect the others, but they should not be silently used as evidence.

---

**2. Store verification attempts, not just final evidence**

Create a table that records every verification run.

CREATE TABLE fact_verification_runs (

    id                  BIGSERIAL PRIMARY KEY,

    fact_id             BIGINT NOT NULL REFERENCES facts(id),

    verifier_version    TEXT NOT NULL,

    retriever_version   TEXT,

    chunker_version     TEXT,

    model_name          TEXT,

    started_at          TIMESTAMPTZ DEFAULT now(),

    completed_at        TIMESTAMPTZ,

    final_status        TEXT,

    final_score         NUMERIC

);

Then store candidate-level results:

CREATE TABLE fact_verification_candidates (

    id                    BIGSERIAL PRIMARY KEY,

    run_id                BIGINT NOT NULL REFERENCES fact_verification_runs(id),

    fact_id               BIGINT NOT NULL REFERENCES facts(id),

    chunk_id              BIGINT NOT NULL REFERENCES document_chunks(id),

    candidate_rank        INT,

    candidate_source      TEXT, -- provenance, keyword, semantic, temporal, hybrid

    entity_score          NUMERIC,

    concept_score         NUMERIC,

    temporal_score        NUMERIC,

    semantic_score        NUMERIC,

    keyword_score         NUMERIC,

    provenance_score      NUMERIC,

    support_label         TEXT, -- supports, contradicts, insufficient

    support_score         NUMERIC,

    evidence_quote        TEXT,

    evidence_char_start   INT,

    evidence_char_end     INT,

    missing_elements      JSONB,

    contradictions        JSONB,

    verifier_reason       TEXT,

    created_at            TIMESTAMPTZ DEFAULT now()

);

This gives you an audit trail. When manual review finds a bad fact, you can see whether the problem came from retrieval, entity matching, date handling, or the final support verifier.

---

**3. Use fail reasons, not only scores**

A single numeric score is not enough. You want actionable failure categories.

Example verifier output:

{

  "label": "insufficient",

  "support_score": 0.41,

  "failure_reasons": [

    "missing_required_product",

    "date_mentions_different_event"

  ],

  "supported_elements": [

    "company: Acme GmbH",

    "concept: leniency"

  ],

  "missing_elements": [

    "product: Product X",

    "temporal role: infringement period 2018-2020"

  ],

  "evidence_quote": null,

  "reason": "The chunk mentions Acme GmbH and leniency, but the date refers to the decision date, not the infringement period."

}

Use structured output for this rather than free-form text. OpenAI’s Structured Outputs are designed to make model responses conform to a supplied JSON schema, which is useful for this kind of verification pipeline where missing keys or invalid labels would be dangerous. ([OpenAI Developers](https://developers.openai.com/api/docs/guides/structured-outputs?utm_source=chatgpt.com "Structured model outputs | OpenAI API"))

---

**4. Recommended verification labels**

I would use this compact label set:

supports

contradicts

insufficient

partially_supports

ambiguous

Then map those to operational statuses:

supports + high score

    → verified_supported

supports + low score

    → weak_support

partially_supports

    → weak_support or multi_hop_required

insufficient

    → insufficient_evidence

contradicts

    → contradicted

ambiguous

    → manual_review_required

For final answer generation, I would normally allow only:

verified_supported

manual_approved

Everything else should be blocked, hedged, or reviewed.

---

**5. Define hard failure conditions**

Some failures should trigger immediate rejection before any LLM judge.

**Entity failure**

Fact requires company Acme GmbH.

Candidate chunk does not contain Acme GmbH or a canonical alias.

→ fail: missing_required_entity

This catches many false positives from semantic search.

**Product failure**

Fact is about Product X.

Chunk mentions Product Y or only the industry category.

→ fail: missing_required_product

**Temporal failure**

Fact says infringement period is 2018-2020.

Chunk contains 2020, but as decision date.

→ fail: wrong_temporal_role

**Relation failure**

Fact says Acme received leniency.

Chunk says another company received leniency.

→ fail: wrong_subject_relation

**Quote failure**

Verifier says "supports" but cannot return a precise quote.

→ fail or downgrade: no_evidence_quote

This one is very useful. If the system cannot quote the supporting sentence or paragraph, the evidence is often not strong enough.

---

**6. Verification logic by fact type**

Use different fail checks depending on the fact content.

**A. Entity-heavy facts**

Example:

Acme GmbH marketed Product X in Germany.

Required checks:

company present

product present

market/location present if material

relation expressed

Failure alerts:

missing_company

missing_product

wrong_company

wrong_product

relation_not_stated

**B. Concept-heavy facts**

Example:

Acme received leniency for its role in the infringement.

Required checks:

company present

concept present or paraphrased

relation present

same proceeding/event

Failure alerts:

concept_only_mentioned

wrong_recipient

same_terms_but_no_relation

semantic_match_without_support

This is a common RAG failure: the chunk contains all the right words but does not support the claim.

**C. Temporal facts**

Example:

The infringement lasted from 2016 to 2019.

Required checks:

date or period present

date normalized correctly

date attached to the correct event

date role matches the fact

Failure alerts:

date_missing

period_overlap_only

wrong_temporal_role

ambiguous_date_anchor

relative_date_unresolved

For date and period checks, use your HeidelTime/TIMEX output as a deterministic precondition. Do not let embedding similarity verify dates.

---

**7. Add a contradiction scan**

Do not only look for supporting chunks. Also search for contradicting chunks.

For each fact, generate a contradiction query using the same required slots:

same company

same product/concept

same event

different value/date/status

Examples:

Fact:

Acme received leniency.

Possible contradiction:

The Commission rejected Acme's leniency application.

Fact:

The infringement lasted until 2019.

Possible contradiction:

The infringement ended in 2017.

Store contradictions separately:

CREATE TABLE fact_contradictions (

    id                  BIGSERIAL PRIMARY KEY,

    fact_id             BIGINT REFERENCES facts(id),

    chunk_id            BIGINT REFERENCES document_chunks(id),

    contradiction_type  TEXT,

    contradiction_score NUMERIC,

    contradiction_quote TEXT,

    created_at          TIMESTAMPTZ DEFAULT now()

);

A simple policy:

If support exists and no contradiction exists:

    verified_supported

If support exists and contradiction exists:

    conflict_manual_review

If no support exists:

    insufficient_evidence

If contradiction exists and no support exists:

    contradicted

---

**8. Alert rules**

You should alert on both **individual fact failures** and **system-level degradation**.

**Individual fact alerts**

Create alerts when:

support_label = contradicts

support_score < threshold

no candidate chunk passes hard gates

required entity missing

required temporal role missing

verifier cannot produce evidence_quote

best supporting chunk changed after re-indexing

manual reviewer rejected fact

Example table:

CREATE TABLE fact_alerts (

    id              BIGSERIAL PRIMARY KEY,

    fact_id         BIGINT REFERENCES facts(id),

    alert_type      TEXT NOT NULL,

    severity        TEXT NOT NULL, -- info, warning, critical

    message         TEXT,

    payload         JSONB,

    resolved        BOOLEAN DEFAULT false,

    created_at      TIMESTAMPTZ DEFAULT now(),

    resolved_at     TIMESTAMPTZ

);

Example insert:

INSERT INTO fact_alerts (

    fact_id,

    alert_type,

    severity,

    message,

    payload

)

VALUES (

    :fact_id,

    'verification_failed',

    'critical',

    'Fact has no supporting chunk after verification.',

    jsonb_build_object(

        'missing_elements', :missing_elements,

        'best_candidate_score', :best_score,

        'verifier_version', :verifier_version

    )

);

---

**9. Batch monitoring queries**

Find facts with no verified evidence:

SELECT

    [f.id](http://f.id/),

    f.statement,

    f.verification_status,

    f.verification_score

FROM facts f

WHERE f.verification_status NOT IN ('verified_supported', 'manual_approved')

ORDER BY f.verification_updated_at DESC;

Find facts where the best evidence is weak:

SELECT

    [f.id](http://f.id/),

    f.statement,

    vc.chunk_id,

    vc.support_label,

    vc.support_score,

    vc.missing_elements,

    vc.verifier_reason

FROM facts f

JOIN fact_verification_runs vr

  ON [vr.id](http://vr.id/) = f.last_verification_run_id

JOIN fact_verification_candidates vc

  ON vc.run_id = [vr.id](http://vr.id/)

WHERE vc.candidate_rank = 1

  AND (

      vc.support_label <> 'supports'

      OR vc.support_score < 0.75

      OR vc.evidence_quote IS NULL

  )

ORDER BY vc.support_score ASC NULLS FIRST;

Find facts where the chunk mentions the right entity but not the right relation:

SELECT

    [f.id](http://f.id/),

    f.statement,

    vc.chunk_id,

    vc.support_label,

    vc.missing_elements,

    vc.verifier_reason

FROM fact_verification_candidates vc

JOIN facts f

  ON [f.id](http://f.id/) = vc.fact_id

WHERE vc.entity_score = 1.0

  AND vc.support_label IN ('insufficient', 'partially_supports')

ORDER BY vc.created_at DESC;

Find temporal failures:

SELECT

    [f.id](http://f.id/),

    f.statement,

    vc.chunk_id,

    vc.temporal_score,

    vc.missing_elements,

    vc.verifier_reason

FROM fact_verification_candidates vc

JOIN facts f

  ON [f.id](http://f.id/) = vc.fact_id

WHERE vc.missing_elements ? 'temporal'

   OR vc.verifier_reason ILIKE '%date%'

   OR vc.verifier_reason ILIKE '%period%'

ORDER BY vc.created_at DESC;

---

**10. Use offline metrics to detect broader problems**

At system level, track metrics like:

verified_fact_rate

contradiction_rate

insufficient_evidence_rate

manual_rejection_rate

citation_support_rate

average_support_score

percentage_of_answers_using_unverified_facts

top_failure_reason_by_fact_type

For RAG-specific evaluation, Ragas’ faithfulness metric is relevant because it measures whether generated answers are factually consistent with retrieved context, and it treats an answer as faithful when its claims are supported by the retrieved context. ([Ragas](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/faithfulness/?utm_source=chatgpt.com "Faithfulness - Ragas"))

You should track two related but separate things:

fact verification:

  Is this extracted fact supported by source evidence?

answer faithfulness:

  Is the final user answer supported by the chunks shown?

A system can pass one and fail the other. For example:

Fact is valid, but answer uses it incorrectly.

Answer is correct, but citation points to the wrong chunk.

So evaluate both.

---

**11. Use exact or high-recall retrieval for verification jobs**

For interactive answering, approximate vector search is fine. For verification and audit jobs, you want higher recall.

pgvector supports exact and approximate nearest-neighbor search, including HNSW and IVFFlat indexes, as well as cosine distance and other distance operators. ([GitHub](https://github.com/pgvector/pgvector?utm_source=chatgpt.com "GitHub - pgvector/pgvector: Open-source vector similarity search for ..."))

For verification, I would use:

candidate generation:

  broad/high recall

final evidence decision:

  strict verifier

Not:

top-1 embedding match decides the source

A good verification job can retrieve 50–200 candidates because it runs offline or asynchronously inside your indexing pipeline.

---

**12. Do not silently repair bad facts**

When verification fails, do not simply pick a better chunk and continue. Decide whether the problem is:

bad citation

bad fact

multi-hop fact

missing source

contradictory source

bad extraction

Recommended behavior:

bad citation, fact still supported elsewhere

    → update fact_evidence

bad fact, contradicted by source

    → mark contradicted, remove from answer eligibility

multi-hop fact

    → require evidence set, not single chunk

missing source

    → mark insufficient_evidence

ambiguous

    → queue manual review

This distinction matters. Your manual check is telling you that sometimes the **fact itself** may be wrong, not only the referenced chunk.

---

**13. Add a manual review queue**

Every manual rejection should become training/evaluation data.

CREATE TABLE fact_manual_reviews (

    id              BIGSERIAL PRIMARY KEY,

    fact_id         BIGINT REFERENCES facts(id),

    reviewer_id     TEXT,

    decision        TEXT, -- approve, reject, needs_more_context

    reason_code     TEXT,

    comment         TEXT,

    corrected_fact  TEXT,

    correct_chunk_id BIGINT REFERENCES document_chunks(id),

    created_at      TIMESTAMPTZ DEFAULT now()

);

Then automatically update the fact:

manual reject

    → verification_status = manual_rejected

manual approve

    → verification_status = manual_approved

manual corrected source

    → update fact_evidence and rerun verifier

Over time, analyze rejections:

SELECT

    reason_code,

    COUNT(*) AS count

FROM fact_manual_reviews

WHERE decision = 'reject'

GROUP BY reason_code

ORDER BY count DESC;

This will show whether your main issue is chunking, temporal anchoring, entity resolution, concept ambiguity, or LLM extraction.

---

**14. Practical verifier prompt**

For the final support check, use a strict prompt like this:

You are verifying whether a document chunk supports an extracted fact.

Rules:

- Mark "supports" only if the chunk directly states the fact or entails it with no missing required element.

- Mark "insufficient" if the chunk is related but does not prove the fact.

- Mark "contradicts" if the chunk states something incompatible with the fact.

- Do not use outside knowledge.

- If the fact contains a date or period, verify that the date is attached to the same event.

- If the fact contains a company, product, authority, or person, verify that the relation applies to that entity.

- Return an exact evidence quote. If no exact quote can be given, do not mark "supports".

Expected JSON:

{

  "label": "supports | contradicts | insufficient | partially_supports | ambiguous",

  "support_score": 0.0,

  "evidence_quote": "string or null",

  "evidence_char_start": 0,

  "evidence_char_end": 0,

  "supported_elements": [],

  "missing_elements": [],

  "contradicted_elements": [],

  "temporal_role_check": {

    "required": false,

    "passed": null,

    "reason": "string or null"

  },

  "entity_check": {

    "required_entities": [],

    "matched_entities": [],

    "missing_entities": []

  },

  "reason": "short explanation"

}

---

**15. Answer-time guardrail**

When generating the user answer, add a final guard:

For each answer claim:

  find supporting verified fact

  find supporting verified chunk/span

  if no verified support:

      remove claim or mark as unsupported

Do not let the LLM answer from unverified facts.

Pseudo-code:

def answer_question(question):

    candidate_facts = retrieve_facts(question)

    verified_facts = [

        f for f in candidate_facts

        if f.verification_status in {"verified_supported", "manual_approved"}

    ]

    if not verified_facts:

        return "I could not find sufficiently verified evidence in the documents."

    answer = generate_answer(question, verified_facts)

    claims = extract_claims(answer)

    for claim in claims:

        evidence = find_verified_evidence_for_claim(claim)

        if not evidence:

            answer = remove_or_hedge_claim(answer, claim)

            create_alert(

                alert_type="answer_claim_without_verified_evidence",

                severity="warning",

                payload={"claim": claim}

            )

    return answer

---

**16. Suggested severity levels**

Use these operational severities:

critical:

  contradicted fact was used in an answer

  answer claim has no verified evidence

  manual reviewer rejected a fact that was previously shown to users

warning:

  fact has weak support

  evidence quote missing

  temporal role ambiguous

  citation changed after re-indexing

info:

  fact requires multi-hop evidence

  lower-ranked chunk replaced previous source

  verification model/version changed result

Critical alerts should create a review task and optionally remove the fact from production use.

---

**17. Minimum viable implementation**

Start with this:

1. Add verification_status to facts.

2. Add fact_verification_candidates table.

3. For each fact, retrieve top candidate chunks using provenance + keyword + semantic + temporal.

4. Run deterministic gates:

   - required entity present

   - required temporal value/role present when relevant

5. Run LLM support verifier on top 10.

6. Require:

   - label = supports

   - support_score >= 0.75

   - evidence_quote is not null

7. Otherwise:

   - mark fact as insufficient_evidence, weak_support, or contradicted

   - alert if the fact was previously used in answers

The core production rule:

No verified evidence → no usable fact.

Bad evidence quote → no citation.

Contradiction found → manual review or quarantine.

That will catch the class of failures your manual review is surfacing.


===


Opus v2



Your design is already strong where most people get it wrong: the hard-gates-vs-soft-scoring split, the "no verified evidence → no usable fact" rule, the audit trail of runs and candidates, and refusing a free LLM planner in favor of a constrained classifier + deterministic router. Keep all of that. The improvements below are mostly about the _unit of verification_ and removing a residual bias that I think is the actual source of your failing manual checks.

## 1. Verify atomic claims, not compound facts

This is the biggest change. A fact like _"Acme GmbH received leniency in 2019 for its participation in the infringement concerning Product X"_ is a compound claim with at least four independent propositions: (entity=Acme), (received leniency), (date role=decision 2019), (product=X). Your current pipeline verifies the sentence as a unit and scores a chunk on coverage — which is exactly how a chunk that supports three of four propositions earns a high `final_score` and slips past as "supports." That's the class of error your manual review keeps catching.

Decompose every fact at extraction into atomic propositions, then verify each one independently against its own best chunk. The fact's status becomes a function of its propositions: `verified` only if all required propositions are individually supported. This makes "missing_required_product" a precise, automatic failure (one proposition has no support) rather than something a weighted blend can paper over. It also makes multi-hop the natural case rather than a bolt-on — each proposition can resolve to a different chunk. This is the same move Ragas-style faithfulness and FActScore make, and it's the single thing most likely to fix your false positives.

## 2. Treat the provenance chunk as a suspect, not a source of truth

Your scoring gives `provenance_score` weight 0.25 and seeds it as a candidate. But the extraction chunk is the _prime suspect_ for the failures you're seeing — the fact was generated from it, so of course it looks related, and trusting it just launders the extraction error back into the verification. Include the provenance chunk in the candidate pool, but give it **zero scoring bonus**. It has to earn its support label on the same terms as any retrieved chunk. If your verified-evidence chunk routinely differs from the original provenance chunk, that delta is itself one of your most valuable alerts: it means extraction is citing the wrong span.

## 3. Don't let similarity vote on the support decision

You have two jobs fused into one `final_score`: ordering candidates for the judge, and deciding support. Keep them strictly separate. A weighted/RRF blend (your existing hybrid search) is fine for _ranking the candidate queue_ — that's a recall problem. But the support _decision_ must be gates + per-proposition entailment only. Semantic similarity should never contribute to whether a chunk "supports," because two chunks about different companies in the same cartel are near-neighbors in embedding space — that's the failure mode you named in Case 1, and the weighted formula quietly reintroduces it. Run the judge blind to the retrieval scores so it doesn't anchor on a high cosine.

## 4. Temporal role assignment is an ingestion-time extraction task, not a verify-time inference

You correctly identify role-match as the hard part (decision date vs. infringement period vs. filing date), but HeidelTime only does TIMEX3 extraction and normalization — it assigns no event roles. "Store or infer an event-time relation" is hand-waving over the actual NLP problem. Do it at ingestion: for each TIMEX, extract the `(trigger_predicate, timex, role)` tuple, either via dependency-based relation extraction or a constrained LLM pass with a _closed_ role enum. For EU cartel decisions the taxonomy is genuinely closed — infringement_start / infringement_end / infringement_period / decision_date / leniency_application_date / settlement_date / publication_date / appeal_judgment_date. Store and index the role with the date range. Then your temporal gate is a deterministic role+range match, and "2020 is present but it's the decision date, not the infringement end" becomes a hard reject instead of a 0.75 overlap score.

## 5. Distinguish contradiction from supersession

A flat contradiction scan is too blunt for this domain. First, a contradiction only counts if the contradicting chunk shares the claim's binding scope (same undertaking, same product/market, same proceeding) — otherwise you'll flag "leniency rejected for Product Y" against a Product X fact. Second, and specific to EU competition law: many apparent contradictions are _supersessions_. A Commission decision can be partially annulled or a fine reduced by the General Court or Court of Justice on appeal, and the infringement period itself can be amended. That's not a logical contradiction — it's an authority + time hierarchy. Model it as supersession (later authority wins within scope) rather than dumping it into `fact_contradictions`, or your appeal-affected facts will all read as conflicts.

## 6. Your manual reviews are the calibration set — close the loop

Where does `support_score >= 0.75` come from? Right now, a guess. The manual checks you're already doing are a labeled gold set of `(proposition, chunk, human_label)` triples. Capture them systematically and use them to (a) set the threshold empirically by precision/recall rather than a round number, (b) measure each hard gate's false-reject rate, and (c) optimize the judge. Since you're on DSPy, the support judge should be a DSPy module with the proposition + chunk as inputs and the label + quote as outputs, optimized with MIPROv2 against those manual labels — the same pattern as your `LegalQA` module. That turns "my manual check questions accuracy" from a recurring annoyance into the training signal for the component that's supposed to prevent it.

## 7. Make the judge output robust

Two concrete fixes. Don't ask the LLM for `evidence_char_start/end` — models can't count characters reliably. Have it return the verbatim quote, then locate the span programmatically (exact match, then rapidfuzz fallback). If the quote can't be found in the chunk, that's a strong hallucination signal → downgrade to insufficient. This makes "no locatable quote → no citation" a free, deterministic guard. Second, a single LLM judgment is noisy for something meant to be more trustworthy than retrieval: run a cheap NLI model (entailment/contradiction/neutral) as a first pass on every candidate and reserve the LLM judge for the top-ranked survivors or NLI/gate disagreements. That cuts cost and gives you a second independent signal you can alert on when the two disagree.

---

Net effect: the unit becomes the atomic proposition; gates and entailment decide support; similarity only orders the queue; provenance earns its place rather than being assumed; temporal roles and supersession are first-class and domain-shaped; and the manual reviews you're already doing become the thing that calibrates and optimizes the judge. Your state/reason taxonomy I'd also split — keep ~5 states (verified / weak / unverified / contradicted / needs_review) and move the dozen detailed codes into a separate `reason_code` dimension, since most of them are failure _reasons_ attached to a proposition, not statuses of a fact.

Want me to write the proposition-decomposition + per-proposition gating as DSPy signatures against your existing `kb_sql`/`rag_search` setup, or work through the temporal-role extraction at ingestion in more detail?


===

# Plan: `evidence_verifier_module.py` — hybrid evidence verifier

## Context

Manual review of facts in the celex-facts system sometimes finds that a fact's _cited chunk does not actually support the claim_ — the chunk was merely retrieved or linked during extraction, not verified. Today a fact is treated as usable simply ==because it was extracted and 1==has `references_doc_json` / `fact_source_link` provenance. There is no step that asks "does this chunk **directly support** this fact?".

This builds a **hybrid evidence verifier** (per the ChatGPT/Opus discussion): deterministic candidate generation + hard gates + weighted scoring, with the LLM used only as (a) a constrained slot classifier and (b) a final support judge — never as a free planner. The output is a verified best-evidence chunk per fact, persisted to a new `fact_evidence` table plus a `verification_status` on the fact, so the answer pipeline can later restrict itself to verified facts.

Decisions confirmed with the user:

- **Persistence**: module + new `fact_evidence` table + `facts.verification_status` + `FactEvidenceRepository`.
- **Slot routing**: hybrid — deterministic weight-profile router + constrained ==LLM slot extractor2==.
- **Evidence source**: reuse existing `heideltime_extractions` (temporal) and `company_canonical2*` / FTS (entities). No new `chunk_entities`/`chunk_timex` tables.

## Reused components (do NOT reinvent)

|Need|Reuse|Path|
|---|---|---|
|FTS / keyword candidates|`ChunkRepository.fulltext_search_in_celex`, `keyword_search`|[chunk_repository.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/chunk_repository.py)|
|Semantic candidates|`ChunkRepository.semantic_search_in_celex`|same|
|Resolve chunk text|`ChunkRepository.get_chunk_by_id`|same|
|Provenance candidates|`LineageRepository.get_sources_for_fact` (gives `trust_rank`, `conflict_flag`)|[lineage_repository.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/lineage_repository.py)|
|Fact loading|`FactRepository.select_fact4celex` / `select_facts4celex`|[fact_repository.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/fact_repository.py)|
|Temporal signal|`heideltime_extractions` via `CelexHeidelTimeExtraction` ORM (`timex_value`, `timex_text`, `fk_chunk_id`)|[models.py:216](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/models.py#L216)|
|Entity aliases|`company_canonical2alias`, `company_canonical2celex_id`|DDL|
|Embedding + cache|`retrieve_qwen3_embedding`, `QueryEmbeddingRepository.get_or_cache`|[qwen3_embedding.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/embeddings/qwen3_embedding.py), [query_embedding_repository.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/query_embedding_repository.py)|
|**LLM-judge gate template**|`KnowledgeIngestor._judge_or_reject` + `KnowHowJudgeSignature` (threshold + exception-safe default)|[knowledge_ingestor.py:308](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/knowledge_ingestor.py#L308), [knowledge_signatures.py:156](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/agent_dspy/knowledge_signatures.py#L156)|
|DSPy module shape|`EnumerationFactModule` (Provisioner + forward dispatch)|[enumeration_fact_module.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/agent_dspy/enumeration_fact_module.py)|

## Files to create / modify

### 1. NEW `src/pipelines/agent_dspy/evidence_verifier_module.py`

Holds the Pydantic VOs, the two DSPy signatures, and the `EvidenceVerifierModule` (mirrors `enumeration_fact_module.py`, which keeps signatures + module together to honour CLAUDE.md "minimise new files").

**Pydantic VOs**

- `FactSlots` — `fact_type: Literal[...]`, `required_entities: list[str]`, `required_concepts: list[str]`, `temporal_role: str | None`, `temporal_value: str | None`.
- `CandidateEvidence` — `chunk_id`, `doc_pos`, `chunk_text`, per-signal scores (`entity_score`, `keyword_score`, `semantic_score`, `temporal_score`, `provenance_score`), `final_score`, `candidate_source`.
- `FactVerificationResult` — `fact_id`, `celex_id`, `fact_name`, `status: VerificationStatus`, `best_chunk_id`, `final_score`, `support_label`, `evidence_quote`, `missing_elements: list[str]`, `candidates: list[CandidateEvidence]` (for audit).
- `VerificationStatus` (StrEnum): `verified_supported`, `weak_support`, `insufficient_evidence`, `contradicted`, `missing_required_entity`, `missing_required_time`, `unverified`.

**Signatures (Rule 1/3 — declarative, Pydantic outputs)**

- `FactSlotSignature` → `FactSlots`: constrained classifier. Inputs: `fact_statement`, `fact_name`, `output_schema`. Emits required entities/concepts/temporal role only — _not_ a retrieval plan.
- `EvidenceSupportSignature` → `EvidenceSupportResult`: the final judge. Inputs: `fact_statement`, `required_entities`, `required_temporal`, `chunk_text`. Output fields mirror discussion §14: `label` (`supports|contradicts|insufficient|partially_supports|ambiguous`), `support_score`, `evidence_quote` (must be exact substring or null), `supported_elements`, `missing_elements`, `temporal_role_check`, `entity_check`, `reason`. Strict rule: no `supports` without an exact quote.

**`EvidenceVerifierModule(dspy.Module)`** — Provisioner pattern, all clients injected in `__init__`: `chunk_repo`, `lineage_repo`, `fact_repo`, `evidence_repo`, `embed_fn=retrieve_qwen3_embedding`, optional `query_embedding_repo`. `forward(celex_id, fact_name)` runs the **Slick Pipe**:

1. **Load** fact via `fact_repo.select_fact4celex`; build `fact_statement` from `value_md`.
2. **Route + classify**: deterministic `_route_profile(fact_name, output_schema, has_heideltime)` picks a `WeightProfile` (entity-heavy / concept-heavy / temporal / mixed) and which gates are _material_; `self.classify = dspy.Predict(FactSlotSignature)` fills required entities/concepts (expanded with company aliases).
3. **Candidate generation** (union, all scoped to `celex_id`):
    - provenance → `lineage_repo.get_sources_for_fact` (`provenance_score = trust_rank/10`)
    - keyword/entity → `fulltext_search_in_celex` + `keyword_search` on entity surfaces
    - semantic → embed statement (via `query_embedding_repo.get_or_cache`) → `semantic_search_in_celex`
    - temporal → `_temporal_candidates()`: query `heideltime_extractions` where `celex_id` matches and `timex_value`/`timex_text` matches the fact's temporal slot; map `fk_chunk_id` → chunk
    - dedup on `chunk_id`; resolve text via `get_chunk_by_id`.
4. **Score** each candidate; `final_score` = weighted sum using the routed profile (weights from discussion §3: entity-heavy ⇒ entity+keyword dominate; concept ⇒ semantic+support; temporal ⇒ temporal+entity).
5. **Hard gates** (`_apply_gates`): required entity must appear in chunk (alias-aware substring/FTS) else reject; temporal role must match when material; drop failing candidates. If _all_ dropped → status `missing_required_entity` / `missing_required_time`.
6. **LLM support judge** over top-N (e.g. 10) gated candidates, ranked by `final_score`. Reuse the `_judge_or_reject` discipline (try/except → log + skip, never crash the run). Accept a candidate only on `label == "supports"` with a non-null exact `evidence_quote`.
7. **Decide status**: first `supports` with `support_score ≥ settings.EVIDENCE_SUPPORT_MIN_SCORE` → `verified_supported`; `supports` below → `weak_support`; any `contradicts` and no support → `contradicted`; none → `insufficient_evidence`.
8. **Persist**: `evidence_repo.upsert_evidence(...)` + `evidence_repo.set_verification_status(fact_id, status)`. Return `FactVerificationResult`.

Module-level constants: weight profiles dict + top-N. Thresholds (`EVIDENCE_SUPPORT_MIN_SCORE`, `EVIDENCE_WEAK_MIN_SCORE`, candidate `LIMIT`s) added to `settings.py` per CLAUDE.md (no magic numbers inline).

### 2. NEW `src/pipelines/rag/pgvector/fact_evidence_repository.py`

`FactEvidenceRepository` (Provisioner; shares engine via `session.py`):

- `upsert_evidence(fact_id, chunk_id, support_label, final_score, sub_scores: dict, evidence_quote, missing_elements)` — atomic upsert on `(fact_id, chunk_id)`.
- `set_verification_status(fact_id, status)` — updates `facts.verification_status`.
- `get_evidence_for_fact(fact_id)`, `list_unverified(celex_id=None)` — monitoring queries from discussion §9.

### 3. NEW `src/pipelines/rag/pgvector/migrate_fact_evidence.sql`

Follows the style of `migrate_lineage_tables.sql`:

- `CREATE TYPE celex_facts.verification_status_enum AS ENUM (...)` (the `VerificationStatus` values + `manual_approved`/`manual_rejected`).
- `ALTER TABLE celex_facts.facts ADD COLUMN verification_status ... DEFAULT 'unverified', ADD COLUMN verification_score numeric, ADD COLUMN verification_updated_at timestamptz`.
- `CREATE TABLE celex_facts.fact_evidence` — `fact_id uuid REFERENCES facts(id) ON DELETE CASCADE`, `chunk_id uuid` (FK type matches `pg_embedding_qwen.id` UUID — per identifier-type memory), `support_label text`, `final_score numeric`, `entity_score/keyword_score/semantic_score/temporal_score/provenance_score numeric`, `evidence_quote text`, `missing_elements jsonb`, `verified_at timestamptz default now()`, `PRIMARY KEY (fact_id, chunk_id)`.

### 4. MODIFY `src/pipelines/rag/pgvector/models.py`

Add `FactEvidence(Base)` ORM mapping for the new table and a `verification_status` column on the existing `Facts` model. (`Facts` is at [models.py:47](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/models.py#L47).)

### 5. MODIFY `src/config/settings.py`

Add `EVIDENCE_SUPPORT_MIN_SCORE`, `EVIDENCE_WEAK_MIN_SCORE`, `EVIDENCE_CANDIDATE_LIMIT`, `EVIDENCE_JUDGE_TOP_N` (env-overridable), mirroring existing `EMBEDDING_TABLE` style.

### 6. NEW tests (CLAUDE.md: `unittest`, in `./tests/`, with `__init__.py`)

- `src/pipelines/agent_dspy/tests/test_evidence_verifier_module.py` — fake `chunk_repo`/`lineage_repo`/`fact_repo` (MagicMock) + fake classifier/judge predictors (`SimpleNamespace(output=...)` pattern from `test_knowledge_judge.py`). Cover: entity hard-gate rejects semantically-similar wrong-entity chunk; temporal gate; `supports` without quote is downgraded; weight-profile routing per fact_type; judge exception → run still completes.
- `src/pipelines/rag/pgvector/tests/test_fact_evidence_repository.py` — upsert idempotency + status update against a mocked session boundary.

## Verification

1. `ruff check src/pipelines/agent_dspy/evidence_verifier_module.py src/pipelines/rag/pgvector/fact_evidence_repository.py` — must pass clean.
2. `.venv/bin/python -m unittest src.pipelines.agent_dspy.tests.test_evidence_verifier_module src.pipelines.rag.pgvector.tests.test_fact_evidence_repository -v` (no DB / no LLM — all boundaries mocked).
3. Apply migration on a scratch DB: `psql < migrate_fact_evidence.sql`; confirm enum, columns, and table exist.
4. Smoke run against one real CELEX (e.g. a leniency fact) with `.venv` + Ollama up: instantiate the module with real repos, call `forward(celex_id, "leniency_granted")`, and assert the returned `FactVerificationResult.status` and that the cited `evidence_quote` is an exact substring of the chosen chunk.

## ==Out of scope3== (noted for follow-up)

`fact_verification_runs`, `fact_alerts`, and the manual-review queue (discussion §2/§8/§13) — deferred per the "Module + fact_evidence + status" choice. The result VO already carries `candidates` so a runs/alerts layer can be layered on later without changing the module API.


The full backfill for `CELEX-31982D0123` is now clean and correct. Final state:

**Run:** 0 errors, 0 caught warnings. Strategies: `deterministic_entity` 19, `deterministic_temporal` 8, `no_provenance` 12, `derived` 8, `llm_support` 4, `trusted_human` 4, excluded 2. Statuses: `verified_supported` 25, `unverified` 12, `insufficient_evidence` 5, `weak_support`/`missing_required_entity` 4 each, `manual_approved` 4, `missing_required_time` 1.

**Invariants:** every non-excluded fact has exactly one ledger row (55 rows = 55 facts, 0 duplicates); only the two `decision_chapter_*` excluded names have no row.

## What this turn fixed (all from the real run you pasted)

1. **`gated_by` enum drift** — `FactSourceLink` ORM now matches the DB enum; lineage reads no longer error.
2. **LLM cost** — `EVIDENCE_JUDGE_TOP_N` 10→3; concept facts dropped from 15 to 4 after routing fix.
3. **Routing precedence** — direct deterministic checks (temporal-with-dates, entity) now win; concept facts branch on provenance (chunks→LLM, only fact-preds→`derived`, neither→`no_provenance`). The `leniency_*` facts you flagged now verify transitively as you intended.
4. **Stale-row bug** — `upsert_evidence` replaces the fact's whole ledger atomically, so strategy changes between runs leave one authoritative row.
5. **HeidelTime matching** — handles the `date`-typed `timex_value` and period ranges by extracting both endpoints with safe casting; temporal facts now verify with exact date quotes.

**Tests:** 26 unit tests + the DB-backed e2e (both companies in one chunk) all green; ruff clean.

The DB changes I made this session: applied `migrate_fact_evidence.sql` (additive), and the `fact_evidence` ledger + `facts.verification_status` are now populated for `CELEX-31982D0123`. The ORM enum corrections (`gated_by`) and the `CelexHeidelTimeExtraction.timex_value` type note are code-only. Nothing else is pending unless you want me to run the corpus-wide backfill (no `--celex-id`).


# quests
## why is the final_score null and verfication_status = weak_support ?

Because they come from two different code paths that don't share a number.

For a **derived** fact, [`_verify_derived`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py#L503) returns a _summary_ result via [`_summary_result`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py#L1022), which leaves `final_score=None` — the derived branch only aggregates predecessor _statuses_, it never computes a numeric confidence. Then [`_persist`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py#L1050) writes that `None` straight through to both `fact_evidence.final_score` and `facts.verification_score`. So `final_score` is null by design for derived (and human / no-provenance) facts — only the entity, temporal, and LLM branches produce a score.

The `weak_support` verdict is independent of the score. In [`_verify_derived`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py#L518-L530):

```python
status = VERIFIED_SUPPORTED
if   any predecessor CONTRADICTED            -> CONTRADICTED
elif any predecessor INSUFFICIENT_EVIDENCE   -> INSUFFICIENT_EVIDENCE
elif not statuses or not statuses ⊆ verified -> WEAK_SUPPORT   # verified = {verified_supported, manual_approved}
```

So `weak_support` means: no predecessor is contradicted/insufficient, but at least one predecessor is in some _other_ non-verified state (e.g. `weak_support`, `unverified`, `missing_required_entity`), **or** the fact had no predecessor rows at all (`not statuses`). Those non-verified predecessor names are exactly what lands in `missing_elements`.

So the row you're looking at is a derived fact whose predecessors aren't all `verified_supported`/`manual_approved` yet.

Two things worth checking / deciding:

1. **Is the empty-predecessor case hiding here?** If `missing_elements` is empty _and_ status is `weak_support`, then `get_predecessor_facts` returned nothing (the `not statuses` branch) — that would point at a lineage gap (routed to `DERIVED` via `source_type='fact'` count, but the `source_fact_id` join in [`get_predecessor_facts`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/fact_evidence_repository.py#L172) finds no row). Worth a look if that's the case.
    
2. **Should derived facts carry a score?** Right now they're deliberately null. If you'd like `weak_support`/`verified_supported` derived facts to get a numeric confidence (e.g. the fraction of predecessors in a verified state, or the min of predecessor `verification_score`s), that's a small change in `_verify_derived`. Want me to add it?

# skip empty value_*

Added an early skip in [`_verify_by_name`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py): a fact is skipped before routing when it has **no verifiable value in either column** — `value_md` is null/`NA` **and** `value_json` is also empty (`null`/`NA`/`[]`/etc.). The new [`_has_no_verifiable_value`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py#L249) predicate reuses the existing `_EMPTY_VALUES` set for both columns.

Per your choice, a fact with structured content in `value_json` but a null/`NA` `value_md` is **still verified** — so entity facts (whose surfaces come from `value_json` via `_extract_surfaces`) aren't dropped.

Skipped facts behave like excluded ones: `forward` returns `None`, no `fact_evidence` row and no `verification_status` written.

Tests added:

- both columns empty (`None/None`, `NA/None`, `NA/[]`) → skipped, nothing written;
- `value_md='NA'` but `value_json` present → still `verified_supported`.

I also filled in values on four existing fixtures (two derived aggregates, the on-demand-predecessor `court_list`, and `human_index_year`) that had omitted a value — they'd otherwise have been caught by the new skip, which doesn't reflect production where those facts carry values.

One consequence worth noting: this carves out a second class of facts (besides the exclude list) that get **no** `fact_evidence` row, so it's a deliberate narrowing of the "complete-ledger" guarantee in the [verification memory](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll~/.claude/projects/-home-job-a-celex-facts/memory/project_fact_classes_and_verification.md). Want me to update that memory note to record the value-empty skip?

see: MEMORY.md in ~/.claude/projects/-home-job-a-celex-facts/memory/MEMORY.md


## odd or reverse case titles
we need a special case for case_title thats the chunk content: # COMMISSION DECISION of 25 November 1981 relating to a proceeding under Article 85 of the EEC Treaty (IV/428 — VBBB/VBVB) this is what the fact exctractor does: [{"case_title": ["IV/428 \u2014 VBBB/VBVB", "relating to a proceeding under Article 85 of the EEC Treaty"]}]


# Boolean fact values

Yes — that's viable, and it's essentially your existing `llm_support` branch. But boolean facts are a distinct class that your current router handles **inconsistently**, and the naive prompt "is case_commission_decision = True?" has three traps worth designing around. Here's the grounded picture.

## What happens to these facts today

None of these are routed as "booleans" — they fall wherever their name substring happens to land in [_route_strategy](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py#L500):

|Fact|Substring hit|Current route|Correct?|
|---|---|---|---|
|`case_commission_decision`|none|`no_provenance` → **unverified**|no|
|`case_commission_no_action`|none|`no_provenance` → **unverified**|no|
|`is_court_involved`|`court` (entity marker)|`deterministic_entity`|**broken** — does a keyword search for the surface `"True"`|
|`leniency_case`, `exemption_case`|`leniency`/`exemption` (concept)|`llm_support`|accidentally OK-ish|
|`infringement_category_abuse_of_dominance`|`infringement`/`dominance`|`llm_support`|accidentally OK-ish|
|`market_outside_eu`, `market_oligopoly`|none|`no_provenance`|no|

So the same logical class scatters across four strategies. The canonical list already exists in [retrieval_modules.py:804-809](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/retrieval_modules.py#L804-L809) — that's the set you'd route on.

## The three traps with "is X = True?"

**1. Polarity is the entire claim.** For a normal concept fact, topic-presence ≈ support. For a boolean, the chunk mentioning "commission decision" tells you nothing about whether the value is `True` or `False`. The judge must verify the _polarity_, and `EvidenceSupportSignature` currently builds its claim from [`_fact_statement`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py#L1063) as `"case_commission_decision: True"` — a snake_case name plus a bare bool is a weak claim string. You already have a far better one: the extractor's natural-language question, e.g. _"The EU commission has taken a decision? The text contains 'HAS ADOPTED THIS DECISION' and 'Article 1', 'Article 2'…"_ ([FACT_EXTRACTOR.yaml:498-501](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/config_main/FACT_EXTRACTOR.yaml#L498-L501)). Feed _that_ as the criterion, not the field name.

**2. `False` asserts absence — you can't prove it from positive chunks.** A document almost never says "the Commission did not take a decision." So:

- `value=True` → verifiable: retrieve, find a supporting chunk, done.
- `value=False` → the best the LLM can do is **contradict** it if a chunk strongly asserts the positive; otherwise it's `insufficient`/`weak`, _not_ `verified_supported`. Treating False symmetrically will produce a corpus of false "insufficient evidence" flags. `case_commission_no_action=True` is the same trap inverted (it's an absence claim phrased positively).

**3. Retrieval needs the question, not the value.** Your candidate generation embeds [`_fact_statement`](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py#L869) — embedding `"case_commission_decision: True"` retrieves poorly. Embed the question text / the marker phrases ("HAS ADOPTED THIS DECISION") instead.

## Recommendation

Add a `DETERMINISTIC_BOOLEAN` / `LLM_BOOLEAN` strategy rather than leaning on accidental substring routing:

1. Route by membership in the canonical boolean-fact set (lift the list from retrieval_modules into `settings`, single source of truth — don't duplicate).
2. For `case_commission_decision` specifically, you can stay **deterministic**: its truth condition is literally a string marker ("HAS ADOPTED THIS DECISION" + numbered Articles). A SQL/keyword presence check is cheaper and more reliable than the LLM — same spirit as your HeidelTime temporal branch.
3. Send only the genuinely semantic booleans (`leniency_case`, `exemption_case`, abuse-of-dominance) to the judge, fed the **extractor question** as the claim and judging polarity explicitly.
4. Handle `False`/no-action facts asymmetrically: verifiable only via contradiction, never via positive support.

Want me to sketch the boolean branch + router change in [evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py), or first dump the actual True/False/NA distribution for these names from the DB so we size which ones even need the LLM?

**[settings.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/config/settings.py)** — `EVIDENCE_BOOLEAN_FACT_NAMES` frozenset (env-overridable), the single source of truth for which facts are polarity claims.

**[evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py)**

- `VerificationStrategy.BOOLEAN` + docstring entry.
- `BooleanPolaritySignature` — judges a chunk against the _asserted_ True/False value, with explicit instructions that a False/absence value cannot be proven from positive text.
- `_BOOLEAN_CLAIMS` table — NL claim + literal marker phrases per fact (claims drawn from the extractor questions in [FACT_EXTRACTOR.yaml](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/config_main/FACT_EXTRACTOR.yaml#L498-L501)).
- `boolean_judge` injected in `__init__` (mockable at the boundary).
- Router: boolean check sits **after** the `derived` short-circuit (provenance shape still wins) and **before** the name-marker branches — so `is_court_involved` no longer misroutes to `deterministic_entity`.
- `_verify_boolean` (marker fast-path → polarity judge), `_boolean_result` (asymmetric mapping), `_boolean_marker_hit`, `_judge_boolean`.

**[test_evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/tests/test_evidence_verifier_module.py)** — 3 routing tests + a `TestBoolean` class covering: marker fast-path (no LLM), True-via-judge, False→`contradicted`, False→`weak_support`, True→`insufficient_evidence`.

## Behavior worth confirming on real data

The asymmetry is now enforced: a `False` boolean can only land `contradicted` or `weak_support`, never `verified_supported`. That's deliberate, but it means a backfill will show many `weak_support` rows for legitimately-False facts. Before a full run, it's worth dumping the True/False/NA split per name to see how many of these even reach the LLM versus the deterministic marker path — I can write that query if useful.

One follow-up I did **not** do: the marker phrases in `_BOOLEAN_CLAIMS` are first-guess strings (e.g. `"hereby adopted"`). They should be tuned against actual chunk text — easy to do once you point the backfill at a sample CELEX with `--dry-run`.

# negative statements 
name            | value_md | verification_status | support_label | final_score |                                                                                  evidence_quote                                                                                   
---------------------------+----------+---------------------+---------------+-------------+-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
 case_commission_no_action | False    | verified_supported  | supports      |        0.95 | AB Lietuvos geležinkeliai shall bring to an end the infringement referred to in Article 1 and submit to the Commission within three months a proposal for measures to that effect

I fixed a verification logic issue where boolean facts with `False` values were being incorrectly marked as `weak_support` when the evidence actually contradicted the positive claim.
### Root Cause

When the evidence verifier processed a boolean fact with value `False`, it would pass the original positive claim to the LLM judge without indicating that we were testing the opposite claim. For `case_commission_no_action = False` (meaning "the Commission DID take action"), the judge was receiving the claim "Commission need not take action" without understanding that we needed evidence of the opposite.

### The Fix

I made two key changes in [evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py):

1. **Inverted claims for False facts** (lines 920-925): For False facts, I invert the original claim to its logical opposite before sending it to the judge. For example:
    
    - Original: "The Commission need not take action; the articles are declared inapplicable."
    - Inverted: "The Commission issued a formal decision specifying remedial measures and enforcement actions."
2. **Correct value assertion** (lines 920-925): When passing the inverted claim to the judge, I also pass `judge_value = "True"` instead of `"False"`. This tells the judge: "Evaluate whether the inverted claim is true," which is the correct interpretation for testing a False fact.



## Implemented: extraction question feeds the evidence verifier

The verifier now reuses each fact's original extraction **question** (from `celex_facts.fact_ingestion_audit_trail`) for the two LLM branches, where short values (`True`/`False`, `high`/`low`) gave retrieval and the judge little to work with.

**Changes**

- [settings.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/config/settings.py) — new `EVIDENCE_USE_EXTRACTION_QUESTION` toggle (default on).
- [fact_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/fact_repository.py) — `select_latest_question(celex_id, fact_name)`, the latest successful audit row's `question` (keyed on `target_fact_name`, `success DESC, created_at DESC` — handles re-runs / NULL `fact_id`).
- [evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py):
    - `_clean_question` (module-level) strips `#`-comment lines like `# Constraint: reply True/False`.
    - `_extraction_question` (cached per `(celex_id, name)`, fails soft) and `_retrieval_query` helpers.
    - `_verify_llm_support` / `_verify_boolean` embed `question + statement` for retrieval and pass `question` to the judges.
    - Both DSPy signatures gained a declarative `extraction_question` InputField (Rule 1/3 preserved).
- [test_evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/tests/test_evidence_verifier_module.py) — defaulted `select_latest_question` to `None` in the mock builders, added `TestExtractionQuestion` (retrieval+judge enrichment, statement fallback, flag-off, caching, boolean branch, `_clean_question`).

**Status**: verifier suite green (42/42), `ruff check` clean on all four files. Full `unittest` suite running in the background — I'll report once it finishes.

## Summary

The evidence verifier now leverages each fact's original **extraction question** (from `celex_facts.fact_ingestion_audit_trail`) to score chunks — closing the gap for short-value facts (`True`/`False`, `high`/`low`) where the value alone gave retrieval and the LLM judge nothing to anchor on.

**Files changed**

- [src/config/settings.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/config/settings.py) — `EVIDENCE_USE_EXTRACTION_QUESTION` toggle (default on).
- [src/pipelines/rag/pgvector/fact_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/fact_repository.py) — `select_latest_question()`, latest successful audit row keyed on `(celex_id, target_fact_name)`, `success DESC, created_at DESC` (handles re-runs / NULL `fact_id`).
- [src/pipelines/agent_dspy/evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/evidence_verifier_module.py) — `_clean_question` (strips `#`-constraint lines), cached `_extraction_question`, `_retrieval_query`; both LLM branches now embed `question + statement` for retrieval and pass the question to the judges; both DSPy signatures gained a declarative `extraction_question` InputField.
- [test_evidence_verifier_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/tests/test_evidence_verifier_module.py) — new `TestExtractionQuestion` class (retrieval+judge enrichment, statement fallback, flag-off, caching, boolean branch, `_clean_question`).

**Verification**

- Verifier suite: **42/42 pass**.
- `ruff check`: clean on all four files.
- Full suite: 819 tests; the 18 failures are all pre-existing live-model `src.agents_usr.tests.*` integration tests — none reference the new code, and every change is additive.

A note worth keeping in mind: the win is concentrated where the value is short **and** the question is descriptive (`infringement_gravity`, `infringement_subcategory_*`). Keyword-style questions (`is_court_involved`→`"Court"`, `leniency_case`→`"leniency"`) add little beyond the existing markers — the cleaning step doesn't hurt them, but don't expect a retrieval lift there.

One follow-up you may want eventually (left out per the plan's scope): the hardcoded `_BOOLEAN_CLAIMS` map now overlaps conceptually with the audit-trail questions. The question augments rather than replaces it today; consolidating them later would remove that duplication.