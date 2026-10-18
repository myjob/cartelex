
Status: implemented (live since 2026-05-30, `fact` source type added 2026-08)
Owner tables: `celex_facts.fact_source_registry`, `celex_facts.fact_source_link`
Code: [lineage_repository.py](src/pipelines/rag/pgvector/lineage_repository.py), [models.py](src/pipelines/rag/pgvector/models.py)
Related: [Data Lineage Option & Implementation](docs/Data%20Lineage%20Option%20&%20Implementation.md) (option analysis and change log), [Design Fact Verifier](docs/Design%20Fact%20Verifier.md), [DESIGN Knowledge](docs/DESIGN%20Knowledge.md)

---

## 1. Purpose

Every fact in `celex_facts.facts` is an assertion about an EU cartel decision. In a legal
domain the assertion is worthless without audit-grade traceability back to the exact
piece of evidence it came from. `fact_source_registry` is the **single catalogue of
evidence** in the system: one row per distinct piece of evidence, whatever its kind, and
`fact_source_link` is the many-to-many edge table that says which fact rests on which
evidence, how strongly, and via which relation.

The legacy mechanism was `facts.references_doc_json` — a JSONB array of integer chunk
positions. It cannot express non-document evidence (a book chapter, an LEI record, a
curated CSV row, a HeidelTime timex), cannot be queried across facts, cannot carry trust
or conflict metadata, and breaks whenever a document is re-chunked. The registry replaces
it as the authoritative provenance store; `references_doc_json` is dropped.

Design choice, from the option analysis: **Option 2 (normalized relational lineage table)
plus selective Option 3 (typed edge semantics)**. Not a graph database, not an
event-sourced ledger — the traversal that Option 3 buys is delivered by one recursive SQL
function, `celex_facts.fact_provenance()`.

---

## 2. Position in the data model

```
        EVIDENCE PRODUCERS                    CATALOGUE                 CONSUMERS
  ------------------------------      ----------------------      ---------------------

  pg_embedding_qwen (chunk)  ---.
  pg_embedding_book_qwen     ---|
  heideltime_extractions     ---+--> fact_source_registry <--+-- fact_source_link --> facts
  corp_lei (LEI)             ---|      source_id (PK)        |     (fact_id, source_id)
  human-curated CSV          ---|      source_type enum      |      relation_type
  facts (predecessor fact)   ---'      + one natural-key col |      trust_rank
                                                             |      pipeline/dspy_module
                                                             |
                                                             '-- knowledge_edges.dst_source_id
                                                                    --> knowledge_items

  Read side:  celex_facts.fact_provenance(fact_id)  -- recursive CTE over
              fact_source_link x fact_source_registry, emitting terminal (non-fact) sources
              joined with fact_evidence.final_score by LineageRepository.
```

Two subsystems ground against the same catalogue: **facts** through `fact_source_link`,
and **knowledge items** through `knowledge_edges.dst_source_id`. That was a deliberate
collapse — `knowledge_edges` previously carried its own `dst_chunk_id` pointing straight
at `pg_embedding_qwen`, a second parallel way of naming raw evidence. Adding a new
`source_type` now reaches both subsystems with no schema churn
(`migrate_knowledge_edge_source_id.sql`).

The **entity graph** (`migrate_entity_graph.sql`) does not link to the registry directly;
its edges carry `source_fact_id -> facts.id`, which keeps every edge resolvable through
`fact_provenance()` one hop later.

---

## 3. Schema

### 3.1 `celex_facts.fact_source_registry`

One row per distinct piece of evidence, shared by every fact it supports.

| Column | Type | Role |
|---|---|---|
| `source_id` | `uuid` PK, `gen_random_uuid()` | surrogate key we own |
| `source_type` | `source_type_enum` NOT NULL | discriminator |
| `celex_id` | `text` | document scope; carries the `CELEX-` prefix as stored in the chunk tables |
| `chunk_id` | `varchar` | natural key for `chunk` / `book_case` — matches `pg_embedding_qwen.id`, a LangChain-owned varchar holding a UUID-format string |
| `chunk_seq` | `int[]` | `doc_pos` values, display and backfill only — never a key |
| `heideltime_event_id` | `uuid` | natural key for `heideltime` -> `heideltime_extractions.id` |
| `lei_id` | `text` | natural key for `lei_corporate` -> `corp_lei.lei` |
| `csv_row` | `int` | with `celex_id`, natural key for `csv_human` |
| `source_fact_id` | `uuid` FK -> `facts(id)` ON DELETE CASCADE | natural key for `fact` |
| `description` | `text` | human-readable label (timex surface form, legal name, book chapter) |
| `created_at` | `timestamptz` NOT NULL | |

