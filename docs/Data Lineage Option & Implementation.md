

Data origination 
740 celex case documents in markdown format split in chunks of  ~400 token mostly with a starting heading
Book of ~100 celex cases with details about infringments
csv file with ~300 human indexed celex cases
corporate index from LEI registery data source
heideltime module providing normalizied event dates from the 740 celex case documents

All of that is aggregated in a fact table within a references_doc_json which encode a list of number if the chunk  sequence but not if the sources are not in the document chunks.

for proper data lineage in this legal domain, I need a comprehensive registry for each fact.  what are my design options?



## Data Lineage Registry Design Options

This is a rich provenance problem — legal facts need audit-grade traceability back to specific evidence, not just source names. Here are your main design options, ordered from pragmatic to rigorous:

---

### Option 1 — Flat Provenance Envelope (Simplest)

Extend each fact record with a `provenance` object inline:

```json
{
  "fact_id": "F-00123",
  "value": "2021-03-15",
  "provenance": {
    "sources": [
      { "type": "chunk", "chunk_id": "CEL-0042-chunk-07", "celex": "32021R0001" },
      { "type": "csv_index", "row": 142, "field": "infringement_date" },
      { "type": "heideltime", "normalized": "2021-03-15", "original": "March 2021" }
    ],
    "confidence": "multi-source-confirmed",
    "generated_at": "2025-05-30T..."
  }
}
```

**Good for:** Quick implementation, works inside your existing `references_doc_json` structure. **Bad for:** Querying across facts by source type, detecting conflicts, diff-tracking over time.

---

### Option 2 — Separate Lineage Table (Relational / Graph-ready)

Decouple facts from their evidence with a normalized join table:

```
fact_registry          fact_source_registry             fact_source_link
──────────────         ───────────────             ────────────────
fact_id (PK)           source_id (PK)              fact_id (FK)
fact_type              source_type                 source_id (FK)
fact_value             celex_id                    extraction_method
celex_scope            chunk_seq []                trust_rank
created_at             lei_id                      conflict_flag
                       csv_row                     created_at
                       heideltime_event_id
```

The `source_type` enum covers: `chunk`, `csv_human`, `book_case`, `lei_corporate`, `heideltime`.

**Good for:** Cross-source queries, surfacing conflicts (`WHERE conflict_flag = true`), joining LEI corporate data to specific chunks. **Bad for:** Slightly more overhead to maintain; your `references_doc_json` chunk sequences need a translation step.

---

### Option 3 — Provenance Graph (Most Rigorous)

Model lineage as a directed graph where nodes are facts, sources, and transforms — and edges are derivation relationships:

```
[chunk CEL-0042-chunk-07]
        │ extracted_by heideltime
        ▼
[event_date: "2021-03-15"]
        │ corroborated_by
        ▼
[csv_index row:142]
        │ entity_resolved_via
        ▼
[LEI: 549300XYZ...]
        │ asserted_in
        ▼
[fact F-00123]
```

Implementable in: a property graph (Neo4j, Kuzu), or as a JSON-LD/RDF structure for interoperability with legal ontologies (ELI, ECLI).

**Good for:** Full audit trails, explaining _how_ a fact was derived, legal defensibility, detecting circular or weak derivations. **Bad for:** Heavier infrastructure; overkill if your consumers only need source attribution.

---

### Option 4 — Event-Sourced Ledger (For Mutation Tracking)

If your facts are updated over time (re-indexing, corrections, new documents added), log every derivation as an immutable event:

```json
{
  "event": "fact_asserted",
  "fact_id": "F-00123",
  "run_id": "pipeline-2025-05-30",
  "sources_at_assertion": [...],
  "supersedes": null
}
```

New pipeline runs emit `fact_revised` or `fact_retracted` events. You can reconstruct the state of any fact at any point in time.

**Good for:** Regulatory audits, pipeline reruns, catching regressions when heideltime or the CSV index changes. **Bad for:** Most complex to query current state; needs a reducer/materialized view layer on top.

---

### Recommendation for Your Setup

Given your mix of structured (CSV, LEI) and unstructured (chunks, heideltime) sources, the pragmatic path is **Option 2 as the core**, with **Option 3's edge semantics added selectively** for high-stakes facts:

|Concern|Approach|
|---|---|
|Chunk sequences in `references_doc_json`|Map chunk numbers → `source_id` via a chunk registry at ingest|
|Sources absent from doc chunks (CSV, LEI, book)|Assign synthetic `source_id`s with `source_type` flag — no chunk reference|
|HeidelTime dates|Store both raw string and normalized ISO date; link to originating chunk|
|Conflict detection|Add `conflict_flag` + `conflict_note` to `fact_source_link`|
|LEI corporate resolution|Treat as a resolution step, not a primary source — encode as `derived_from` edge|

The key design principle throughout: **chunk sequence numbers are not stable identifiers** — your registry needs to anchor to `(celex_id, chunk_seq)` pairs, so a re-chunking run doesn't silently invalidate your lineage.

Want me to sketch out a concrete schema (SQL DDL, JSON Schema, or RDF/JSON-LD) for any of these?



# Implemented


**New files:**

