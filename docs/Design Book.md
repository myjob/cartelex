# Book Ingestion and Search — Design Document

## Overview

The book subsystem ingests a domain-specific legal reference book (Russo, Schinkel, Günster & Carree 2010) into pgvector and exposes a two-step hybrid search used by the QA retrieval pipeline. The book is stored separately from CELEX case documents in its own embedding table (`pg_embedding_book_qwen`), identified by `book_name` rather than `celex_id`.

> **Reranker:** `HybridBookSearch` takes whatever ranker is injected; the reranker is pluggable per *profile* via `ranker_factory.create_ranker(profile)`: the **batch** profile (`RERANKER_BACKEND`, ingestion CLIs) defaults to `BgeReranker` (`BAAI/bge-reranker-v2-m3`), the **ui** profile (`UI_RERANKER_BACKEND`, the oi_dspy chat server) defaults to `FlashRanker`. See **[Design BgeRanker](Design%20BgeRanker.md)** and **[Design UI Modules](Design%20UI%20Modules.md#the-reranker-why-this-service-runs-its-own-profile)**. A bare `create_ranker()` in a batch entry point is the batch profile, so the injected instance is `BgeReranker` unless overridden.

---

## Ingestion Pipeline

### 1. Source format

Books are converted to Markdown by **MinerU** before ingestion. MinerU renders every heading as `# H1`, so logical depth must be inferred from the heading-text numbering pattern rather than from HTML heading levels.

### 2. Tree parsing — `InfringementTreeBuilder`

**File:** `src/pipelines/rag/pgvector/embeddings/book_tree.py`

`InfringementTreeBuilder.build(md_file)` parses the MinerU Markdown into a `BookNode` tree. It is scoped to chapters 2–8 of the Russo-Schinkel book (the infringement part). The builder:

1. Pre-scans the raw Markdown source for `(heading_text, line_number)` pairs so every node carries its 1-based `text_line`.
2. Runs `mistune` in AST mode to iterate tokens without lossy regex splits.
3. Detects the window entry/exit via hard-coded regexes (`_START_PATTERN`, `_END_PATTERN`) that match the raw heading text — including any trailing page-number artefact injected by MinerU — so table-of-contents repetitions are skipped.
4. Assigns logical depth via `_detect_level`, which matches a priority-ordered list of patterns against the heading text:

| Level | Pattern | Example |
|-------|---------|---------|
| 1 | `\d+ ` | `2 Horizontal restrictions` |
| 2 | `\d+\.\d+ ` | `2.1 Price fixing` |
| 3 | `[A-Z] ` | `A Introduction` |
| 4 | `[A-Z]\.\d+ ` | `F.1 Vitamins` |
| 5 | `[a-z]\. ` | `a. Relevant facts` |
| 6 | anything else | leaf |

5. Builds a stack-based parent-child relationship as the document is scanned; the virtual root node (`title="Infringements"`, `level=0`) is never persisted.
6. Strips trailing page-number artefacts from heading titles with `_strip_page_number`.
7. Computes recursive `token_count` (cl100k_base) across the whole tree via `_compute_token_counts`.

The `BookNode` Pydantic model stores: `title`, `level`, `path` (ancestor chain, `" -> "` joined), `text_line`, `raw_text` (heading + body text), `token_count`, and `children`.

### 3. Ingestion orchestrator — `BookMarkdownIngestor`

**File:** `src/pipelines/rag/pgvector/embeddings/book_chunks2pgvector.py`

`BookMarkdownIngestor` is provider-agnostic: any LangChain `Embeddings` instance is injected at construction time (Provisioner pattern). The pgvector backend is always `CelexPGVectorPgBookQwen3` (`pg_embedding_book_qwen`) — the `settings.EMBEDDING_PROVIDER` toggle and its OpenAI-legacy branch were removed in Aug 2026.

#### `ingest_book(book_name, md_file)` — full pipeline

```
InfringementTreeBuilder.build()
    └── tree: BookNode

_split_markdown(md_file)
    └── doc_total_tokens (for sanity-check only)

_persist_md_chapters(book_name, md_file, tree, doc_total_tokens)
    ├── flatten tree → list[BookNode] (root excluded)
    ├── wrap each node as langchain Document with metadata
    └── vector_store.add_documents(doc_chunks)

_verify_load_by_token_count(book_name, chunk_token_sum)
    └── SELECT SUM(token_count) FROM pg_embedding_book_qwen WHERE book_name = :book_name
```

#### Document metadata per chunk

Each `Document` written to the store carries:

| Field | Source |
|-------|--------|
| `book_name` | argument |
| `doc_type` | `"book_section"` (constant) |
| `path` | `BookNode.path` |
| `level` | `BookNode.level` |
| `title` | `BookNode.title` |
| `text_line` | `BookNode.text_line` |
| `token_count` | `BookNode.token_count` |
| `source` | `md_file` path |
| `chunk_hash` | MD5 of `raw_text` |
| `doc_pos` | sequential index in the flattened node list |

#### Token-coverage sanity check

Before calling `add_documents`, a loose assertion verifies that `chunk_token_sum` is within ±10 % of `doc_total_tokens` (counted via `tiktoken` on the raw file). After ingestion, a strict DB assertion confirms `SUM(token_count)` in the table equals the chunk sum.

### 4. Embedding provider — `Qwen3OllamaEmbeddings`

**File:** `src/pipelines/rag/pgvector/embeddings/qwen3_embedding.py`

A LangChain `Embeddings` subclass that delegates to a local Ollama server. The model and host are read from `settings.EMBEDDING_MODEL_qwen3` and `settings.OLLAMA_HOST`. Input is rejected if it exceeds 2 048 cl100k_base tokens.

### 5. Vector store backend — `CelexPGVectorPgBookQwen3`

**File:** `src/pipelines/rag/pgvector/celex_pgvector.py`

`CelexPGVectorPgBookQwen3` extends `CelexPGVector` (itself a `langchain_postgres.PGVector` subclass) and targets the `pg_embedding_book_qwen` table. Key differences from the CELEX document store:

- **Deduplication key:** `(book_name, chunk_hash)` instead of `(celex_id, chunk_hash)`.
- **Promoted columns:** `book_name`, `level`, `title`, `text_line`, `source` (in addition to the shared `doc_type`, `token_count`, `path`, `chunk_hash`, `doc_pos`).
- **`_handle_field_filter`:** routes filter expressions on book-specific columns to SQLAlchemy column attributes rather than JSONB.

The ORM model is `PgEmbeddingBookQwen3Store` (same file), registered under `VectorBase` to isolate table management from the application's main `Base`.

---

## Database Schema

Table: `celex_facts.pg_embedding_book_qwen`

| Column | Type | Notes |
|--------|------|-------|
| `id` | varchar | UUID PK |
| `collection_id` | uuid | FK to `langchain_pg_collection` |
| `embedding` | vector | Qwen3 embedding |
| `document` | text | `raw_text` of the `BookNode` |
| `cmetadata` | jsonb | Full metadata dict |
| `book_name` | varchar | Indexed; dedup key (part 1) |
| `doc_type` | varchar | Always `"book_section"` |
| `token_count` | integer | cl100k_base token count |
| `path` | varchar | Ancestor chain (`" -> "` delimited) |
| `level` | integer | Indexed; logical depth (1–6) |
| `title` | varchar | Heading text |
| `text_line` | integer | 1-based source line |
| `source` | varchar | Source `.md` file path |
| `chunk_hash` | varchar | Indexed; MD5 of `document`; dedup key (part 2) |
| `doc_pos` | integer | Sequential position for ordering |
| `ingestion_timestamp` | timestamptz | Server-default `now()` |

---

## Search

### `BookChunkRepository` — data access layer

**File:** `src/pipelines/rag/pgvector/book_chunk_repository.py`

Focused DAO for `pg_embedding_book_qwen`. All queries use the shared engine from `session.py`. Exposes:

| Method | SQL |
|--------|-----|
| `keyword_search(keyword, limit)` | `document ILIKE '%keyword%' ORDER BY doc_pos` |
| `semantic_search(query_embedding, hits)` | `embedding <=> CAST(:qemb AS vector) ORDER BY distance` |
| `fetch_chapter_text(row_id)` | Derives a chapter prefix (first 3 path segments) from the matched row, then returns all rows whose `path` starts with that prefix, ordered by `doc_pos` |
| `keyword_exists(keyword)` | `keyword_search(limit=1)` + length check |

`keyword_search` and `semantic_search` return `list[ChunkVo]`; `fetch_chapter_text` returns `list[BookChapterVo]`.

### `QueryEmbeddingRepository` — embedding cache

**File:** `src/pipelines/rag/pgvector/query_embedding_repository.py`

Shared read-through cache backed by `celex_facts.query_embeddings_qwen`. `get_or_cache(query, embed_fn)` looks up the query text first; on a miss it calls `embed_fn`, stores the result (`ON CONFLICT DO NOTHING`), and returns it. This avoids repeated Ollama round-trips for identical queries.

### `HybridBookSearch` — two-step retrieval

**File:** `src/extract/rag/hybrid_book_search.py`

```
search(query, keyword)
    ├── _find_best_match(query, keyword)
    │     ├── skip keyword search for single unsplittable words
    │     ├── _keyword_search(keyword, limit=15)   → list[ChunkVo]
    │     ├── _semantic_search(query, hits=15)     → list[ChunkVo]
    │     ├── _deduplicate(keyword_chunks, semantic_chunks)
    │     └── if ranker: FlashRanker.rank_chunkvo(top_k=1)
    │         else: first semantic hit
    │
    └── _fetch_chapter_text(best_id) → list[BookChapterVo]
```

**Step 1 — find best chunk.** Merges ILIKE keyword hits with pgvector cosine-similarity hits. For single unsplittable words (no spaces, no underscore) the keyword leg is skipped to avoid false positives. When a `FlashRanker` is injected the merged list is reranked and the top-1 is selected; otherwise the first semantic hit wins.

**Step 2 — chapter expansion.** The matched row's `path` is split on `" -> "` and truncated to the first 3 segments to form a chapter prefix. Every row in the table whose path starts with that prefix is returned ordered by `doc_pos`, giving the full chapter context rather than a single chunk.

**Embedding cache.** `_get_or_cache_embedding` delegates to `QueryEmbeddingRepository.get_or_cache` — the same cache table used by the CELEX document search.

### `HybridBookSearchToolkit` — pipeline adapter

`HybridBookSearchToolkit` wraps `HybridBookSearch` and is constructed from a `CelexDatabaseToolkit`, inheriting the shared engine and embedding provider. It exposes three convenience methods used by the DSPy retrieval tools:

| Method | Behaviour |
|--------|-----------|
| `keyword_exists(keyword)` | Existence check — delegates to `_keyword_search(limit=1)` |
| `search_keyword(keyword)` | Keyword-first: top keyword hit → chapter expansion |
| `search_semantic(query)` | Semantic-first: top semantic hit → chapter expansion |
| `search(query, keyword)` | Full hybrid: delegates to `HybridBookSearch.search` |

---

## Value Objects

**File:** `src/extract/rag/models/chunk_vo.py`

| Class | Purpose |
|-------|---------|
| `ChunkVo` | Base retrieval result — `id`, `celex_id`, `chunk`, `origin`, `similarity`, `doc_pos` |
| `RankedChunkVo` | Adds `score` and `base_score` from the reranker |
| `BookChapterVo` | Chapter-expansion result — `chapter_title`, `sub_chapter_title`, `level`, `document`, `path`, `token_count`, `doc_pos` |

---

## Integration with the QA Pipeline

Book search is used inside `_ToolDispatcher` in the DSPy retrieval pipeline as the `book_search` tool. When the QA planner emits a `book_search` retrieval step, the dispatcher calls `HybridBookSearchToolkit.search(query, keyword)` and formats the returned `BookChapterVo` rows as evidence text for the synthesizer.

The `keyword_exists` check is used as a lightweight gate before committing to a full semantic search: if no chunk matches the keyword, the tool can skip the embedding call.

---

## Splitter Utility — `TokenTargetMarkdownSplitter`

**File:** `src/pipelines/utils/TokenSizeTargetMarkdownSplitter.py`

Used by `BookMarkdownIngestor._split_markdown` for counting `doc_total_tokens` on the raw file (via `tiktoken`). The splitter itself is **not** used for the book tree — tree nodes are chunking units. It is used for CELEX case document ingestion (`celex_chunks2pgvector.py`) and may be used for other general-purpose Markdown ingestion tasks.

The splitter wraps LangChain's `MarkdownHeaderTextSplitter` and adds:
- **Orphaned-header merge:** a buffer under 50 tokens adopts the next section's metadata rather than being flushed as a standalone chunk.
- **Overflow recursive split:** content exceeding `chunk_size` (default 500 tokens) is split further with `RecursiveCharacterTextSplitter` from tiktoken.

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| Separate `pg_embedding_book_qwen` table | Books are identified by `book_name`, not `celex_id`; promoted columns differ (adds `level`, `title`, `text_line`, `source`). Mixing with CELEX document embeddings would pollute the cosine-similarity space. |
| Tree-based chunking instead of fixed-size | Preserves the logical structure (chapter → section → sub-case). Retrieval can expand a matched sub-section to its full chapter without re-parsing. |
| Chapter expansion as step 2 | A single chunk match is rarely sufficient for legal reasoning. Returning the full chapter gives the LLM enough context without an extra retrieval round-trip. |
| `InfringementTreeBuilder` is book-specific | The Russo-Schinkel book uses a custom numbering scheme not found in any CELEX document. The builder is not reusable for generic Markdown; CELEX ingestion uses `TokenTargetMarkdownSplitter` instead. |
| `(book_name, chunk_hash)` dedup key | Prevents re-ingestion of unchanged sections when the book is re-run, matching the `(celex_id, chunk_hash)` approach used for CELEX documents. |
| Embedding cache (`query_embeddings_qwen`) | Ollama/Qwen3 embedding calls are slow; caching by query string avoids repeated round-trips for common search queries. |
