#facts #ingestion
# Design — Fact completeness checker

`tools.fact_completeness_checker` answers one question for a list of decisions:
**are all facts extracted?** It compares the facts a default
`Main_ingest_generic_facts` run is expected to produce with what is stored in
`celex_facts.facts`, and separates genuine gaps from expected absences and from
facts that are stored but empty.

It was written while checking the 41 decisions promoted on 2026-11-01
(`reports/celex_list_new_decisions_20261101.txt`); §5 documents what that check
found, because the findings explain the gap classes the tool reports.

Source: `src/tools/fact_completeness_checker.py`, tests:
`src/tools/tests/test_fact_completeness_checker.py`.

Related: [DESIGN Celex-Facts](DESIGN%20Celex-Facts.md), [Design DSPY](Design%20DSPY.md).

---

## 1. Usage

From the repo root:

```bash
.venv/bin/python -m tools.fact_completeness_checker \
    --celex-list-file reports/celex_list_new_decisions_20261101.txt

# full (celex_id x fact) frame for further analysis
.venv/bin/python -m tools.fact_completeness_checker \
    --celex-list-file <file> --csv reports/fact_gaps.csv
```

The list file has the same format as `--celex-list-file` of
`Main_ingest_generic_facts`: one id per line, `#` comments and blank lines
ignored. The tool is read-only.

---

## 2. What "complete" means

**Expected facts** are the configs in `FACT_EXTRACTOR.yaml` (path from
`settings.FACT_EXTRACTOR_CONFIG_DIR`) with `run_state_active` true. A missing
key counts as true, the same default `Main_ingest_generic_facts._candidate_structs`
applies; a plain truthiness test would silently drop facts such as
`NACE_group_code`, which carries no such key.

Every decision is crossed with every expected fact. Each cell is classified:

| Class | Meaning | Action |
|---|---|---|
| present | at least one row in `celex_facts.facts` | none |
| present, **empty value** | stored, but `value_md` is `NA`, `[]`, `{}` or blank | usually wrong: the ingest cascade treats it as unfulfilled, so dependants are skipped |
| missing, **conditional** | the config has a `condition:` | expected when the condition is not met |
| missing, **unconditional** | no `condition:` | genuine gap |

`EMPTY_FACT_VALUES` mirrors `Main_ingest_generic_facts._has_value`. If that
rule changes, change the constant too.

### Why conditional gaps cannot be confirmed

`celex_facts.fact_ingestion_audit_trail` records *attempts* only. A fact
skipped because its condition was not met, or because a predecessor was
empty, leaves no row. The checker therefore reports `never_attempted`
(no audit row) next to `last_audit_ok`, but cannot distinguish
"condition false" from "never tried". Logging skips with a reason would make
this exact.

---

## 3. Report sections

Logged via `logging` (no `print`):

1. **Header** — number of decisions, number of expected facts, ids with no fact
   row at all.
2. **Per decision** — `have`, `missing_uncond`, `missing_cond`, `empty_values`;
   sorted with the largest genuine gap first.
3. **Missing facts by name** — count per fact, conditional flag, how many were
   never attempted.
4. **Unconditional gaps** — the (decision, fact) list with last audit outcome
   and error.
5. **Empty (NA-like) stored values** — per fact, the decisions holding an empty
   value. This is the section that points at root causes (§5).
6. **Duplicate fact rows** — (decision, fact) pairs with more than one row.

---

## 4. Structure

The DB and YAML boundaries are thin; the analysis is a pure function so the
unit tests need neither database nor YAML on disk.

| Function | Role |
|---|---|
| `read_celex_list(path)` | parse the id list |
| `load_catalogue(path)` | `FactCatalogue(active, conditional)` from the YAML |
| `fetch_rows(engine, ids)` | `FactRow`s from `celex_facts.facts`, latest `AuditRow` per (decision, fact) via `DISTINCT ON` |
| `build_completeness_frame(...)` | pure: one polars row per (decision, fact) |
| `summarize_per_decision(frame)` | section 2 |
| `unconditional_gaps(frame)` | section 4 |

The engine comes from `pipelines.rag.pgvector.session.make_engine()`
(`settings.DATABASE_URL`); boundary rows are validated as pydantic models.

---

## 5. Findings of the first run (2026-11-02, 41 new decisions)

Result: every decision has facts and there are no duplicates, but 13 decisions
have 49 unconditional gaps. Two independent causes:

### 5.1 `products = NA` — non-deterministic empty extraction

All 13 incomplete decisions have `products = NA`; they are the only `NA`
products rows in the corpus (13 of 744). `products` has no predecessors itself,
but `NACE_group_code`, `NACE_class_code` and `OECD_sector` list it as
predecessor, so an `NA` there skips all three (an empty predecessor counts as
unfulfilled).

The cause is not missing input. For DEC-AT39847-20121212 the 2026-11-01 run and
a manual rerun on 2026-11-02 had:

- the same chunks (ingested 2026-11-01 15:16) and the same registry link;
- the same retrieval (`Best cosine 0.319 below floor 0.35 for 40 chunks;
  retaining top-10`);
- the same prompt (4823 prompt tokens), `LLM_TEMPERATURE = 0.0`.

On 11-01 the `ChainOfThought` extract call answered with 245 completion tokens
and an empty `extracted_facts`; on 11-02 it answered `e-books`. The number of
LLM calls tells the two apart in the log: `UniversalFactModule` calls
`find_doc_references` only when items were found, so a successful extraction
shows two `llm_usage` lines and the empty one shows one. Temperature 0 on
Ollama is not bit-reproducible (batching and server load), and short decisions
(avg 74 chunks vs 141) with weak retrieval sit on the "no product found"
boundary. A rerun is a coin flip, not a fix; a retry on empty extraction for
facts that must not be empty would be the robust remedy.

### 5.2 `case_summary = NA` — ordering bug, deterministic

`case_summary` is the first config in `FACT_EXTRACTOR.yaml` and summarises 46
`context_facts`, but declares none of them as `predecessors`. Only predecessors
influence the execution sequence, so on a decision's first run it executes
before any fact exists and stores `NA` (log: `No facts found for celex_id ...
Cannot generate summary.`). Decisions in the 2026-11-01 rerun got a real
summary only because `force_override: true` regenerated it on the second pass;
the 10 decisions not rerun kept `NA`.

Remedy: place `case_summary` after all other selected facts in the execution
sequence.

### 5.3 `recitals_max` missing on 10 decisions

The same 10 decisions were never rerun after `recitals_max` became active; the
pending corpus-wide `recitals_max` run (ISSUES.md) covers them.

Both 5.1 and 5.2 are tracked in `ISSUES.md` (20261102) with the affected ids.

---

## 6. Limitations

- Conditional gaps are reported, not verified (§2).
- Only `run_state_active` facts are expected; facts pulled in by
  `--filter-fact` predecessor closure or `--all-facts` are not.
- `empty_value` flags `NA` also where `NA` is a legitimate answer (e.g.
  `leniency_applicants` in a case without leniency), so read that section per
  fact, not as a total.
