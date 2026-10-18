 use in my agentic system a RAG and extracted  facts about legal cases to answer user questions. A complex REACT agent is extracting further know how.

I wonder how to persist and use this knowledge. Options:

1) store in a knowledge table and extend the agent to consider the knowhow table first

2) add the knowledge snippets to the fact table  but do not associate it to a specific legal case if general know how

What options would you prefer?

-->

## Review Opus

The overall direction (Option 1, hybrid retrieval) is right, and I would adopt it. But the proposal as written drifts toward over-engineering for the *current* state of the repo, and a few schema choices will hurt you. Here is what I would change, grounded in what is already there.

---

### What the proposal gets right

1. **Separating `case_facts` from `knowledge_items` is the correct call.** They have different lifecycles, citation status, and decay profiles. Conflating them via `case_id = NULL` is exactly the kind of nullability-as-semantics anti-pattern that bites you 6 months in.
2. **Query-dependent routing over "always check know-how first."** Always-first know-how would cause the agent to apply general heuristics to specific cases — for cartel decisions that means the agent will confidently mis-quote Art 101(3) doctrine on a case that is actually an Art 102 abuse-of-dominance matter.
3. **Lineage with multiple relation types** (`derived_from`, `specializes`, `replaces`, `contradicts`) and **acyclicity on lineage edges**. Both are right.
4. **The hard rule that every promoted item must have an auditable path back to source text.** This is the single most important rule in the entire proposal for a legal system. Keep it.

---

### What I would push back on

**1. The examples are wrong for this project.**
"Swiss employment cases", "consumer credit acceleration date", "breach-of-contract damages" — none of that fits `src/agents_usr/` or the EU cartel/CELEX domain. The know-how shape here is things like:

- *"Parental liability presumption applies when the parent holds ~100% of the subsidiary's capital (Akzo Nobel doctrine)."*
- *"For SCI (single and continuous infringement) findings, identify the common objective, conduct contribution, and awareness elements separately."*
- *"Leniency reductions follow the 2006 Notice tiers; the first-in applicant can obtain 100% immunity if the conditions in paras 8(a)/(b) are met."*
- *"For fine calculation, separate basic amount, gravity multiplier, duration, and the 10% turnover cap analysis."*

Re-do the worked examples in this domain before you commit to a `knowledge_type` taxonomy; the taxonomy you end up with will look different. Likely candidates: `doctrine`, `procedural_rule`, `calculation_pattern`, `precedent_pattern`, `extraction_heuristic`.

**2. The polymorphic `knowledge_relations` table will fight you in Postgres.**

Your final schema has:

```
source_type, source_id, target_type, target_id
```

This is a classic polymorphic association. You lose foreign-key integrity, every join needs `WHERE source_type = 'X'`, and orphaned rows accumulate silently. In Postgres specifically I would do one of two things instead:

- **Option A (recommended for now):** one edge table with *typed nullable FK columns* and a CHECK constraint enforcing exactly-one-source and exactly-one-target:

  ```sql
  CREATE TABLE knowledge_edges (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      src_knowledge_id uuid REFERENCES knowledge_items(id),
      src_case_fact_id uuid REFERENCES facts(id),
      src_source_span_id uuid REFERENCES source_spans(id),
      dst_knowledge_id uuid REFERENCES knowledge_items(id),
      dst_case_fact_id uuid REFERENCES facts(id),
      relation_type text NOT NULL,
      confidence real,
      created_by text,
      agent_run_id uuid,
      created_at timestamptz DEFAULT now(),
      CHECK (num_nonnulls(src_knowledge_id, src_case_fact_id, src_source_span_id) = 1),
      CHECK (num_nonnulls(dst_knowledge_id, dst_case_fact_id) = 1)
  );
  ```

  You keep referential integrity and CASCADE behavior. The CHECK constraints fully replace the `_type` discriminator columns.

- **Option B:** one edge table per (src_type, dst_type) pair (`kk_edges`, `kf_edges`, `ks_edges`). More tables, but each one is a clean FK graph. Worth it if you only need 3 or 4 combinations.

Avoid the `(type, id)` columns. That schema is fine in MongoDB; in Postgres it forfeits the database's strongest feature.

**3. The status state machine is under-specified.**

`draft → validated → deprecated → superseded` is fine, but who promotes? Today the project already has `src/pipelines/rag/pgvector/models.py:47-58` where `Facts` uses a simple `is_valid: bool`, and `src/pipelines/rag/pgvector/models.py:89-114` where `CelexFactIngestionAuditTrail` already records `pipeline`, `dspy_module`, `predecessors` (JSONB). You already have a lineage primitive — re-use that pattern for knowledge items rather than inventing a parallel one. Concretely:

