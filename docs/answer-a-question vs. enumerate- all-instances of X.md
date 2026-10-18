
> **Reranker:** the analysis below says `FlashRanker` throughout; read it as "the injected reranker". the reranker is pluggable per *profile* via `ranker_factory.create_ranker(profile)`: the **batch** profile (`RERANKER_BACKEND`, ingestion CLIs) defaults to `BgeReranker` (`BAAI/bge-reranker-v2-m3`), the **ui** profile (`UI_RERANKER_BACKEND`, the oi_dspy chat server) defaults to `FlashRanker`. See **[Design BgeRanker](Design%20BgeRanker.md)** and **[Design UI Modules](Design%20UI%20Modules.md#the-reranker-why-this-service-runs-its-own-profile)**.

The core issue here isn't a bug in FlashRanker — it's a task mismatch. A cross-encoder reranker optimizes _precision@k_: it's built to surface the few chunks most relevant to a query and push everything else down. "Retrieve all courts involved" is a _recall_ task — closer to exhaustive entity extraction than to ranking. Your pipeline is doing exactly what it was designed to do, which is the wrong thing for this query type.

Two parts of your current implementation actively work against you here:

- **`top_k` truncation.** A court named once, in passing, scores low against a query like "courts involved" because the chunk's content is dominated by whatever else it's about. If it lands at rank 40, top-15 discards it. No threshold tuning fixes the tail because rare mentions are _intrinsically_ low-scoring.
- **The Legal Climax boost.** A court named only in the fact recitals (early in the document) gets the _smallest_ `position_boost`, since `depth` is near zero. So an early, seldom-mentioned court is penalized twice: weak semantic match _and_ weak structural boost. The boost is great for "what was the holding" and counterproductive for "enumerate every X."

So the design question is really: how do you add a recall-oriented path without breaking the precision path you already like.

Here are your options, roughly from smallest change to most robust.

**1. Threshold on `base_score`, not adjusted score, and drop `top_k` for these queries.** Your decoupled scoring already supports this. For enumeration queries, rank on `base_score` (bypassing the position boost) and return everything above a low cutoff instead of a fixed k. Cheap, but only a partial fix — rare mentions can still fall below any sane threshold.

**2. Make the Legal Climax boost a flag.** Add a `apply_position_boost: bool = True` parameter to `rank_chunkvo`. Enumeration queries pass `False`. This is a one-line change that removes one of the two biases working against you. Worth doing regardless of what else you pick.

**3. Hybrid retrieval — add a lexical/BM25 stage.** Court names are named entities with specific surface forms ("Bundesgerichtshof", "Court of Justice of the European Union", "Oberlandesgericht …"). Bi-encoders embed rare proper nouns poorly, but BM25 matches the literal string regardless of how rare it is. Run BM25 alongside your dense retrieval and union the candidate sets before reranking. This is the standard fix for "the embedding model doesn't know this token."

**4. Reframe enumeration as a map over all chunks, not a sort-and-truncate.** This is the real fix. Instead of "rank chunks by relevance to 'courts' and take the top 15," ask of _every_ chunk independently: "does this chunk name a court? which one(s)?" Recall is then bounded by chunk coverage, not by `k`. The reranker becomes irrelevant to _discovering_ the set — it can still be used afterward to pick the best passage describing each court's _role_. This costs more compute (one pass per chunk) but it's the only approach where recall doesn't degrade with corpus size.

**5. NER / gazetteer pass for known entity types.** "Courts" is a closed-ish, well-structured category. A gazetteer of court names plus pattern matching (legal court names are fairly regular across CELEX documents) gives you near-guaranteed recall for that specific entity type, independent of any retrieval scoring. Combine with option 4 as a high-recall candidate generator, then canonicalize/dedupe (the same court may appear under several surface forms).

**6. Use a finer chunk granularity for the extraction path.** A rare court buried in a chunk that's mostly about something else gets a diluted score. Sentence- or paragraph-level windows for the enumeration path raise the signal-to-noise ratio per unit of text. You can keep your current chunk size for the QA path.

**7. Query expansion / multi-query.** Generate sub-queries ("referring court", "appellate court", "tribunal", "national court", plus known court-type names), retrieve for each, and union. Helps, but it's still ranking-bounded — use it to _widen_ the candidate pool feeding option 4, not as the answer by itself.

My recommendation is a two-mode pipeline. Classify the query intent up front — "answer a question" vs. "enumerate all instances of X." The question path keeps FlashRanker exactly as is, position boost and all. The enumeration path turns off the boost, uses hybrid (dense + BM25) retrieval with generous recall, and treats the result as a _map-and-extract over all candidates_ rather than a top-k. For a fixed, structured entity type like courts, layer a gazetteer/NER pass on top for a recall floor. The reranker still has a job in the enumeration path — ordering the evidence passage for each distinct court once you've found the full set — but it's no longer the thing responsible for _finding_ the set.

One practical note: decide explicitly whether your desired output is "the set of distinct courts" or "every passage that mentions a court." Those need different post-processing (dedup/canonicalization vs. not), and conflating them is a common source of "missing" results that are actually just collapsed duplicates or unmerged variants.

If you tell me how query intent currently arrives (is it a free-text user query, or do you control the call sites?), I can sketch where the mode split should live relative to `rank_chunkvo`.

### consequently, the enumeration path would need a own module and the retrieval Module answering users Q&A would have to decide which path to go, correct?
>> 
Mostly right, with one refinement worth making on _where_ the decision lives.

The "decide which path" logic shouldn't sit _inside_ the Q&A retrieval module. If it does, that module grows a second responsibility and ends up importing the enumeration module — now the two are coupled and the Q&A path can't be reasoned about or tested on its own. Better to lift the decision into a thin layer _above_ both:

- A **router / dispatcher** — its only job is to classify query intent ("answer a question" vs. "enumerate all X") and delegate. It knows about both downstream modules; neither downstream module knows about the other.
- A **Q&A retrieval strategy** — essentially your current pipeline (dense retrieval → FlashRanker with the Legal Climax boost → top-k).
- An **enumeration strategy** — the new module (hybrid dense+BM25 retrieval → boost disabled → map-and-extract over all candidates, optionally a gazetteer/NER floor).

So it's three components, not two, and the Q&A module stays exactly as cohesive as it is today.

The other thing to keep in mind: these are two _strategies_, not two independent stacks. Both still need the vector store, chunk loading, and the `Ranker`. FlashRanker doesn't get duplicated — it becomes a shared primitive that each strategy _uses differently_ (boost on vs. off, sort-and-truncate vs. order-the-evidence). The BM25 index is the main piece of infrastructure genuinely new to the enumeration path. If you picture it as "two strategies over shared retrieval primitives, selected by a router," you avoid both the coupling problem and the temptation to fork the whole pipeline.

One open decision inside the router: how it classifies intent. A keyword/heuristic classifier ("all", "every", "list every", "which courts") is cheap, transparent, and easy to test, but brittle at the edges. An LLM intent classifier is more robust to phrasing but adds latency and a failure mode. For a fixed set of query shapes I'd start heuristic and only reach for the LLM if real queries prove too varied — and either way, make the chosen path observable in your output so a misroute is obvious in logs rather than silent.


# Implementation Plan

**Ready for review**

Select text to add comments on the plan

# Plan — Query Router + Enumeration Strategy

## Context

The current pipeline is recall-bound for enumeration queries like `court_list_raw` ("List all courts with assessment, order, ruling, decision or judgement?"). FlashRanker's `top_k` truncation and the Legal Climax `position_boost` are tuned for precision@k on Q&A queries; both work _against_ surfacing rare, briefly-mentioned entities. A court named once in the fact recitals scores low semantically (chunk dominated by other content) **and** receives the smallest structural boost (`depth ≈ 0`) — it never reaches the final top-K.

The fix is architectural, not a knob tweak: introduce a **router** that classifies each question as Q&A vs. enumeration, and an **enumeration strategy** that uses hybrid dense+BM25 retrieval (no `top_k` truncation, position boost off) followed by a per-chunk extraction map — i.e. `for each candidate chunk: extract entities`. Recall is then bounded by chunk coverage, not by `k`. FlashRanker is **not** duplicated: it stays a shared primitive both strategies use with different flags.

### Evaluation of router placement (the open question)

The router belongs as a **new agent registered in the universal batch pipeline's `_build_agents()` registry** at [Main_ingest_generic_facts.py:326-379](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/batch/Main_ingest_generic_facts.py#L326-L379), opted into per-fact via the YAML `pipeline:` field — **not** inside `UniversalFactModule`. Reasons:

- The `agents` dict is already a registry of named pipeline strategies (`"universal"`, `"keyword_search"`, `"keyword_regex"`, `"doc_search"`, `"fact_context"`, …). Adding `"router"` and `"enumeration"` is the natural extension of an existing pattern, not new infrastructure.
- Keeps `UniversalFactModule` (`pipeline: "universal"`) cohesive — it never imports the enumeration module, can still be tested in isolation. This matches your stated directive that "the Q&A path stays exactly as cohesive as it is today."
- Existing YAML targets (`fines`, `leniency_applicants`, …) using `pipeline: "universal"` keep working unchanged. Only entries that should benefit (e.g. `court_list_raw`) flip to `pipeline: "router"`.
- The chat-server entry [src/ui/agno-ui/oi_dspy.py:89](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/ui/agno-ui/oi_dspy.py#L89) is a separate concern (it builds `QARetrievalPipeline`, not `UniversalFactModule`) and can adopt routing later if useful — out of scope here.

## Architecture

```
YAML target_fact_name -> pipeline: "router"
                                 |
                                 v
                       QueryRouterModule.forward(params)
                                 |
                heuristic classifier on params.question
                /                                       \
       enumeration intent                          q&a intent
              |                                         |
   EnumerationFactModule                       UniversalFactModule
   (BM25 + dense, boost off,                   (HybridSearch, boost on,
    map-extract over all chunks)                top_k cutoff — unchanged)
              \                                         /
               \                                       /
                v                                     v
                  Prediction(output.items, output.references_doc,
                             routing_decision)   <-- shape unchanged
                                 |
                                 v
                        FactRagPipeline.process()
                        (ingestor.save_fact — unchanged)
```

Both strategies emit the same `prediction.output` shape so [FactRagPipeline.process()](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/agent_dspy/universal_fact_extraction_pipeline.py#L58-L130) requires no changes.

## Files to create

|Path|Purpose|
|---|---|
|`src/extract/rag/enumeration_search.py`|Sibling to `HybridSearch`; dense+BM25 union, rank with `apply_position_boost=False`, no `top_k` truncation.|
|`src/pipelines/agent_dspy/enumeration_fact_module.py`|`EnumerationFactModule(dspy.Module)` — calls `EnumerationSearch`, then maps a generic `EntityExtractionSignature` over every candidate chunk and aggregates distinct entities.|
|`src/pipelines/agent_dspy/query_router.py`|`QueryRouterModule(dspy.Module)` — heuristic classifier + dispatch.|
|`src/pipelines/rag/pgvector/migrations/add_chunk_fts.sql`|DDL: generated `document_tsv tsvector` column + GIN index on `celex_facts.pg_embedding_qwen`. One-time migration.|
|`src/extract/rag/tests/test_enumeration_search.py`|unittest, mirrors [test_hybrid_search.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/extract/rag/tests/test_hybrid_search.py).|
|`src/pipelines/agent_dspy/tests/test_enumeration_fact_module.py`|unittest for extraction + aggregation.|
|`src/pipelines/agent_dspy/tests/test_query_router.py`|unittest for the heuristic classifier and dispatch.|

## Files to modify

|Path|Change|
|---|---|
|[src/extract/rag/ranker/flash_ranker.py:24](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/extract/rag/ranker/flash_ranker.py#L24)|Add `apply_position_boost: bool = True` parameter to `rank_chunkvo`. When `False`, skip the `position_boost = (capped_depth ** 4) * 2.0` block and use `base_score` as the final score. Default `True` preserves current behavior.|
|[src/pipelines/rag/pgvector/chunk_repository.py:60](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/chunk_repository.py#L60)|Add `fulltext_search_in_celex(celex_id, query, hits)` method using `document_tsv @@ plainto_tsquery('english', :query)` ordered by `ts_rank(...)`. Mirrors [knowledge_repository.py:235-270](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/knowledge_repository.py#L235-L270). Returns `ChunkVo` with `origin="pgvector_fts"`.|
|[src/pipelines/agent_dspy/dspy_protocols.py:93](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/agent_dspy/dspy_protocols.py#L93)|Extend `ChunkRetriever` Protocol with `fulltext_search_in_celex(celex_id, query, hits) -> list[ChunkVo]`.|
|[src/batch/Main_ingest_generic_facts.py:326-379](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/batch/Main_ingest_generic_facts.py#L326-L379)|In `_build_agents()`, instantiate `EnumerationSearch(repo=chunk_repo, ranker=ranker)`, `EnumerationFactModule(sql_store=shared_repo, searcher=enumeration_search)`, then `QueryRouterModule(qa=universal, enum=enumeration)`. Register both `"enumeration"` and `"router"` keys in the returned dict.|
|[src/config/settings.py:79-82](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/config/settings.py#L79-L82)|Add `ENUMERATION_DENSE_HITS = int(os.getenv("ENUMERATION_DENSE_HITS", "120"))`, `ENUMERATION_BM25_HITS = int(os.getenv("ENUMERATION_BM25_HITS", "120"))`.|
|[config_main/FACT_EXTRACTOR.yaml:444-452](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/config_main/FACT_EXTRACTOR.yaml#L444-L452)|Change `court_list_raw` from `pipeline: "universal"` to `pipeline: "router"` (or `pipeline: "enumeration"` for explicit opt-in). No schema change.|

## Existing components to reuse

- [FlashRanker](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/extract/rag/ranker/flash_ranker.py) — same instance, called with `apply_position_boost=False` from the enumeration path. `base_score` is already tracked on [RankedChunkVo](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/extract/rag/models/chunk_vo.py#L15), so observability is free.
- [ChunkRepository.semantic_search_in_celex](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/chunk_repository.py#L192) — dense leg of the enumeration retrieval.
- [HybridSearch.deduplicate_chunks](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/extract/rag/hybrid_search.py#L30) — reusable dedup helper; lift into a small `chunk_utils.py` module **only if** both strategies need it (otherwise import directly from `HybridSearch`).
- [HybridSearch.relative_scoring](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/extract/rag/hybrid_search.py#L132) — usable on the dense leg of the enumeration path with a more permissive `delta` (e.g. 0.25).
- [FactRagPipeline.process](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/agent_dspy/universal_fact_extraction_pipeline.py#L58) — drives both strategies unchanged because both emit the same `prediction.output` shape.
- [knowledge_repository.search_knowledge_fulltext](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/knowledge_repository.py#L235) — the FTS template to copy verbatim into `chunk_repository.fulltext_search_in_celex`.
- [knowledge_items DDL](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/rag/pgvector/pg_celex-ddl.sql#L483) — template for the `document_tsv` generated column + GIN index on `pg_embedding_qwen`.

## Component details

### 1. `FlashRanker` flag (smallest, foundational change)

```python
def rank_chunkvo(
    self, query: str, chunks: List[ChunkVo],
    top_k: Optional[int] = None,
    apply_position_boost: bool = True,
) -> List[RankedChunkVo]:
```

When `apply_position_boost=False`, set `adjusted_score = base_score` and skip the depth math. Re-sorting and `top_k` slicing unchanged. Default `True` is a no-op for every existing caller.

### 2. `EnumerationSearch`

`search(celex_id: str, query: str) -> list[RankedChunkVo]`:

1. Dense: `repo.semantic_search_in_celex(celex_id, query_embedding, hits=settings.ENUMERATION_DENSE_HITS)`.
2. BM25: `repo.fulltext_search_in_celex(celex_id, query, hits=settings.ENUMERATION_BM25_HITS)`.
3. Optional permissive `relative_scoring(dense, delta=0.25)` to drop near-zero similarity noise on the dense leg only.
4. Dedup union by `chunk.id`.
5. `ranker.rank_chunkvo(query, unique, top_k=None, apply_position_boost=False)` — order only, no truncation.

No keyword-reserve logic (the BM25 leg already guarantees exact-string hits survive — that _is_ the lexical channel).

### 3. `EnumerationFactModule`

```python
class EnumerationFactModule(dspy.Module):
    def __init__(self, sql_store, searcher: EnumerationSearch):
        self.searcher = searcher
        self.extract = dspy.Predict(EntityExtractionSignature)
        self.aggregate = dspy.Predict(EntityAggregationSignature)

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        ranked = self.searcher.search(params.celex_id, params.question)
        per_chunk = [
            self.extract(question=params.question,
                         output_schema=params.output_schema,
                         chunk=c.chunk).entities
            for c in ranked
        ]
        merged = self.aggregate(
            question=params.question,
            output_schema=params.output_schema,
            entity_lists=per_chunk,
        )
        return dspy.Prediction(output=FactExtractionOutput(
            items=merged.items,
            references_doc=[c.id for c in ranked if ...],
        ))
```

Signatures (declarative, per CLAUDE.md Rule 1):

- `EntityExtractionSignature` — inputs `(question, output_schema, chunk)`, output `entities: list[dict]` typed by a `pydantic.BaseModel` whose fields are derived from `output_schema` (e.g. `"court, country, jurisdiction"` → three string fields). One LLM call per chunk.
- `EntityAggregationSignature` — inputs `(question, output_schema, entity_lists)`, output `items: list[dict]` deduplicated and canonicalized. This is where the surface-form normalization the YAML's `court_list` target currently does as a _separate_ pass moves to — i.e. enumeration can subsume `court_list_raw` _and_ the canonicalization step that `court_list` performs.

**Output shape is identical to `UniversalFactModule`** (`prediction.output.items`, `prediction.output.references_doc`) so [PipelineFactIngestor.save_fact](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/pipelines/agent_dspy/universal_fact_extraction_pipeline.py#L98) requires zero changes.

### 4. `QueryRouterModule`

```python
class QueryRouterModule(dspy.Module):
    def __init__(self, qa: UniversalFactModule, enum: EnumerationFactModule):
        self.qa = qa
        self.enum = enum

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        intent = classify_intent(params.question)   # "enumeration" | "qa"
        pred = (self.enum if intent == "enumeration" else self.qa)(params=params)
        pred.routing_decision = intent              # observability
        return pred
```

Classifier — heuristic for v1 (your stated preference):

```python
_ENUM_PATTERNS = (
    r"\blist all\b", r"\ball\b.*\b(courts?|parties|companies|fines)\b",
    r"\bevery\b", r"\beach\b", r"\bwhich (courts?|companies|parties)\b",
    r"\benumerate\b",
)
def classify_intent(question: str) -> str:
    q = question.lower()
    return "enumeration" if any(re.search(p, q) for p in _ENUM_PATTERNS) else "qa"
```

`routing_decision` lands on the prediction so misroutes are obvious in logs (per your point: "make the chosen path observable in your output so a misroute is obvious in logs rather than silent").

### 5. DDL migration

```sql
ALTER TABLE celex_facts.pg_embedding_qwen
  ADD COLUMN document_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', document)) STORED;

CREATE INDEX pg_embedding_qwen_document_tsv_gin
  ON celex_facts.pg_embedding_qwen USING gin (document_tsv);
```

Run once. Generated column is automatically maintained on insert/update; no application changes for ingestion.

## Verification

End-to-end check on the motivating fact:

```bash
# Before: current behavior (universal). Note the missed rare-mention courts.
PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39226_8310_11 \
    --filter-fact court_list_raw \
    --limit 1 --overwrite

# Flip YAML: court_list_raw -> pipeline: "router"

# After: routed to enumeration strategy.
PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39226_8310_11 \
    --filter-fact court_list_raw \
    --limit 1 --overwrite

# Confirm via the dependent fact's canonicalization step.
PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id 39226_8310_11 \
    --filter-fact court_list \
    --limit 1 --overwrite
```

Expected: the post-change `court_list_raw` enumerates strictly more distinct courts than the pre-change baseline. Logs show `routing_decision="enumeration"`.

Smoke test that **no** existing fact regresses:

```bash
PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --filter-celex-id CELEX-31982D0367 --filter-fact fines --limit 1
# fines stays on pipeline: "universal" -> UniversalFactModule unchanged.
```

Unit tests (project convention: `unittest`, `.venv/bin/python -m unittest`):

```bash
.venv/bin/python -m unittest \
    extract.rag.ranker.tests.test_flash_ranker \
    extract.rag.tests.test_enumeration_search \
    pipelines.agent_dspy.tests.test_enumeration_fact_module \
    pipelines.agent_dspy.tests.test_query_router

ruff check
```

Test coverage to add:

- `test_flash_ranker.py` — new test: with `apply_position_boost=False`, `result.score == result.base_score` for every chunk, and a chunk with `doc_pos == max` no longer outranks a chunk with `doc_pos == 0` that has a higher `base_score`.
- `test_enumeration_search.py` — mirror [test_hybrid_search.py](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/extract/rag/tests/test_hybrid_search.py): mock `ChunkRepository` exposing all three search methods; assert dense+BM25 union is deduplicated and ranker is called with `apply_position_boost=False, top_k=None`.
- `test_enumeration_fact_module.py` — mock `EnumerationSearch` returning N chunks, mock the DSPy `Predict`s; assert the extract step is invoked N times (one per chunk) and the aggregator receives all N entity lists.
- `test_query_router.py` — classify a battery of phrasings; assert dispatch goes to the correct sub-module by spying on `.qa(...)` vs `.enum(...)` calls; assert `routing_decision` is set on the returned prediction.

## Out of scope (follow-ups)

- LLM-based intent classifier — only if real queries prove the heuristic is too brittle.
- Gazetteer / NER floor for known entity types (e.g. a court-name list) — strengthens recall further but is per-entity-type work.
- Chat-server entry [oi_dspy.py:89](vscode-webview://03rrh6vleso8fogvbj8ps5ttn1972ofbmrebkedv34qcta1cfd58/src/ui/agno-ui/oi_dspy.py#L89) — could adopt the router pattern later; today the urgent failure mode is in the batch path.
- Migrating other "list all X" YAML entries (`companies_all`, `leniency_applicants`, `decision_remedies`, …) to `pipeline: "router"` — do one at a time after verifying `court_list_raw` works.


## Implemented

**Code changes**

- [flash_ranker.py:24](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/extract/rag/ranker/flash_ranker.py#L24) — added `apply_position_boost: bool = True` flag (default preserves Q&A behavior; enumeration passes `False`).
- [chunk_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/extract/rag/chunk_repository.py) + [new chunk_repository.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/chunk_repository.py) — added `fulltext_search_in_celex` to both legacy and new repos (BM25 via `document_tsv @@ plainto_tsquery` + `ts_rank`).
- [dspy_protocols.py:93](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/dspy_protocols.py#L93) — extended `ChunkRetriever` Protocol with the new method.
- [settings.py:84-87](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/config/settings.py#L84-L87) — added `ENUMERATION_DENSE_HITS` / `ENUMERATION_BM25_HITS` (default 120 each).
- [enumeration_search.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/extract/rag/enumeration_search.py) — new: dense ∪ BM25 union, boost off, no `top_k` truncation, no keyword-reserve.
- [enumeration_fact_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/enumeration_fact_module.py) — new: map a per-chunk `EntityExtractionSignature` over **every** ranked candidate, then aggregate via `EntityAggregationSignature`. Emits the same `FactOutput` shape as `UniversalFactModule` so `FactRagPipeline.process` is unchanged.
- [query_router.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/query_router.py) — new: pure heuristic `classify_intent` (over `list all` / `every` / `enumerate` / etc.) plus `QueryRouterModule` that dispatches and tags `prediction.routing_decision` for log observability.
- [Main_ingest_generic_facts.py:_build_agents](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/batch/Main_ingest_generic_facts.py#L318-L342) — registered `"enumeration"` and `"router"` agents alongside `"universal"`.
- [FACT_EXTRACTOR.yaml:447](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/config_main/FACT_EXTRACTOR.yaml#L447) — flipped `court_list_raw` to `pipeline: "router"`.
- [migrate_pg_embedding_qwen_add_chunk_fts.sql](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/migrate_pg_embedding_qwen_add_chunk_fts.sql) — new: idempotent DDL for the generated `document_tsv` column + GIN index.

**Tests** — 13 new, all passing (`unittest`); ruff clean across all touched files.

**Manual steps remaining for you**

1. Apply the DDL: `psql … -f src/pipelines/rag/pgvector/migrate_pg_embedding_qwen_add_chunk_fts.sql`
2. Verify on the motivating fact (`court_list_raw` is `run_state_active: false` in YAML, so you'll need to flip that or use `--filter-fact court_list_raw --overwrite`):
    
    ```bash
    PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_generic_facts.py \
        --filter-celex-id 39226_8310_11 --filter-fact court_list_raw --limit 1 --overwrite
    ```
    
    Expect `QueryRouter routed target 'court_list_raw' to enumeration strategy` in logs.



### is the router pipeline already dedup the entries ?

Three layers of dedup are in play; only two are inside the router stack.

**Inside the router/enumeration stack** (already there):

1. **Chunk-level dedup** — [enumeration_search.py:_deduplicate_by_id](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/extract/rag/enumeration_search.py) collapses the same `chunk.id` showing up via both the dense leg and the BM25 leg. Silent, deterministic, by primary key.
    
2. **Entity-level dedup** — [enumeration_fact_module.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/enumeration_fact_module.py) calls `EntityAggregationSignature` after the per-chunk map. That signature's prompt tells the LLM: _"Merge surface-form variants of the same real-world entity into a single row. Use STRICTLY the requested keys."_ So "Bundesgerichtshof" / "BGH" / "German Federal Court of Justice" should collapse into one row in `court_list_raw`'s output.
    

**Outside the router stack**:

3. **Domain-specific canonicalization** — your existing [court_list](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/config_main/FACT_EXTRACTOR.yaml#L455-L468) YAML target is a _second pass_ with rules my generic aggregator doesn't know: "ignore 'litigation' / 'case' / specific company names like 'Lagap'", "merge Patents vs Patent litigation", the "Aggressive Merge Rule". That rule set is court-specific and lives in the question text, not in the router.

So practically:

- `court_list_raw` (now routed) → produces an already much-deduplicated list (#1 + #2).
- `court_list` (predecessor: `court_list_raw`) → still useful for the domain-specific rules (#3) — but its job is now easier, not redundant.

One thing to be aware of: `court_list`'s question text doesn't match any enumeration heuristic (no "list all", "every", etc. — it's normalization rules), so `classify_intent` will route it to the **qa** path (UniversalFactModule). That's the right call — it's a transformation over its predecessor's output, not a retrieval task — but it means if you set `pipeline: "router"` on `court_list` too, it stays on the Q&A strategy. If you'd rather make that explicit, leave `court_list` at `pipeline: "universal"`.


see [Implementation Plan  Intent-Based Routing into QARetrievalPipeline](Implementation%20Plan%20%20Intent-Based%20Routing%20into%20QARetrievalPipeline.md)