- [migrate_lineage_tables.sql](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/migrate_lineage_tables.sql) — idempotent DDL (both enum types + 2 tables + indexes; already applied)
- [lineage_repository.py](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/lineage_repository.py) — `LineageRepository` with one `register_*` method per source type, `link_fact_to_source` (upsert), `flag_conflict`, and read methods
- [tests/test_lineage_repository.py](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/tests/test_lineage_repository.py) — 25 tests covering unit (no DB), source registration, linking, idempotency, conflict flagging, and Option 3 relation types
- [Main_migrate_lineage_backfill.py](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/batch/Main_migrate_lineage_backfill.py) — one-shot backfill from `references_doc_json`; supports `--dry-run` and `--celex-id` flags

**Modified files:**

- [models.py](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/models.py) — added `SourceRegistry` and `FactSourceLink` ORM models with proper `PgEnum` types pointing at the schema-qualified DB enums
- [PipelineFactIngestor.py](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/tools/PipelineFactIngestor.py) — added `LineageSourceVO` pydantic model; `save_fact()` accepts an optional `lineage_sources` list; `FactIngestor.__init__` accepts optional `lineage_repo`; fully backward compatible
- [pg_celex-ddl.sql](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/pg_celex-ddl.sql) — canonical schema doc updated with the new DDL



# Review knowledge_edges vs fact_source_registry

## The two subsystems model different layers

You actually have a **two-tier provenance system**, and the overlap is only partial:

