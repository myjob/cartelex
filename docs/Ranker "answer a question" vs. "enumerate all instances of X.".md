I have a challenge with my ranking in RAG using FlashRanker. The retrieval performance is good when I need the top 15 chunks with relevant info. But if I want to retrieve **ALL** terms such as Courts involved my current implementation misses those which are named seldom. what are my design options
[FlashRanker (obsolete)](FlashRanker%20(obsolete).md) · [Design BgeRanker](Design%20BgeRanker.md)

The core issue here isn't a bug in FlashRanker — it's a task mismatch. A cross-encoder reranker optimizes _precision@k_: it's built to surface the few chunks most relevant to a query and push everything else down. "Retrieve all courts involved" is a _recall_ task — closer to exhaustive entity extraction than to ranking. Your pipeline is doing exactly what it was designed to do, which is the wrong thing for this query type.

Two parts of your current implementation actively work against you here:

- **`top_k` truncation.** A court named once, in passing, scores low against a query like "courts involved" because the chunk's content is dominated by whatever else it's about. If it lands at rank 40, top-15 discards it. No threshold tuning fixes the tail because rare mentions are _intrinsically_ low-scoring.
- **The Legal Climax boost.** A court named only in the fact recitals (early in the document) gets the _smallest_ `position_boost`, since `depth` is near zero. So an early, seldom-mentioned court is penalized twice: weak semantic match _and_ weak structural boost. The boost is great for "what was the holding" and counterproductive for "enumerate every X."

So the design question is really: how do you add a recall-oriented path without breaking the precision path you already like.

Here are your options, roughly from smallest change to most robust.

**1. Threshold on `base_score`, not adjusted score, and drop `top_k` for these queries.** Your decoupled scoring already supports this. For enumeration queries, rank on `base_score` (bypassing the position boost) and return everything above a low cutoff instead of a fixed k. Cheap, but only a partial fix — rare mentions can still fall below any sane threshold.

**2. Make the Legal Climax boost a flag.** Add a `apply_position_boost: bool = True` parameter to `rank_chunkvo`. Enumeration queries pass `False`. This is a one-line change that removes one of the two biases working against you. Worth doing regardless of what else you pick.

**3. Hybrid retrieval — add a lexical/BM25 stage.** Court names are named entities with specific surface forms ("Bundesgerichtshof", "Court of Justice of the European Union", "Oberlandesgericht …"). Bi-encoders embed rare proper nouns poorly, but BM25 matches the literal string regardless of how rare it is. Run BM25 alongside your dense retrieval and union the candidate sets before reranking. This is the standard fix for "the embedding model doesn't know this token."

**4. Reframe enumeration as a map over all chunks, not a sort-and-truncate.** This is the real fix. Instead of "rank chunks by relevance to 'courts' and take the top 15," ask of _every_ chunk independently: "does this chunk name a court? which one(s)?" Recall is then bounded by chunk coverage, not by `k`. The reranker becomes irrelevant to _discovering_ the set — it can still be used afterward to pick the best passage describing each court's _role_. This costs more compute (one pass per chunk) but it's the only approach where recall doesn't degrade with corpus size.

**5. NER / gazetteer pass for known entity types.** "Courts" is a closed-ish, well-structured category. A gazetteer of court names plus pattern matching (legal court names are fairly regular across CELEX documents) gives you near-guaranteed recall for that specific entity type, independent of any retrieval scoring. Combine with option 4 as a high-recall candidate generator, then canonicalize/dedupe (the same court may appear under several surface forms).

**6. Use a finer chunk granularity for the extraction path.** A rare court buried in a chunk that's mostly about something else gets a diluted score. Sentence- or paragraph-level windows for the enumeration path raise the signal-to-noise ratio per unit of text. You can keep your current chunk size for the QA path.

**7. Query expansion / multi-query.** Generate sub-queries ("referring court", "appellate court", "tribunal", "national court", plus known court-type names), retrieve for each, and union. Helps, but it's still ranking-bounded — use it to _widen_ the candidate pool feeding option 4, not as the answer by itself.

My recommendation is a two-mode pipeline. Classify the query intent up front — "answer a question" vs. "enumerate all instances of X." The question path keeps FlashRanker exactly as is, position boost and all. The enumeration path turns off the boost, uses hybrid (dense + BM25) retrieval with generous recall, and treats the result as a _map-and-extract over all candidates_ rather than a top-k. For a fixed, structured entity type like courts, layer a gazetteer/NER pass on top for a recall floor. The reranker still has a job in the enumeration path — ordering the evidence passage for each distinct court once you've found the full set — but it's no longer the thing responsible for _finding_ the set.

One practical note: decide explicitly whether your desired output is "the set of distinct courts" or "every passage that mentions a court." Those need different post-processing (dedup/canonicalization vs. not), and conflating them is a common source of "missing" results that are actually just collapsed duplicates or unmerged variants.

If you tell me how query intent currently arrives (is it a free-text user query, or do you control the call sites?), I can sketch where the mode split should live relative to `rank_chunkvo`.