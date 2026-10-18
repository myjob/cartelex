 `2026-08-25
The `information_schema` genuinely has no comment support — that part of your hesitation is accurate. But comments aren't _in_ `information_schema`; the actual API is `pg_catalog`:

```sql
SELECT obj_description('celex_facts.facts'::regclass, 'pg_class');
SELECT col_description('celex_facts.facts'::regclass, 3);
```

Via `::regclass` this is stable and readable — no raw OID juggling. That's what I used to check your schema during the catalogue work (every table came back `NULL`; you have **zero** `COMMENT ON` statements in the repo).

The real awkwardness is elsewhere, and it's worth naming precisely:

- Comments are separate statements from `CREATE TABLE`, so they drift from the table they describe
- Nothing enforces that a new table or column gets one
- `DROP TABLE` + recreate loses them silently
- Long prose inside SQL string literals needs dollar-quoting to survive apostrophes

Notably, "hard to query" is _not_ on that list, and neither is "lost on backup" — `pg_dump` carries comments through faithfully.

## Why this matters more for your project than most

Two project-specific reasons push this from "nice hygiene" to "actually load-bearing":

1. **You have LLM agents that see your schema.** In a DSPy system where a module may introspect tables or generate SQL, table and column comments are the highest-leverage documentation in the entire codebase — they land directly in `\d+` output and in any schema dump you hand a model. A markdown file in `docs/` does not.
2. **Your migrations already demonstrate the drift problem.** [ISSUES.md](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/ISSUES.md) issue #1 is exactly this: five migration files still target `source_registry` while the live DB has `fact_source_registry`. Nothing caught it.

## The options

|Option|Source of truth|Cost|Verdict|
|---|---|---|---|
|**A.** Prose docs only (today + the catalogue)|`docs/`|Zero|Invisible to psql, tooling, and agents. Doc rots independently of schema.|
|**B.** `COMMENT ON` inside each `migrate_*.sql`|Scattered across 20+ files|Low|**Avoid.** Reproduces exactly the drift that caused ISSUES #1 — you'd have to trace which migration last set a comment.|
|**C.** SQLAlchemy `comment=` in [models.py](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/models.py)|ORM|Low|Attractive, but **won't reach your DB.** See below.|
|**D.** One idempotent `comments.sql` + generated catalogue|One file|Medium|**Recommended.**|

**Why C fails here specifically:** SQLAlchemy does support `Column(..., comment="...")`, and `create_all` emits the `COMMENT ON` — but only when it actually creates the table. Your own docstring at [llm_index_repository.py:208](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/corp_index/llm_index_repository.py#L208) says it: _"create_all stops at `CREATE TABLE IF NOT EXISTS`"_. Every one of your 43 tables already exists, so ORM comments would land on precisely nothing. It's also only a partial map — `models.py` and `celex_pgvector.py` cover maybe half your relations.

`★ Insight ─────────────────────────────────────`

- **`COMMENT ON` is naturally idempotent** — it overwrites unconditionally, with no `IF NOT EXISTS` dance. That single property is what makes a re-runnable "one big file" pattern work for comments when it would be reckless for `ALTER TABLE`.
- **It fails loudly on a missing relation.** Running `comments.sql` against your DB would have thrown on `source_registry` the day that rename happened — the file doubles as a free schema-drift canary, which is the exact failure ISSUES #1 describes.
- **Direction of generation matters.** If the DB is the source and the markdown is generated, "maintaining two things" collapses back into one. If you write both by hand, you've made your original worry real.`─────────────────────────────────────────────────`

## What I'd actually do

**One file, `src/pipelines/rag/pgvector/schema_comments.sql`**, re-run wholesale after any migration:

```sql
-- Idempotent. Safe to re-run in full. Fails loudly if a relation was renamed.
COMMENT ON TABLE celex_facts.facts IS
  $$Central fact ledger: one row per (celex_id, name) with markdown value,
    structured value_json and denormalised verification verdict.$$;

COMMENT ON COLUMN celex_facts.company_investigated_celex_id2opencorp_index_company.resolution_rung IS
  $$Which relaxation step of the OpenCorporates resolution ladder produced this candidate.$$;
