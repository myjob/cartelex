# CLAUDE.md — celex-facts

## Multi Threading
dont worry about the Obsidian vault in the project but use it as knowledge:
- the mass of markdown docs (225) may have outdated content 
- ISSUE.md and other docs contain tasks prefixed with "- [ ]"; if the prefix is "- [x]", then the issue is solved
- user may edit docs and source during your activities -- only raise any updates if they cause conflicts with your work

## infrastructure
- any inference task goes either on the remote Ollama server deployed on NVIDIA Spark DGX or any configured external LLM -- never on the local GeForce GPU with small memory and outdated driver support
- GeForce GPU can be used for cross ranker such as BGE rather than CPU in Docker and local runtime.

## File Access
Anything under these folders may be read and written by the Claude agent without asking:
- ~/a/celex-facts/
- ~/a/celex_docs/

## documentation 
copy all implementation plans into folder ~/a/celex-facts/docs/claude/plans
if implementation unveil any issue, task, or consideration, add on top of ~/a/celex-facts/ISSUES.md with this prefix "- [ ] #high " + datetag in format YYYYMMDD

## DSPy Rules (non-negotiable)

**Rule 1 — Declarative Signatures Only.**
No f-string prompt templates. Every task definition is a `dspy.Signature` with `desc="..."` on every field.

**Rule 2 — Logic-Free Signatures.**
`dspy.Signature` classes are purely declarative. All business logic, SQL lookups, and vector searches live in the `forward()` method of a `dspy.Module`.

**Rule 3 — Structured Outputs.**
All extraction `OutputField`s reference a `pydantic.BaseModel`. Never return raw strings from extraction tasks.

**Design Patterns:**
- *Provisioner*: DB clients and vector stores are injected in `__init__`, not created inside `forward`.
- *Forward Dispatch*: `forward()` implements the "Slick Pipe" — e.g., SQL → fallback → vector → DSPy extraction.
- *Functional Composition*: Prefer focused `dspy.Module` components over monolithic agents.

## Pydantic Rules
- Use `pydantic.BaseModel` with `Field(desc="...")` for all structured data — inputs, outputs, configs.
- Validate at system boundaries (DB results, LLM outputs, user input). Trust internal code.
- Prefer `model_validator` / `field_validator` over manual `if` checks in business logic.

## Preferred Tools
- ALWAYS use polars instead of pandas for data frame manipulation unless a framework insists on dependency to pandas.

## Testing
- Framework: `unittest` (not pytest) — project convention.
- Test files go in `./tests/` relative to the source package (e.g., `src/pipelines/agent_dspy/tests/`).
- Each test file needs a corresponding `__init__.py` in its directory.
- if integration tests are demanded:
    - then: use the system configs in config.settings DATABASE_URL, LLM_MODEL_ID, etc
    - else: tests must be runnable in isolation; inject mocks only at system boundaries (DB, LLM calls).

## Code Style
- Python 3.12 — use `X | Y` unions, `match`, `tomllib` where appropriate.
- Type-annotate all public functions and class attributes.
- Keep modules small and single-responsibility. No god-classes.
- Logging via `logging.getLogger(__name__)` — never `print()` in production code.
- Constants and config values from `src/config/settings.py`; no magic strings/numbers inline.
- NEVER use emoji, or unicode that emulates emoji (e.g. ✓, ✗). The only exception is when writing tests and testing the impact of multibyte characters.
- MUST include docstrings for all public functions, classes, and methods
- MUST document function parameters, return values, and exceptions raised

## Self-Check Before Delivering Code
* Imports use `.venv`; no assumption of a global Python environment.
* No overlap with existing modules in `/src` (check before adding a new util).
-  Test file provided in the correct `./tests/` subdirectory.
* `ruff check` passes with no errors.

