
!! this is a Q&A cache rather than a knowledge graph


Here's the lifecycle of celex_facts.knowledge_items 

1. [knowledge_ingestor.py:219-225](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/knowledge_ingestor.py#L219-L225) — `KnowledgeItem(...)` is constructed **without** a `status` argument, so it falls through to the server default in [models.py:304](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/models.py#L304): `server_default="draft"`.
    
2. There is no code in the ingestion pipeline that promotes items to `validated`. `update_knowledge_item()` exists in [knowledge_repository.py:160](vscode-webview://09s9jltolj53fe3oilakakddlc8mm5ps5gkvumjtnesl3k3i7ld9/src/pipelines/rag/pgvector/knowledge_repository.py#L160) for exactly that purpose, but nothing calls it post-ingest.
    

The intended three-state lifecycle is:

- `draft` — newly extracted by the pipeline (machine output, unreviewed)
- `validated` — promoted by a human or downstream validation step
- `deprecated` — retired

So if processing succeeded, `draft` is correct — it means "ingested and grounded, awaiting validation." If you want the LLM judge's acceptance to auto-promote items to `validated`, you could pass `status="validated"` in `ingest()` (line 219), but that would skip the human review gate.

Do you want to add an auto-promote to `validated` after a successful judge pass, or is the manual validation step intentional here?