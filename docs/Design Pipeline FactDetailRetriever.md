#design #pipeline #detail_retriever #ingestion

# Design — `detail_retriever` Pipeline (`FactDetailRetriever`)

`detail_retriever` is a fact-extraction variant that **enriches a known roster of
entities with per-entity detail fields**. Where the [`universal`](docs/Design%20Pipeline%20Universal.md)
pipeline answers "what entities/values are in this document?" from a single pooled
retrieval, and `fact_context` derives a fact purely from another fact's text with no
document access, `detail_retriever` sits between them: it takes the entity list from a
predecessor fact and, **for each entity separately**, runs a targeted document search
and one focused extraction call. The result is exactly one enriched row per entity,
and no entity is ever silently dropped.

- DSPy module: [`FactDetailRetriever`](src/pipelines/agent_dspy/universal_modules.py) (`forward` in the same file)
- Signature: [`EntityDetailExtractionSignature`](src/pipelines/agent_dspy/dspy_signature.py)
- Controller: [`FactRagPipeline.process`](src/pipelines/agent_dspy/universal_fact_extraction_pipeline.py#L60) (shared, pipeline-agnostic)
- Parameters: [`FactExtractorParamVO`](src/pipelines/agent_dspy/fact_extractor_param.py)
- Batch orchestration: [`PipelineExecutor` / `FactIngestionOrchestrator`](src/batch/Main_ingest_generic_facts.py)
- Registered under the `"detail_retriever"` key in [`PIPELINE_DSPY_MODULE_CLASSES`](src/batch/Main_ingest_generic_facts.py#L66)
- Unit tests: [test_fact_detail_retriever.py](src/pipelines/agent_dspy/tests/test_fact_detail_retriever.py); integration: [test_fact_detail_retriever_integration.py](src/pipelines/agent_dspy/tests/test_fact_detail_retriever_integration.py)

---

## 1. Why it exists

`companies_address` and `companies_country` need one detail value *per company*. Two
existing pipelines each fall short:

- **`universal`** ignores predecessor content and re-derives the company list from a
  single pooled hybrid search over the whole document. A retrieval miss on one company
  silently drops that company (and its detail) from the output — the roster is not
  authoritative. (See the `companies_country` note in [ISSUES.md](ISSUES.md).)
- **`fact_context`** feeds the predecessor company list straight to the LLM but gives
  it **no document access**, so the detail (address, country) is inferred from the
  company name alone — reliable for well-known names, weak for obscure entities. This
  is why `companies_address` was marked "not working" under `fact_context`.

`detail_retriever` keeps the authoritative roster (like `fact_context`) *and* grounds
each detail in freshly retrieved document evidence (like `universal`), by decoupling
"which entities" from "what is each entity's detail".

---

## 2. Where it sits

Placement in the batch flow is identical to every other pipeline — the difference is
entirely inside `forward()`. Each fact is one YAML entry in
[`config_main/FACT_EXTRACTOR.yaml`](config_main/FACT_EXTRACTOR.yaml):

```
FACT_EXTRACTOR.yaml  (one struct per fact)
        |
        v
FactExtractorParamVO.from_yaml_struct(celex_id, struct)   # typed param object
        |
        v
FactIngestionOrchestrator                                 # topo-sort by `predecessors`
        |
        v
PipelineExecutor.process_celex(celex_id)                  # gate on predecessors existing
        |
        v
FactRagPipeline.process(params, dspy_agent=FactDetailRetriever, context_data)
        |
        v
FactDetailRetriever.forward(params)                       # per-entity enrichment
```

The `detail_retriever` YAML `pipeline:` value resolves (via
`PIPELINE_DSPY_MODULE_CLASSES`) to a single shared `FactDetailRetriever` instance
provisioned in [`_build_agents`](src/batch/Main_ingest_generic_facts.py#L361) with
three injected collaborators (Provisioner pattern):

- `sql_store`: a `FactRepository` — the fact cache and roster source.
- `keyword_search`: a [`KeywordSearch`](src/extract/rag/key_word_search.py) — exact lexical (FTS/trigram) leg, tried first per entity.
- `hybrid_search`: a [`HybridSearch`](src/extract/rag/hybrid_search.py) — FTS + pgvector + reranker, the fallback leg per entity.

No changes to the shared controller are needed: `FactRagPipeline.process` calls the
module, then normalizes, persists, audits, and embeds the returned `FactOutput` exactly
as for `universal`.

---

## 3. The forward dispatch

`FactDetailRetriever.forward(params)` runs these stages. Constants live at module top:
`_MAX_REFERENCES_DOC = 3`, `_NA_SENTINELS = {"na", "n/a", "none", "null"}`, and the
`_SOURCE = "detail_retriever"` tag.

### Stage 0 — SQL cache short-circuit
[`_check_fact_exists`](src/pipelines/agent_dspy/universal_modules.py#L142) (shared
helper) returns any previously stored `value_json` for this fact immediately — no
retrieval, no LLM. Skipped when `force_override=True`.

### Stage 1 — resolve the roster
`_resolve_roster_name` takes **the first `predecessors` entry** as the entity roster
(e.g. `companies_investigated`). Empty `predecessors` -> `ValueError`. Additional
predecessors are logged and ignored (single-roster semantics; precedent:
`NaceModule` also reads `predecessors` pipeline-specifically).

### Stage 2 — load the entity list
`_load_roster_entities` reads the roster fact via `select_fact4celex`:

- Missing/empty roster -> `ValueError("Missing prerequisite fact: '<roster>'")` (fails
  loudly; the executor's predecessor gate normally prevents this).
- Roster is the `'NA'` placeholder (`value_md == "NA"`) -> return empty `FactOutput`
  (mirrors `FactContextModule`'s NA short-circuit — nothing to enrich).
- Entities come from the **last roster row's `value_json`** (`facts[-1]` convention),
  accepted as either a JSON string (live DB `String` column) or an already-parsed
  `list` (tests/cached paths). A roster with no structured `value_json` -> `ValueError`
  (no brittle markdown parsing; every roster-producing pipeline persists `value_json`).
- Per row, the **first key's value** is the entity name; empty/`'NA'` values are
  skipped; names are deduplicated preserving order.

### Stage 3 — parse the output schema
`schema_keys = params.output_schema.split(",")`. Fewer than two keys -> `ValueError`
(this pipeline needs an entity key **and** at least one detail key, e.g.
`companies, address`). The **first key is the entity column**; the rest are the detail
columns the LLM fills.

### Stage 4 — per-entity loop
For each entity (`_retrieve_chunks`):

1. **Keyword leg first**: `keyword_search.search(celex_id, query=entity,
   top_k=settings.DETAIL_RETRIEVER_TOP_K, section_types=params.section_scope)` — exact
   lexical match on the entity name, precise for company names.
2. **Hybrid fallback** when the keyword leg is empty:
   `hybrid_search.search(celex_id, query=f"{entity} {params.question}",
   top_k=..., section_types=params.section_scope)`.
3. **No chunks from either leg** -> build an all-`NA` detail row and continue. The
   extractor is **not** called; the entity stays in the output.
4. Otherwise assemble a `[doc_pos: n]`-tagged context and size-check it with
   `_check_context_token_limit` (per entity; with `top_k=8` this effectively never
   trips, and an overflow propagates — the established chain-break contract).
5. **One extraction call**: `EntityDetailExtractionSignature` receives `context`, the
   explicit `entity` (so the model anchors on this entity), `target_fact_name`,
   `schema_description` (via the shared `_resolve_schema_description`), and `question`.

### Stage 5 — defensive row assembly (`_build_row`)
Keys are forced to exactly `schema_keys`. The **entity column always carries the roster
name**, never the LLM's rendering (so the roster can never be corrupted or renamed by
the model). Each detail value is taken by exact key from the LLM dict; blank or
placeholder values (`_NA_SENTINELS`) become the literal `"NA"`. Extra keys the LLM adds
are dropped; missing keys become `"NA"`.

### Stage 6 — references and return
Each entity contributes its top chunk's `doc_pos`; the ordered, deduplicated union is
capped at `_MAX_REFERENCES_DOC` (3, matching the `universal` convention) and stored as
`references_doc` (or `None` when no chunk was used anywhere). The module returns
`dspy.Prediction(output=FactOutput(items=rows, source="detail_retriever",
references_doc=...))`.

### Controller post-processing
[`FactRagPipeline.process`](src/pipelines/agent_dspy/universal_fact_extraction_pipeline.py#L60)
then (unchanged from `universal`): optional `normalize_iso_country` on a `country`
column, markdown rendering, `FactIngestor.save_fact` with chunk lineage from
`references_doc`, an audit row, and a Qwen fact-embedding upsert.

---

## 4. How existing facts are leveraged to build output records

Unlike `universal` — where the predecessor is only a gate and its **content never
enters the prompt** (see [Design Pipeline Universal §3](docs/Design%20Pipeline%20Universal.md)) —
`detail_retriever` uses the predecessor fact as **both the gate and the driver**:

| Lever | YAML key | Where used | Effect on the output record |
|-------|----------|------------|-----------------------------|
| Ordering + existence gate | `predecessors` | Batch executor | Roster fact must be produced first, else the fact is skipped as `dependency_missing` |
| Entity roster (the iteration) | `predecessors[0]` | `FactDetailRetriever` Stages 1-2 | The roster fact's rows *are* the output rows; one output row per roster entity |

The crucial distinction: `detail_retriever` reads the predecessor's **structured
`value_json` rows** to enumerate entities, but the **detail evidence is retrieved fresh
from the document per entity** — it is not inferred from the roster text (that is
`fact_context`), nor pooled across all entities at once (that is `universal`). The
`verify_fact_name` lever of `universal` is not used here; correctness comes from the
roster being authoritative plus per-entity grounding.

---

## 5. Worked example — `companies_address`

```yaml
- target_fact_name: companies_address
  question: What is the registered address of this company ?
  output_schema: "companies, address"
  predecessors: ["companies_investigated"]
  pipeline: detail_retriever
  section_scope: [operative]   # addresses live in the addressee block
  force_override: true
  run_state_active: true
```

> **Retrieval targeting matters.** Without `section_scope`, the per-entity
> keyword search on a company name lands on the name-dense "undertakings
> concerned" recitals, which describe the company but only give a city
> (*"based in St Louis, United States of America"*) — so the extractor returns
> `NA` for a *registered address*. The full postal addresses live in the
> decision's **addressee block** (`section_type = operative`). Scoping
> retrieval to `[operative]` surfaces them. This mirrors why `fines` uses
> `section_scope: [fines, operative]`: for a detail fact, choose the section
> where that detail actually lives. (Verified on `CELEX-32000D0146`: every
> ground-truth postal code resolves to the single `operative` chunk.)

Reading the record end to end:

1. **Gate + roster (`predecessors: ["companies_investigated"]`).** The topo-sort runs
   `companies_investigated` first; the executor confirms it is non-empty, then
   `detail_retriever` loads its `value_json` rows and reads each row's **first key**
   to get the entity name. (The roster stores rows under the key `company`, not
   `companies` — the first-key convention makes the module agnostic to that key
   name.) For CELEX-32000D0146 the roster is exactly the two *investigated*
   undertakings: `Anheuser-Busch Incorporated` and `Scottish & Newcastle plc`.
   Anheuser-Busch European Trade Limited (ABET) appears in the addressee block but
   is not an investigated undertaking, so it is **not** enriched — the roster, not
   the document, decides the entity set.

2. **Schema (`output_schema: "companies, address"`).** `companies` is the entity
   column (filled from the roster), `address` is the single detail column the LLM fills.

3. **Per-entity retrieval + extraction.** For `Anheuser-Busch Incorporated`:
   `KeywordSearch("Anheuser-Busch Incorporated")` returns the parties/recitals chunks
   naming it; if empty, `HybridSearch("Anheuser-Busch Incorporated What is the
   registered address ...")` fills in. `EntityDetailExtractionSignature` extracts just
   this company's address from that focused context. Repeated independently for each of
   the three companies (one LLM call each).

4. **Row assembly.** Output rows use exactly `{companies, address}`, entity names taken
   from the roster:

   | companies | address |
   |:--|:--|
   | Anheuser-Busch Incorporated | One Busch Place, St Louis, MO 63188, USA |
   | Scottish & Newcastle plc | 50 East Fettes Avenue, Edinburgh EH4 1RR, United Kingdom |

   One row per roster entity (two here). A company with no retrievable address would
   still appear, with `address: "NA"` — never dropped.

5. **Persistence.** Up to three supporting `doc_pos` values are attached as
   `references_doc`, then the controller renders the table to markdown, stores it, and
   records chunk lineage.

This is the ground truth asserted by
[test_fact_detail_retriever_integration.py](src/pipelines/agent_dspy/tests/test_fact_detail_retriever_integration.py)
against `CELEX-32000D0146` (postal-code substrings, tolerant of LLM formatting).

---

## 6. Configuration & tuning

- **`DETAIL_RETRIEVER_TOP_K`** ([settings.py](src/config/settings.py), default `8`):
  per-entity chunk budget. Small on purpose — each entity has its own focused context
  and its own LLM call, so a wide pool is unnecessary and costly.
- **`section_scope`** (optional YAML key): passed through to **both** search legs, so a
  detail fact can restrict retrieval to classified sections (e.g. `[parties]` for
  addresses) exactly like `universal`.
- **`normalize_iso_country`** (optional YAML key): works out of the box on a `country`
  detail column via the shared controller post-step.

---

## 7. Known caveats (see ISSUES.md)

- **Latency scales with roster size**: N entities = N keyword searches (+ up to N
  hybrid fallbacks) + N sequential LLM calls per fact per document. Fine for dev /
  single-doc; a roster cap or async fan-out is the corpus-scale follow-up.
- **`references_doc` capped at 3** loses per-entity provenance for large rosters. True
  per-entity lineage would emit `FactOutput.lineage_sources` (`LineageSourceVO`) per
  entity, as `FactCaseTitleModule` does — a follow-up, not in scope.
- **Roster must carry `value_json`**: an md-only legacy roster fact hard-fails with a
  clear `ValueError`; re-run the roster pipeline (e.g. `companies_investigated`) to fix.
- **Keyword precision on short/ambiguous names** (legal-form suffixes like `SA`, `AG`):
  the hybrid fallback covers *empty* keyword results but not *noisy* matches. If detail
  quality is poor on such names, consider stripping legal-form suffixes from the keyword
  query.