```

Dollar-quoting keeps apostrophes and multi-line prose painless.

**Be selective on columns.** Skip `id`, `created_at`, and anything self-evident. Comment only the ones a newcomer would guess wrong — `resolution_rung`, `hash_q_a`, `trust_rank`, `doc_pos`, `context_prefix`, `case_x`. Maybe 40 columns total, not 400. Comment volume is the thing that kills these efforts.

**Enforce with a test, not discipline.** A `unittest` integration test asserting every table in `celex_facts` has a non-null `obj_description`, with an explicit exclusion list for the legacy shells (`langchain_pg_embedding`, `rule_facts.facts`). That's the same complete-ledger-plus-exclude-list shape your fact verification already uses, so it'll read as native to the codebase.

**Then generate the catalogue.** [docs/Data Table Catalogue.md](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/docs/Data%20Table%20Catalogue.md) becomes output of a script that reads comments + live row counts, rather than a hand-maintained second copy. The doc keeps the prose I wrote; the script keeps it honest.

## Division of labour

Keep these separate, or comments will bloat until nobody reads them:

- **In the DB:** one or two sentences. _What is this, what's the grain, what's the gotcha._
- **In `docs/`:** rationale, alternatives rejected, diagrams, history — everything in [Design fact_source_registry](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/docs/Design%20fact_source_registry.md) that doesn't fit in two sentences. Reference the doc name from the comment if you like, but never paste the design into the schema.

**One honest caveat:** this is a real ~200-line authoring effort for 53 relations, and the failure mode is comments going stale while looking authoritative — arguably worse than no comment. The test guards _existence_, not _accuracy_. If you're not going to re-run the file after migrations, stay on Option A; a stale catalogue in `docs/` at least announces its own age via git.

Want me to draft `schema_comments.sql` and the enforcement test? I have all 53 relations and their column lists from the catalogue work, so it's mostly mechanical from here.


`★ Insight ─────────────────────────────────────`

- **Applying the file _is_ the test.** All 161 statements ran with `ON_ERROR_STOP=1` and zero errors, which proves every relation and column name in the file matches the live schema — a spell-checked column name would have raised. Writing comments fails loud in exactly the way reading them fails quiet.
- **`\d+` caught an error I made.** My comment on `legal_provisions.valid_from` said "NULL means open-ended", but the psql output showed `not null`. Putting documentation next to the constraint makes that class of mistake visible; the same sentence in a markdown file would have sat there uncontradicted.
- **A test that has never failed is unverified.** I stripped the comment off `celex_facts.entities`, confirmed the suite failed and named that exact relation, then restored it by re-running the file — which also demonstrated the idempotency the whole design rests on.`─────────────────────────────────────────────────`

## Delivered

**[src/pipelines/rag/pgvector/schema_comments.sql](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/schema_comments.sql)** — 538 lines, 161 statements: a comment on all **53 relations** and **108 of 461 columns**, grouped in the same 10 sections as the catalogue. Header states the contract (idempotent, loud on drift, re-run whole) and the psql invocation with the 5433 port caveat.

Column comments target only what a newcomer misreads. A sample of what's now in the schema:

- `pg_embedding_qwen.celex_id` — keeps the `CELEX-` prefix; stripping it returns zero rows
- `resolution_rung` — lower rungs are stricter queries, so a high-rung match is weaker evidence
- `lei2company_..._mv.distance` — trigram distance, **lower** is better
- `jurisdication_code` — flags that the misspelling is the real column name, and that it joins to a correctly-spelled one
- `langchain_pg_embedding`, `query_embeddings`, `rule_facts.facts` — marked RETIRED; `handelsregister_company_additional_data` marked PLACEHOLDER; `opencorp_index_company_detail` notes its single row is the API budget, not a broken pipeline

**[tests/test_schema_comments.py](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/tests/test_schema_comments.py)** — 4 `unittest` integration tests, passing, `ruff` clean:

|Test|Catches|
|---|---|
|`test_every_relation_has_a_comment`|new relation added without documentation|
|`test_file_covers_exactly_the_live_relations`|drift in **both** directions — including a comment left behind for a renamed relation, the ISSUES #1 failure mode|
|`test_commented_columns_still_exist`|a column comment outliving its column|
|`test_comments_are_not_placeholders`|stub comments under 30 chars gaming the coverage check|

No exclusion list was needed — coverage is 53/53, so the stricter form holds.

**[docs/Data Table Catalogue.md](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/docs/Data%20Table%20Catalogue.md)** — new "In-database counterpart" section near the top with the artefact table, the apply command, and the division of labour (two sentences in the schema; grouping, counts and rationale in the doc). Also cross-linked your new [Howto document database tables](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/docs/dev/Howto%20document%20database%20tables.md), which I noticed you created mid-task.

**[ISSUES.md](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/ISSUES.md)** — one entry: the file is a drift canary that nothing currently runs, so it needs wiring into the migration runbook.

## Two things to know

**I applied the comments to your live database** (port 5433) — 161 metadata-only statements, no data touched, reversible with `COMMENT ON ... IS NULL`. I did it because it's the only way to verify the file and the test; say the word if you'd rather I hadn't.

**Your howto doc references `comments.sql`**; the actual file is `schema_comments.sql`, and it says "43 tables" where the full relation count is 53. I left that doc alone since it's your capture — happy to reconcile the names if you want.