
Status: implemented; provenance closed 2026-08-29
Owner tables: `celex_facts.heideltime_extractions`
Facts produced: `heideltime_raw`, `heideltime_event`, `heideltime_event_agg`, `heideltime_cartel_period`
Code: `src/pipelines/heideltime/extract_heideltime.py`, `src/pipelines/heideltime/heideltime_module.py`, `src/pipelines/rag/pgvector/heideltime_ingestor.py`, `src/pipelines/rag/pgvector/event_repository.py`
Related: [Design fact_source_registry](docs/Design%20fact_source_registry.md), [Design Fact Verifier](docs/Design%20Fact%20Verifier.md), [Design DSPY](docs/Design%20DSPY.md)

---

## 1. Purpose

Cartel decisions are argued in time. The infringement window, the date of the first
contact, the last meeting before the inspection — these are the facts on which fine
calculations and limitation-period arguments turn. Extracting them with an LLM alone is
unreliable: dates appear in a dozen surface forms (`30.6.2010`, `30 June 2013`,
`14May1971`), often relative to a document date, and a model asked to "find the dates"
will paraphrase, normalise inconsistently, and hallucinate.

The HeidelTime subsystem separates the two problems:

- **Finding and normalising temporal expressions** is deterministic, done by the
  HeidelTime JAR (a rule-based TIMEX3 tagger). No LLM.
- **Deciding what happened on those dates** is a language task, done by DSPy modules
  that receive the already-normalised dates as input and may not invent new ones.

That split is the core design decision. Everything below follows from it.

---

## 2. Pipeline shape

Four facts, in a strict dependency chain declared by `predecessors` in
`config_main/FACT_EXTRACTOR.yaml`:

```
    chunks (pg_embedding_qwen)
         |
         |  HeidelTime JAR, deterministic, no LLM
         v
  heideltime_extractions          198,302 rows / 739 documents
         |
         +--> heideltime_raw            proxy fact: counts + min/max date
                   |
                   |  derived_from
                   v
              heideltime_event          LLM: date -> {actor, predicate_object}
                   |
         +---------+---------+
         |                   |
    derived_from        derived_from
         v                   v
  heideltime_event_agg   heideltime_cartel_period
   (1-2 critical events)  ("1992-05-22 : 2007-12-19")
```

| pipeline | module | LLM | reads | writes |
|---|---|---|---|---|
| `heideltime_raw` | `HeidelTimeModule` | no | chunk text | `heideltime_extractions` + proxy fact |
| `heideltime_event` | `HeidelTimeEventModule` | yes | extractions + chunk text | event list fact |
| `heideltime_event_agg` | `HeidelTimeEventAggregateModule` | yes | `heideltime_event` JSON | critical-event fact |
| `heideltime_cartel_period` | `HeideltimeCartelPeriodModule` | yes | `heideltime_event` JSON | date-range fact |

All four are `run_state_active: false` — they are run deliberately, not in the default
sweep.

### 2.1 Why `heideltime_raw` is a "proxy" fact

The real output of stage one is 198,302 rows in a side table, which does not fit the
`facts` value model. `heideltime_raw` therefore stores a summary — `extraction_count`,
`chunk_count`, `canonical_date_count`, `min_date`, `max_date` — and acts as the fact-graph
node standing for the extraction table. This matters for provenance (§5): it is the
node through which the entire derived chain reaches its evidence.

---

## 3. The JAR wrapper

`extract_heideltime.HeidelTime` deliberately bypasses `py_heideltime.heideltime()`, which
has three defects that caused the production "Server[50]" crash:

1. **A multiprocessing pool inside a threaded process.** It spawns
   `Pool(processes=cpu_count())` — 12 Python+Java processes — *per chunk*. Created inside
   a `ThreadPoolExecutor`, `os.fork()` is unsafe and UIMA worker init fails.
2. **A `config.props` race.** It writes the file to the current working directory at the
   start of each call and deletes it at the end; concurrent workers delete it while others
   are still reading.
3. **Silent stderr.** `os.popen()` on Linux discards stderr, so Java errors vanish.

