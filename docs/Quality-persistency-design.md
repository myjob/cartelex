I have two ways implemented to get facts selected from the database
home/job/a/celex-facts/src/pipelines/rag/pgvector/db.py class FactTable or 
CelexDatabaseToolkit select_facts4celex() and others

what is the better design for persistency services ?

===

Let me examine both implementations to give you a proper architectural comparison.

Viewed db.py:1-155
Viewed celex_tools.py:1-528
Listed directory tools
Listed directory pgvector

Now let me also look at the related components to understand the full picture:

Viewed PipelineFactIngestor.py:1-110
Viewed models.py:1-169
Viewed session.py:1-42
Viewed chunk_repository.py:1-168

Now let me check who consumes these — how the DSPy pipeline modules access data:

Searched for "FactTable"
Searched for "CelexDatabaseToolkit"
Searched for "select_fact"

Let me also check the DSPy protocol and how the modules consume these:

Viewed dspy_protocols.py:1-92
Viewed universal_fact_extraction_pipeline.py:1-167

Now I have a thorough understanding of both approaches. Let me produce the analysis artifact.

Created persistence_layer_analysis.md

## TL;DR

**`FactTable` is the better design** — and it's not close.

The full analysis is in the artifact, but the key points:

1. **`FactTable`** uses ORM models, satisfies your `SQLFactStore` protocol (the contract all DSPy modules depend on), and delegates engine creation to `session.py`.

2. **`CelexDatabaseToolkit`** is a 528-line monolith that mixes 16 concerns (facts CRUD, vector search, embedding caching, LLM validation, company inserts) and creates **its own `create_engine` at line 31** — meaning you're running **two separate connection pools** against the same database.

3. **The recommended path**: evolve `FactTable` → `FactRepository` by absorbing the ~4 fact-query methods currently stranded in `CelexDatabaseToolkit` (like `select_facts4celex`, `select_available_fact_names`). Then inject this repository into both the Toolkit and the Pipeline — following the same "Provisioner" pattern you already successfully used with `ChunkRepository`.

The artifact includes architecture diagrams, a concrete `FactRepository` code sketch, and the migration steps. Want me to proceed with implementing any of these steps?