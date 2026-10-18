#adr

# Embeddings
#article
## costs
the following local embeddings model where considered [](</docs/gemini-gems/Local Legal Text Embedding Models.md>)
Decsision: go local with Qwebn
## benchmark
openai vs. qwen

### MLEB is the benchmark you want

The **Massive Legal Embedding Benchmark** (Isaacus, arXiv 2510.19365) scores both models directly. It consists of ten expert-annotated datasets spanning the US, UK, EU, Australia, Ireland, and Singapore, covering cases, legislation, regulatory guidance, contracts, and literature. All figures NDCG@10:

| Rank | Model                  | Task avg | Judicial | Contractual | Regulatory |
| ---- | ---------------------- | -------- | -------- | ----------- | ---------- |
| 4    | Qwen3 Embedding 8B     | 82.96    | 78.49    | 82.88       | 88.99      |
| 6    | Qwen3 Embedding 4B     | 81.96    | 76.14    | 84.19       | 87.49      |
| 9    | Text Embedding 3 Large | 78.91    | 75.24    | 79.70       | 83.00      |

So your switch is supported — about 4 points on task average, and the Judicial and Regulatory splits (closest to CELEX decisions) favour Qwen by 3.2 and 6.0 respectively.

Two caveats before you treat that as settled for your corpus:

**Coverage.** MLEB is deliberately English-only and Anglosphere-heavy. The single EU dataset is GDPR Holdings Retrieval — 500 fact patterns paired with holdings in European regulatory and court decisions, built from GDPRHub. Nothing on competition law, nothing from CELEX itself. Directional, not predictive.

**Domain adaptation beats general rank.** The paper's headline finding is that qualities making a model good at general multilingual IR aren't the same as those making it good at legal IR — Gemini is 1st on MTEB but 7th on MLEB, and Voyage Law 2, older and likely smaller than most high scorers, ranks 8th, ahead of Text Embedding 3 Large. The top two (Kanon 2 Embedder 86.03, Voyage 3 Large 85.71) are both API-only, which conflicts with your local-only setup. Worth noting the authors also flag potential data leakage for Voyage, Jina and Google models, whose ToS opt users into training by default.

Given 4B is only one point behind 8B at 2560 dims, it's worth a head-to-head on your own eval — you already have ~50 labelled examples from the MIPROv2 work to seed a CELEX retrieval set, and re-embedding 740 documents is cheap.

### Two things about the switch itself

**The 4096 dims are a pgvector problem.** (I read ">4000 params" as output dimensions — 8B params, 4096 dims.) pgvector's HNSW/IVFFlat cap is 2000 dims on `vector`; `halfvec` raises it to 4000. 4096 clears neither. So `pg_embedding_qwen` at native width stores fine but can't be HNSW-indexed — you're on sequential scan unless you've already handled it. Options:

- MRL-truncate to ≤2000 (`vector`) or ≤4000 (`halfvec`), renormalising after slicing. Qwen3-Embedding supports user-defined dims down to 32.
- Binary quantize to `bit` (indexable to 64,000) as a first stage, exact-rerank the candidates.

Since you're already adding `bge-reranker-v2-m3`, truncating to 1024–2048 and leaning on the reranker is probably a better tradeoff than 4096 unindexed.

**Instruction prefix.** Qwen3-Embedding is instruction-aware, and using instructions typically yields a 1–5% improvement over not using them. The prefix goes on **queries only** — documents stay raw:

```
Instruct: Given a legal question about EU competition law, retrieve relevant passages from Commission decisions and Court judgments
Query: {query}
```

Ollama's embed endpoint won't add this for you. If you swapped models without it — or worse, applied it symmetrically to documents — that's free recall you're leaving behind.
### vector index on the default embedding table.
`pg_celex-ddl.sql:397` (HNSW on `pg_embedding_qwen`, the default `settings.EMBEDDING_TABLE`) and `:495` (`knowledge_question_embeddings`) are commented out. Every semantic search is a sequential scan. Root cause: the embeddings are 3072-dim and pgvector caps HNSW on plain `vector` at 2000 dims — the index must use a `halfvec(3072)` cast expression, and queries must use the exact same expression to hit it.
**Decision:** However, only 30k of records does not justify the rather low performance gain with the slightly lower precision. this needs t o be revised if docs are growing.