The replacement writes `config.props` once at singleton construction into the
`py_heideltime` package directory (a stable path independent of cwd), invokes the JAR
directly via `subprocess.run()` capturing both streams, and processes texts sequentially —
one JVM per text, no pool. For chunks under ~30 kB that is *faster* than spawning 12 JVMs.

---

## 4. Data model

`celex_facts.heideltime_extractions` — one row per temporal expression:

| column | role |
|---|---|
| `id` | uuid PK, surrogate; the natural key for `heideltime` lineage sources |
| `celex_id` | document scope |
| `fk_chunk_id` | varchar FK to `pg_embedding_qwen.id` (LangChain-owned id) |
| `tid` | HeidelTime's per-chunk TIMEX id (`t3`) — unique only within a chunk (§6) |
| `timex_text` | surface form as written (`30.6.2010`) |
| `timex_type` | `DATE`, `TIME`, `DURATION`, `SET` |
| `timex_value` | normalised `date`, or NULL for durations/partials |
| `span_start` / `span_end` | character offsets within the chunk |
| `raw_jsonb` | verbatim JAR output, with `id` back-filled so the row is self-describing |

`HeidelTimeVO` (`models.py`) is the immutable (`frozen=True`) value object between JAR and
DB, decomposing `span: [117, 129]` into the two integer columns.

Two properties worth stating explicitly:

- **`timex_value` is nullable and often NULL.** Of 198,302 rows only 67,601 carry a
  calendar date; the rest are durations (`P3Y`), sets, or year-only partials that
  `_parse_timex_date` cannot map to a SQL `DATE`.
- **`span_start`/`span_end` make this the most precise evidence in the system.** No other
  source type localises down to a character range.

---

## 5. Provenance

### 5.1 The problem that was fixed on 2026-08-29

The chain was fully wired and still resolved to nothing. `fact_provenance()` emits only
terminal (non-`fact`) sources, and the recursion ran:

```
heideltime_cartel_period --derived_from--> heideltime_event
                         --derived_from--> heideltime_raw --> (nothing)
```

`heideltime_raw` carried no sources of its own, so the traversal reached it and returned an
empty set. Verified live before the fix: `heideltime_cartel_period` for `CELEX-31964D0599`
resolved to **0** terminal sources. The only heideltime facts with any evidence at all were
the 647 of 737 cartel-period facts that `heideltime_source_register_fisher.py` had reached
by matching `timex_value` strings document-wide.

The `derived_from` edges themselves were never missing — `FactIngestor._derive_predecessor_lineage`
writes them from the YAML `predecessors` on every save. The terminal node was the gap.

### 5.2 What each fact now cites

- **`heideltime_raw`** cites every extraction row it produced (`source_type='heideltime'`,
  natural key `heideltime_event_id` -> `heideltime_extractions.id`) plus every distinct
  chunk it read (`source_type='chunk'`, carrying `doc_pos` as `chunk_seq`). The fact's
  value is a count over exactly those rows, so evidence and claim are the same set.
- **`heideltime_event`** emits one `heideltime` source per extraction row behind an event
  that survived deduplication. `select_heideltime_chunks_with_dates` returns a
  `date_sources` mapping (ISO date -> `{id, timex_text}` rows) alongside the
  `canonical_dates` the LLM sees, so an event resolves only to extractions **in the chunk
  the LLM actually read**. UUIDs never enter the prompt.
- **`heideltime_event_agg` / `heideltime_cartel_period`** carry no direct evidence; they
  reach it through `derived_from` and the recursive CTE.

### 5.3 Recording lineage at extraction time, not after

The fisher re-derives links by matching a fact's date string against `timex_value` across
the whole document. That is irreducibly ambiguous: if `1992-05-22` appears in four
paragraphs, all four are registered and the true one is not recoverable. Live data shows
the ambiguity is the norm — in one document, **243 dates resolve to more than one
extraction row within a single chunk**.

Recording the link while the producing chunk is still in scope avoids the guess entirely.
This is the general rule the subsystem now follows: *provenance is captured where the
evidence is used, never reconstructed from values afterwards.*