`source_type_enum`: `chunk`, `csv_human`, `book_case`, `lei_corporate`, `heideltime`, `fact`.

The table is a **sparse polymorphic catalogue**: exactly one natural-key column is
meaningful per `source_type`, and a partial UNIQUE index enforces it.

| Index | Key | Applies to |
|---|---|---|
| `uq_source_registry_chunk` | `(source_type, chunk_id)` WHERE `chunk_id IS NOT NULL` | `chunk`, `book_case` |
| `uq_source_registry_heideltime` | `(heideltime_event_id)` WHERE NOT NULL | `heideltime` |
| `uq_source_registry_lei` | `(lei_id)` WHERE NOT NULL | `lei_corporate` |
| `uq_source_registry_csv` | `(celex_id, csv_row)` WHERE `csv_row IS NOT NULL` | `csv_human` |
| `uq_source_registry_fact` | `(source_fact_id)` WHERE NOT NULL | `fact` |

Plus lookup indexes on `celex_id`, `source_type`, `chunk_id`, `heideltime_event_id`.

`source_type` is part of the chunk key because a decision chunk and a book chunk can in
principle share an id namespace; the other keys are globally unique on their own.

### 3.2 `celex_facts.fact_source_link`

| Column | Type | Role |
|---|---|---|
| `fact_id` | `uuid` FK -> `facts(id)` CASCADE | PK part |
| `source_id` | `uuid` FK -> `fact_source_registry(source_id)` | PK part |
| `extraction_method` | `text` NOT NULL | the fact name / job that produced the link |
| `pipeline` | `text` | producing pipeline, mirrors `fact_ingestion_audit_trail.pipeline` |
| `dspy_module` | `text` | producing DSPy module class name |
| `relation_type` | `relation_type_enum` NOT NULL DEFAULT `extracted_from` | edge semantics |
| `trust_rank` | `smallint` NOT NULL DEFAULT 5, CHECK 1..10 | evidence strength |
| `conflict_flag` / `conflict_note` | `bool` / `text` | contradiction marker |
| `created_at` | `timestamptz` NOT NULL | |

`relation_type_enum`:

| Value | Meaning | In lineage traversal? |
|---|---|---|
| `extracted_from` | primary extraction — the fact's value was read out of this source | yes |
| `corroborated_by` | a second source confirms a fact extracted elsewhere (book cross-check) | yes |
| `entity_resolved_via` | an entity-resolution step, e.g. an LEI lookup that canonicalised a name | yes |
| `derived_from` | the fact was computed from a predecessor fact | yes, recursed through |
| `gated_by` | **control flow, not data** — a YAML `condition` decided whether this fact was extracted at all (e.g. `court_list_raw` only runs when `is_court_involved == true`) | **no, excluded** |

`gated_by` is the one edge that exists to be excluded. It records why extraction happened
without polluting the answer's citation trail with a boolean flag the user never asked
about.

### 3.3 Trust ranks

Defaults live in `_TRUST_RANK_DEFAULTS` in [lineage_repository.py](src/pipelines/rag/pgvector/lineage_repository.py),
resolved in Python when the caller passes `source_type` (no DB round-trip), otherwise by a
registry read:

```
csv_human 9  >  book_case 8  >  lei_corporate 7  >  heideltime 6  >  chunk 5  >  fact 4
```

The ordering encodes editorial judgment: a human-curated index row outranks a book
chapter, which outranks a machine-derived record, which outranks a raw chunk; a fact
derived from another fact ranks last because it is the most transformed. `trust_rank` is
a tiebreaker in the read path, never a filter.

---

## 4. Invariants

