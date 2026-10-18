#retrieval #chunking
# Design — Contextual chunk prefixes (Anthropic-style contextual retrieval)

This document describes the role, architecture, and operational setup of the
**contextual chunk prefix** enrichment introduced in **Phase 3** of the
knowledge-representation review (Weakness 4: *decision structure and per-chunk
context are invisible to retrieval*).

Source of truth for the plan: [phase3-decision-structure.md](claude/plans/phase3-decision-structure.md),
chapter 5. Implementation: [Main_enrich_chunk_context.py](../src/batch/Main_enrich_chunk_context.py).

Related: [DESIGN Search](DESIGN%20Search.md), [Design BgeRanker](Design%20BgeRanker.md),
[Data Ingestion](Data%20Ingestion.md).

---

## 1. Problem — chunks without identity

Chunks in `pg_embedding_qwen` are token-targeted slices of a single Commission
decision's markdown. A slice like:

> The fine is set at EUR 30 million.

embeds without any trace of *which* case it belongs to or *which* section it
sits in. So a natural user query —

> what fine did the Commission impose in the X cartel?

cannot reach that chunk through the dense (vector) leg: the embedding carries
no case identity to match against. The lexical (FTS) leg fares no better when
the case name and section never appear in the chunk body.

This is the failure mode Anthropic's *contextual retrieval* addresses:
prepend a short, model-written context line to each chunk **before embedding**,
so the case, section, and subject are baked into both the vector and the
full-text index. The published technique reports a ~35-50% reduction in
retrieval-failure rate.

---

## 2. What the enrichment does

For each chunk the local LM writes a **1-2 sentence `context_prefix`** naming
the case (parties / cartel / market), the decision section, and what the chunk
covers. The chunk is then **re-embedded** as `context_prefix + document`, and
both the prefix and the new vector are persisted.

Two storage effects follow from one write:

- **Dense leg** — the `embedding` column is overwritten with the vector of
  `context_prefix + document`. This is the *mechanism* of the feature, not a
  side effect: without re-embedding, dense retrieval is unchanged and only
  half the feature is delivered.
- **Lexical leg** — `document_tsv` is a `GENERATED ALWAYS ... STORED` column
  over `coalesce(context_prefix, '') || ' ' || document`, so it recomputes
  automatically and the FTS leg sees the prefix with no extra work.

The stored `document` column is **never modified**. The full chunk text is what
gets shown to users as evidence and what FTS indexes; only the transient string
handed to the embedding model may be tail-truncated (see §5).

### Why overwrite the old vector

`prefix + document` supersedes `document` as the answer to "what is this
chunk?". Keeping both vectors would double the table and split retrieval across
two representations of the same chunk. So the old vector is replaced in place.

---

## 3. Position in the pipeline

The enrichment is the **second** structural pass over a decision, and it
depends on the first:

```
ingest -> chunk (pg_embedding_qwen)
      -> Main_classify_sections.py     (writes section_type)
      -> Main_enrich_chunk_context.py  (writes context_prefix + re-embed)
```

`section_type` must exist first because it is one of the inputs to the prefix
prompt. Chunks the classifier left `NULL` are still enriched — the section
input degrades to `"unknown"` rather than blocking.

---

## 4. Prefix generation (DSPy)

Per CLAUDE.md DSPy Rules 1-3, the generation is a declarative signature plus a
module, both living in the single-consumer batch file
([Main_enrich_chunk_context.py](../src/batch/Main_enrich_chunk_context.py)):

- **`ChunkContextSignature(dspy.Signature)`** — instructs the model to pack the
  identifying facts a searcher would query for (case parties/subject, decision
  section, what this chunk covers) into 1-2 plain sentences, no markdown, no
  restating of the chunk. `desc=` on every field.
- **`ChunkContextModule(dspy.Module)`** — wraps `dspy.Predict(ChunkContextSignature)`
  behind an injectable `predictor` seam (tests pass a stub; production defaults
  to the real predictor). `forward()` returns a `dspy.Prediction` whose
  `context_prefix` is stripped.

### Inputs per chunk (never the whole document)

| Input | Source |
|---|---|
| `case_hint` | first `_CASE_HINT_CHARS` (500) of the doc_pos-0 chunk, via `select_first_page` — names the case and parties |
| `celex_id` | the CELEX identifier |
| `section_label` | the chunk's classified `section_type`, or `"unknown"` |
| `preceding_heading` | nearest markdown heading at or before the chunk, tracked across chunks with the shared `iter_headings` / `_HEADING_RE` from `section_classifier.py` |
| `chunk_text` | the chunk body |

Whole-document context is deliberately avoided: the Qwen3 embedder has a
2048-token cap and runs on a local model, so the context must be a compact,
targeted hint rather than the full decision.

The LM is configured via `dspy.LM(f"ollama/{settings.LLM_MODEL_ID}", api_base=
settings.OLLAMA_HOST, model_type="chat")` with a `dspy.JSONAdapter()`, matching
the other batch scripts.

