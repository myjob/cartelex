

!! this is a Q&A cache rather than a knowledge graph

Ingest extracted know-how from the ReAct agent's QA traces into the knowledge-graph repository (`knowledge_items`, `knowledge_question_embeddings`, `knowledge_edges`) as a **separate batch job**, modelled on the existing `Main_ingest_generic_facts.py` → `FactRagPipeline` → `FactIngestor` pattern.
#### Ingestion (`KnowledgeIngestModule`, a `dspy.Module`)

Extraction runs as its own batch job with its own audit row — the agent does **not** insert know-how in the same turn it answers a user. Model this on how `src/batch/Main_ingest_generic_facts.py` handles facts.

`forward()` takes the ReAct agent's extracted know-how plus the trace's supporting facts, then writes the item, its question variants + embeddings, and the `derived_from`/`supports` edges in one transaction. The deferred trigger guarantees nothing lands ungrounded. Log every ingest in an audit table that mirrors `CelexFactIngestionAuditTrail` (pipeline, dspy_module, predecessors JSONB).

![Pasted image 20260522193315](assets/Design%20KnowledgeIngestModule/Pasted%20image%2020260522193315.png)


### What was built

| Layer | File | What |
|-------|------|------|
| DDL | [pg_celex-ddl.sql](file://~/a/celex-facts/src/pipelines/rag/pgvector/pg_celex-ddl.sql) | Queue table + audit table + deferred grounding trigger |
| ORM | [models.py](file://~/a/celex-facts/src/pipelines/rag/pgvector/models.py) | `KnowledgeExtractionQueue` + `KnowledgeIngestAuditTrail` |
| Signatures | [knowledge_signatures.py](file://~/a/celex-facts/src/pipelines/agent_dspy/knowledge_signatures.py) | `KnowHowExtractionSignature` + `QuestionVariantSignature` |
| Ingestor | [knowledge_ingestor.py](file://~/a/celex-facts/src/pipelines/rag/pgvector/knowledge_ingestor.py) | Single-transaction persistence with deferred trigger |
| Module | [knowledge_ingest_module.py](file://~/a/celex-facts/src/pipelines/agent_dspy/knowledge_ingest_module.py) | `dspy.Module` — extract → variants → persist → audit |
| Batch | [Main_ingest_knowledge.py](file://~/a/celex-facts/src/batch/Main_ingest_knowledge.py) | Queue-driven batch runner with `--limit` / `--dry-run` |
| Agent | [oi_dspy.py](file://~/a/celex-facts/src/ui/backend_server/oi_dspy.py) | Fire-and-forget queue writer after answer synthesis |
| Tests | [test_knowledge_ingest_module.py](file://~/a/celex-facts/src/pipelines/agent_dspy/tests/test_knowledge_ingest_module.py) | 13 tests (unit + integration) |

The existing [test_knowledge_repository.py](file://~/a/celex-facts/src/pipelines/rag/pgvector/tests/test_knowledge_repository.py) was updated to work with the deferred grounding trigger.


## Closing the loop: grounding, queue gates, question probe (20260911)

Trigger: the UI prompt "list all cases related to NSK Deutschland GmbH" never hit a knowledge item. It was queued five times; every row ended `done`, yet the audit trail logged `ungrounded: extractor cited no supporting facts` for each. Across the store, 295 of 485 ingest attempts had failed this way, and none had succeeded.

### Why the loop was starved

```
planner Rule 4: company name -> doc_search
        -> evidence = chunk ids only, fact_ids = []
        -> queued anyway, marked done
        -> KnowledgeIngestModule grounds on facts only -> every nugget "ungrounded"
        -> no knowledge item, so nothing to find next time
```

Three separate defects, each reasonable on its own:

1. **Routing.** `QAPlannerSignature` Rule 4 sent every company name to `doc_search` ("entity names are in document chunks"). That predates the graph views: `company_case_relations_vw` holds fact-backed rows for NSK with a `source_fact_id` each, which `_run_kb_sql` already maps into `fact_ids`.
2. **Queue.** The producer enqueued answers with no facts and no dedup. The batch spent an extraction LLM call on each, only to drop every nugget.
3. **Lookup.** `knowledge_question_embeddings` was written for every item but had no reader. `knowledge_search` ran AND-FTS over the raw question, so question words ("list", "cases") that nuggets never contain zeroed it. The planner was also told both "use as first step" and "supplementary only".

### 1. Planner routing

Code: `agent_dspy/dspy_signature.py` (Rule 4, Rule 9, tool list) and the `_GRAPH_SCHEMA` prose in `agent_dspy/retrieval_dispatch.py`.

- **Rule 4(a)**: company/undertaking names go to `kb_sql` over `company_case_relations_vw` whenever the injected kb_schema lists it (`settings.GRAPH_QA_ENABLED`); otherwise `doc_search`. **Rule 4(b)**: commissioners, towns, products and other free-text entities stay on `doc_search`.
- **Whole-word company match**, not a substring stem. The Rule 9 stem rule gets an exception for company names.

| pattern | NSK cases | Bayer cases |
|---|---|---|
| `company_name ILIKE '%nsk%'` | 16 (Dansk, Svenska, Slovenske, ...) | 22 (6 are "Bayerische ...") |
| `company_name ~* '[[:<:]]nsk[[:>:]]'` | 1 | 16 |

  The POSIX `[[:<:]]` / `[[:>:]]` form is used instead of `\m` / `\M` because the plan travels as JSON, where `\m` is not a valid escape.
- **`knowledge_search` is supplementary everywhere.** Knowledge from a previously answered question now arrives through the probe (section 3).

Live check on `qwen3-coder:30b`:

| prompt | planned tool |
|---|---|
| list all cases related to NSK Deutschland GmbH | `kb_sql` (view, word-boundary regex) |
| which cases involved Koninklijke | `kb_sql` |
| cases involving Commissioner Karel Van Miert | `doc_search` |
| which companies were in cartels together with Bayer | `graph_search` |
| what fines were imposed in CELEX-31994D0823? | `case_facts` |

### 2. Queue gates

Code: `agent_dspy/knowledge_stage_filter.py`, `ui/backend_server/oi_dspy.py` (`_enqueue_for_knowledge_extraction`), `batch/Main_ingest_knowledge.py`, `rag/pgvector/knowledge_repository.py`.

- **Grounding rule**: `apply_stage_filter(prompt, answer, fact_ids=...)` rejects with reason `no_fact_ids` when the list is empty (`None` skips the rule). It runs last, so structural reasons such as `control_command` still win. One rule serves both sides: the producer never enqueues such rows, and the batch marks the existing backlog `filtered` without an LLM call.
- **Dedup**: `normalize_prompt()` removes the router's `[context: ...]` suffix, maps typographic quotes to ASCII, lowercases and collapses whitespace. `KnowledgeRepository.find_queued_duplicate(answer, key, normalize)` narrows candidates by exact answer in SQL and compares prompts in Python. The normaliser is injected so the key has one definition, and `rag/pgvector` stays free of `agent_dspy` imports.

### 3. Pre-plan question probe

Code: `agent_dspy/knowledge_probe.py` (`KnowledgeProbe`), `KnowledgeRepository.search_knowledge_by_question`, the `KnowledgeQuestionMatcher` protocol in `agent_dspy/dspy_protocols.py`, and wiring in `QAExecutorModule` / `QARetrievalPipeline` / `oi_dspy.py`.

The probe embeds the question with the toolkit's cached qwen3 embedder, the model the variants were ingested with. It then takes each knowledge item's closest stored variant within the distance ceiling. The executor prepends the survivors as `--- Step 0 (knowledge_match) ---`, whatever tools the planner chose. Step 0 does not count as a plan hit, so the empty-plan fallbacks still run. Rendering is shared with `knowledge_search` via `render_knowledge_items`. A probe failure (embedder or DB) is logged and treated as no match.

**Calibration (20260911, live store).** Cosine distance from the question to the nearest stored variant:

| probe | distance |
|---|---|
| true paraphrases | 0.030 - 0.083 (one outlier 0.220) |
| entity swap: "list cartel peers of NSK" -> NTN item | 0.167 |
| entity swap: "list cartel peers of Bayer" -> NTN item | 0.194 |
| scope swap: "list leniency cases" -> variant scoped to 39309_3643_4 | **0.066** |

Embeddings capture the shape of a question far more than the entity it names, so distance alone cannot tell these apart. The scope swap sits inside the paraphrase band. The probe therefore pairs the threshold with a deterministic **anchor guard** (`anchors_compatible`):

- **Identifier anchors**: tokens containing a digit (celex ids, case numbers, years; a leading `CELEX-` removed) must form the *same set* in the question and the matched variant. A case-scoped variant cannot answer an unscoped question, and vice versa. The router's context suffix is part of the probed question, so a sticky case only matches variants scoped to that case.
- **Name anchors**: capitalised tokens of the question (question openers and institutional words like Commission, Article excluded) must each occur as a token of the variant or the knowledge text.

Live result with the defaults: all three paraphrases hit (9, 4 and 1 items), while the entity swap, scope swap and unrelated questions miss.

| setting | default | meaning |
|---|---|---|
| `KNOWLEDGE_PROBE_ENABLED` | `true` | false restores plan-only evidence (A/B) |
| `KNOWLEDGE_PROBE_MAX_DISTANCE` | `0.10` | cosine ceiling; above the paraphrase band, below the entity swaps |
| `KNOWLEDGE_PROBE_LIMIT` | `20` | list answers are distilled one item per entry (one item per cartel peer), all sharing a seed question |

### Known limits

- **Grounding does not guarantee an item.** The LLM judge rejects per-case facts as not generalizable (it rejected "list cartel peers of NSK"). An entity-to-case question may therefore never produce a knowledge item, which is acceptable: the view answers it directly and exactly.
- **Legacy variants.** Items ingested before the stage filter carry seed questions like `reset  [context: ...]` or `set all`. The anchor guard keeps them from matching unscoped questions, but they are noise in the variant table.
- **The probe is a cache, not a judge.** Its hits are evidence for the synthesizer, not a returned answer, so a false match adds noise rather than replacing the fresh retrieval.

Plan: `docs/claude/plans/knowledge-routing-and-question-probe-20260911.md`.


## Where supporting facts are checked (20260912)

A recurring question when reading this pipeline: does anything verify that a
queue row's supporting facts **exist** before the row is accepted? No — and
that is deliberate. Four layers each check something different, and none of
them is an existence precheck.

| # | Layer | Code | Checks | Does *not* check |
|---|---|---|---|---|
| 1 | Stage-1 structural filter | `agent_dspy/knowledge_stage_filter.py` | `fact_ids` list is non-empty (`no_fact_ids`) | that any id is well-formed or resolvable |
| 2 | Fact-id parse | `batch/Main_ingest_knowledge.py::_parse_fact_ids` | every id is a well-formed UUID | that a row exists behind it |
| 3 | Catalog build | `agent_dspy/knowledge_ingest_module.py::_build_fact_catalog` | reads the facts, renders `[i] name: value` | nothing — a missing fact renders `<unknown>` and is **not** rejected |
| 4 | FK at COMMIT | `knowledge_edges.dst_fact_id -> facts.id` | existence, authoritatively | — |

**The judge is not part of this chain.** `KnowledgeJudgeRejected`
(`rag/pgvector/knowledge_ingestor.py`) is a nugget-quality gate: four
criteria (specificity, generality, correctness, non_triviality) plus a
`_JUDGE_MIN_CRITERION_SCORE` floor of 0.5 on the minimum, so an `accept=True`
verdict with one weak criterion still rejects. It runs *first* in `ingest()`,
ahead of even the `if not supporting_fact_ids: raise ValueError` check,
because a rejection should cost zero DB work. It never sees a fact id.

**Why layer 3 tolerates a missing fact.** `_build_fact_catalog` keeps a
placeholder slot rather than dropping the row, because the extractor cites
facts by **catalog index** and `_resolve_fact_refs` maps those indices back to
`supporting_fact_ids`. Dropping a missing fact would shift every later index,
so the model's `source_fact_refs` would silently resolve to the *wrong* facts.
The placeholder preserves the index-to-UUID bijection; a nugget that cites the
`<unknown>` slot then dies at layer 4 instead of being mis-attributed.

**Why existence is not prechecked.** Grounding is enforced by construction,
not by validation: the deferred `trg_knowledge_grounding` trigger rejects an
item with zero outgoing edges, and the `dst_fact_id` FK rejects an edge to a
phantom fact. Both fire at `COMMIT`, inside the single transaction
`ingest()` opens. A `SELECT`-based precheck would be a second, weaker source
of truth for an invariant the schema already owns, and it would still race
against a concurrent delete. A phantom id therefore surfaces as an
`IntegrityError`, is caught by the per-nugget `except Exception` in
`forward()`, audited with `success=False`, and the queue row ends `failed`.

### Fix: unparseable `fact_ids` no longer end `done` (20260912)

Layer 2 previously logged a warning and fell through with `fact_ids = []`.
Layer 1 had already passed the row (the raw JSONB list was non-empty), so the
run continued with an **empty catalog**: every nugget resolved to zero cited
facts, each was skipped as `ungrounded`, `result.errors` stayed empty, and the
row was marked **`done`** — a corrupt row consumed with nothing to show for
it, and one extraction call spent.

`_parse_fact_ids` now normalises `ValueError` / `TypeError` /
`AttributeError` from `uuid.UUID` into a single `ValueError`, and the caller
marks the row `failed` before any LLM call. Parsing is **all-or-nothing**:
keeping the subset that happens to parse would ground the nuggets on partial
evidence, claiming a provenance the agent never had. `failed` (not
`filtered`) is the right terminal state — unlike a stage-filter drop this is a
producer-payload defect, so it stays visible and is re-queued only by a
deliberate reset. Tests: `batch/tests/test_Main_ingest_knowledge.py`.


## The entity graph as validated evidence (20260912)

The typed entity graph is not a second opinion — it is a **verified projection
of the same fact store** this pipeline grounds on. `entity_edges.source_fact_id`
is `NOT NULL`, so an edge exists only because a fact family produced it, and
since the 20260911 routing change `kb_sql` and `graph_search` project exactly
that anchor into the queue row's `fact_ids`.

The graph therefore already reached the ingestor — **stripped of everything
that made it validated**. By the time `KnowledgeIngestModule` saw it, a `fined`
edge from a company to a decision had been flattened into an opaque UUID and
rendered as `[3] fine_amount: EUR 10 000 000`. The judge scored `correctness`
on retrieved prose with a validated relation table one join away.

### What changed

The cited facts are now resolved back to the relations they materialized, and
those relations are treated as ground truth by both the extractor and the
judge. `ingest()` applies its gates cheapest-and-most-authoritative first:

```
1. entity-graph contradiction   deterministic  -- no inference, no DB
2. LLM judge                    one inference
3. supporting_fact_ids empty?   ValueError
4. persist + deferred trigger   one transaction
```

| Layer | Code | Role |
|---|---|---|
| Value object | `graph/entity_graph_models.py::ValidatedRelation` | Both endpoint names, relation, decision, lineage anchor, amount |
| Query | `entity_graph_repository.py::relations_for_facts` | Reverse lineage walk on `ix_edges_source_fact` |
| Evidence | `agent_dspy/knowledge_graph_evidence.py` | Rendering, narrowing, contradiction detection |
| Gate | `knowledge_ingestor.py::KnowledgeGraphContradiction` | Hard reject before the judge |
| Wiring | `knowledge_ingest_module.py` | One lookup per trace, narrowed per nugget |

Relations are resolved **once** for the whole supporting-fact set, then each
nugget receives only those anchored on the facts *it* cited — one query, and no
nugget's evidence widened beyond its own citation.

### Why a hard reject rather than more judge context

Because the graph is authoritative, a nugget denying one of its relations is
wrong by construction, not merely low quality. That verdict needs no inference,
so it is settled first and the LLM is never called. This is the rare case where
a deterministic check can safely overrule an accepting judge.

The detector is **high precision, low recall** by design. It fires only when
one sentence names the relation's source entity, carries a negation, and that
negation sits close in front of a surface form of the relation. Every guard has
a test:

| Guard | Rationale |
|---|---|
| Negation must **precede** the surface | "participated but was not fined" denies `fined`, not `participated_in` |
| Hedge words disqualify | "it is not clear whether X was fined" is uncertainty, not denial |
| Word-boundary entity match | `\bbayer\b` does not match "Bayerische Hypo- und Vereinsbank AG" — the same discipline as the graph's own `\m`/`\M` seeds |
| Distinctive tokens only | a name reducing to "Holding GmbH" matches nothing, so the relation is skipped |
| Case scope via `extract_anchors` | a claim about `CELEX-31994D0823` cannot refute a `39922_2067_2` edge |
| No surface form, no check | `parent_of`, `active_in` and `dated_in` render as evidence but never contradict |

Skipping is always the safe default: a missed contradiction still reaches the
judge, whereas a false one would discard a sound nugget with no LLM in the loop.

### Two caps, because the fan-out is skewed

A participation fact family emits one edge per party. Measured on the live
graph (20260912): **avg 4.3 edges per fact, p95 15, max 196**. A wide trace
would otherwise render thousands of lines into the prompt.

| setting | default | bounds |
|---|---|---|
| `KNOWLEDGE_GRAPH_EVIDENCE_ENABLED` | `true` | false restores the prose-only pipeline (A/B) |
| `KNOWLEDGE_GRAPH_EVIDENCE_MAX_RELATIONS` | `500` | the lookup — kept generous so the deterministic gate stays wide |
| `KNOWLEDGE_GRAPH_EVIDENCE_RENDER_LIMIT` | `40` | the prompt — a truncated block says it is partial, so absence is never read as denial |

### The abbreviation trap

The first live probe returned **clean** for a denial it should have caught.
Real canonical names are full of abbreviations, and a naive `(?<=[.!?;])\s+`
split shattered the claim into three fragments:

```
'Hitachi-LG Data Storage, Inc.'
'and Hitachi-LG Data Storage Korea, Inc.'
'was not fined in this decision.'
```

The fragment carrying the denial named no company, so the entity check never
fired — the gate would have been silently inert on exactly the corpus it
serves. `_split_sentences` now rejects a boundary whose preceding word is a
known abbreviation or a single letter.

### Known limits

- **Corroboration only reaches cited facts.** The graph can confirm or refute a
  nugget grounded on facts that produced edges; a nugget grounded elsewhere
  gets no graph signal at all.
- **A graph outage degrades silently.** `_resolve_validated_relations` logs a
  WARNING and continues with no relations: the graph is an *additional* trust
  signal, so losing it must weaken the gate, never fail the run.
- **No entity linkage yet.** `knowledge_edges` still has no `dst_entity_id`, so
  "what do we know about NSK?" cannot be answered by walking from the node.
- **Redundancy is not enforced.** A nugget restating a graph relation is only
  nudged down through the judge's `non_triviality` prose, not deterministically
  rejected.

Plan: `docs/claude/plans/knowledge-ingest-entity-graph-evidence-20260912.md`.