1. **Normalization.** One registry row per distinct evidence item. Every `register_*`
   method upserts on the natural key; the same chunk supporting forty facts is catalogued
   once. Earlier ingestion appended a fresh row per fact;
   `migrate_lineage_unique_keys.sql` collapsed those duplicates, repointed the links, and
   installed the UNIQUE indexes that keep it collapsed.
2. **Idempotency everywhere.** `link_fact_to_source` uses `ON CONFLICT (fact_id,
   source_id) DO UPDATE`, so re-running any backfill heals stale `pipeline` /
   `dspy_module` values instead of duplicating edges. Source re-registration with a
   non-null `description` refreshes the label; a `None` description never clears one.
3. **Lineage never breaks ingestion.** `FactIngestor._persist_lineage` wraps each source
   in a `SAVEPOINT` and swallows failures with a warning. The fact is already persisted;
   provenance is a byproduct, and losing one evidence edge must not lose the fact.
4. **Incomplete natural key means no dedup.** `_find_existing_source` returns `None` when
   the key column for the type is missing, forcing a fresh insert rather than silently
   merging unrelated evidence.
5. **Registry rows outlive their creator.** Deleting a fact cascades its links, but the
   registry row survives if another fact still cites it. Integration test teardown deletes
   a source only when it is orphaned — the same rule any cleanup script must follow.

---

## 5. Write paths

| Writer | Source types | Relation | Trigger |
|---|---|---|---|
| [PipelineFactIngestor.py](src/tools/PipelineFactIngestor.py) `FactIngestor.save_fact` | `chunk`, `book_case`, `heideltime`, `csv_human`, `lei_corporate` | `extracted_from` (default) | every fact ingestion; either a module emits `lineage_sources: list[LineageSourceVO]`, or the ingestor derives chunk sources from `references_doc` via `ChunkRepository.resolve_chunk_ids_by_doc_pos` |
| [PipelineFactIngestor.py](src/tools/PipelineFactIngestor.py) `_derive_predecessor_lineage` | `fact` | `derived_from` / `gated_by` | same call: each YAML `predecessors` entry is resolved to its `facts` row and classified against the fact's `condition` |
| [book_cross_check_module.py](src/pipelines/agent_dspy/book_cross_check_module.py) `_record_corroboration` | `book_case` | `corroborated_by` | book confirms a decision-extracted fact |
| [heideltime_source_register_fisher.py](src/pipelines/heideltime/heideltime_source_register_fisher.py) | `heideltime` | `extracted_from` | one-shot: reconnects `heideltime_cartel_period` facts to the timex rows matching their boundary dates |

`LineageSourceVO` (in `PipelineFactIngestor.py`) is the boundary value object. Its
`model_validator` refuses a source whose natural-key fields for the declared type are
missing — a chunk source without a `chunk_id`, or a fact source without a
`source_fact_id`, fails at construction rather than at the UNIQUE index.

`pipeline` / `dspy_module` are recorded from the live run: the pipeline passes
`params.pipeline` and `dspy_agent.__class__.__name__`, whose classes are registered once in
`PIPELINE_DSPY_MODULE_CLASSES` (`Main_ingest_generic_facts.py`).

**Chunk and predecessor lineage are additive, not alternative.** `save_fact` concatenates
the two lists, so a derived fact that also cites chunks records both. Predecessor
resolution costs one `select_fact4celex` per declared predecessor and is skipped entirely
for the majority of facts, which declare none.

---

## 6. Read paths

### 6.1 `celex_facts.fact_provenance(p_fact_id uuid)`

A `LANGUAGE sql STABLE` function (defined in [pg_celex-ddl.sql](src/pipelines/rag/pgvector/pg_celex-ddl.sql))
built on a recursive CTE:

- **Base:** all non-`gated_by` links of the target fact.
- **Recurse:** whenever a source is itself of `source_type = 'fact'`, expand that
  predecessor's own links. The join is on `sr_parent.source_fact_id`, not on the registry
  surrogate PK — a subtle bug the current version fixes: `source_id` never matches a
  `facts.id`.
- **Guards:** a `visited uuid[]` array blocks cycles; `depth < 10` bounds runaway chains.
- **Emit:** only terminal, non-`fact` sources, each carrying `ancestry_path_names` — the
  fact-name trail built with `string_agg` over `unnest(ancestry_path) WITH ORDINALITY`,
  e.g. `fines -> fine`.