### Decision:  Qwen3 Embedding 8B



# Polars rather than Pandas
#adr 
❯ check all dependency of pandas and recommend if they easily can migrated to polars

I'll find every place pandas is used in the codebase, then assess each for polars migration difficulty.

  Searched for 1 pattern

Polars is already a transitive/declared dependency (polars==1.39.3). Let me examine how pandas is actually used in each of the 5 files.

  Ran 1 shell command

Let me look at the trickier usages — to_sql, read_sql_query, to_excel, to_markdown — since those are the polars migration friction points.

  Searched for 1 pattern, ran 2 shell commands

Here's the full picture. Pandas (2.3.3) is declared in both pyproject.toml and requirements.txt, and polars 1.39.3 is already installed — so migrating doesn't add a dependency, it removes one.

Pandas is used in 5 files. The reads/transforms migrate trivially; the friction is concentrated in two spots: to_sql inside a live transaction, and to_markdown (which polars has no equivalent for).

Per-file assessment

| File                                   | Pandas usage                                       | Difficulty         | Verdict                |
| -------------------------------------- | -------------------------------------------------- | ------------------ | ---------------------- |
| heideltime/qa/scan_dates.py            | read_sql_query, iterrows+isna, DataFrame, to_excel | 🟢 Easy            | Migrate                |
| utils/canonical_openai.py              | DataFrame, sort_values; returns pd.DataFrame       | 🟢 Easy            | Migrate (+1 consumer)  |
| tools/markdown_utils.py                | DataFrame + to_markdown                            | 🟡 No polars equiv | Drop pandas, hand-roll |
| legacy_rule_engine/rule_fact_loader.py | DataFrame + to_sql in engine.begin()               | 🔴 Hard            | Keep / rewrite raw     |
| rag/pgvector/celex_case_id_loader.py   | read_csv, rename/select, to_sql in txn             | 🔴 Hard            | Keep / rewrite raw     |

Details

🟢 scan_dates.py — standalone QA script, no public API. Direct mappings:
- pd.read_sql_query(query, conn) → pl.read_database(query, conn) ✓
- df.iterrows() + pd.isna(x) → df.iter_rows(named=True) + x is None
- df.to_excel(path, index=False) → df.write_excel(path) ✓ (needs xlsxwriter)

Fully self-contained — clean migration.

🟢 canonical_openai.py — results_table()/audit_table() just build a DataFrame from dicts and sort. pl.DataFrame(rows).sort(["mentions","canonical_name"], descending=[True,False]). Catch: the return type is public and its one consumer (load_company_canonical_alias.py:49) uses pandas idioms that must change too:
- .empty → .is_empty()
- .iterrows() → .iter_rows(named=True)
- list(reg_row["aliases"]) stays the same

Small, contained — migrate the function and its single caller together.

🟡 markdown_utils.py — the catch here: polars has no to_markdown. This is also the hottest path (3 production consumers via format_fact_to_markdown). Don't reach for polars — just hand-roll the markdown table (it's ~5 lines) or use tabulate. That removes pandas from this hot path entirely without pulling polars into it.

🔴 rule_fact_loader.py & celex_case_id_loader.py — both call df.to_sql(..., con=connection, schema=..., if_exists="append", chunksize=1000) inside an engine.begin() transaction (and celex_case_id_loader does a TRUNCATE first in the same txn). This is the real blocker:
- Polars write_database takes a URI/engine, not a live SQLAlchemy Connection — so it can't join the existing transaction, breaking the atomicity with the preceding existence-check / TRUNCATE.
- schema= becomes dotted table_name, and chunksize has no direct analog.

Migrating these to polars is a net regression. If you want pandas gone here, the better move is a plain SQLAlchemy bulk insert (connection.execute(insert(...), records)) — no DataFrame library at all, and it keeps the transaction intact.

Recommendation

You can't cleanly drop the pandas dependency as-is, because of the two to_sql writers and to_markdown. But a pragmatic path gets you there:

1. Quick wins (do now): migrate scan_dates.py and canonical_openai.py to polars — low risk, polars is already installed.
2. De-pandas without polars: hand-roll markdown_utils.py; convert the two to_sql loaders to raw SQLAlchemy inserts.
3. After those, remove pandas==2.3.3 from pyproject.toml/requirements.txt.

## Decision: stay with mix for the time being