- `knowledge_items.status` enum: `draft | validated | deprecated`.
- Promotion `draft → validated` is its own DSPy module (a `dspy.Module` that asserts: >=1 path to a source span, no `contradicts` edge to a validated item, reviewer attestation if `scope='global'`). Run it as a batch job. Log every promotion in an audit-trail table that mirrors `CelexFactIngestionAuditTrail`.
- `valid_from` / `valid_to` are nice but you do not need bitemporality on day one. `status` + `updated_at` + an audit row is enough.

**4. The `canonical_id` + `version` versioning duplicates what relations already give you.**

If you have `replaces` edges, you can derive "current version" as "the knowledge_item with no outgoing `replaces` edge." Adding `canonical_id` *and* `replaces` is two sources of truth. Pick one. I would pick the relation form because it falls out naturally from your lineage DAG and avoids a UNIQUE constraint dance every time you supersede an item.

**5. The query classifier is hand-waved.**

The pseudocode `if query_mentions_specific_case: ... elif ...` will not survive contact with real queries ("How was the Akzo presumption applied in cases where the parent's stake was below 100%?" — that is both specific *and* doctrinal). The CLAUDE.md mandates DSPy. Make the classifier a `dspy.Signature` that returns a `RouteDecision` pydantic model with weights over `{case_specific, doctrinal, comparative}` rather than a hard label, and let downstream retrieval read those weights as RRF weights. This composes with the existing hybrid FTS+vector+RRF retrieval rather than fighting it.

**6. Embeddings are missing from the schema.**

The current repo already has multiple embedding tables (`celex-facts-dll-20260426.sql:187`, `pg_embedding_qwen`, `question2fact2embeddings`). The new schema needs to say *which* embedding strategy applies to `knowledge_items`. Two reasonable patterns:

- Embed `knowledge_text` directly on `knowledge_items` (one row, one embedding column, `vector(3072)`, HNSW + cosine, matching the project's existing convention).
- Embed *paraphrased question variants* of each knowledge item separately (mirroring the `question2fact2embeddings` pattern already used for facts). For procedural know-how this is materially better because the user query rarely matches the knowledge statement lexically.

I would use the question-variant pattern, because it already works in this codebase and because know-how is more retrieval-fragile than facts.

**7. Confidence propagation is unspecified.**

If K2 `derived_from` K1, K2's effective confidence cannot exceed K1's. The proposal mentions `confidence` columns but never says this. State it as an invariant and either enforce it on insert or compute an `effective_confidence` view that walks the lineage DAG (recursive CTE, bounded depth 3-4).

**8. "Expand one or two hops" is too generic.**

Different relation types should expand differently at retrieval time:

- `supports` / `cites` / `derived_from`: expand to surface provenance to the agent.
- `specializes`: expand *downward* when query is case-specific, *upward* when query is doctrinal.
- `contradicts`: always surface, always with a label, never silently merge.
- `replaces`: never traverse to a deprecated item unless the query explicitly asks for history.

This is a few lines of code per relation type, not a policy hand-wave.

---

### What I would actually build first (MVP)

Walking back from the full design, here is the minimum that earns its keep:

1. `knowledge_items` table: `id, text, knowledge_type, scope, jurisdiction, status, confidence, created_by, agent_run_id, created_at, updated_at` + `embedding vector(3072)` + `tsvector` generated column + HNSW index. Mirror the conventions in `src/pipelines/rag/pgvector/models.py`.
2. `knowledge_edges` table with typed nullable FK columns and CHECK constraints (option A above). Three relation types only: `supports`, `derived_from`, `contradicts`. Add `specializes` / `replaces` when you actually have a case for them.
3. A `KnowledgeIngestModule` (`dspy.Module`) whose `forward()` takes the ReAct agent's extracted know-how + the trace's supporting facts and writes both the item and the `derived_from` / `supports` edges in one transaction. No item lands without at least one outbound edge to a `case_fact` or `source_span`.
4. A `RouterModule` (`dspy.Signature` -> pydantic `RouteDecision`) that returns RRF weights, not a hard branch.
5. Expose three DSPy tools: `search_case_facts`, `search_knowledge`, `search_sources`. Do not let the agent insert know-how itself in the same turn it is answering — extraction is a separate run with its own audit row, the same way `src/batch/Main_ingest_generic_facts.py` handles facts today.
6. Skip `canonical_id` / `version` / `valid_from` / `valid_to` until you have >=1 real supersession event.

Everything else in the proposal (rich relation taxonomy, bitemporal versioning, knowledge-from-knowledge chains beyond depth 1) is correct in the abstract but should be added when a concrete failure mode demands it. Building it speculatively will slow you down and the schema will end up shaped by guesses rather than evidence.

---

### One concern worth naming out loud

The proposal cheerfully allows knowledge derived from knowledge, then adds a "hard rule" that every item must trace to source. Those two things are in tension and the tension only resolves in code. Encode it as a `CHECK` or a deferred constraint, not as a comment in a markdown doc — otherwise the rule will be broken within a sprint and you will not notice until a citation goes wrong in front of a user.

----------------------------

#  **MVP implementation plan**

**1. Database migration**

Create three objects:

CREATE TYPE knowledge_status AS ENUM ('draft', 'validated', 'deprecated');

CREATE TYPE knowledge_type AS ENUM (

  'doctrine',

  'procedural_rule',

  'calculation_pattern',

  'precedent_pattern',

  'extraction_heuristic'

);

CREATE TABLE knowledge_items (

  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  text text NOT NULL,

  knowledge_type knowledge_type NOT NULL,

  scope text NOT NULL DEFAULT 'domain',

  jurisdiction text DEFAULT 'EU',

  status knowledge_status NOT NULL DEFAULT 'draft',

  confidence real NOT NULL CHECK (confidence >= 0 AND confidence <= 1),

  created_by text,

  agent_run_id uuid,

  created_at timestamptz DEFAULT now(),

  updated_at timestamptz DEFAULT now(),

  embedding vector(3072),

  search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED

);

Add indexes:

CREATE INDEX knowledge_items_embedding_hnsw

ON knowledge_items USING hnsw (embedding vector_cosine_ops);

CREATE INDEX knowledge_items_search_tsv_idx

ON knowledge_items USING gin (search_tsv);

CREATE INDEX knowledge_items_status_type_idx

ON knowledge_items (status, knowledge_type, jurisdiction);

Create typed edge table:

CREATE TABLE knowledge_edges (

  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  src_knowledge_id uuid REFERENCES knowledge_items(id) ON DELETE CASCADE,

  src_case_fact_id uuid REFERENCES facts(id) ON DELETE CASCADE,

  src_source_span_id uuid REFERENCES source_spans(id) ON DELETE CASCADE,

  dst_knowledge_id uuid REFERENCES knowledge_items(id) ON DELETE CASCADE,

  dst_case_fact_id uuid REFERENCES facts(id) ON DELETE CASCADE,

  relation_type text NOT NULL CHECK (

    relation_type IN ('supports', 'derived_from', 'contradicts')

  ),

  confidence real CHECK (confidence >= 0 AND confidence <= 1),

  created_by text,

  agent_run_id uuid,

  created_at timestamptz DEFAULT now(),

  CHECK (num_nonnulls(src_knowledge_id, src_case_fact_id, src_source_span_id) = 1),

  CHECK (num_nonnulls(dst_knowledge_id, dst_case_fact_id) = 1)

);

MVP invariant: **every knowledge_item must have at least one provenance edge to a fact or source_span before it can be validated.**

**2. SQLAlchemy models**

Add KnowledgeItem and KnowledgeEdge beside the existing pgvector models. Mirror the existing facts conventions: UUID primary key, is_valid-equivalent via status, JSON/audit compatibility, pgvector embedding field, and generated search field.

Do not add canonical_id, version, valid_from, or valid_to yet.

**3. Ingestion module**

Create KnowledgeIngestModule(dspy.Module).

Input:

class ExtractedKnowHow(BaseModel):

    text: str

    knowledge_type: Literal[

        "doctrine",

        "procedural_rule",

        "calculation_pattern",

        "precedent_pattern",

        "extraction_heuristic",

    ]

    confidence: float

    supporting_fact_ids: list[UUID] = []

    supporting_source_span_ids: list[UUID] = []

Behaviour:

1. Validate that at least one supporting fact/span exists.
2. Deduplicate against existing knowledge_items using vector similarity + FTS.
3. Insert knowledge_item(status='draft').
4. Insert derived_from edges from facts/spans to the item.
5. Insert supports edges where the know-how supports a fact interpretation.
6. Write audit row using the existing audit-trail pattern.
7. Commit everything in one transaction.

Do **not** let the answer-time ReAct agent write directly to the knowledge table.

**4. Promotion module**

Create KnowledgePromotionModule(dspy.Module) as a batch job.

Promotion checks:

- knowledge item has >= 1 provenance edge

- source path reaches source_span or case_fact

- no contradicts edge from a validated item

- confidence >= threshold, e.g. 0.75

- global-scope items require reviewer attestation

Only this module may change:

draft -> validated

validated -> deprecated

The Swiss Re chatbot documentation uses human/expert evaluation, source checking, and iterative retesting before release; use the same spirit here for promotion rather than trusting the extractor blindly.

**5. Retrieval tools**

Expose exactly three tools to the agent:

search_case_facts(query, filters, k)

search_knowledge(query, filters, k)

search_sources(query, filters, k)

search_knowledge should combine:

vector score

+ FTS score

+ metadata filters: status, jurisdiction, knowledge_type

+ edge expansion

Default filter:

status = 'validated'

Draft knowledge is visible only to ingestion/evaluation workflows.

**6. Router module**

Use weighted routing, not hard branching:

class RouteDecision(BaseModel):

    case_specific: float

    doctrinal: float

    comparative: float

    source_lookup: float

Then apply weights to RRF:

case_facts_weight = case_specific + comparative

knowledge_weight = doctrinal + comparative

source_weight = source_lookup + case_specific

This fits modular RAG patterns: routing and hybrid search are explicitly useful for choosing among retrieval paths and merging results.

**7. Relation expansion rules**

MVP rules:

derived_from / supports:

  expand to provenance and citations

contradicts:

  always surface with warning label

deprecated:

  never retrieve by default

Do not add specializes or replaces until you have real examples requiring them.

**8. Answer pipeline**

Runtime flow:

user query

-> RouterModule

-> parallel retrieval: facts / knowledge / sources

-> RRF merge

-> edge expansion for provenance + contradictions

-> context compression

-> answer generation

-> citation validator

Hard rule: the final answer can use knowledge only if its source path is present in context.

**9. Evaluation**

Start with a small golden set:

20 case-specific questions

20 doctrinal questions

10 comparative questions

10 contradiction/provenance tests

Track:

context precision

context recall

faithfulness

answer correctness

citation validity

contradiction surfacing rate

These match common RAG evaluation dimensions: context relevance/precision/recall, answer correctness, and faithfulness.

**10. Delivery sequence**

Week 1:  
Schema migration, SQLAlchemy models, indexes.

Week 2:  
KnowledgeIngestModule, audit rows, transactional edge creation.

Week 3:  
search_knowledge, hybrid retrieval, RRF router.

Week 4:  
Promotion module, evaluation set, answer-time integration.

Week 5:  
Regression tests, contradiction tests, citation validation, rollout behind feature flag.

**MVP acceptance criteria**

The MVP is done when:

- know-how is stored separately from case facts

- every validated knowledge item traces to facts/source spans

- the agent can retrieve facts and knowledge together

- query routing produces weighted retrieval, not hard branches

- contradictions are surfaced, not hidden

- no answer-time agent writes directly into validated knowledge

- evaluation shows no citation regressions versus facts-only RAG


`knowledge_tsv` is a column that stores the **full-text-search representation** of `knowledge_text`. It's the piece that makes the keyword/lexical half of your hybrid retrieval work.

### What `tsvector` is

`tsvector` is a built-in Postgres data type for full-text search. When you run `to_tsvector('english', some_text)`, Postgres:

- splits the text into tokens (words),
- lowercases them,
- drops stop words ("the", "of", "and"),
- reduces each word to its stem ("infringement", "infringements", "infringing" → roughly the same root).

So `to_tsvector('english', 'Parental liability presumption applies')` produces something like `'appli':4 'liabil':2 'parent':1 'presumpt':3` — stemmed lexemes with their positions. You then match against it with a `tsquery`:

```sql
SELECT * FROM knowledge_items
WHERE knowledge_tsv @@ websearch_to_tsquery('english', 'parental liability');
```

The `@@` operator is "does this tsvector match this query," and `websearch_to_tsquery` parses a Google-style query string. You can also rank results with `ts_rank()`.

### Why it's a `GENERATED ALWAYS AS ... STORED` column

```sql
knowledge_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', knowledge_text)) STORED
```

This makes the column a **generated column**: Postgres computes it automatically from `knowledge_text` on every insert/update, and you can't write to it directly. `STORED` means the computed value is physically saved on disk (Postgres only supports stored, not virtual, generated columns).

The alternative — computing `to_tsvector(...)` inside every query, or maintaining it with a trigger — is more error-prone. A generated column guarantees the FTS representation can never drift out of sync with the text.

### Why it belongs in this design

Your retrieval is hybrid: vector similarity (the `embedding` columns) plus lexical FTS, fused with RRF. The embeddings catch semantic matches; `knowledge_tsv` catches exact-term matches — important in a legal domain where a query naming "Art 101(3)" or "Akzo" should hit on that literal token even if the embedding doesn't rank it highly.

One practical note: this column only matters if you actually query `knowledge_text` lexically. Since Phase 1 of the plan embeds _question variants_ rather than the statement itself, decide whether your FTS should run against `knowledge_text` (the column above) or against the question variants. If the lexical channel should also search the variants, you'd want the `tsvector` on `knowledge_question_embeddings.question_variant` instead, or in addition. And to make it fast, add a GIN index:

```sql
CREATE INDEX ON knowledge_items USING gin (knowledge_tsv);
```

Without the GIN index the `@@` match still works but does a sequential scan.
## Embedding query + response together

I plan to use query + response for creation of embeddings of a knowledge item -- good idea? When do search for query with tsv and when with semantic search?
-->
I'd push back on concatenating them into a single vector. Two reasons:
* [ ] check other fact semantic search for merging query/response !
* [ ] add tsv vector to fact table and search full text ⏫

**Dilution.** An embedding model produces one vector for the whole input. A short question concatenated with a longer knowledge statement means the statement's tokens dominate the vector. You end up with something that's neither cleanly question-shaped nor cleanly statement-shaped — a compromise that's optimal for neither incoming query type.

**Asymmetry.** At retrieval time the agent embeds a pure user _question_. You want the indexed vectors to live in question-space so they match well. The question-variant pattern works precisely because you're matching question against question. Mixing the response text back in moves the indexed vector away from that space.

There's also a "which query?" problem. A knowledge item extracted by the ReAct agent has _one_ originating query, but the know-how is meant to be reused across many _future_ queries. Binding the embedding to the single triggering query overfits. Synthetic question _variants_ (LLM paraphrases, plural) generalize far better — which is what the plan already proposed.

What I'd actually do:

- Primary channel: N synthetic question variants per item, embedded separately, one row each in `knowledge_question_embeddings`. This is your main semantic recall path.
- Optional second channel: embed the knowledge statement itself once, as a separate column/row — useful when a query happens to be phrased statement-like.
- Fuse the two channels with RRF if you keep both. Separate vectors let you _tune_ the balance; a concatenated vector throws that knob away permanently.

One model detail: your repo uses `pg_embedding_qwen`, and Qwen3 embedding models are instruction-tuned (distinct query vs. document prefixes). Embedding questions with the query instruction and statements with the document instruction respects the model's design — concatenation muddies that too.

## tsv vs. semantic — when to use which

The honest answer: in a production legal system, run **both, on every query**, and fuse with RRF. The real question isn't "which channel" but "how to weight them" — and that's exactly what the `RouterModule` weights are for. Gating one channel off is rarely worth it: you're already paying the query-embedding cost, so adding the lexical channel (GIN-indexed, no model call) is nearly free.

Where each channel actually carries the result:

**tsv / full-text search wins on exact tokens.** CELEX numbers, "Art 101(3)", "Article 102 TFEU", case names like "Akzo Nobel", quoted phrases, defined terms of art. Embeddings systematically underweight rare proper nouns and identifiers — the vectors for `CELEX:62009CJ0097` and `CELEX:62009CJ0098` are nearly identical, so semantic search _cannot reliably tell them apart_. In a legal domain that's a serious failure mode: retrieving the wrong case because two citations embed similarly. The lexical channel anchors on the literal string. So you essentially _always_ want tsv contributing here.

**Semantic search wins on paraphrase and concepts.** "Who's on the hook when a parent owns a subsidiary" won't lexically match "parental liability presumption" — no shared stems — but it's semantically close. It also handles users who don't know the precise terminology, and natural-language questions in general. It's weak on negation and exact numeric thresholds ("below 100%" vs "100%").

If you ever genuinely must route to one channel (latency budget), use query shape as the signal, not as an on/off switch:

- Query contains identifiers, citations, case names, quoted phrases → high RRF weight on lexical. Detect with a regex/NER pass for CELEX patterns, `Art(icle)?\s*\d`, capitalized multi-word names.
- Natural-language conceptual question, no identifiers → high weight on semantic.
- Comparative/doctrinal questions → semantic-leaning, plus relation-hop expansion.

But keep both weights non-zero. A conceptual question can still name a case; a citation lookup still benefits from semantic neighbors.

One thing to settle: your plan puts `tsv` on `knowledge_text` (the statement) and embeddings on the question variants — so the two channels search _different text_. That's fine and complementary (lexical hits terms-of-art in the statement, semantic hits question shape), but be deliberate about it. If you also want lexical matching against the question variants, add a `tsvector` there too.


I have 3 sources of wisdom which knowledge could generated from: pg_embedding_qwen -- a document/ chunk keeping table with prim key case_id & doc_pos facts -- extracted from pg_embedding_qwen with prim key id and lineage to doc_pos knowledge_items -- other items

how to model the source_spans
-->
Good — this clarification actually dissolves the open question from before. The review _assumed_ a `source_spans` table, but you already have the thing it was reaching for: **`pg_embedding_qwen` is your source-span table.** It's the only table holding actual source text. So in most cases you shouldn't create a separate `source_spans` table at all — you'd just be adding an indirection layer over a table that already exists.

What a knowledge item can be grounded in is now a clean three-way set:

- a **chunk** — `pg_embedding_qwen`, keyed `(case_id, doc_pos)`
- a **fact** — `facts`, keyed `id`, and itself already carries lineage to a `doc_pos`
- another **knowledge_item** — keyed `id`

That's exactly the polymorphic-destination shape the review told you to handle with typed nullable FKs + CHECK. No new table needed; you just point the edge table at the three you have.

### The one real decision: granularity

The only reason to build an actual `source_spans` table is if **chunk-level grounding isn't precise enough**.

- If `pg_embedding_qwen` chunks are paragraph-sized, citing the whole chunk is fine. Point edges straight at `pg_embedding_qwen`. Done.
- If chunks are large (whole sections or documents), then "this knowledge traces to chunk X" is a weak citation in a legal system. In that case `source_spans` becomes a real, thin **child table of `pg_embedding_qwen`**: `(span_id, case_id, doc_pos, char_start, char_end)` or sentence indices — the precise span within the chunk. Then edges point at `source_spans`, and `source_spans` FKs up to `pg_embedding_qwen`.

Pick based on your chunk size. I'd default to "no table, cite the chunk" and only add `source_spans` when a citation is visibly too coarse.

### Edge DDL — handling the composite PK

The wrinkle is that `pg_embedding_qwen` has a composite key, so the chunk destination is _two_ columns and needs a composite FK:

```sql
CREATE TABLE knowledge_edges (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    src_knowledge_id uuid NOT NULL REFERENCES knowledge_items(id),

    -- destination: exactly one of {chunk, fact, knowledge_item}
    dst_case_id      <case_id_type>,     -- chunk target, part 1
    dst_doc_pos      <doc_pos_type>,     -- chunk target, part 2
    dst_fact_id      uuid REFERENCES facts(id),
    dst_knowledge_id uuid REFERENCES knowledge_items(id),

    relation_type    text NOT NULL,      -- supports | derived_from | contradicts
    confidence       real,
    created_by       text,
    agent_run_id     uuid,
    created_at       timestamptz NOT NULL DEFAULT now(),

    FOREIGN KEY (dst_case_id, dst_doc_pos)
        REFERENCES pg_embedding_qwen (case_id, doc_pos),

    -- chunk target is both-or-neither
    CHECK ((dst_case_id IS NULL) = (dst_doc_pos IS NULL)),
    -- exactly one destination type (dst_case_id stands in for the chunk pair)
    CHECK (num_nonnulls(dst_case_id, dst_fact_id, dst_knowledge_id) = 1)
);
```

Use the actual types of `case_id` and `doc_pos` from `pg_embedding_qwen`. The two CHECKs together do the work: the first ties the chunk pair so you can't have a half-specified chunk; the second uses `dst_case_id` as the proxy for "chunk target present" and enforces exactly-one-of-three.

If the composite FK feels clunky and you'd rather have uniform single-column FKs, the alternative is adding a surrogate `span_id` (identity or uuid, unique) to `pg_embedding_qwen` and referencing that. It's additive and non-breaking, but it rewrites a presumably large table. I'd only do that if you expect more source types later; for three, the composite FK is fine.

### Traceability invariant, restated for three sources

The rule "every knowledge item has an auditable path to source text" now means: every `knowledge_items` row must reach a `pg_embedding_qwen` chunk. Two things make this easy:

- A knowledge→fact edge is **transitively grounded** for free, because `facts` already has lineage to `doc_pos`. So the grounding trigger can accept a fact destination as satisfying the rule — no direct chunk edge required.
- Knowledge-from-knowledge is allowed but must terminate. The cleanest enforcement, if items are created bottom-up: require ≥1 outbound `derived_from`/`supports` edge whose destination is a chunk, a fact, **or an already-grounded knowledge_item**. That keeps the check depth-1 instead of a recursive walk.

So your deferred constraint trigger from Phase 1 stays — it just treats "destination is a fact" and "destination is a grounded knowledge_item" as also satisfying it, not only "destination is a chunk."
-----------------
# copilot obsolsete

I would prefer **option 1: a separate knowledge / know-how table**, but I would **not** make the agent always consult it first. I would make retrieval **query-dependent**.

The best design is a **hybrid**:

Keep **case facts** and **general know-how** logically separate, but expose them through one coordinated retrieval layer.

This gives you clean provenance, better retrieval control, and fewer legal-domain risks.

**Why I prefer a separate knowledge table**

Case facts and extracted know-how are different objects.

A **case fact** is source-bound:

“In Case A, the court held X.”  
“The claimant argued Y.”  
“The contract was terminated on date Z.”

A **knowledge item** is generalized or procedural:

“In termination disputes, check whether notice requirements were contractually modified.”  
“For Swiss employment cases, distinguish ordinary termination from immediate termination.”  
“When answering damages questions, retrieve procedural posture before remedy analysis.”

Those two types have different lifecycles, reliability, citation requirements, and retrieval behavior. Legal RAG work specifically emphasizes retrieving precise, highly relevant legal snippets because broad or irrelevant context can increase hallucination risk and make citations harder to verify. ([arXiv](https://arxiv.org/html/2408.10343v1 "LegalBench-RAG: A Benchmark for Retrieval-Augmented Generation in the Legal Domain"))

So I would avoid putting general know-how into the fact table with case_id = NULL. That tends to create semantic ambiguity:

case_id NULL

could mean:

general knowledge

unknown case

cross-case synthesis

agent-generated heuristic

not yet linked to provenance

Those are very different things.

**Recommended model**

Use something like this:

case_facts

----------

id

case_id

fact_text

fact_type              -- holding, issue, party, argument, remedy, date, procedural posture, etc.

source_document_id

source_span_id

jurisdiction

court

decision_date

confidence

extraction_run_id

created_at

knowledge_items

---------------

id

knowledge_text

knowledge_type         -- rule, heuristic, workflow, definition, issue_pattern, citation_policy, argument_pattern

scope                  -- global, jurisdiction-specific, practice-area-specific, tenant-specific

jurisdiction

practice_area

issue_tags

status                 -- draft, validated, deprecated

confidence

created_by             -- agent, human, rule, batch job

agent_run_id

valid_from

valid_to

created_at

updated_at

knowledge_support

-----------------

knowledge_id

support_type           -- supports, contradicts, example, derived_from

case_fact_id nullable

case_id nullable

source_document_id nullable

source_span_id nullable

note

The important part is the knowledge_support table. Even if the knowledge is “general,” you usually want to know **why the system believes it**. In a legal setting, I would treat agent-extracted know-how as **draft knowledge** until it is linked to evidence or reviewed.

This also lets you support cross-case knowledge:

Knowledge item:

“In similar consumer-credit disputes, limitation-period analysis often turns on the date of acceleration.”

Supported by:

- Case fact 123 from Case A

- Case fact 456 from Case B

- Statutory source span 789

**Retrieval behavior I would use**

I would not do:

Always search know-how first.

Instead:

Classify the query -> retrieve from the right stores -> combine with clear labels.

Example routing:

if query_mentions_specific_case:

    retrieve case_facts filtered by case_id

    retrieve relevant knowledge_items only as reasoning aids

    retrieve raw source spans for citation verification

elif query_is_general_legal_question:

    retrieve validated knowledge_items first

    retrieve supporting case_facts / statutes / source spans

    use raw RAG fallback if confidence is low

elif query_is_comparative_or_strategy_question:

    retrieve knowledge_items

    retrieve multiple related case_facts

    rerank jointly

Frameworks now commonly support long-term memory / knowledge stores organized by namespace and key, and semantic memory is commonly modeled as facts or knowledge stored in searchable collections. ([LangChain Docs](https://docs.langchain.com/oss/python/langchain/long-term-memory "Long-term memory - Docs by LangChain")) Metadata filtering is also a standard RAG pattern, useful when you need to filter retrieved documents by attributes not contained in the text itself, such as jurisdiction, case ID, practice area, or knowledge type. ([Developer Documentation](https://developers.llamaindex.ai/typescript/framework/modules/rag/query_engines/metadata_filtering/ "Metadata Filtering | Developer Documentation"))

**How the agent should use the knowledge table**

I would expose the knowledge table to the agent as a **separate tool**, for example:

search_case_facts(...)

search_legal_knowhow(...)

search_sources(...)

Then give the agent rules like:

- Use case_facts for claims about a specific case.

- Use knowledge_items for reasoning patterns, issue spotting, and answer structure.

- Do not cite knowledge_items as legal authority unless they are linked to authoritative support.

- If knowledge_items and case_facts conflict, prefer source-grounded case facts and retrieve raw sources.

- Label general know-how separately from facts of the case.

This avoids a common failure mode where the model answers a specific legal question using a general heuristic that does not actually apply to the case.

**When option 2 is acceptable**

Option 2 can work only if your “fact table” is really a more general **knowledge unit table** with explicit typing.

For example:

knowledge_units

---------------

id

text

unit_type        -- case_fact, general_knowhow, statute_summary, procedural_rule, agent_memory

scope

source_status

embedding

Then you can have subtype tables:

case_fact_details

general_knowledge_details

statute_details

But I would not use a plain fact_table with nullable case_id as the only distinction. That will become painful once you need filtering, evaluation, deletion, provenance, or conflict handling.

**My preferred architecture**

Use **separate logical stores**, with a **shared retrieval orchestrator**:

                   User question

                        |

                 Query classifier

                        |

       ------------------------------------

       |                 |                |

case_facts       knowledge_items     raw sources

       |                 |                |

       -------- retrieval + reranking ----

                        |

             answer composer with labels

                        |

         citations / confidence / caveats

In the prompt/context, keep sections separate:

[Case-specific facts]

...

[Validated general know-how]

...

[Source excerpts]

...

That separation matters a lot. It lets the model know what is **evidence**, what is **derived know-how**, and what is merely **reasoning support**.

**Final recommendation**

Choose **option 1**, but modify it:

Store know-how in a separate knowledge_items table, link it back to supporting facts or source spans, validate/promote it before using it as authoritative, and retrieve it based on query intent rather than always using it first.

I would only choose option 2 if you redesign the fact table into a typed, generic knowledge-object table. Otherwise, mixing case facts and general know-how will likely make retrieval, citation, and legal reliability worse over time.

Yes, correct. knowledge_support can absolutely reference **former / existing knowledge_items**. In fact, I would usually model this as a small **knowledge graph** rather than a simple support table.

For example, a new know-how item may be derived from:

case facts

source excerpts

statutes

previous knowledge items

human annotations

agent reasoning traces

So instead of only this:

knowledge_support

-----------------

knowledge_id

case_fact_id

source_document_id

source_span_id

support_type

I would generalize it into a relation table.

**Better model: knowledge relations**

knowledge_relations

-------------------

id

from_item_type        -- knowledge_item, case_fact, source_span, statute, regulation, etc.

from_item_id

to_item_type          -- knowledge_item, case_fact, source_span, statute, regulation, etc.

to_item_id

relation_type         -- supports, contradicts, derived_from, generalizes, specializes, updates, replaces, example_of

confidence

created_by            -- agent, human, rule, batch

agent_run_id

created_at

note

Then you can express:

Knowledge item K2 is derived from knowledge item K1.

Knowledge item K3 generalizes knowledge items K1 and K2.

Knowledge item K4 contradicts K1.

Knowledge item K5 replaces K2.

Case fact F1 supports knowledge item K1.

Source span S1 supports case fact F1.

Example:

-- K2 was derived from K1

from_item_type = 'knowledge_item'

from_item_id   = 'K1'

to_item_type   = 'knowledge_item'

to_item_id     = 'K2'

relation_type  = 'derived_from'

Or, depending on your direction convention:

source_knowledge_id = 'K1'

target_knowledge_id = 'K2'

relation_type       = 'derived_from'

The exact direction does not matter as long as you are consistent.

**I would distinguish support from lineage**

I would avoid using one vague relation like supports for everything. For agentic legal systems, these distinctions matter:

supports        = provides evidence for the claim

derived_from    = was generated using this prior item

generalizes     = abstracts from several narrower items

specializes     = narrows a broader rule to a jurisdiction, issue, or context

contradicts     = conflicts with another item

replaces        = supersedes an older item

example_of      = illustrates a general rule

cites           = points to legal authority or source text

For example:

K1:

"In employment termination cases, notice-period compliance is often a threshold issue."

K2:

"In Swiss employment termination cases, distinguish ordinary termination from immediate termination before analyzing notice periods."

Relation:

K1 -> K2, relation_type = 'specializes'

Another example:

K3:

"When answering damages questions, first identify whether liability has already been established."

K4:

"For breach-of-contract damages, retrieve findings on causation, foreseeability, mitigation, and proof of loss."

Relation:

K3 -> K4, relation_type = 'specializes'

**Important: avoid unsupported knowledge chains**

The risk with reflexive knowledge relations is that the system starts building know-how from know-how from know-how, without grounding.

That can create this bad chain:

Source span -> case fact -> knowledge item -> broader knowledge item -> even broader knowledge item

After several hops, the final item may sound plausible but no longer be strongly supported.

So I would require every promoted knowledge item to have at least one path back to a primary source:

knowledge_item

   -> previous knowledge_item

      -> case_fact

         -> source_span

            -> document

In legal applications, I would treat this as a hard rule:

A knowledge item may be derived from another knowledge item, but it should not be treated as authoritative unless it has an auditable path back to source material or human validation.

**Add versioning**

For former knowledge items, I would include versioning or replacement relations.

knowledge_items

---------------

id

canonical_id          -- same across versions

version

knowledge_text

status                -- draft, validated, deprecated, superseded

valid_from

valid_to

created_at

updated_at

Then:

knowledge_relations

-------------------

from_item_id = old_K1

to_item_id   = new_K1

relation_type = 'replaces'

This lets the agent avoid using stale know-how while preserving the reasoning history.

**Prevent problematic cycles**

Since knowledge items can reference other knowledge items, cycles can appear:

K1 derived_from K2

K2 derived_from K3

K3 derived_from K1

Some relation types can tolerate cycles, such as related_to. But for lineage relations like these, I would enforce acyclicity:

derived_from

generalizes

specializes

replaces

For these relation types, keep them as a DAG if possible.

You can enforce this in application logic rather than SQL:

def can_add_relation(source, target, relation_type):

    if relation_type in {"derived_from", "generalizes", "specializes", "replaces"}:

        return not creates_cycle(source, target, relation_type)

    return True

**Practical recommendation**

I would rename knowledge_support to something broader:

knowledge_edges

knowledge_relations

knowledge_lineage

knowledge_links

My preferred version would be:

knowledge_items

---------------

id

canonical_id

version

text

knowledge_type

scope

jurisdiction

practice_area

status

confidence

created_by

created_at

knowledge_relations

-------------------

id

source_type

source_id

target_type

target_id

relation_type

confidence

created_by

created_at

agent_run_id

note

Then, for retrieval, the agent can do things like:

1. Retrieve relevant knowledge_items.

2. Expand one or two hops to supporting items.

3. Check whether each item has primary-source grounding.

4. Prefer validated items over draft or deeply derived items.

5. Surface contradictions or superseded knowledge.

So yes: **make it reflexive**, but treat it as **lineage plus evidential support**, not just a generic support table. That will give you much better control over provenance, confidence, versioning, and legal reliability.