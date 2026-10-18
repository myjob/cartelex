#design #pipeline #universal #ingestion

# Design — `universal` Pipeline

The `universal` pipeline is the default fact-extraction path in celex-facts. It is
the "Slick Pipe" implementation of Rule 2 (logic-free signatures; all logic in
`forward()`): for one `target_fact_name` of one CELEX document it tries the cheapest
source first and escalates only when needed — SQL cache -> already-extracted facts ->
full-document context (small docs) or hybrid retrieval -> LLM extraction -> optional
verification -> document-reference back-linking.

- DSPy module: [`UniversalFactModule`](src/pipelines/agent_dspy/universal_modules.py) (`forward` at [universal_modules.py:192](src/pipelines/agent_dspy/universal_modules.py#L192))
- Controller: [`FactRagPipeline.process`](src/pipelines/agent_dspy/universal_fact_extraction_pipeline.py#L60)
- Parameters: [`FactExtractorParamVO`](src/pipelines/agent_dspy/fact_extractor_param.py)
- Batch orchestration: [`PipelineExecutor` / `FactIngestionOrchestrator`](src/batch/Main_ingest_generic_facts.py)
- Registered under the `"universal"` key in [`PIPELINE_DSPY_MODULE_CLASSES`](src/batch/Main_ingest_generic_facts.py#L66)

---

## 1. Where it sits

Each fact is one YAML entry in [`config_main/FACT_EXTRACTOR.yaml`](config_main/FACT_EXTRACTOR.yaml).
At batch time the flow is:

```
FACT_EXTRACTOR.yaml  (one struct per fact)
        |
        v
FactExtractorParamVO.from_yaml_struct(celex_id, struct)   # typed param object
        |
        v
FactIngestionOrchestrator                                 # LLM + agents + pipelines
   - topological sort of facts by `predecessors`
   - one FactRagPipeline per fact, one shared FactRepository/pool
        |
        v
PipelineExecutor.process_celex(celex_id)                  # per document
   - iterate facts in dependency order, sharing a context_data dict
   - gate each fact on its predecessors  (existence check only)
        |
        v
FactRagPipeline.process(params, dspy_agent=UniversalFactModule, context_data)
   - condition gate -> existence gate -> run module -> normalize -> persist -> embed
        |
        v
UniversalFactModule.forward(params)                       # the Slick Pipe
```

The `universal` YAML `pipeline:` value resolves (via `PIPELINE_DSPY_MODULE_CLASSES`)
to a single shared `UniversalFactModule` instance provisioned in
[`_build_agents`](src/batch/Main_ingest_generic_facts.py#L361) with two injected
collaborators (Provisioner pattern):

- `sql_store`: a `FactRepository` — the fact cache and golden-reference source.
- `searcher`: a [`HybridSearch`](src/extract/rag/hybrid_search.py) — FTS + pgvector + reranker.

---

## 2. The forward dispatch ("Slick Pipe")

`UniversalFactModule.forward(params)` runs these stages in order and returns the
first conclusive result.

### Stage 0 — SQL cache short-circuit
[`_check_fact_exists`](src/pipelines/agent_dspy/universal_modules.py#L142) reads
`select_fact4celex(celex_id, target_fact_name)`. If a prior run already stored a
structured `value_json` for this fact, it is returned immediately as the output —
no retrieval, no LLM. Skipped entirely when `force_override=True`.

### Stage 1 — choose the extraction context (source)
Three mutually-exclusive sources feed the extractor, tried in this order:

- **`source = "facts"`** — if a fact row exists but only as markdown (`value_md`,
  no JSON), that markdown becomes the context. This lets the LLM re-structure text
  it previously produced.
- **`source = "full_doc"`** — the small-document bypass (see below). When the whole
  CELEX document fits the budget and the fact is not section-scoped, retrieval is
  skipped and *every* chunk is fed to the extractor in `doc_pos` order.
- **`source = "hybrid"`** — the normal path for larger documents.
  `self.searcher.search(...)` runs the hybrid retrieval leg over the CELEX document,
  restricted by `top_k = settings.HYBRID_SEARCH_TOP_K` and, crucially, by
  `section_types = params.section_scope`. The retrieved chunks are joined into a
  single context string, each prefixed with `[doc_pos: <n>]` so Stage 4 can cite them.

An empty context yields an empty `FactOutput` (`source="none"`). Otherwise the
context is size-checked against `settings.OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT`
(tiktoken `cl100k_base`); overflow raises and breaks the chain rather than sending a
truncated prompt.

#### Small-document full-context bypass (`source = "full_doc"`)
Retrieval is a liability on *short* decisions. A hybrid query is only as good as its
embedding, and a loaded question ("list companies under investigation") embeds poorly
against a terse negative-clearance or notification decision — the very chunks naming
the parties can score below the ranker's cut and never reach the LLM. On a document of
a few thousand tokens this is pure downside: the whole text fits the prompt budget many
times over, so there is nothing to gain from ranking and everything to lose from a
mis-rank.

[`UniversalFactModule._use_full_document`](src/pipelines/agent_dspy/universal_modules.py)
therefore short-circuits to the full document when **both** hold:

- the fact is **not section-scoped** (`params.section_scope` is falsy) — scoped facts
  such as `fines` (`section_scope: [fines, operative]`) keep their section-restricted
  retrieval, which is a deliberate noise filter, not an accident of size; and
- the document is **small enough**:
  `ChunkRepository.sum_document_tokens(celex_id) <= settings.SMALL_DOC_CONTEXT_TOKEN_LIMIT`
  (default 12,000, well under the LLM prompt ceiling). The sum uses the persisted
  `token_count` column, so the check costs one cheap aggregate query and never
  tokenises a large document just to reject it.

When it fires, the context is built from
[`ChunkRepository.select_chunks_for_sections(celex_id)`](src/pipelines/rag/pgvector/chunk_repository.py)
(all chunks, `doc_pos` order), tagged `[doc_pos: <n>]` exactly like the hybrid path so
Stage 4 back-linking still works. The `chunk_repo` collaborator is injected in
`_build_agents`; when omitted it falls back to `searcher.repo`, and if no store can
report a concrete integer token count the bypass is declined and the hybrid path runs.

> Why this exists: `companies_investigated` returned `NA` for the tiny (8-chunk,
> 2,631-token) negative-clearance decision **CELEX-31985D0564** even though the sole
> party is named three times. The loaded query embedded at only 0.327 cosine, the dense
> leg was dropped, retrieval collapsed to two off-topic FTS chunks, and the LLM saw no
> party. Feeding the whole (small) document fixes this class of short decisions. A
> companion hardening in [`HybridSearch.relative_scoring`](src/extract/rag/hybrid_search.py)
> also makes a weak dense leg *non-destructive* — below the cosine hard floor it now
> retains the top-N chunks by similarity (which still feed RRF) instead of discarding
> the leg outright. Tracked in [`ISSUES.md`](ISSUES.md).

### Stage 2 — extraction
`dspy.ChainOfThought(GenericExtractionSignature)` is called with:

- `context` — the text from Stage 1,
- `target_fact_name` — steers what to look for,
- `schema_description` — from [`_resolve_schema_description`](src/pipelines/agent_dspy/universal_modules.py#L116); for `output_type="dict"` this is the YAML `output_schema` (the exact dict keys to emit, e.g. `company, amount`), or the bare `target_fact_name` when no schema is given; for `output_type="str"` it forces a single `{"value": ...}` row,
- `question` — the YAML `question`.

Output is a validated `ExtractionResult` (`extracted_facts: list[dict]`) — Rule 3,
structured outputs only.

#### Question framing is part of the contract
Because the signature is deliberately generic, the YAML `question` is the only thing
telling the model *which* entities to emit — and a poorly-framed question is
indistinguishable from a genuine "nothing found". Prefer neutral, inclusive framing
over a loaded label:

- **Bad** — `companies_investigated: "list companies under investigation"`. The phrase
  "under investigation" makes the model exclude the party in exemption,
  negative-clearance and notification decisions (there *is* no investigation), and
  biases it against trade or professional associations. On **CELEX-31987D0013**
  (Belgische Vereniging der Banken/Association Belge des Banques) this returned an empty
  list *deterministically* even though the association is the addressee.
- **Good** — frame by the entity's *role in the decision*: "the undertakings, companies,
  or associations named as parties or addressees … whose conduct or agreements the
  Commission assessed. Include trade or professional associations. Report each name
  verbatim." This reliably captures association parties without over-capturing
  third parties, and reproduces multi-company rosters unchanged (verified against
  CELEX-31980D0234's ~62 dairy cooperatives).

The `target_fact_name` and `output_schema` keys are not enough on their own: they name
the *bucket*, not the *inclusion rule*. When a fact is silently returning `NA`, suspect
the question framing before the retrieval.

#### Deterministic decoding
Fact extraction is a faithfulness task, so the batch LM runs greedy
(`settings.LLM_TEMPERATURE`, default `0.0`, wired in
[`_configure_llm`](src/batch/Main_ingest_generic_facts.py)). At the provider's default
temperature the *same* document flips between extracting an entity and returning `NA`,
and varies how many entities it emits (e.g. the association alone vs. the association
plus its member banks) — which makes ingestion irreproducible and re-runs noisy.
Greedy decoding removes that jitter; it does not fully fix a bad question (a loaded
question decodes to `NA` *stably*), so framing and temperature are complementary levers.

### Stage 3 — verification (optional, driven by `verify_fact_name`)
If `params.verify_fact_name` is set, the module builds a **golden reference list**
from the store: `select_fact4celex(celex_id, verify_fact_name)` -> the `value_md`
values. `dspy.ChainOfThought(GenericVerificationSignature)` then re-reads the same
`context` and reconciles the extracted rows against that golden list. Per the
signature `desc`, the golden list is a *guide*, not a whitelist: an entity absent
from it but clearly supported by the context is still kept, while extracted entities
with no textual support are dropped. The verified rows replace the raw extraction and
the source is tagged `<source>_verified`.

### Stage 4 — document-reference back-linking
Only when the context came from the document (`source in {"hybrid", "full_doc"}`) and
rows survived: `DetermineSupportingChunksSignature` picks up to three `doc_pos` integers
from the `[doc_pos: n]`-tagged context that support the extracted rows. These become
`references_doc`, later resolved to chunk lineage by the ingestor.

The module returns `dspy.Prediction(output=FactOutput(items, source, references_doc))`.

### Controller post-processing
[`FactRagPipeline.process`](src/pipelines/agent_dspy/universal_fact_extraction_pipeline.py#L60)
then: optionally ISO-normalizes country fields (`normalize_iso_country`), renders the
rows to markdown, persists via `FactIngestor.save_fact` (with chunk lineage derived
from `references_doc`), writes an audit row, and upserts a Qwen fact embedding.

---

## 3. How existing facts are leveraged to build output records

This is the subtle part. The `universal` pipeline touches previously-extracted facts
through **two independent levers**, and it is important that they do *different*
things:

| Lever | YAML key | Where used | Effect on the output record |
|-------|----------|------------|-----------------------------|
| Ordering + existence gate | `predecessors` | Batch executor only | Guarantees the prerequisite fact was produced first; **does not enter the LLM prompt** |
| Golden reference for verification | `verify_fact_name` | `UniversalFactModule` Stage 3 | Cross-checks extracted rows against the prior fact's values |

### 3a. `predecessors` — orchestration gate, not context
`predecessors` is consumed entirely outside the module:

1. [`FactIngestionOrchestrator._topological_order`](src/batch/Main_ingest_generic_facts.py#L580)
   sorts facts so every predecessor runs before its dependent.
2. [`PipelineExecutor._missing_predecessors`](src/batch/Main_ingest_generic_facts.py#L224)
   checks, per CELEX, that each predecessor has a non-empty value (in the shared
   `context_data` or persisted in the fact table). A missing/empty/`NA` predecessor
   yields `status="dependency_missing"` and **stops the remaining sequence** for that
   document.

Critically, `UniversalFactModule.forward` **never reads `params.predecessors`**. The
predecessor's *text is never injected* into the prompt. For the `universal` pipeline,
`predecessors` is purely a "did the upstream fact get produced?" guard.

> Contrast: [`FactContextModule`](src/pipelines/agent_dspy/universal_modules.py#L849)
> (`pipeline: fact_context`) is the module that *does* feed predecessor `value_md`
> straight into the LLM as context and bypasses hybrid search. If a fact must be
> derived *from* another fact's content (e.g. `companies_country` from the company
> list), that is `fact_context`, not `universal`. See the predecessor-audit note in
> [`ISSUES.md`](ISSUES.md).

### 3b. `verify_fact_name` — golden reference at verification
This is how `universal` actually *uses* a prior fact's values: as the
`golden_reference_list` in Stage 3. It never becomes extraction context; it is a
post-extraction reconciliation guide that catches hallucinated entities while
allowing well-supported new ones.

---

## 4. Worked example — `fines`

```yaml
- target_fact_name: fines
  question: List all fines
  output_schema: company, amount
  verify_fact_name: company
  pipeline: universal
  predecessors: ["companies"]
  # Restrict retrieval to the sections where amounts and orders live
  # (section_type metadata from Main_classify_sections.py).
  section_scope: [fines, operative]
```

Reading the record end to end:

1. **Ordering / gate (`predecessors: ["companies"]`).** The topological sort places
   the companies fact before `fines`. Before `fines` runs, `_missing_predecessors`
   confirms the companies fact exists and is non-empty for this CELEX; if not, `fines`
   is skipped as `dependency_missing` and the sequence breaks. No company text is
   passed into the fines extraction — the gate only asks "do we have companies?".

2. **Retrieval, scoped to where fines live (`section_scope: [fines, operative]`).**
   `forward` takes the `hybrid` source and calls
   `searcher.search(celex_id, query="List all fines", top_k=HYBRID_SEARCH_TOP_K,
   section_types=["fines", "operative"])`. The `section_scope` filter restricts the
   hybrid legs to chunks classified (by `Main_classify_sections.py`) as the fines and
   operative sections — the parts of a decision where penalty amounts and orders sit —
   so the context is dense with amounts rather than diluted by the whole judgment.

3. **Extraction (`output_schema: company, amount`).** `GenericExtractionSignature`
   receives the retrieved fines/operative chunks and is told to emit dict rows using
   exactly the keys `company` and `amount` (and nothing else). It produces rows like
   `[{"company": "...", "amount": "..."}]`.

4. **Verification against the company list (`verify_fact_name: company`).** The module
   loads the previously-extracted companies fact's `value_md` as the
   `golden_reference_list` and runs `GenericVerificationSignature` over the same
   context. This is where the companies are "used in the search of fines":
   - a fined entity that matches a known company is confirmed;
   - a fined entity absent from the list but explicitly supported by the section text
     is still kept (the golden list is a guide, not a hard filter);
   - a row with no textual support is dropped as a likely hallucination.

   The result is tagged `hybrid_verified`.

5. **Back-linking + persistence.** Up to three supporting `doc_pos` values are attached
   as `references_doc`, then `FactRagPipeline.process` stores the `(company, amount)`
   rows and records chunk lineage from those positions.

So for `fines`, the companies fact is leveraged **twice, indirectly**: as a *gate*
(fines only runs if companies exist) and as a *verification anchor* (extracted fined
companies are reconciled against the known list). The companies are never concatenated
into the fines prompt, and the actual fine text is retrieved fresh from the
fines/operative sections.

---

## 5. Variant: `detail_retriever` — per-entity detail enrichment

[`FactDetailRetriever`](src/pipelines/agent_dspy/universal_modules.py) (YAML
`pipeline: detail_retriever`) combines the two levers above where neither alone
suffices: `universal` re-derives entities from a retrieval lottery (misses drop
companies), while `fact_context` has no document access. The variant iterates
the predecessor roster and searches the document *per entity*:

1. **Roster** = first `predecessors` entry (e.g. `companies_investigated`);
   entities come from the roster fact's `value_json` rows (first key of each
   row). A roster without structured `value_json` is a hard error; extra
   predecessors are ignored with a warning.
2. **Per-entity retrieval**: `KeywordSearch` with the exact entity name first;
   empty -> `HybridSearch` with `"<entity> <question>"`. Both legs honor
   `section_scope` and are capped at `settings.DETAIL_RETRIEVER_TOP_K`
   (default 8).
3. **One LLM call per entity** via `EntityDetailExtractionSignature` (explicit
   `entity` input field anchors the model on one entity).
4. **Row contract**: keys are exactly the `output_schema` keys; the FIRST key
   always carries the roster entity name verbatim (never the LLM's rendering);
   missing/placeholder detail values are normalized to the literal `NA`.
   Entities are never dropped — no retrieval hit yields an all-`NA` detail row.
5. **Provenance**: `references_doc` = ordered deduped union of each entity's
   top chunk `doc_pos`, capped at 3 (matching the universal convention).

Example consumer (`companies_address`):

```yaml
- target_fact_name: companies_address
  question: What is the registered address of this company ?
  output_schema: "companies, address"
  predecessors: ["companies_investigated"]
  pipeline: detail_retriever
  section_scope: [operative]   # addresses live in the addressee block
```

Unlike `universal`, here the predecessor is *both* the gate and the driver:
the roster content defines the iteration, but the detail evidence is still
retrieved fresh from the document per entity. Tests:
`test_fact_detail_retriever.py` (unit) and
`test_fact_detail_retriever_integration.py` (live DB + LLM,
CELEX-32000D0146 ground truth).

---

## 6. Known caveat (see ISSUES.md)

Resolved 2026-08-28. The committed `fines` block is still
`run_state_active: false`, but both stale company references are migrated: its
`predecessors` and its `verify_fact_name` now name `companies_investigated`, the
live roster fact. Previously they named `companies` / `company`, neither of which
exists as a target — and because `universal` ignores predecessor *content*, the
stale `predecessors` entry was merely inert while the stale `verify_fact_name`
resolved to an empty golden list, silently disabling verification rather than
failing. `test_every_verify_fact_name_resolves_to_a_defined_fact` in
`src/batch/tests/test_fact_extractor_yaml_integrity.py` now guards against a
dangling golden list reappearing.

Note the asymmetry that made this easy to miss: in the same block,
`output_schema: company, amount` legitimately keeps `company`, because
`output_schema` names the **JSON row key** inside `value_json`, not a fact name.
Only `verify_fact_name` and `predecessors` are resolved against
`celex_facts.facts.name`.

The example YAML earlier in this document (section 5) predates the migration and
is kept verbatim as the historical illustration the surrounding prose walks
through; read `config_main/FACT_EXTRACTOR.yaml` for the live record.