---

## 5. Embedding-input length guard

The Qwen3 embedder **hard-fails** above 2048 tokens
([qwen3_embedding.py:32-35](../src/pipelines/rag/pgvector/embeddings/qwen3_embedding.py#L32-L35)
raises `ValueError`), and chunks are already token-targeted near that budget.
Prepending even a two-sentence prefix can push a chunk over the cap and crash
the batch.

`build_embedding_input(context_prefix, document, token_limit)` prevents this. It
is a **pure, unit-tested** helper:

- keeps the **full prefix** (the retrieval signal being added);
- cuts only as many tokens as needed from the **end** of the *document copy
  sent to the embedder* — tail-cut because the chunk head carries the
  heading / topic sentence, so the tail is the least-harm place to trim;
- counts tokens with **tiktoken `cl100k_base`** — the same tokenizer as the
  embedder's own guard, so the fit check matches the guard exactly;
- `token_limit` = `int(settings.OLLAMA_EMBEDDING_QWEN3_TOKEN_LIMIT)` (2048);
- pathological case (prefix alone exceeds the limit): falls back to the bare
  document truncated to the limit.

Crucially this affects **only the string sent to the embedding model**. The
stored `document` and `context_prefix` columns — and therefore FTS and the
evidence text shown to users — keep the complete text.

---

## 6. Persistence and resumability

The DB boundary is [ChunkRepository](../src/pipelines/rag/pgvector/chunk_repository.py):

- **`update_chunk_context(chunk_id, context_prefix, embedding)`** — one
  transaction: `UPDATE ... SET context_prefix=:p, embedding=CAST(:emb AS vector)
  WHERE id=:id`. The prefix and re-embed land together; `document_tsv`
  regenerates automatically. A raw keyed `UPDATE` is required because
  `add_embeddings` mints new row uuids and dedups on `chunk_hash` — it cannot
  update in place (precedent: `heideltime/qa/scan_dates.py`).
- **`select_chunks_missing_context(celex_id)`** — the resumable reader:
  `WHERE context_prefix IS NULL`, ordered by `doc_pos`. Returns `id`,
  `doc_pos`, `document`, `section_type`.
- **`select_celex_ids_missing_context()`** — distinct CELEX ids with at least
  one unenriched chunk; drives a full re-run to revisit only unfinished
  documents.

### Idempotency

Only `context_prefix IS NULL` chunks are written. The batch iterates **all**
chunks of a celex in `doc_pos` order so the nearest preceding heading is
tracked across already-enriched chunks, but skips the write for any chunk whose
id is not in the "missing" set (counted as `skipped_existing`). An interrupted
run resumes exactly where it stopped. `--dry-run` performs no LM calls, no
embedding calls, and no writes.

Stats counters: `enriched`, `skipped_existing`, `errors`. Per-chunk failures
are logged and counted, not fatal to the run.

---

## 7. CLI and scope

```
PYTHONPATH=src .venv/bin/python src/batch/Main_enrich_chunk_context.py --celex-id CELEX-32002D0759
PYTHONPATH=src .venv/bin/python src/batch/Main_enrich_chunk_context.py --celex-id CELEX-32002D0759 --dry-run
PYTHONPATH=src .venv/bin/python src/batch/Main_enrich_chunk_context.py --limit 20
```

| Flag | Effect |
|---|---|
| `--celex-id` | restrict to a single document (pilot mode) |
| `--dry-run` | count the would-be work; no LM / embedding / writes |
| `--limit` | cap total enriched chunks (smoke tests) |

**Pilot only** (decision 2026-07-05): the first runs are on `CELEX-32002D0759`
plus a handful of known Phase-1 verification cases. A corpus-wide run is a
separate, later decision, taken **after inspecting retrieval deltas** on the
pilot set — the re-embed is a full table rewrite of the affected rows and its
benefit is measured before committing to it.

---

## 8. Future ingest integration

To keep newly ingested chunks enrichable without re-running everything:

- `celex_pgvector.py` declares both `context_prefix` and the (unchanged)
  `embedding` columns on the Qwen ORM model, adds `context_prefix` to the
  insert dict and allowlist, and **excludes both from the
  `on_conflict_do_update` set_** so a re-ingest can never null an
  already-computed prefix or overwrite an enriched vector.
- **Run book**: after any new ingest, re-run `Main_classify_sections.py` then
  `Main_enrich_chunk_context.py`. Both are idempotent and touch only
  `NULL`-prefix / unclassified rows.

---

## 9. Testing

[test_Main_enrich_chunk_context.py](../src/batch/tests/test_Main_enrich_chunk_context.py)
covers, with mocks at the DB and LLM boundaries:

- `build_embedding_input` tail-truncation (full prefix kept, document tail cut,
  pathological oversized-prefix fallback);
- `--dry-run` writes nothing and makes no LM/embedding calls;
- `NULL`-only resumability (enriched rows skipped, headings still tracked);
- the module driven by a stub predictor.
