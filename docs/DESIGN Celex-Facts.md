
## Search Variants

	[Design Search](DESIGN%20Search)
# Fact Format: MD vs  json?



## Ranking with Flash Ranker
#ranking

> **Superseded:** the reranker is pluggable per *profile* via `ranker_factory.create_ranker(profile)`: the **batch** profile (`RERANKER_BACKEND`, ingestion CLIs) defaults to `BgeReranker` (`BAAI/bge-reranker-v2-m3`), the **ui** profile (`UI_RERANKER_BACKEND`, the oi_dspy chat server) defaults to `FlashRanker`. See **[Design BgeRanker](Design%20BgeRanker.md)** and **[Design UI Modules](Design%20UI%20Modules.md#the-reranker-why-this-service-runs-its-own-profile)**.

[FlashRanker (obsolete)](FlashRanker%20(obsolete).md) 
1. **Role and Architecture**: Explained the transition from pgvector/Qwen bi-encoder similarity to cross-encoder reranking via the `flashrank` library (using the `ms-marco-MiniLM-L-12-v2` model) for joint query-document cross-attention.
2. **Core Class**: Detailed the implementation of the `FlashRanker` class in [flash_ranker.py](file://~/a/celex-facts/src/extract/rag/ranker/flash_ranker.py), outlining its `rank_chunkvo` (for document chunks) and `rank_nacevo` (for NACE classification nodes) methods.
3. **Algorithms & Heuristics**:
   - **Relative Depth**: Calculates the depth of a chunk relative to the document size.
   - **Legal Climax Boost**: Employs the formula $(\text{capped\_depth})^4 \times 2.0$ to boost final holdings without completely masking middle-of-document semantic matches.
   - **The 0.98 Cap**: Restricts the maximum depth calculation to prevent trailing boilerplate and signatures from receiving unfair structural boosts.
4. **Integration Points**:
   - **Hybrid Retrieval** ([hybrid_search.py](file://~/a/celex-facts/src/extract/rag/hybrid_search.py)): Reranks merged semantic and keyword hits, using a keyword-hit reservation mechanism to guarantee at least 5 keyword matches are kept.
   - **Hybrid Book Search** ([hybrid_book_search.py](file://~/a/celex-facts/src/extract/rag/hybrid_book_search.py)): Selects the single best seed chunk (`top_k=1`) to expand it to the full chapter context.
   - **Keyword-Only Search** ([key_word_search.py](file://~/a/celex-facts/src/extract/rag/key_word_search.py)): Sorts keyword SQL results relative to query relevance.
   - **NACE Taxonomy Mapping** ([universal_modules.py](file://~/a/celex-facts/src/pipelines/agent_dspy/universal_modules.py)): Matches product descriptions to NACE classes using taxonomy node title text.
   - **Company Alias Extraction** ([corp_ranker.py](file://~/a/celex-facts/src/extract/rag/ranker/corp_ranker.py)): Combines `FlashRanker` with a DSPy Predict module ("Slick Pipe" pattern) to extract genuine corporate aliases from candidate matches.
5. **Configuration**: Documented relevant configuration parameters in [settings.py](file://~/a/celex-facts/src/config/settings.py) such as `settings.CACHE_DIR`.
6. **Verification & Tests**: Checked and ran the unit tests in [test_flash_ranker.py](file://~/a/celex-facts/src/extract/rag/ranker/tests/test_flash_ranker.py) to ensure all tests pass successfully.

alternative considered [BGE-Reranker](BGE-Reranker.md)

Challenge: [answer-a-question vs. enumerate- all-instances of X](answer-a-question%20vs.%20enumerate-%20all-instances%20of%20X.md)


## DSPY
[Design DSPY](Design%20DSPY.md)
## retrieval_module
###  kb_sql step 
[kb_sql](kb_sql.md)




# Fact Ingestion Verifier
see [Design Fact Verifier](Design%20Fact%20Verifier.md)