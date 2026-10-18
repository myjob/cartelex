Looking at what you actually loaded into the queue, the honest headline is: **almost none of it is `knowledge_items` material**, and that's not a quality problem — it's a _targeting_ problem. "Is this a good answer" and "is this reusable know-how" are different questions, and conflating them is what makes the queue feel full of rubbish. A perfectly correct answer like "court occurs in 4025 chunks across 497 cases" is still garbage for `knowledge_items`, because it's a volatile corpus aggregate, not doctrine.

So validation here is mostly a **routing/gating** problem, not a scoring one. Let me categorize what's in your queue, then give the filter.

### What's actually in the queue

- **System meta-tasks** — the bulk of it. Rows where the "prompt" is `### Task: Suggest 3-5 relevant follow-up questions`, `Generate a concise … title`, `Generate 1-3 broad tags`. These are UI scaffolding LLM calls. They should never have entered the queue at all.
- **Control commands & chitchat** — `set case all`, `reset celex_id`, `nice` → "The input does not contain a question…". No knowledge content.
- **Failed / empty answers** — the case-not-found error template; the top-5 list with empty values (`39612_12448_6 —` and nothing after the dash).
- **Null-result answers** — "No occurrences of 'court' were found", "No court references were found". Valid answers, but zero positive knowledge.
- **Corpus aggregates** — "4025 chunks across 497 cases", "238 cases". Correct, but volatile — they change as the corpus grows. Cache them as query results if you want; never as `knowledge_items`.
- **Case-specific facts** — the Servier and Sandoz summaries. These are good content, but they belong in `facts`, not `knowledge_items`. Per the earlier design, `knowledge_items` is for _general_ doctrine/procedure/patterns, not per-case facts.

Net: in this sample, the count of rows that are genuine general know-how is roughly **zero**. Expect that, and treat a near-empty validator output as the validator working correctly.

### The staged filter (cheap → expensive)

Run candidates through gates that get progressively more expensive, so the costly LLM checks only see survivors.

1. **Structural rejects — pure rules, no LLM.** Drop rows where the prompt matches a meta-task template (`### Task:`, "follow-up questions", "title", "tags"); control commands (`set`/`reset`/single-token inputs); error-template answers; empty answers or answers with empty list items; null-result answers ("No … were found"). This alone removes the large majority.
    
2. **Type classification — a DSPy signature.** Takes `(prompt, answer)` → label: `general_knowhow` | `case_fact` | `corpus_aggregate` | `non_answer`. Only `general_knowhow` proceeds; `case_fact` routes to the `facts` pipeline; the rest are discarded.
    
3. **Groundedness check.** A candidate must trace to source text — the same hard rule from your `knowledge_edges` design. Does the answer carry an evidence tag (`[case_facts]`, `[step 1]`)? Can you resolve it to ≥1 `facts` row or `pg_embedding_qwen` chunk? No grounding → reject (it would fail the deferred constraint anyway).
    
4. **Contradiction check — critical given your data.** Your queue contains the _same_ query "count occurance of court" answered both "No occurrences" and "4025 chunks", and CELEX-32003D0600 answered both "IV/38279" and "No court references". Cluster the queue by prompt-equivalence; if answers within a cluster disagree, **none auto-promote** — flag for human or re-run. Also check candidates against already-`validated` items: a contradiction creates a `contradicts` edge and blocks promotion.
    
5. **Quality scoring — LLM-as-judge, last and on the smallest set.** Score specificity, generality (states a reusable rule vs. a one-off), correctness given evidence, non-triviality.
    

Survivors land as `status = 'draft'`, never `validated`. Promotion to `validated` stays a separate gated step with human attestation for `scope = 'global'`.

### Fix it upstream, and reconsider the input

Two structural points worth more than the validator itself:

The meta-tasks and control commands shouldn't be _enqueued_ at all. Add a producer-side filter so `knowledge_extraction_queue` only ever receives genuine user-question + agent-answer turns. Filtering them out after the fact is paying to clean up a mess you can avoid making.

More fundamentally: mining `(user-prompt, final-answer)` pairs is the wrong source for _general_ know-how. Final answers are case-specific or aggregate — that's why this queue yields case-facts and counts. Genuine doctrine ("Akzo presumption applies at ~100% capital", "leniency follows the 2006 Notice tiers") comes from the agent's _reasoning trace_ or a dedicated extraction pass over source documents, not from answer text. The earlier review said as much — extraction should be its own run. If your goal is the `knowledge_items` table as originally scoped, point the extractor at ReAct traces or the corpus, not at the answer log.

Want me to sketch the DSPy type-classifier signature, or the producer-side enqueue filter?



  
Done. Summary of changes:

**Stage-1 structural filter** — new module [src/pipelines/agent_dspy/knowledge_stage_filter.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/knowledge_stage_filter.py). Pure-Python rules that reject meta-tasks (`### Task:`, follow-up/title/tag generation), control commands (`reset`, `set case all`, single-token chit-chat), error templates ("case not found", "Pipeline error"), null-result answers ("No occurrences of … were found"), corpus aggregates ("4025 chunks across 497 cases"), and empty/dashed-stub answers. Wired in at both ends:

- **Producer**: [src/ui/agno-ui/oi_dspy.py:204](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/ui/agno-ui/oi_dspy.py#L204) — `_enqueue_for_knowledge_extraction` now short-circuits before the SQL INSERT, so rubbish never reaches `knowledge_extraction_queue`.
- **Consumer**: [src/batch/Main_ingest_knowledge.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/batch/Main_ingest_knowledge.py) — re-applies the filter inside `run()` against the existing pending backlog and marks each filtered row `done` with `error_message='stage_filter: <reason>'` so the queue drains instead of forever re-trying.

**LLM-as-judge** — added `KnowHowJudgeSignature` + `KnowHowJudgeResult` (Pydantic, four-criterion scoring) to [src/pipelines/agent_dspy/knowledge_signatures.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/knowledge_signatures.py). Injected into `KnowledgeIngestor` via `__init__(judge=...)` ([src/pipelines/rag/pgvector/knowledge_ingestor.py](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rag/pgvector/knowledge_ingestor.py)); `ingest()` runs `_judge_or_reject` _before_ opening a DB session, so a reject costs zero writes — no `knowledge_item`, no `knowledge_edge`. Rejection raises `KnowledgeJudgeRejected`; the batch module catches it and records a `success=False` audit row with the full verdict in `predecessors.judge_verdict`. A crashing judge defaults to accept (logged) so an LLM hiccup never blocks the queue. CLI toggle: `--no-judge` in `Main_ingest_knowledge.py`.

**Tests** — 20 stage-filter tests + 6 judge tests, all green. Pre-existing 13 `test_knowledge_ingest_module` tests still pass.