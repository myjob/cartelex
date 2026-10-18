# Search Design

## Overview

The project implements multiple search strategies, each optimised for a different retrieval goal. All of them share a common infrastructure: PostgreSQL + pgvector as the single vector store, a cross-encoder reranker as the unified scoring layer (backend per profile, see the note below), and the Provisioner pattern (all database clients and embedding callables injected at construction, never created inside `forward()`).

> **Reranker backend:** the reranker is pluggable per *profile* via `ranker_factory.create_ranker(profile)`: the **batch** profile (`RERANKER_BACKEND`, ingestion CLIs) defaults to `BgeReranker` (`BAAI/bge-reranker-v2-m3`), the **ui** profile (`UI_RERANKER_BACKEND`, the oi_dspy chat server) defaults to `FlashRanker`. See **[Design BgeRanker](Design%20BgeRanker.md)** and **[Design UI Modules](Design%20UI%20Modules.md#the-reranker-why-this-service-runs-its-own-profile)**. The table's "FlashRank" column entries therefore describe the shape of the scoring layer, not which model a given process loaded.

| Strategy | Corpus | Lexical leg | Vector leg | Merge | Reranker | Structural boost | Goal |
|---|---|---|---|---|---|---|---|
| **HybridSearch** | single CELEX case | FTS `websearch_to_tsquery` (OR lexemes) | cosine (pgvector) | RRF | FlashRank | yes — `RANKER_BOOST_MODE` (default section_type) | precision@k Q&A |
| **EnumerationSearch** | single CELEX case | BM25 (`plainto_tsquery`) | cosine (pgvector) | dedup | FlashRank (no boost) | no | recall-tuned exhaustive extraction |
| **KeywordSearch** | single CELEX case | FTS `websearch_to_tsquery` (AND lexemes) + trigram fallback | — | — | FlashRank | yes — `RANKER_BOOST_MODE` | keyword-only / existence fallback |
| **HybridBookSearch** | book chapter corpus | ILIKE keyword | cosine (pgvector) | FlashRank (optional, top-1) | no | best-chapter retrieval |
| **ChunkRepository** | configurable | ILIKE / BM25 / — | cosine (pgvector) | — | — | raw data access |
| **KnowledgeRepository** | knowledge graph | BM25 (tsvector) | cosine (pgvector) | — | — | knowledge nugget search |
| **RegexRetriever** | single CELEX case | regex | — | — | — | structured pattern extraction |

### Caller mapping

Two independent call paths consume the search layer. They share the same search classes but wire them through different orchestration modules.

| Caller path                                                                | Pipeline key / tool name | DSPy module                            | Search class                              |
| -------------------------------------------------------------------------- | ------------------------ | -------------------------------------- | ----------------------------------------- |
| **batch:** `Main_ingest_generic_facts` → `universal_modules`               | `doc_search`             | `DocHybridSearchModule`                | `HybridSearch`                            |
| **batch:** `Main_ingest_generic_facts` → `universal_modules`               | `enumeration`            | `EnumerationFactModule`                | `EnumerationSearch`                       |
| **batch:** `Main_ingest_generic_facts` → `universal_modules`               | `keyword_search`         | `DocKeywordSearchModule`               | `KeywordSearch`                           |
| **batch:** `Main_ingest_generic_facts` → `universal_modules`               | `keyword_regex`          | `KeywordRegexModule`                   | `RegexRetriever`                          |
| **batch:** `Main_ingest_generic_facts` → `universal_modules`               | `book_keywords`          | `BookKeywordModule`                    | `HybridBookSearchToolkit`                 |
| **batch:** `Main_ingest_generic_facts` → `universal_modules`               | `book_universal`         | `BookFactModule`                       | `HybridBookSearchToolkit`                 |
| **batch:** `Main_ingest_generic_facts` → `universal_modules`               | `book_infringement_type` | `BookFactChapterTitleModule`           | `HybridBookSearchToolkit`                 |
| **batch:** `Main_ingest_knowledge`                                         | —                        | direct call                            | `KnowledgeRepository`                     |
| **CLI:** `search_cli.py`                                                   | `--type hybrid`          | direct (no DSPy module)                | `HybridSearch`                            |
| **CLI:** `search_cli.py`                                                   | `--type keyword`         | direct (no DSPy module)                | `KeywordSearch`                           |
| **CLI:** `search_cli.py`                                                   | `--type semantic`        | direct (no DSPy module)                | `ChunkRepository` + `FlashRanker`         |
| **interactive:** `oi_dspy` → `retrieval_modules`                           | `doc_search`             | `_ToolDispatcher._run_doc_search`      | `ChunkRepository` (semantic + keyword)    |
| **interactive:** `oi_dspy` → `retrieval_modules`                           | `rag_search`             | `_ToolDispatcher._run_rag_search`      | `ChunkRepository` (semantic_search_facts) |
| **interactive:** `oi_dspy` → `retrieval_modules`                           | `case_facts` / `kb_sql`  | `_ToolDispatcher`                      | `FactRepository`                          |
| **interactive:** `oi_dspy` → `retrieval_modules`                           | `rag_search_in_case`     | `QAExecutorModule` (`_ToolDispatcher`) | `HybridSearch`                            |
| **interactive:** `oi_dspy` → `retrieval_modules` → `RetrievalRouterModule` | (router-selected)        | `EnumerationFactModule`                | `EnumerationSearch`                       |
| **interactive:** `oi_dspy` → `retrieval_modules`                           | `knowledge_search`       | `_ToolDispatcher._run_knowledge_search` | `KnowledgeRepository` (FTS, with `search_knowledge_any_terms` fallback)   |

The batch path (`Main_ingest_generic_facts.py`) selects the DSPy module for each fact definition via a `"pipeline"` key in the fact config. The interactive path (`oi_dspy.py`) provisions `HybridSearch` and `EnumerationSearch` at startup and passes them to `retrieval_modules`, where `RetrievalRouterModule` dispatches between them based on question intent.

Entry points for agents and tools are:

- `CelexDatabaseToolkit` (`src/tools/celex_tools.py`) — thin adapter over `ChunkRepository`, used by DSPy tool dispatch.
- `HybridSearcher` / `KeywordSearch` protocols (`src/pipelines/agent_dspy/dspy_protocols.py`) — typed interfaces consumed by `QAExecutorModule`.
- `RetrievalRouterModule` (`src/pipelines/agent_dspy/retrieval_router.py`) — intent-based dispatcher that picks `QARetrievalPipeline` or `EnumerationFactModule` based on the question.

---

## Cross-cutting concerns

### Embedding cache

`QueryEmbeddingRepository` (`src/pipelines/rag/pgvector/query_embedding_repository.py`) wraps every embedding call. On first access the vector is computed by the caller-supplied `embed_fn` and written to `query_embeddings` (OpenAI) or `query_embeddings_qwen` (Qwen3). Subsequent identical queries read from the cache table without hitting the model provider.

### Relative score filter

Before entering the reranker, vector candidates are filtered by `HybridSearch.relative_scoring()`. This removes candidates whose cosine similarity falls more than `delta` below the top score in the result set (default `delta=0.15`). A hard floor of `0.35` rejects the entire batch when the best score is too low to be meaningful. An alternative ratio-based threshold (`threshold = best_score * ratio`) is supported for cases where proportional filtering is preferable.

### FlashRank reranker

All document-chunk search strategies funnel their merged candidates through `FlashRanker.rank_chunkvo()`. The ranker formats each `ChunkVo` as a FlashRank passage and calls the cross-encoder. The returned scores are optionally augmented by a structural boost (see below) before sorting.

### Structural boost (`settings.RANKER_BOOST_MODE`)

When a call site leaves `apply_position_boost=True`, `structural_boost()` (`ranker/position_boost.py`, shared by both reranker backends) adds a prior selected by `settings.RANKER_BOOST_MODE`:

- **`section_type`** (default) — additive weight per classified decision section (`SECTION_TYPE_BOOSTS`: operative 0.30, fines 0.30, legal_assessment 0.10, others 0.0). Encodes where holdings and amounts live *structurally*, so it survives documents with long annexes; unclassified chunks (e.g. book chapters) get no boost. Requires the `Main_classify_sections.py` backfill.
- **`legal_climax`** — the legacy positional prior, kept only for A/B comparison until the section weights are validated:

```
depth        = chunk.doc_pos / local_max_doc_pos   # 0.0 (start) → 1.0 (end)
capped_depth = min(depth, 0.98)                    # exclude trailing boilerplate
boost        = (capped_depth ** 4) * 2.0
score        = cross_encoder_base_score + boost
```

- **`none`** — cross-encoder score only.

The boost can be disabled per call (`apply_position_boost=False`) for recall tasks.

### Section filter (`section_types`)

`websearch_search_in_celex`, `semantic_search_in_celex` and `fulltext_search_in_celex` accept an optional `section_types` list (`AND section_type = ANY(...)`), threaded through `HybridSearch.search` / `EnumerationSearch.search` / `KeywordSearch.search` and opted into per fact via the `section_scope:` key in `config_main/FACT_EXTRACTOR.yaml` (e.g. the `fines` fact restricts to `[fines, operative]`).

---

## HybridSearch

**Source:** `src/extract/rag/hybrid_search.py`

**Purpose:** Precision-at-k retrieval for "answer this question" Q&A over a single CELEX document.

**Pipeline:**

```
FTS (websearch, OR lexemes) ──┐
                              ├─ RRF fuse ─ FlashRank (boost ON) ─ top_k ─ lexical reserve
vector (cosine) ─ rel-score ──┘
```

1. **FTS lexical leg** — `ChunkRetriever.websearch_search_in_celex(celex_id, query, hits=settings.HYBRID_FTS_HITS)`. Runs `websearch_to_tsquery('english', query)` on the raw question (no client-side keyword extraction) over the stored `document_tsv` GIN column. English stemming handles plurals ("breweries" → "brewery"/"brewing"); the stemmed lexemes are OR-combined so a long question still surfaces literal mentions of any salient term. `ts_rank` in `similarity`.
2. **Vector leg** — `ChunkRetriever.semantic_search_in_celex(celex_id, embedding, hits=settings.SEMANTIC_SEARCH_HITS)`. The embedding is computed by the injected `embed_query` callable (cached externally).
3. **Relative score filter** — applied to vector candidates only (delta=0.15, floor=0.35) before the merge.
4. **Reciprocal Rank Fusion** — `reciprocal_rank_fusion(fts, vector)` fuses the two best-first lists on rank position (`score = sum 1/(RRF_K + rank)`, `RRF_K=settings.RRF_K`), deduplicating by chunk `id`. This replaces the old union-dedup + fabricated `similarity=0.5` sentinel: ts_rank and cosine live on different scales and can't be compared directly, so RRF fuses on rank. The FTS list is passed first, so a chunk in both legs keeps its FTS origin.
5. **FlashRank** — cross-encoder over the fused pool with the structural boost (`RANKER_BOOST_MODE`), truncated to `top_k`.
6. **Lexical reserve** — if fewer than 5 FTS-origin chunks survive the `top_k` cut, the missing ones are injected from an uncapped ranking pass. This guarantees literal surface-form matches, which the cross-encoder may score low in isolation, are never silently dropped.

**Key parameters:** `top_k` (optional), `delta=0.15`, `settings.HYBRID_FTS_HITS`, `settings.RRF_K`, `_LEXICAL_RESERVE=5`.

---

## EnumerationSearch

**Source:** `src/extract/rag/enumeration_search.py`

**Purpose:** Recall-tuned retrieval for "list every X" extraction tasks. Maximises coverage of rare mentions at the cost of precision.

**Pipeline:**

```
BM25 (tsvector / plainto_tsquery) ──┐
                                    ├─ dedup by id ─ FlashRank (boost OFF, no top_k)
vector (cosine)                   ──┘
```

Design differences from `HybridSearch`:

- **BM25 instead of ILIKE.** `ChunkRetriever.fulltext_search_in_celex()` uses a GIN-indexed `document_tsv` column with `plainto_tsquery`. BM25 surfaces all question lexemes simultaneously; no separate keyword argument is required.
- **Position boost disabled.** Rare entities often appear once in the early recitals (`depth ≈ 0`). The Legal Climax boost would unfairly penalise these. `rank_chunkvo` is called with `apply_position_boost=False`.
- **No `top_k` truncation.** The downstream extraction module maps over the full ranked list. Truncating would discard exactly the rare-mention chunks the strategy is designed to recover.
- **No keyword reserve.** BM25 already treats lexical hits as first-class; no rescue pass is needed.

**Key settings:** `ENUMERATION_DENSE_HITS`, `ENUMERATION_BM25_HITS` (from `src/config/settings.py`).

---

## KeywordSearch

**Source:** `src/extract/rag/key_word_search.py`

**Purpose:** Keyword-only fallback and boolean "does this document mention X" existence checks (`DocKeywordSearchModule`).

**Pipeline:**

```
FTS (websearch, AND lexemes) ─[empty]→ trigram fallback ─ FlashRank (boost ON) ─ top_k
```

The raw query drives `websearch_to_tsquery` with **AND** semantics (`use_or=False`): every salient lexeme must be present, so a document is not matched merely because it shares a ubiquitous word like "case" — the right precision for existence checks. When FTS returns nothing (a vocabulary or diacritic mismatch), an accent-folded `trigram_search` fallback runs so variant spellings still surface. The result is reranked by the cross-encoder.

---

## HybridBookSearch

**Source:** `src/extract/rag/hybrid_book_search.py`

**Purpose:** Locate the most relevant section in the reference book corpus and return its full chapter text.

**Corpus:** `pg_embedding_book_qwen` table (Qwen3 embeddings, separate from the CELEX case corpus).

**Pipeline (two stages):**

**Stage 1 — find best chunk:**

```
keyword (ILIKE, limit=15) ──┐
                             ├─ dedup ─ [FlashRank top-1, optional] ─ best id
vector (cosine, hits=15)  ──┘
```

Single-word queries skip the keyword leg (ILIKE on one unsplittable token adds noise without benefit). When a `FlashRanker` is injected, merged candidates are reranked and the top-1 is selected; without a ranker the first semantic hit is used as the fallback.

**Stage 2 — chapter expansion:**

The matched chunk's hierarchical path is truncated to the first 3 segments (chapter prefix). All rows sharing that prefix are fetched ordered by `doc_pos`, returning complete chapter text as `BookChapterVo` objects.

**Toolkit wrapper:** `HybridBookSearchToolkit` exposes `search_keyword()`, `search_semantic()`, and `search()` methods with the same chapter-expansion contract.

---

## ChunkRepository

**Source:** `src/pipelines/rag/pgvector/chunk_repository.py`

**Purpose:** Raw data-access object for CELEX document chunks. Implements the `ChunkRetriever` protocol consumed by all higher-level search strategies.

| Method | Scope | Query type | Notes |
|---|---|---|---|
| `keyword_search(celex_id, keyword)` | single case | ILIKE `%keyword%` | `origin="sql_keyword"`, `similarity=0.5` (sentinel); legacy substring path |
| `keyword_search_all_cases(keyword, hits=20)` | cross-case | ILIKE + `DISTINCT ON celex_id` | one best chunk per case (proper-noun leg) |
| `keyword_count_all_cases(keyword)` | cross-case | count by `celex_id` | returns `[{"celex_id": str, "count": int}]` |
| `fulltext_search_in_celex(celex_id, query, hits=120)` | single case | BM25 (`plainto_tsquery` + `ts_rank` on `document_tsv`) | EnumerationSearch leg; `origin="pgvector_fts"` |
| `websearch_search_in_celex(celex_id, query, hits=40, use_or=True)` | single case | `websearch_to_tsquery` + `ts_rank` | HybridSearch lexical leg; OR lexemes (recall) or AND (`use_or=False`, existence); `origin="pgvector_fts"` |
| `fulltext_search_all_cases(query, hits=20)` | cross-case | `websearch_to_tsquery` (OR) + `DISTINCT ON celex_id` | multi-word `doc_search`; fixes dead multi-word ILIKE |
| `trigram_search(celex_id, term, hits=20)` | single case | accent-folded `f_unaccent` + trigram GIN + `word_similarity` | fuzzy entity-name lookup; `origin="pgvector_trgm"` |
| `semantic_search_in_celex(celex_id, embedding, hits=40)` | single case | cosine (`embedding <=> vector`) | `origin="pgvector_semantic"` |
| `semantic_search(embedding, hits=40)` | cross-case | cosine | no celex scope filter |
| `semantic_search_with_meta(embedding, celex_id=None, limit=5)` | single/cross | cosine | returns full metadata dicts including `path`, `chunk_hash`, `token_count` |

---

## KnowledgeRepository

**Source:** `src/pipelines/rag/pgvector/knowledge_repository.py`

**Purpose:** Data-access object for the knowledge graph (distilled nuggets, not raw case chunks). Used by the cross-case `rag_search` tool in the DSPy retrieval dispatcher.

**Search methods:**

- `search_knowledge_fulltext(query, limit=20)` — `plainto_tsquery('english', ...)` on the server-generated `knowledge_tsv` column, ordered by `ts_rank`. Returns `KnowledgeItem` list.
- `search_similar_questions(embedding, limit=10)` — cosine similarity (`<=>`) on `knowledge_question_embeddings`. Returns dicts with `id`, `knowledge_id`, `question_variant`, `distance`. Lower distance = more similar.

---

## RegexRetriever

**Source:** `src/extract/rag/regex_retriever.py`

**Purpose:** Structured extraction of spans that match a caller-supplied regular expression from chunks of a single CELEX document. No ranking — returns raw regex matches. Used for deterministic pattern-based extraction (dates, article references, monetary amounts) where embedding-based retrieval adds no value.

---

## DSPy Retrieval Pipeline

**Source:** `src/pipelines/agent_dspy/retrieval_modules.py`, `src/pipelines/agent_dspy/retrieval_dispatch.py`, `src/pipelines/agent_dspy/retrieval_router.py`

**Purpose:** Orchestrates the search strategies for agent-driven Q&A. The pipeline is a DSPy `Module` sequence: Celex Selection → Plan → Execute → Synthesize.

### RetrievalRouterModule

Intent-based dispatcher that reads the incoming question before touching any retrieval:

- **Axis 1 — enumeration intent:** regex patterns match questions asking for lists ("list all", "how many", "enumerate", etc.).
- **Axis 2 — CELEX scope:** detects a CELEX ID in the question.
- **Decision:** both axes true → `EnumerationFactModule`; otherwise → `QARetrievalPipeline`.

### QAExecutorModule tool dispatch

The executor exposes a named-tool interface. Each tool maps to a specific search strategy:

| Tool name | Implementation | Notes |
|---|---|---|
| `kb_sql` | direct SQL on `celex_facts.facts` (read-only, guarded) | structured fact lookup |
| `case_facts` | full fact bag for a single case | `FactRepository.select_facts4celex` |
| `rag_search` | `kb_searcher.semantic_search_facts` | cross-case fact-embedding search |
| `rag_search_in_case` | `HybridSearch.search` | keyword + vector + FlashRank in-case |
| `doc_search` | `ChunkRepository` keyword (ILIKE proper-noun) + FTS (multi-word) + semantic supplement | cross-case, per-case dedup |

**Fallback chain** (when all steps return empty):

1. Synonym fallback — fetch full fact bag, filter by topic synonyms.
2. Fact-name fallback — direct SQL query for fact names mentioned in the question.