### 6.2 `LineageRepository.get_fact_provenance(celex_id, fact_name)`

The function returns the whole tree; the UI wants one line. `_FACT_PROVENANCE_SQL` wraps
it and picks the single best terminal:

1. A `lineage_facts` CTE collects the fact plus its transitive `derived_from`
   predecessors.
2. Each terminal chunk is left-joined to `celex_facts.fact_evidence` for the max
   `final_score` produced by the evidence verifier **for any fact in that lineage** — an
   aggregate fact such as `fines` has no chunk score of its own, so it inherits its
   predecessor's score on the shared chunk.
3. Ordering: verifier-scored terminals first, then by score, then by shallowest `depth`,
   then by highest `trust_rank`. `LIMIT 1`.

The result renders as `fines -> fine -> [score=1, doc, pos=28]`. This is the join that
makes the registry and the verifier ([Design Fact Verifier](docs/Design%20Fact%20Verifier.md))
one system rather than two ledgers.

### 6.3 Presentation

[retrieval_provenance.py](src/pipelines/agent_dspy/retrieval_provenance.py) turns those
rows into the `References:` block: chunk terminals become collapsible `<details>` sections
holding the verbatim source text; non-chunk terminals render as plain bullets. Lookup
failures are logged and swallowed — a provenance error must never break an answer.

### 6.4 Other readers

- `evidence_verifier_module._provenance_chunks` — reads `chunk` / `book_case` sources of a
  fact with their trust ranks to seed verification candidates.
- `knowledge_ingestor.ingest` — walks the supporting facts' links so a new knowledge item
  inherits edges to the same catalogue rows (`supports`, or `derived_from` when the source
  is itself a fact).

---

## 7. Rationale for the non-obvious choices

**Why a surrogate `source_id` and not the natural key as PK?** The key column differs per
type. A composite or sparse PK would be unusable as an FK target;
`knowledge_edges.dst_source_id` and `fact_source_link.source_id` both need one narrow
uuid column. This follows the project rule: uuid for surrogate PKs we own, varchar for
natural keys we do not.

**Why `chunk_id varchar` and not `uuid`?** `pg_embedding_qwen.id` is LangChain-owned and
declared varchar even though it holds UUID-format strings. FK type must equal referenced
PK type; `migrate_harmonize_identifier_types.sql` retyped the column from uuid to varchar
for exactly this reason.

**Why keep `chunk_seq int[]` when `chunk_id` is the key?** Position is what a human reads
(`pos=28`) and what the legacy `references_doc_json` stored. It is display and backfill
metadata, deliberately excluded from every unique index, because re-chunking a document
changes positions but not the identity of the evidence.

**Why catalogue a predecessor fact as a source at all?** It makes derivation homogeneous
with extraction: one traversal walks `fact -> fact -> ... -> chunk` without a second edge
table, and the recursion terminates naturally because only non-`fact` rows are emitted.

**Why `gated_by` instead of just not writing the edge?** The gate is real provenance for
an auditor ("why was this fact extracted?"), but it is not the answer to "where does this
value come from?". Recording it and excluding it from traversal keeps both questions
answerable.

---

## 8. Known drift and open items

1. **The migration files still name the old table.** `migrate_lineage_tables.sql`,
   `migrate_lineage_unique_keys.sql`, `migrate_add_fact_source_type.sql`,
   `migrate_knowledge_edge_source_id.sql`, and `migrate_harmonize_identifier_types.sql`
   all say `celex_facts.source_registry`. The live database and the ORM say
   `fact_source_registry`, and no rename migration exists in the repo. Running the
   migration files as-is on a fresh database creates a second, wrong-named, empty table.
   Use `pg_celex-ddl.sql` for a fresh install; see the runbook.
2. **Trust-rank comment drift.** The DDL comment block lists defaults without `fact=4`,
   which Python does define. The Python table is authoritative.
3. **86 facts cite chunk positions that no longer resolve.** They carry a non-empty
   `references_doc_json` but no lineage edge, because their `doc_pos` values match no row
   in `pg_embedding_qwen` — almost certainly a re-chunking that moved positions. Position
   is display metadata precisely because it is unstable (section 7); these rows are the
   residue of the era when it was also the key. They need re-extraction, not a lineage
   repair.