## Key Decisions (Architecture)
- **Vector DB**: PostgreSQL + pgvector (cosine similarity). Qwen3 is the sole embedder (`qwen3-embedding:8b`, 2048-token ctx) in table `settings.EMBEDDING_TABLE` (`pg_embedding_qwen`). The former OpenAI `text-embedding-3-large` implementation was pruned in Aug 2026 — code paths, `EMBEDDING_MODEL_openai`, the `EMBEDDING_PROVIDER` toggle, and the `langchain-openai` dependency all removed; it can only be restored from backups.
- **Chunking**: `TokenTargetMarkdownSplitter` — preserves header/subheader context; not a naive fixed-size splitter.
- **Retrieval**: `HybridSearch` fuses a Postgres FTS lexical leg (`websearch_to_tsquery`, OR-combined stemmed lexemes) with the pgvector dense leg via Reciprocal Rank Fusion (`settings.RRF_K`), then reranks with a pluggable cross-encoder plus a structural boost (`settings.RANKER_BOOST_MODE`, default `section_type` weighting; the legacy Legal Climax depth boost remains selectable for A/B — both in `ranker/position_boost.py`). An optional `section_types` filter (per-fact `section_scope:` key in `FACT_EXTRACTOR.yaml`) restricts retrieval to classified decision sections (`Main_classify_sections.py` backfill). A lexical reserve guarantees FTS hits survive the top_k cut. `EnumerationSearch` keeps its own BM25/`plainto_tsquery` leg for recall-tuned enumeration.
- **Reranker**: backend chosen per *profile* via `ranker_factory.create_ranker(profile)` — a frozen pydantic `RankerProfile(name, backend, model_id)` mirroring the LLM profiles. `batch_ranker_profile()` reads `settings.RERANKER_BACKEND` / `RERANKER_MODEL_ID` (default `bge`) and is what every bare `create_ranker()` call (ingestion CLIs, `ranker or create_ranker()` fallbacks) gets; `ui_ranker_profile()` reads `settings.UI_RERANKER_BACKEND` / `UI_RERANKER_MODEL_ID` (default `flashrank`) and is passed only by the oi_dspy backend server, so the chat container stays light while ingestion keeps the cross-encoder. Backends: `bge` (`BgeReranker`, sentence-transformers `CrossEncoder`, default `BAAI/bge-reranker-v2-m3`, sigmoid-scored) or `flashrank` (`ms-marco-MiniLM`, ignores the model id). The factory caches one live instance per (backend, model id), never per profile name; tests that patch the model boundary must call `reset_ranker_cache()`. Model-agnostic: point the model id at any CrossEncoder-compatible reranker (e.g. a `Qwen3-Reranker` size served on a GPU). Design doc: [docs/Design BgeRanker](docs/Design%20BgeRanker.md).
- **LLM**: use (`settings.LLM_MODEL_ID`) 
- **Agents**: If Agno, then  with `add_history_to_context=False, read_chat_history=False` for batch/generate mode.
- **Orchestration**: PydanticAI — dependencies injected as typed structs, not globals.
- **Prompts**: Agents with more than one logical section use a separate `.md` file in `prompts/`.

## What to Avoid
- Do not use `gpt-oss-120b` — unstable with Ollama/agentic workflows.
- Do not use ChromaDB or Weaviate — pgvector is the chosen vector store.
- Do not use LangGraph for new agent work — DSPy are preferred.
- Do not create new files unless strictly necessary; prefer editing existing ones; explicitly exempting test scaffolding.
- Do not add backwards-compat shims or `_unused` variables.
- Do not read or comment on `src/pipelines/rag/pgvector/work-script-pgvector.sql`


## Role
Senior Python engineer working on an EU cartel decision fact-extraction system.

## Environment
- Virtual environment: `.venv` — all commands and imports must target this path.
- Run tests with:  `.venv/bin/python -m unittest`
- Linter: `ruff check` — run before delivering code.
- Config: `src/config/settings.py` (`Config` class, env-overridable). Do not hardcode connection strings or model IDs; reference `settings.*`.
- secrets: `config_main/.env` readable for you and Docker image but .gitignored and not shareable 