|Table|What it relates|Relation semantics|Purpose|
|---|---|---|---|
|[fact_source_registry](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_lineage_tables.sql#L49) + [fact_source_link](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_lineage_tables.sql#L92)|`fact → raw evidence`|`extracted_from`, `corroborated_by`, `entity_resolved_via`, `derived_from`|catalog of **primary evidence atoms** (chunk, csv, book, lei, heideltime) + fact grounding|
|[knowledge_edges](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql#L499)|`knowledge_item → {chunk \| fact \| knowledge_item}`|`supports`, `derived_from`, `contradicts`|the **knowledge reasoning graph**|

The crucial asymmetry: `fact_source_registry.source_type_enum` contains only _raw, external_ source types — there is no `fact` and no `knowledge_item`. That's a deliberate (and correct) statement: **the registry is a catalog of primary evidence, not of derived assertions.**

`knowledge_edges`, by contrast, can express `knowledge_item --contradicts--> knowledge_item`. That is _not_ provenance — it's a logical/semantic relationship between two derived nodes. **`fact_source_registry` structurally cannot represent that**, so a full replacement would lose your knowledge graph.

So the clean mental model is three tiers:

```
Tier 0  raw evidence        fact_source_registry        (chunk, csv, book, lei, heideltime)
            ▲ fact_source_link
Tier 1  facts               facts
            ▲ knowledge_edges (dst_fact_id)
Tier 2  knowledge_items     knowledge_items   ◄──┐ knowledge_edges (dst_knowledge_id:
                                                 └─  supports / contradicts)
```

## What you've actually spotted: a real duplication

The genuine overlap is the **`knowledge_edges.dst_chunk_id`** arm. Right now a knowledge_item grounded directly in a chunk points at [pg_embedding_qwen](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql#L505) via `dst_chunk_id`, while a fact grounded in that _same chunk_ goes through `fact_source_registry`. Two different ways to point at the same evidence, at different grain (raw varchar PK vs. registry UUID with `chunk_seq`/`celex_id` metadata). And as you anticipated — the moment a knowledge_item needs to be grounded in a CSV row, a LEI record, or a HeidelTime date, `knowledge_edges` has no column for it and you'd be adding `dst_csv_*`, `dst_lei_*` columns forever.

## Recommendation: don't replace — collapse the raw-evidence arm into the registry

Keep `knowledge_edges` for the **derived-node graph only** (`dst_fact_id`, `dst_knowledge_id` — the supports/contradicts/derived_from reasoning), and replace its `dst_chunk_id` with a `dst_source_id → fact_source_registry`:

```sql
-- knowledge_edges destination becomes:
dst_source_id     uuid REFERENCES celex_facts.fact_source_registry(source_id),  -- raw evidence (any type)
dst_fact_id       uuid REFERENCES celex_facts.facts(id),                   -- derived: Tier 1
dst_knowledge_id  uuid REFERENCES celex_facts.knowledge_items(id),         -- derived: Tier 2
CHECK (num_nonnulls(dst_source_id, dst_fact_id, dst_knowledge_id) = 1)
```

This gives you:

- **One canonical evidence catalog.** Any grounding to raw evidence — whether asserted by a fact or a knowledge_item — flows through `fact_source_registry`. Add a new `source_type` once and both subsystems get it for free. Directly solves your "something other than a fact as source of a knowledge_item" concern.
- **No loss of the knowledge graph.** `contradicts`/`supports` between knowledge_items stays where it belongs.
- **Cleaner grounding trigger.** Your [trg_knowledge_must_have_edge](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql#L568) keeps working unchanged — an edge to a `source_id` still counts as grounding.

The alternative — a symmetric `knowledge_source_link` table mirroring `fact_source_link` — is also defensible and gives you the richer provenance metadata (`trust_rank`, `conflict_flag`, `conflict_note`) that `knowledge_edges` currently lacks. The trade-off: knowledge grounding then splits across two tables (`knowledge_source_link` for raw evidence, `knowledge_edges` for the derived graph), which complicates the grounding trigger. I'd only go this way if you want conflict-detection on knowledge grounding as a first-class feature.

## Bottom line


## The two subsystems model different layers

You actually have a **two-tier provenance system**, and the overlap is only partial:

|Table|What it relates|Relation semantics|Purpose|
|---|---|---|---|
|[fact_source_registry](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_lineage_tables.sql#L49) + [fact_source_link](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_lineage_tables.sql#L92)|`fact → raw evidence`|`extracted_from`, `corroborated_by`, `entity_resolved_via`, `derived_from`|catalog of **primary evidence atoms** (chunk, csv, book, lei, heideltime) + fact grounding|
|[knowledge_edges](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql#L499)|`knowledge_item → {chunk \| fact \| knowledge_item}`|`supports`, `derived_from`, `contradicts`|the **knowledge reasoning graph**|

The crucial asymmetry: `fact_source_registry.source_type_enum` contains only _raw, external_ source types — there is no `fact` and no `knowledge_item`. That's a deliberate (and correct) statement: **the registry is a catalog of primary evidence, not of derived assertions.**

`knowledge_edges`, by contrast, can express `knowledge_item --contradicts--> knowledge_item`. That is _not_ provenance — it's a logical/semantic relationship between two derived nodes. **`fact_source_registry` structurally cannot represent that**, so a full replacement would lose your knowledge graph.

So the clean mental model is three tiers:

```
Tier 0  raw evidence        fact_source_registry        (chunk, csv, book, lei, heideltime)
            ▲ fact_source_link
Tier 1  facts               facts
            ▲ knowledge_edges (dst_fact_id)
Tier 2  knowledge_items     knowledge_items   ◄──┐ knowledge_edges (dst_knowledge_id:
                                                 └─  supports / contradicts)
```

## What you've actually spotted: a real duplication

The genuine overlap is the **`knowledge_edges.dst_chunk_id`** arm. Right now a knowledge_item grounded directly in a chunk points at [pg_embedding_qwen](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql#L505) via `dst_chunk_id`, while a fact grounded in that _same chunk_ goes through `fact_source_registry`. Two different ways to point at the same evidence, at different grain (raw varchar PK vs. registry UUID with `chunk_seq`/`celex_id` metadata). And as you anticipated — the moment a knowledge_item needs to be grounded in a CSV row, a LEI record, or a HeidelTime date, `knowledge_edges` has no column for it and you'd be adding `dst_csv_*`, `dst_lei_*` columns forever.

## Recommendation: don't replace — collapse the raw-evidence arm into the registry

Keep `knowledge_edges` for the **derived-node graph only** (`dst_fact_id`, `dst_knowledge_id` — the supports/contradicts/derived_from reasoning), and replace its `dst_chunk_id` with a `dst_source_id → fact_source_registry`:

```sql
-- knowledge_edges destination becomes:
dst_source_id     uuid REFERENCES celex_facts.fact_source_registry(source_id),  -- raw evidence (any type)
dst_fact_id       uuid REFERENCES celex_facts.facts(id),                   -- derived: Tier 1
dst_knowledge_id  uuid REFERENCES celex_facts.knowledge_items(id),         -- derived: Tier 2
CHECK (num_nonnulls(dst_source_id, dst_fact_id, dst_knowledge_id) = 1)
```

This gives you:

- **One canonical evidence catalog.** Any grounding to raw evidence — whether asserted by a fact or a knowledge_item — flows through `fact_source_registry`. Add a new `source_type` once and both subsystems get it for free. Directly solves your "something other than a fact as source of a knowledge_item" concern.
- **No loss of the knowledge graph.** `contradicts`/`supports` between knowledge_items stays where it belongs.
- **Cleaner grounding trigger.** Your [trg_knowledge_must_have_edge](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql#L568) keeps working unchanged — an edge to a `source_id` still counts as grounding.

The alternative — a symmetric `knowledge_source_link` table mirroring `fact_source_link` — is also defensible and gives you the richer provenance metadata (`trust_rank`, `conflict_flag`, `conflict_note`) that `knowledge_edges` currently lacks. The trade-off: knowledge grounding then splits across two tables (`knowledge_source_link` for raw evidence, `knowledge_edges` for the derived graph), which complicates the grounding trigger. I'd only go this way if you want conflict-detection on knowledge grounding as a first-class feature.

## Bottom line

- **Replace `knowledge_edges` entirely with `fact_source_registry`?** No — you'd lose knowledge↔knowledge semantic edges, which the registry can't model.
- **Link them?** Yes. Point the _raw-evidence_ arm of `knowledge_edges` at `fact_source_registry` (`dst_source_id`) instead of a bare `dst_chunk_id`. That unifies the evidence catalog without flattening derived assertions into it.

The one principle to hold onto: **`fact_source_registry` is for primary evidence; facts and knowledge_items are derived nodes and must never become rows in it.** Keep that boundary and the two subsystems compose cleanly into one provenance chain (`knowledge → fact → source`).

Want me to write the migration (drop `dst_chunk_id`, add `dst_source_id` + the new CHECK, backfill existing chunk edges into `fact_source_registry`) and update the `KnowledgeEdge` model + `add_edge()` signature in [knowledge_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/knowledge_repository.py#L446)?


## Summary

I routed `knowledge_edges`' raw-evidence grounding through the canonical `fact_source_registry`, so facts and knowledge items now ground against **one** evidence catalog — while preserving the knowledge↔knowledge reasoning graph (`supports`/`contradicts`) that the registry can't express.

**Schema** — [migrate_knowledge_edge_source_id.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_knowledge_edge_source_id.sql) (new, idempotent):

- Adds `dst_source_id → fact_source_registry(source_id)`.
- Backfills: catalogues every chunk currently in `dst_chunk_id` as a `chunk` source (idempotent on `chunk_id`, casting the UUID-format varchar PK), repoints each edge, then drops `dst_chunk_id` (which also drops the old `num_nonnulls` CHECK and the FK to `pg_embedding_qwen`).
- Re-asserts the "exactly one destination" CHECK over `{dst_source_id, dst_fact_id, dst_knowledge_id}` and indexes the new FK.

**Canonical DDL** — [pg_celex-ddl.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql): updated the `knowledge_edges` definition; the FK is added in a dated migration block after `fact_source_registry` (which is defined later in the file), keeping a fresh top-to-bottom build valid.

**Model** — [models.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/models.py): `KnowledgeEdge.dst_chunk_id` → `dst_source_id` (FK to `fact_source_registry.source_id`), with the layered-provenance rationale in the docstring.

**Repository** — [knowledge_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/knowledge_repository.py): `insert_edge` and `_validate_edge_destinations` now take `dst_source_id`.

**Ingestor** — [knowledge_ingestor.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/knowledge_ingestor.py): `supports` edges now register the chunk in `fact_source_registry` (via an injected `LineageRepository`, sharing the engine, enrolled in the same transaction) and link via `dst_source_id`. A new `_register_chunk_source` resolves the chunk's CELEX scope and raises `ValueError` for unknown chunks — preserving the integrity guarantee the dropped FK used to give.

**Tests** — [test_knowledge_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/tests/test_knowledge_repository.py): validation tests and assertions updated to `dst_source_id`.

**Verification**: ruff introduces no new warnings (the 4 reported all pre-exist on baseline); ORM mappers configure cleanly with the new FK; the ingestor imports; the 6 DB-free unit tests pass. The integration tests in this module and the ingest-module tests require a live `celex_facts` database, which I didn't run — and the migration itself needs to be applied to the DB before they will pass against the new schema.

One thing worth flagging: `fact_source_registry.chunk_id` is `UUID` while `pg_embedding_qwen.id` is `varchar` (holding UUID-format strings). That mismatch predates this change and is why there's no real FK between them — the migration casts explicitly. Not in scope here, but worth a future cleanup.

fixed 
**Your question: no, don't harmonize to varchar.** Harmonize _up_ to native `uuid`. The cast friction wasn't UUID-the-type — it was an incomplete migration where surrogate keys holding UUID values were `varchar` in the legacy core but native `uuid` in the newer lineage/knowledge subsystem. They clashed at the FKs. Dropping everything to varchar would've made a 36-char text column the join key for your vector/lineage tables — slower joins, bigger indexes, no format guarantees.

**Rule adopted:** an FK column's type always equals the PK it references. Surrogate PKs you own = `uuid`; external natural keys (`celex_id`, `lei_id`, `case_id`) = `text`.

### Changes

- **New migration** [migrate_harmonize_identifier_types.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_harmonize_identifier_types.sql) — idempotent, guarded; `facts.id` varchar→uuid (drops/recreates dependent FKs incl. `celex_fact_ingestion_audit_trail.fact_id`), and `fact_source_registry.chunk_id` uuid→varchar (rebuilds its indexes). Verified: ran twice, second run is a clean no-op.
- **DDL** [pg_celex-ddl.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql) — `facts.id uuid`, `audit_trail.fact_id uuid`, `fact_source_registry.chunk_id varchar`.
- **ORM/Python** — `SourceRegistry.chunk_id`→String, `LineageSourceVO.chunk_id`→str, repo signatures, `resolve_chunk_ids_by_doc_pos`, `knowledge_ingestor`, backfill batch; dropped the `::uuid` casts in [migrate_knowledge_edge_source_id.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_knowledge_edge_source_id.sql); removed a now-unused import; updated test fixtures to pass `str` chunk ids.

### Chunk arm note

The plan/memory recorded your _first_ answer (varchar) and your second selection said "uuid everywhere." I implemented **varchar** — it's the correct call regardless: `pg_embedding_qwen.id` is a LangChain-PGVector-owned varchar PK, and the rule says match the referenced PK without mutating that table. If you truly want chunk ids native-uuid, that's a larger, separate change to the LangChain vector tables and their ingestion path — say the word and I'll scope it.

### Verification

- DB end-state: `facts.id=uuid`, `audit_trail.fact_id=uuid`, `fact_source_registry.chunk_id=character varying`. ✅
- Applied the pending edge migration (you approved): `knowledge_edges.dst_source_id` present, legacy `dst_chunk_id` dropped. ✅
- `ruff check` clean on all changed files. ✅
- pgvector suite: 139 tests, only the 1 pre-existing unrelated `test_knowledge_judge` error remains (proven not mine). Lineage (34) and knowledge-edge tests now green. PipelineFactIngestor: 5 OK.

I saved the type convention to memory so future schema work follows the same rule.

One caveat worth flagging: the migration runs `ALTER COLUMN ... TYPE uuid` which rewrites `facts` and rewrites/locks dependents — fine on dev, but run it in a maintenance window on prod, and run the pre-flight format check first (it's in the migration header).




# Plan: Fill `pipeline` + `dspy_module` on `fact_source_link`, incl. derived-fact lineage

## Context

The data-lineage registry (commit `b86edc0`) added `pipeline` and `dspy_module` columns to `celex_facts.fact_source_link`, and the live ingestion path now populates them (`PipelineFactIngestor.save_fact` → `link_fact_to_source`). But the one-shot backfill `src/batch/Main_migrate_lineage_backfill.py` — which reconstructs lineage from the legacy `facts.references_doc_json` column — still writes `pipeline="backfill"` and leaves `dspy_module` NULL ([Main_migrate_lineage_backfill.py:142-150](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/batch/Main_migrate_lineage_backfill.py#L142-L150)).

Two gaps to close:

1. **Real provenance on extraction edges.** Each fact's true `pipeline` lives in `config_main/FACT_EXTRACTOR.yaml` (the `pipeline:` field per `target_fact_name`), and the `dspy_module` is the class that pipeline maps to in `FactIngestionOrchestrator._build_agents` ([Main_ingest_generic_facts.py:339-394](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/batch/Main_ingest_generic_facts.py#L339-L394)). The backfill should look these up by `facts.name` and write the real values, **overwriting** the placeholder `pipeline="backfill"` rows.
    
2. **Derived-fact lineage.** Many facts are computed from other facts, declared via `predecessors:` in the YAML (e.g. `court_list` ← `court_list_raw` ← `is_court_involved`; also `NACE_*`, `leniency_*`, `market_outside_eu`, `heideltime_event*`, etc. — 20+ entries). These have no direct chunk references and are skipped today. Per decision, we model the predecessor fact itself as a **new `'fact'` source type** in `fact_source_registry` and link the derived fact to it with `relation_type='derived_from'`.
    

## Decisions (confirmed with user)

- **Derived facts** → add a `'fact'` source type; register the predecessor fact as a `fact_source_registry` row and link `derived_fact --derived_from--> predecessor_fact`.
- **Overwrite** existing rows with the YAML-derived `pipeline` + resolved `dspy_module` (idempotent upsert, not skip-if-exists).

## Changes

### 1. Schema — add `'fact'` source type + predecessor FK column

`fact_source_registry` currently keys each source type on a natural-key column (`chunk_id`, `heideltime_event_id`, `lei_id`, `csv_row`). Add an analogous `source_fact_id` for the new type.

- **`src/pipelines/rag/pgvector/models.py`** ([SourceRegistry, 464-543](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/models.py#L464-L543)):
    
    - Add `"fact"` to the `source_type_enum` value list (line 523).
    - Add column `source_fact_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.facts.id", ondelete="CASCADE"), nullable=True)` — the predecessor fact this source represents.
    - Add partial unique index `uq_source_registry_fact` on `source_fact_id` `postgresql_where=text("source_fact_id IS NOT NULL")` (mirrors the existing `uq_source_registry_*` indexes, 487-512) so the same predecessor fact is catalogued once.
    - Extend the class docstring's source-type table with the `fact` row.
- **New migration `src/pipelines/rag/pgvector/migrate_add_fact_source_type.sql`** (follows the idempotent `DO $$ ... END $$` style of [migrate_rename_audit_trail.sql](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/migrate_rename_audit_trail.sql)):
    
    - `ALTER TYPE celex_facts.source_type_enum ADD VALUE IF NOT EXISTS 'fact';` (note: `ADD VALUE` cannot run inside a txn block on older PG — run as its own statement / autocommit; document this in the file header).
    - `ALTER TABLE celex_facts.fact_source_registry ADD COLUMN IF NOT EXISTS source_fact_id uuid REFERENCES celex_facts.facts(id) ON DELETE CASCADE;`
    - `CREATE UNIQUE INDEX IF NOT EXISTS uq_source_registry_fact ON celex_facts.fact_source_registry (source_fact_id) WHERE source_fact_id IS NOT NULL;`
- **Fresh-create DDLs** — mirror the enum value + column + index for greenfield installs: the `CREATE TYPE ... source_type_enum` and `fact_source_registry` blocks in [migrate_lineage_tables.sql:19,51](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/migrate_lineage_tables.sql#L19) and [pg_celex-ddl.sql:644,665](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/pg_celex-ddl.sql#L644).
    

### 2. `LineageRepository` — register a fact as a source

**`src/pipelines/rag/pgvector/lineage_repository.py`**:

- Add `register_fact_source(celex_id, source_fact_id: UUID, description=None, session=None) -> UUID` alongside the other `register_*` methods (after [register_lei_source, 249-278](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/lineage_repository.py#L249-L278)), delegating to `_upsert_source(source_type="fact", source_fact_id=...)`.
- `_upsert_source` ([486-539](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/lineage_repository.py#L486)): add `source_fact_id` param; pass to both `_find_existing_source` and the `SourceRegistry(...)` constructor.
- `_find_existing_source` ([541-584](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/lineage_repository.py#L541)): add `case "fact": if source_fact_id is None: return None; filter on source_fact_id`.
- `_TRUST_RANK_DEFAULTS` ([40-46](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/lineage_repository.py#L40)): add `"fact": 4` (derived value, ranked just below a raw chunk's 5). Update the docstrings that enumerate the defaults (model + repo).

### 3. Pipeline → DSPy-module-name mapping (single source of truth)

Avoid drift from the inline registry in `_build_agents`. In **`src/batch/Main_ingest_generic_facts.py`**, add a module-level constant `PIPELINE_DSPY_MODULE_CLASSES: dict[str, type]` mapping each pipeline string to its DSPy module **class** (the classes already imported at module top), e.g. `{"universal": UniversalFactModule, "router": QueryRouterModule, "fact_context": FactContextModule, ...}` covering every key currently in `_build_agents` (339-394). Refactor `_build_agents` to reference these class objects where it instantiates, so the two never diverge. The backfill derives names via `cls.__name__`, matching live ingestion's `dspy_agent.__class__.__name__`.

### 4. Backfill — `src/batch/Main_migrate_lineage_backfill.py`

- **Load YAML once** into `fact_name -> {pipeline, predecessors}` using `settings.FACT_EXTRACTOR_CONFIG_DIR` and `yaml.safe_load` (same load the orchestrator does at [402-409](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/batch/Main_ingest_generic_facts.py#L402)). Build `pipeline -> dspy_module_name` from `PIPELINE_DSPY_MODULE_CLASSES`.
- **Helper** `_provenance_for(fact_name) -> (pipeline, dspy_module)`: YAML pipeline for the fact (fallback `"backfill"`, `None` when name not in YAML / pipeline not in the class map).
- **Phase A — extraction edges (existing flow, corrected):** keep chunk resolution, but replace `pipeline="backfill"`/no-`dspy_module` in the `link_fact_to_source` call (148-149) with the resolved provenance, and **drop the skip-if-exists guard** (139-141) so `link_fact_to_source`'s upsert overwrites placeholder rows. Track `updated` vs `inserted` (pre-check existence only for the counter, still call the upsert).
- **Phase B — derived-from edges (new):** fetch `(id, celex_id, name)` for **all** facts (not just those with `references_doc_json`), grouped by celex, into a `(celex_id, name) -> fact_id` lookup. For each fact whose YAML entry has `predecessors`, for each predecessor name present in the same celex: `register_fact_source(celex_id, source_fact_id=predecessor_fact_id)` then `link_fact_to_source(fact_id=derived_fact_id, source_id, extraction_method="backfill", relation_type="derived_from", source_type="fact", pipeline=<derived fact's pipeline>, dspy_module=<resolved>)`, all inside one `repo.Session()` transaction (mirrors 129-151). Skip predecessors with no matching fact row in that celex (log at debug, count `skipped_no_predecessor`).
- Extend the summary log counters (`linked`, `derived_linked`, `updated`, `skipped_no_chunks`, `skipped_no_predecessor`, `errors`) and the module docstring to describe the two phases.

### 5. Tests (`unittest`, mocks only at DB boundary)

- **`src/pipelines/rag/pgvector/tests/test_lineage_repository.py`**: add cases for `register_fact_source` (insert + idempotent reuse on `source_fact_id`), the `"fact"` trust-rank default (4), and `link_fact_to_source(relation_type="derived_from", source_type="fact")`.
- **`src/batch/tests/`**: a focused test for the backfill's YAML→provenance resolution and Phase-B derived-edge construction (e.g. `court_list`→`court_list_raw`), with `LineageRepository`/`ChunkRepository`/DB fetch mocked. Reuse the existing test style in [src/batch/tests/test_Main_ingest_generic_facts.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/batch/tests/test_Main_ingest_generic_facts.py). Add `__init__.py` if a new test module/dir is introduced.

## Verification

1. `ruff check` clean.
2. `.venv/bin/python -m unittest` — new + existing lineage/batch tests pass.
3. Apply migration: `psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/migrate_add_fact_source_type.sql`, then confirm `'fact'` in `source_type_enum` and the `source_fact_id` column + `uq_source_registry_fact` index exist.
4. Dry run: `PYTHONPATH=src .venv/bin/python src/batch/Main_migrate_lineage_backfill.py --dry-run --celex-id <CELEX with court_list>` — log shows derived edges that would be created.
5. Real run on one celex, then SQL-verify:
    - `fact_source_link` rows for that celex have non-NULL `pipeline`/`dspy_module` matching the YAML (e.g. `court_list_raw` → `router`/`QueryRouterModule`).
    - A `derived_from` edge exists from `court_list` to a `'fact'` source whose `source_fact_id` is the `court_list_raw` fact id.
    - Re-running is idempotent (no dupes; placeholder `pipeline='backfill'` rows now carry real values).


 ## ummary

The backfill now fills `pipeline` and `dspy_module` on `celex_facts.fact_source_link` from real provenance, and handles derived facts via a new `'fact'` source type.

### Schema — new `'fact'` source type

- [models.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/models.py): added `"fact"` to `source_type_enum`, a `source_fact_id` UUID FK→`facts.id` column, the `uq_source_registry_fact` partial unique index, and `fact=4` trust rank in the docstring.
- New migration [migrate_add_fact_source_type.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_add_fact_source_type.sql) (idempotent `ADD VALUE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` / unique index).
- Mirrored the enum value + column in fresh-create DDLs [migrate_lineage_tables.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_lineage_tables.sql), [pg_celex-ddl.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql), and the unique index in [migrate_lineage_unique_keys.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_lineage_unique_keys.sql).

### Repository

- [lineage_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/lineage_repository.py): added `register_fact_source(...)`, threaded `source_fact_id` through `_upsert_source`/`_find_existing_source` (idempotent on `source_fact_id`), and added `"fact": 4` to `_TRUST_RANK_DEFAULTS`.

### Single source of truth for `dspy_module`

- [Main_ingest_generic_facts.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/batch/Main_ingest_generic_facts.py): added module-level `PIPELINE_DSPY_MODULE_CLASSES` (pipeline → DSPy class) and refactored `_build_agents` to instantiate from it, so the backfill's recorded class names can never drift from live ingestion.

### Backfill (two phases)

- [Main_migrate_lineage_backfill.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/batch/Main_migrate_lineage_backfill.py): **Phase A** resolves each fact's `pipeline` from `FACT_EXTRACTOR.yaml` (keyed by `facts.name`) and `dspy_module` from the class map, then upserts the `extracted_from` chunk edges — **overwriting** old `pipeline='backfill'`/NULL rows. **Phase B** registers each predecessor fact (e.g. `court_list_raw`) as a `'fact'` source and links the derived fact with `relation_type='derived_from'` carrying its own provenance.

### Tests

- [test_lineage_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/tests/test_lineage_repository.py): `register_fact_source` in the protocol list, `fact=4` rank, and an integration test for fact-source registration + `derived_from` linking.
- New [test_Main_migrate_lineage_backfill.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/batch/tests/test_Main_migrate_lineage_backfill.py): provenance resolution and Phase-B derived-edge construction with the repo mocked.

`ruff check` clean; all 25 DB-free tests pass.

## Remaining manual steps (need a live DB)

These are in the plan's verification section but mutate the database, so I left them for you:

1. Apply the migration: `psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/migrate_add_fact_source_type.sql`
2. Run the lineage integration tests + a `--dry-run` then real backfill on one CELEX.

Want me to apply the migration and run the integration tests against the DB now?

- [ ]  rename fact_source_registry table and source to fact_source_registry; vibe: change the table name celex_facts.fact_source_registry to celex_facts.fact_source_registry I will do that in the database


## What was wrong

Two independent bugs, as you suspected — and the "broken FK" was actually a query bug in the function, not corrupt data.

**Bug 1 — traversal dead-ended** ([pg_celex-ddl.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/pg_celex-ddl.sql)): the recursive CTE in `fact_provenance()` climbed via `sr_parent.source_id` (the `fact_source_registry` surrogate PK) instead of `sr_parent.source_fact_id` (the predecessor fact's id). Since no `facts.id` ever equals a registry PK, `fsl2.fact_id = sr_parent.source_id` matched nothing and the recursion died at depth 0 — so `court_list` never reached `court_list_raw`'s chunk. Fixed in 4 places: `current_fact_id`, the `fsl2` join, and both array appends, plus the cycle guard.

**Bug 2 — the spurious 3rd row** ([Main_migrate_lineage_backfill.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/batch/Main_migrate_lineage_backfill.py)): Phase B wrote a `derived_from` edge for _every_ YAML `predecessor`, but `is_court_involved` is a **gate** (`condition: 'is_court_involved == true'`), not a data source of `court_list_raw`. Per your choice, it's now recorded with a new `relation_type='gated_by'` and excluded from traversal. The discriminator: a fact with its own chunk evidence (`references_doc_json`) is a primary extraction, so its predecessor is a gate; a fact with no chunk evidence is a pure transform, so its predecessor is real lineage. (This correctly separates `court_list_raw`→gate from `court_list`→derived, which "predecessor appears in condition" could not, since _both_ reference their predecessor in `condition`.)

## Changes

- DDL: added `gated_by` enum value; fixed the recursion; excluded `gated_by` from base + recursive terms.
- Backfill: `_backfill_derived_edges` now classifies each edge, new `gated_linked` stat.
- Tests: updated signature + added `test_primary_extraction_predecessor_uses_gated_by_relation`. 8/8 backfill + 39/39 lineage-repo tests pass, ruff clean.

## To fix the live DB (in order)

1. Apply the enum first and **commit** before anything uses it (Postgres won't let a new enum value be used in the same transaction that adds it):
    
    ```sql
    ALTER TYPE celex_facts.relation_type_enum ADD VALUE IF NOT EXISTS 'gated_by';
    ```
    
2. Re-run the `CREATE OR REPLACE FUNCTION celex_facts.fact_provenance(...)` body (no migration — just the new definition).
3. Re-run the backfill to flip the existing edge: `PYTHONPATH=src .venv/bin/python src/batch/Main_migrate_lineage_backfill.py --celex-id 32002D0180`. The `(fact_id, source_id)` link upserts `relation_type` to `gated_by`.

Two caveats worth knowing:

- The shared `fact_source_registry` row for `is_court_involved` keeps its old `description='derived_from is_court_involved'` — `_upsert_source` doesn't rewrite description on reuse. It's cosmetic (the authoritative semantic lives on the link), but if it bothers you I can make the description neutral (a source can be a gate for one fact and data for another, so a relation-prefixed label on the shared row is arguably wrong anyway).
- Your uncommitted [docs/Data Lineage.md](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/docs/Data%20Lineage.md) may want a line about `gated_by`; I left it alone since you have edits in flight.

Want me to make the source-row description neutral, or update the lineage doc?


our instinct to run it for all was the right thing to check first, because my **original** gate rule (the one from the previous turn) would have corrupted the global run. The DB query proved it: `companies_country`, `OECD_sector`, `fines`, `company_recidivism`, `market_outside_eu` all have chunk evidence but their predecessors are real data lineage — and `court_list` itself had 5 rows that would have re-broken the lineage we just fixed.

So I replaced the discriminator with a **per-predecessor, condition-based** rule ([_is_gate_predecessor](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/batch/Main_migrate_lineage_backfill.py#L105)): a predecessor is a gate only when the `condition` tests it as a boolean (`<name> == true`) or existence (`exists("<name>")`) — not when its value is consumed (`'court' in court_list_raw`) or there's no condition. Validated against all 20 predecessor-facts in the YAML; it produces exactly this gate set, everything else stays `derived_from`:

|`gated_by` (control gates)|gated on|
|---|---|
|`court_list_raw`|`is_court_involved == true`|
|`leniency_applicants`, `leniency_granted`, `leniency_rejected`|`leniency_case == true` (their `companies` pred stays `derived_from`)|
|`case_infringement_book`, `case_infringement_type_book`|`exists("case_is_in_book")`|

Everything else (`companies_country`, `fines`, `products`-derived, `heideltime_*`, `timeline`, `court_list→court_list_raw`, …) is correctly `derived_from`.

13/13 tests pass, ruff clean.

## To run for all

```bash
# 1. enum, committed on its own first
psql ... -c "ALTER TYPE celex_facts.relation_type_enum ADD VALUE IF NOT EXISTS 'gated_by';"
# 2. re-apply the fixed CREATE OR REPLACE FUNCTION celex_facts.fact_provenance(...)
# 3. dry-run to eyeball counts, then the real run (no --celex-id = all)
PYTHONPATH=src .venv/bin/python src/batch/Main_migrate_lineage_backfill.py --dry-run
PYTHONPATH=src .venv/bin/python src/batch/Main_migrate_lineage_backfill.py
```

It's idempotent — the existing spurious `court_list_raw → is_court_involved` `derived_from` links get upserted to `gated_by`. The final log line now reports `gated_linked` alongside `derived_linked` so you can confirm the counts.

One residue I mentioned earlier still stands: the shared `fact_source_registry.description` for reused fact-sources keeps its old `derived_from …` text (the authoritative semantic is on the link's `relation_type`). Say the word and I'll make that description neutral.


**Neutral description** ([Main_migrate_lineage_backfill.py:294](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/batch/Main_migrate_lineage_backfill.py#L294)): fact-source rows are now registered with `description="fact:<predecessor_name>"` instead of `"<relation> <predecessor_name>"`. The registry row is shared across every fact a predecessor supports (and a predecessor can be a gate for one fact and data for another), so the row identifies _the source_; the edge semantic (`derived_from` / `gated_by`) lives where it belongs — on `fact_source_link.relation_type`.

**Self-healing on re-run** ([lineage_repository.py:562](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/lineage_repository.py#L562)): `_upsert_source` previously only filled a description when the existing one was empty, so your stale `"derived_from is_court_involved"` rows would never update. Changed to refresh when a non-`None` description differs (`None` still never clears). So re-running the backfill rewrites the old labels to `"fact:is_court_involved"` in place — no manual SQL needed.

**Tests**: added `test_register_description_refreshes_on_reuse` (refresh-on-reuse + `None`-doesn't-clear). 53/53 lineage + backfill tests pass.

This rides along with the same backfill re-run you'd already do for the `gated_by` reclassification — no extra step.

---

# Closed 2026-08-25 — the backfill is retired

Everything above documents `src/batch/Main_migrate_lineage_backfill.py`, which **no longer exists**. It was a bridge from the era when the ingestion path could not express derived-fact lineage: `LineageSourceVO.source_type` had no `"fact"` member, so `derived_from` / `gated_by` edges could only ever be reconstructed after the fact by a one-shot script.

That gap is now closed in the ingestion path itself — `FactIngestor._derive_predecessor_lineage` resolves each YAML `predecessor` to its `facts` row and classifies it with `_is_gate_predecessor` (moved verbatim out of the backfill, so the rule and its rationale survive), and `save_fact` appends those sources to whatever chunk lineage it already derived. A derived fact's lineage is complete at the moment it is written.

The script was run once more over the whole corpus before deletion (`linked=1, updated=34021, derived_linked=14889, gated_linked=4054, errors=0`), so nothing it could repair was left behind. The 86 facts still lacking a chunk edge carry `doc_pos` values that resolve to no row in `pg_embedding_qwen` — a re-chunking artefact the backfill never could fix; they need re-extraction.

Repairs are now just re-ingestion: `Main_ingest_generic_facts.py --filter-celex-id <CELEX> --filter-fact <name> --overwrite`, where `--filter-fact` pulls in the predecessor closure. See [Design fact_source_registry.md](docs/Design%20fact_source_registry.md) §5 and runbook R3.

Keep the history above as the record of *why* the semantics are what they are — the gate-vs-source discriminator in particular was got wrong twice before landing on the per-predecessor condition test.