Closed on 2026-08-25: `v_fact_provenance` (the comments and work-script queries now name
`fact_provenance(uuid)`, the only supported entry point) and the missing runtime
`derived_from` (section 5).

---

# Runbook — fact_source_registry

All commands assume the project root and the project venv. `PSQL` below stands for your
`psql` invocation against the `celex_facts` database (credentials from
`config_main/.env`; never inline a connection string).

## R1. Fresh install

Order matters — the registry references `facts`, and `knowledge_edges` references the
registry.

```bash
# 1. Full schema, including fact_source_registry, fact_source_link, the enums,
#    the knowledge_edges FK, and celex_facts.fact_provenance().
$PSQL -f src/pipelines/rag/pgvector/pg_celex-ddl.sql
```

Do **not** run `migrate_lineage_tables.sql` on a fresh database: it creates
`celex_facts.source_registry` under the pre-rename name (section 8, item 1). The lineage
migration files are history for databases provisioned before the rename.

Verify:

```sql
SELECT to_regclass('celex_facts.fact_source_registry'),
       to_regclass('celex_facts.fact_source_link');
SELECT enumlabel FROM pg_enum e
JOIN pg_type t ON t.oid = e.enumtypid
WHERE t.typname = 'relation_type_enum' ORDER BY e.enumsortorder;
-- expect: extracted_from, corroborated_by, entity_resolved_via, derived_from, gated_by
```

## R2. Enum changes

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block on older PostgreSQL, and
a new enum value is not usable by other statements in the same transaction. Run enum
additions **alone and first**, then everything that uses them:

```bash
# 1. enum only, committed on its own
$PSQL -c "ALTER TYPE celex_facts.relation_type_enum ADD VALUE IF NOT EXISTS 'gated_by';"
# 2. only then re-apply the function that references it
$PSQL -f src/pipelines/rag/pgvector/pg_celex-ddl.sql   # or just the CREATE OR REPLACE FUNCTION block
```

## R3. Repairing missing lineage

There is no lineage backfill any more. `src/batch/Main_migrate_lineage_backfill.py` was
retired on 2026-08-25 once the ingestion path began writing its own `derived_from` /
`gated_by` edges; keeping it would have meant two divergent implementations of the same
classification. **Re-ingesting the fact is the repair**, and it rebuilds chunk *and*
predecessor edges in one write:

```bash
PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-32002D0180 --filter-fact court_list --overwrite
```

`--filter-fact` pulls in the fact's whole predecessor closure, so a derived fact and the
facts it rests on are re-extracted together. `--overwrite` is required when the fact row
already exists, otherwise the pipeline short-circuits on the existence check and writes
nothing.

HeidelTime cartel-period lineage is a separate one-shot:

```bash
PYTHONPATH=src .venv/bin/python src/pipelines/heideltime/heideltime_source_register_fisher.py --dry-run
PYTHONPATH=src .venv/bin/python src/pipelines/heideltime/heideltime_source_register_fisher.py
```

## R4. Health checks