### 5.4 Coverage after the 2026-08-29 backfill

739 documents, 0 LLM calls, 9h49m. Registry 33,753 -> 252,925 rows; links 72,423 -> 311,125.

| fact | resolving | avg terminal sources |
|---|---|---|
| `heideltime_cartel_period` | 737/737 (100%) | 335 |
| `heideltime_event_agg` | 699/699 (100%) | 259 |
| `heideltime_event` | 738/744 (99.2%) | 323 |
| `heideltime_raw` | 738/745 (99.1%) | 323 |

Every non-resolver is a document in which HeidelTime found zero temporal expressions, so
there is nothing to cite. Effective coverage is 100% of documents that have extractions.

---

## 6. Known limitations

**Date-free chunks are re-extracted forever.** A chunk the JAR read and found no temporal
expression in writes no row, so it is indistinguishable from one never attempted and is
re-run on every pass. Measured: 15,050 of 55,451 chunks (27%) count as unprocessed, and the
2026-08-29 backfill spent 9h49m re-extracting them while `heideltime_extractions` finished
at exactly the same 198,302 rows it started with. There is no "processed, empty" marker in
the schema, and `source_hash` does not supply one -- a chunk with no row has nowhere to
carry a hash. Budget any `heideltime_raw --overwrite` run in hours, not minutes.

**Chunk staleness is tracked, chunk *attempts* are not.** Since 2026-08-31 every extraction
row carries `source_hash`, the md5 of the chunk text it was read from, and
`EventRepository.heideltime_fresh_chunk_ids` treats a chunk as done only while every one of
its rows still matches `md5(document)`. A NULL hash reads as stale. This is what makes an
in-place chunk-text repair self-correcting: before it, the 2026-04-29 `scan_dates.py` run
rewrote 1,453 chunks and left their extractions frozen at pre-repair contents that no
`--overwrite` could reach, because the skip test was "has at least one row". Rewriting chunk
text now deletes the derived rows and their lineage in the same transaction. See
[heideltime-stale-extraction-fix.md](claude/plans/heideltime-stale-extraction-fix.md).

**`heideltime_event` still resolves through `heideltime_raw`.** Its per-event lineage
(§5.2) only materialises on a re-run, which is a 739-document LLM job that would also
overwrite existing fact values with fresh non-deterministic output. Until then a
date-specific fact cites the document's entire extraction set — avg 323 sources rather than
the handful behind its own events. Correct, but coarse.

**The fisher is now redundant and hazardous.** Re-running
`heideltime_source_register_fisher.py` broadly would layer document-wide guesses on top of
the exact links, and once both are in `fact_source_link` they are indistinguishable. Scope
it strictly to documents lacking exact sources, or retire it.

**`tid` is not a key.** HeidelTime numbers TIMEX ids per *chunk* invocation, so 198,302
rows share only 202 distinct `tid` values (`t1`..`t202`). Measured: 14,934 `(celex_id, tid)`
pairs repeat, while `(fk_chunk_id, tid)` has zero duplicates. `tid` is therefore unique only
within a chunk; reference `id`, or `(fk_chunk_id, tid)` if a natural key is needed.

---

## 7. Operations

Run one stage over a document set (the CELEX list is best built from the extraction table
itself, so coverage matches the data rather than a stale master sheet):

```bash
.venv/bin/python src/batch/Main_ingest_generic_facts.py \
    --celex-list-file <ids.txt> \
    --filter-fact heideltime_raw \
    --limit 0 --overwrite
```

`--filter-fact` overrules `run_state_active: false`. `--overwrite` is **required**: without
it `FactRagPipeline` short-circuits on the fact-existence check and no lineage is written.

`heideltime_raw` rebuilds lineage on its fast-return path, so re-running heals provenance
from persisted rows without re-invoking the JAR on chunks that already produced output.

Cost reference (739 documents, 2026-08-29): lineage writes ~3.6 ms/source; the JAR
dominates everything else, per §6.