```sql
-- What kinds of evidence are catalogued, and how much of each?
SELECT source_type, count(*) FROM celex_facts.fact_source_registry GROUP BY 1 ORDER BY 2 DESC;


-- chunk	14905
-- fact	10284
-- heideltime	4433
-- book_case	239

-- Edge mix. gated_by should be a small minority; extracted_from should dominate.
SELECT relation_type, count(*) FROM celex_facts.fact_source_link GROUP BY 1 ORDER BY 2 DESC;
-- extracted_from 44224
-- derived_from 19926
-- corroborated_by 4219
-- gated_by 4054


-- Links whose producer provenance is a scar from the retired backfill, not a
-- live run. As of 2026-08-25: ~8.4k rows with pipeline='backfill' (the fact name
-- was absent from FACT_EXTRACTOR.yaml, so no pipeline could be resolved) and
-- ~58k with extraction_method='backfill'. Nothing can repair these in place any
-- more -- only re-ingesting the fact overwrites them with live provenance.
SELECT count(*) FROM celex_facts.fact_source_link
WHERE dspy_module IS NULL OR pipeline = 'backfill' OR extraction_method = 'backfill';
--66359

-- Facts with no lineage at all — the real coverage gap.
SELECT f.name, count(*) AS facts_without_lineage
FROM celex_facts.facts f
LEFT JOIN celex_facts.fact_source_link l ON l.fact_id = f.id
WHERE l.fact_id IS NULL
GROUP BY 1 ORDER BY 2 DESC;
-- heideltime_raw 744
-- companies 740
-- case_is_landmark_in_book 723
-- law_article_all_relevant 714
-- leniency_case 689
-- ..

-- Orphaned registry rows (catalogued but cited by nothing, facts or knowledge).
SELECT count(*) FROM celex_facts.fact_source_registry r
WHERE NOT EXISTS (SELECT 1 FROM celex_facts.fact_source_link l WHERE l.source_id = r.source_id)
  AND NOT EXISTS (SELECT 1 FROM celex_facts.knowledge_edges k WHERE k.dst_source_id = r.source_id);

-- Normalization check: any natural key catalogued more than once?
-- (Zero rows expected; a non-zero result means a UNIQUE index is missing.)
SELECT source_type, chunk_id, count(*) FROM celex_facts.fact_source_registry
WHERE chunk_id IS NOT NULL GROUP BY 1,2 HAVING count(*) > 1;

-- Recorded contradictions awaiting review.
SELECT f.celex_id, f.name, l.conflict_note
FROM celex_facts.fact_source_link l
JOIN celex_facts.facts f ON f.id = l.fact_id
WHERE l.conflict_flag;
```

## R5. Trace one fact end to end

```sql
-- Full lineage tree for a named fact in one case.
SELECT p.ancestry_path_names, p.depth, p.source_type, p.chunk_seq,
       p.trust_rank, p.relation_type, p.pipeline, p.dspy_module
FROM celex_facts.facts f
CROSS JOIN LATERAL celex_facts.fact_provenance(f.id) p
WHERE f.celex_id = 'CELEX-32002D0180'
  AND f.name     = 'court_list'
ORDER BY p.depth, p.trust_rank DESC;

-- The evidence text behind those chunk terminals.
SELECT f.name, sr.source_type, d.doc_pos, left(d.document, 300) AS snippet
FROM celex_facts.fact_source_link fsl
JOIN celex_facts.fact_source_registry sr ON sr.source_id = fsl.source_id
JOIN celex_facts.facts f                 ON f.id = fsl.fact_id
JOIN celex_facts.pg_embedding_qwen d     ON d.id = sr.chunk_id
WHERE f.celex_id = 'CELEX-32002D0180' AND f.name = 'court_list';
```

Remember the `celex_id` storage format: the chunk and registry rows keep the `CELEX-`
prefix. Querying `32002D0180` returns nothing.

## R6. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Answer has no `References:` block | fact has no `fact_source_link` rows | R4 "facts with no lineage", then re-ingest the fact (R3) |
| Reference shows the wrong lineage, e.g. a company chunk under `fines` | a `derived_from` predecessor's terminal outranked the fact's own | check `fact_evidence.final_score` for the fact's lineage; the ranking prefers verifier-scored terminals, so an unverified fact falls back to depth/trust ordering |
| `ancestry_path_names` shows `?` in the trail | an ancestry fact id no longer resolves in `facts` (deleted fact, links cascaded but path array retained) | re-ingest that case (R3) |
| `derived_from` edges missing after ingestion | the predecessor fact row did not exist when the derived fact was written, so it was skipped | re-ingest with `--filter-fact`, which pulls in the predecessor closure (R3) |
| Duplicate registry rows for one chunk | UNIQUE indexes absent on a pre-rename database | apply the index block from `pg_celex-ddl.sql`, after the dedup step in `migrate_lineage_unique_keys.sql` |
| `unsafe use of new value ... of enum type` | enum value added in the same transaction as its first use | R2: enum alone, commit, then the rest |
| FK violation writing a knowledge edge | the registry row was not committed before the edge insert | `knowledge_ingestor` reads sources before opening its write transaction — keep that order |

## R7. Tests

```bash
# Unit tests run without a database; the integration classes need a live celex_facts DB.
PYTHONPATH=src .venv/bin/python -m unittest \
    src/pipelines/rag/pgvector/tests/test_lineage_repository.py -v

# Ingestion-side lineage: gate classification, predecessor sources, and the
# additive chunk+predecessor assembly (repository mocked at the DB boundary).
PYTHONPATH=src .venv/bin/python -m unittest \
    src/tools/tests/test_PipelineFactIngestor.py -v

# Provenance rendering.
PYTHONPATH=src .venv/bin/python -m unittest \
    src/pipelines/agent_dspy/tests/test_provenance_references.py -v
```

Integration teardown must delete a registry row **only when orphaned**
(`_DELETE_ORPHANED_SOURCE_SQL`): sources are shared, and an unguarded delete either
violates the `fact_source_link` FK or strips provenance from facts the test never created.



# Quest on empty R5

R5's first query (the recursive `fact_provenance()`) correctly returns the terminal chunk one hop up, at `depth=1`, ancestry `court_list → court_list_raw`. The reingest worked fine.

**Why the second R5 query came back empty:** it's a flat, non-recursive join —

```sql
JOIN celex_facts.fact_source_link fsl ...
WHERE f.name = 'court_list'
```

That only sees `fact_source_link` rows where `fact_id` = the `court_list` fact itself. But `court_list` (pipeline `fact_context`, `predecessors: [court_list_raw]` in [FACT_EXTRACTOR.yaml:513-526](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/config_main/FACT_EXTRACTOR.yaml#L513-L526)) is a purely _derived_ fact — `FactContextModule` doesn't re-extract from chunks, it just consumes `court_list_raw`'s output. So `court_list`'s only registry row is `source_type='fact'` (`chunk_id IS NULL`, pointing at `court_list_raw`). The actual chunk evidence (`source_type='chunk'`, `extracted_from`) is registered under `court_list_raw`, one hop away in the lineage tree — exactly what section 7 of the design doc calls out as the reason predecessor facts get catalogued as sources at all: _"one traversal walks `fact -> fact -> ... -> chunk`"_.

The second query was written as a single-hop illustration ("the evidence text behind those chunk terminals") and never claimed to walk the tree — it's not wrong, just not the right tool for a derived fact like `court_list`. To get the snippet text for a derived fact, join through the predecessor's `chunk_id` instead:

```sql
SELECT f.name, sr2.source_type, d.doc_pos, left(d.document, 300) AS snippet
FROM celex_facts.facts f
JOIN celex_facts.fact_source_link fsl1 ON fsl1.fact_id = f.id
JOIN celex_facts.fact_source_registry sr1 ON sr1.source_id = fsl1.source_id AND sr1.source_type = 'fact'
JOIN celex_facts.fact_source_link fsl2 ON fsl2.fact_id = sr1.source_fact_id
JOIN celex_facts.fact_source_registry sr2 ON sr2.source_id = fsl2.source_id
JOIN celex_facts.pg_embedding_qwen d ON d.id = sr2.chunk_id
WHERE f.celex_id = 'CELEX-32002D0180' AND f.name = 'court_list';
```

or more generally, use `fact_provenance()` for the ids and then join the terminal `chunk_id` to `pg_embedding_qwen` — which is what `LineageRepository.get_fact_provenance` already does (section 6.2).

`★ Insight ─────────────────────────────────────` This is a nice illustration of why the design doc catalogues predecessor facts as sources at all (section 7, "Why catalogue a predecessor fact as a source?"): it makes derivation homogeneous with extraction, so one recursive function (`fact_provenance`) can walk `fact → fact → chunk` without a second edge table. The cost is that any _hand-written, non-recursive_ SQL against `fact_source_link` only sees one hop — which is exactly the trap the second R5 query fell into today. The runbook's own R6 troubleshooting table doesn't list this failure mode ("query written for a leaf fact returns nothing for a derived one"), which is worth adding. `─────────────────────────────────────────────────`

Since this is a real gap in the runbook (not a bug in the ingestion/lineage code), I'll leave the design doc alone since you're already mid-edit on it (git status shows it modified) — want me to add this failure mode to the R6 troubleshooting table, or would you rather fold it in yourself while you're already in that file?