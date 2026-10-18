# Data Table Catalogue

Inventory of every relation in the `celex` database as introspected on **2026-08-25**.
Schemas covered: `celex_facts` (all production data) and `rule_facts` (legacy rule engine).

Row counts are exact (`count(*)`) at the snapshot date and drift with every pipeline run — treat
them as order-of-magnitude, not as contract. Type key: `table`, `view`, `matview`.

## In-database counterpart

Every description below also lives **in the database itself** as a `COMMENT ON`, so it is visible
from `psql \dt+` and `\d+`, from any GUI client, and to any agent that introspects the schema.

| Artefact | Role |
| --- | --- |
| [schema_comments.sql](../src/pipelines/rag/pgvector/schema_comments.sql) | Authoritative source of the in-database comments — 53 relation comments, 108 column comments. |
| [tests/test_schema_comments.py](../src/pipelines/rag/pgvector/tests/test_schema_comments.py) | Fails when a relation is undocumented or the file and schema disagree. |
| [Howto document database tables](dev/Howto%20document%20database%20tables.md) | Why the comments live in the DDL rather than only here, and the options weighed. |

Apply it — idempotent, safe to re-run in full, and it raises on a renamed relation, so running it
after every migration doubles as a schema-drift canary:

```bash
psql -h localhost -p 5433 -U celex_facts -d celex \
     -f src/pipelines/rag/pgvector/schema_comments.sql
```

Note port **5433** (docker). A bare `psql` reaches the host-native 5432, which does not carry
these schemas.

**Division of labour:** the SQL file holds one or two sentences per relation — what it is, what the
grain is, what the gotcha is. This document holds the grouping, the row counts and the links out to
design rationale. Anything longer than two sentences belongs in a design doc, not in the schema.

---

## 1. Facts core

The extracted-fact ledger and its lineage, verification and audit satellites.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `facts` | table | 42,296 | Central fact ledger: one row per (`celex_id`, fact `name`) with markdown value, structured `value_json`, doc/fact reference arrays and verification status/score. Every downstream view and Q&A path resolves through here. | [fact names](<fact names.md>), [Data Model](<Data Model.md>) |
| `fact_source_registry` | table | 23,995 | Canonical registry of provenance anchors a fact may cite: document chunk, HeidelTime event, LEI record, CSV row or another fact. Decouples "what a source is" from "which fact uses it". | [Design fact_source_registry](<Design fact_source_registry.md>) |
| `fact_source_link` | table | 57,629 | N:M edge between `facts` and `fact_source_registry`, carrying extraction method, relation type, trust rank and conflict flags plus the producing pipeline/DSPy module. | [Data Lineage Option & Implementation](<Data Lineage Option & Implementation.md>) |
| `fact_evidence` | table | 40,908 | Per-chunk verification evidence for a fact: support label, final score and the entity/keyword/semantic/temporal/provenance sub-scores, the supporting quote and missing elements. | [Design Fact Verifier](<Design Fact Verifier.md>), [hybrid evidence verifier](<hybrid evidence verifier.md>) |
| `fact2book_checks` | table | 22,188 | Complete per-fact ledger of the book cross-check: one row per (fact_id, celex_id) with the cited book chunk, judge label and score, explanation and the machine routing decision (corroborated / mismatch / skipped). Rewritten by every run. | [Design Fact 2 Book Reconcilation](<Design Fact 2 Book Reconcilation.md>), [Report Fact 2 Book Reconcilation](<Report  Fact 2 Book Reconcilation.md>) |
| `fact2book_case_runs` | table | 699 | Case-level ledger of the book cross-check driver: one row per (celex_id, case_id) with the outcome of the latest pass — run id, in-book flag, absence reason, reference type and the per-fact counters. Answers "was this case visited at all"; `--resume` skips keys finished under the same run id. | [Design Fact 2 Book Reconcilation](<Design Fact 2 Book Reconcilation.md>) |
| `fact_ingestion_audit_trail` | table | 56,701 | Append-only run log of every fact-extraction attempt: question asked, output schema, success/errors, duration, pipeline, DSPy module and predecessor steps. | [Pipeline Management -- Selective, Quality-driven Reprocessing](<Pipeline Management -- Selective, Quality‑driven Reprocessing.md>) |
| `fact_ingestion_audit_trail_vw` | view | 53,500 | Audit trail joined to the resulting fact value, newest first — the operational "what did the last run produce" lens. | — |
| `legal_provisions` | table | 11 | Reference list of treaty articles (provision, article number, treaty, validity window, canonical concept) used to normalise infringement citations across decision vintages. | [Data Quest Infringement Articles](<Data  Quest Infringement Articles.md>) |
| `rule_facts.facts` | table | 0 | Legacy rule-engine fact store (guid, celex_id, fact, question, value). Drained; retained only as the migration source for company standard aliases. | [Purge Agno](<Purge Agno.md>) |

## 2. Fact-derived reporting views

Thin projections over `facts` that flatten specific fact names into columns.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `celex_index_vw` | view | 702 | One row per decision: case title, industry, cartel period and case summary pivoted out of `facts`. The default case-picker index. | [Data Model](<Data Model.md>) |
| `facts_cartel_legal_period_vw` | view | 702 | Pairs each decision's `cartel_period` with its `legal_assessment_period`. | [Data Model](<Data Model.md>) |
| `facts_cartel_legal_period_category_vw` | view | 702 | Parses both periods into dates and classifies their overlap into a period category — the temporal-consistency QA lens. | [Data Quality Quests & Actions](<Data Quality Quests & Actions.md>) |
| ~~`qa_company_delta_vw`~~ | view | *dropped* | Diffed `companies_investigated` against `companies_canoncial` to QA the rename. Dropped 2026-08-29 with the `companies_canoncial` fact by `migrate_delete_companies_canoncial_fact.sql`: with the canonical side gone every row reported a one-sided set, so the view measured nothing. | [Data Quality Quests & Actions](<Data Quality Quests & Actions.md>) |

## 3. Document corpus and embeddings

Chunked decision text, book text and cached query vectors. Qwen3 (`qwen3-embedding:8b`) is the sole embedder.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `pg_embedding_qwen` | table | 56,005 | Primary vector store (`settings.EMBEDDING_TABLE`): decision chunks with Qwen3 embedding, document text, contextual prefix, `section_type` classification, token count, chunk hash and the FTS `document_tsv` column feeding the hybrid lexical leg. | [DESIGN Search](<DESIGN Search.md>), [Design-Contextual-chunk-prefixes](<Design-Contextual-chunk-prefixes.md>) |
| `pg_embedding_book_qwen` | table | 637 | Same shape for the reference-book corpus (book name, heading level, title, source line) — the retrieval side of the fact-to-book cross-check. | [Design Book](<Design Book.md>) |
| `langchain_pg_collection` | table | 4 | LangChain collection registry (uuid, name, metadata) that the embedding tables key off. | [Data Ingestion](<Data Ingestion.md>) |
| `langchain_pg_embedding` | table | 0 | Original LangChain-default embedding table from the OpenAI `text-embedding-3-large` era. Emptied when that path was pruned in Aug 2026; kept as an empty shell only. | [ADR Architecture Decision Record](<ADR Architecture Decision Record.md>) |
| `query_embeddings_qwen` | table | 2,030 | Cache of Qwen3 query-side vectors keyed by query text, so repeated searches skip an embedding call. | [DESIGN Search](<DESIGN Search.md>) |
| `query_embeddings` | table | 32 | Predecessor query-vector cache from the retired OpenAI embedder. Historical only. | [ADR Architecture Decision Record](<ADR Architecture Decision Record.md>) |
| `heideltime_extractions` | table | 198,302 | Every temporal expression HeidelTime found in a chunk: TIMEX id/type/normalised value, character span and raw JSON. Backs temporal scoring in fact verification. | [HeidelTime Date Format Quest](<HeidelTime Date Format Quest.md>), [Event Extraction Quest](<Event Extraction Quest.md>) |

## 4. Question-and-answer retrieval

Denormalised question/fact pairs used by the Q&A retrieval pipeline.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `question2fact2embeddings` | table | 34,149 | Denormalised Q&A search surface: question text, fact name, answer text, its embedding and a `hash_q_a` change key. Largest relation by embedding volume (625 MB). | [Knowledge Q&A Table](<Knowledge Q&A Table.md>), [Data Model Quest Denormalization](<Data Model Quest Denormalization.md>) |
| `question2fact2embeddings_vw` | view | 34,074 | The above joined back to `facts` for celex/fact context. | [qa_retrieval_pipeline](<qa_retrieval_pipeline.md>) |
| `question2fact_name` | view | 56 | Distinct fact-name to canonical-question mapping, taken from the most recent audit-trail entry per fact name. | [Questions](<Questions.md>) |
| `question2fact` | view | 34,073 | Every fact expressed as a question/answer pair by joining `facts` to `question2fact_name`. | [DSPy QA Complex Retrieval Module](<DSPy QA Complex Retrieval Module.md>) |

## 5. Knowledge layer

Curated, cross-decision knowledge distilled from agent conversations.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `knowledge_items` | table | 15 | Curated knowledge statements with type, scope, jurisdiction, status, confidence, author/agent run and an FTS `knowledge_tsv`. | [DESIGN Knowledge](<DESIGN Knowledge.md>) |
| `knowledge_edges` | table | 93 | Typed relations from a knowledge item to a fact, another knowledge item or a source, with confidence and provenance. | [ADR Knowledge Graph in psql CTE vs. neo4j](<ADR Knowledge Graph in psql CTE vs. neo4j.md>) |
| `knowledge_question_embeddings` | table | 60 | Embedded question variants per knowledge item, so paraphrased user questions still retrieve it. | [DESIGN Knowledge](<DESIGN Knowledge.md>) |
| `knowledge_extraction_queue` | table | 47 | Inbox of UI prompt/answer pairs staged for knowledge extraction, with supporting fact and chunk ids plus processing status. | [WebUI Q&A](<WebUI Q&A.md>) |
| `knowledge_ingest_audit_trail` | table | 156 | Run log for knowledge ingestion, mirroring the fact audit trail (pipeline, DSPy module, predecessors, errors, duration). | [DESIGN Knowledge](<DESIGN Knowledge.md>) |

## 6. Entity graph

Generic entity/edge overlay used for graph-shaped queries without leaving PostgreSQL.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `entities` | table | 5,571 | Typed nodes (company, article, product, jurisdiction, ...) with canonical name, natural key, optional LEI/NACE/article number and free-form `attrs_json`. | [ADR Data Quest Model Entities](<ADR Data  Quest Model Entities.md>) |
| `entity_edges` | table | 9,138 | Directed, typed relations between entities, scoped to a decision and traceable back to the originating fact. | [Graph Databases are Dead](<Graph Databases are Dead.md>), [ADR Data Quest Model Ontology](<ADR Data Quest Model Ontology.md>) |

## 7. CELEX / case identity

Mapping between CELEX document ids and Commission case ids.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `celex_case_id` | table | 699 | Authoritative CSV-loaded mapping of `celex_id` to case number (`case_x`, `case_id`). One-off bulk load. | [CELEX 2 Case ID Mapping](<CELEX 2 Case ID Mapping.md>) |
| `case_celex_id` | table | 1,352 | Mappings *discovered* by regex-scanning document and book chunks for COMP/IV/AT case-ID patterns. Wider and noisier than `celex_case_id`. | [CELEX 2 Case ID Mapping](<CELEX 2 Case ID Mapping.md>) |

Note: `celex_id` values are stored **with** the `CELEX-` prefix (e.g. `CELEX-31990D0645`) in the chunk and embedding tables.

## 8. Companies — decision-derived

Company names as they appear in, or are normalised from, the decisions themselves.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `company_investigated2celex_id` | table | 6,383 | The addressees actually investigated per decision, with address, ISO country and jurisdiction code plus resolved foreign keys to LEI, OpenCorporates and Handelsregister. The join hub of the company model. | [Data Model Companies](<Data Model Companies.md>) |
| `company_canonical2celex_id` | table | 28,270 | Every canonical company name mentioned in a decision (superset of the investigated set). | [Data Model Companies](<Data Model Companies.md>) |
| `company_canonical2alias` | table | 12,393 | Canonical name to alias/spelling variants, self-referencing via `fk_company_canonical`. Feeds fuzzy matching against external registers. | [Data Model Companies](<Data Model Companies.md>) |
| `company_standard2celex_id` | table | 356 | Standardised company names migrated from the legacy rule engine. | [Data Ingestion](<Data Ingestion.md>) |
| `company_standard2celex_id2alias` | table | 356 | The alias variant of the same legacy migration. | [Data Ingestion](<Data Ingestion.md>) |
| `company_investigated2lei2corpidx2hr_vw` | view | 6,109 | Distinct company-to-external-register resolution rows, stripped of the per-decision duplication. | [Data Model Companies](<Data Model Companies.md>) |
| `company_registry_master_vw` | view | 9,670 | Registry master attributes per (roster company, source) -- tall over GLEIF, Handelsregister, OpenCorporates and the LLM index, with a `source_rank` trust ladder (1 = LEI, 4 = LLM). The resolved counterpart to the marker view above. | [Data Model Companies](<Data Model Companies.md>) |

## 9. Companies — external registers

Bulk reference data pulled from GLEIF, OpenCorporates and the German Handelsregister. These dominate database size (~8 GB).

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `corp_lei` | table | 3,285,782 | Full GLEIF LEI register: legal name, category, jurisdiction, status, legal and headquarters addresses, raw `record_json`. 5.9 GB — the largest relation. | [EU Inc Register](<EU Inc Register.md>), [Corp Index & Similarity search](<Corp Index & Similarity search.md>) |
| `handelsregister_companies` | table | 5,461,351 | German commercial-register companies: register number, name, status, jurisdiction, registered address, registrar. | [Handelsregister Index Loader](<Handelsregister Index Loader.md>) |
| `handelsregister_officers` | table | 3,496,627 | Officers per Handelsregister company — role, name parts, city, term dates, dismissal flag. | [Handelsregister Index Loader](<Handelsregister Index Loader.md>) |
| `handelsregister_previous_names` | table | 1,345,716 | Historical company names, essential for matching decision-era names to today's register entries. | [Handelsregister Index Loader](<Handelsregister Index Loader.md>) |
| `handelsregister_company_additional_data` | table | 0 | Placeholder for the Handelsregister document-type flags (AD, CD, DK, HD, SI, UT, VOE). Not yet populated. | [Handelsregister Index Loader](<Handelsregister Index Loader.md>) |
| `gleif_jurisdication2countrycode` | table | 324 | GLEIF jurisdiction code to ISO country and subdivision code, with a source version stamp. Normalises jurisdictions across all three registers. | [Data Model Country Code and Jurisdication](<Data Model Country Code and Jurisdication.md>) |

## 10. Company resolution and matching

Candidate generation and scored matches between decision names and register entries.

| Relation | Type | Rows | Purpose | Detail doc |
| --- | --- | --- | --- | --- |
| `company_investigated_celex_id2opencorp_index_company` | table | 83,423 | Scored candidate matches from investigated companies to OpenCorporates entries, recording the resolution rung, query and jurisdiction that produced each candidate. | [Design Corp Index](<Design Corp Index.md>) |
| `opencorp_index_companies` | table | 99,082 | OpenCorporates search results: name, company number, jurisdiction, incorporation/dissolution dates, status, previous names, address and industry codes. | [OpenCorp API](<OpenCorp API.md>) |
| `opencorp_index_company_detail` | table | 1 | Full per-company OpenCorporates detail payload (officers, filings, financials, beneficial owners). Only fetched on demand — API-budget limited. | [OpenCorp API](<OpenCorp API.md>) |
| `opencorp_index_query` | table | 12,994 | Query log and cache key for OpenCorporates lookups: normalised query, pagination state, result count and error classification. Prevents re-spending API quota. | [Incident OpenCorporates Query Sanitization](<Incident OpenCorporates Query Sanitization.md>) |
| `opencorp_api_daily_calls` | table | 5 | Daily API call counter enforcing the OpenCorporates rate/quota budget. | [OpenCorp API](<OpenCorp API.md>) |
| `lei2company_canonical_matches_mv` | matview | 1,075 | Materialised trigram nearest-neighbour match (distance < 0.3) from canonical company names to `corp_lei.legal_name`. Materialised because the cross join against 3.3 M LEI rows is expensive. | [Corp Index & Similarity search](<Corp Index & Similarity search.md>) |
| `lei2company_canonical2alias_matches_tbl` | table | 2,233 | Frozen snapshot of the alias-level LEI trigram matches with their distances. | [Corp Index & Similarity search](<Corp Index & Similarity search.md>) |
| `llm_index_companies` | table | 5,688 | LLM-enriched company profile: canonical name, abbreviation, legal form, life-cycle status, products, geo market, headquarters and all known register identifiers (LEI, EUID, BRIS, Handelsregister, SIREN), stamped with the producing model id. | [Design Corp Index](<Design Corp Index.md>), [Deepseek LLM choice for LLM corp index](<Deepseek LLM choice for LLM corp index.md>) |

---

## Cross-cutting notes

- **Identifier types** — surrogate primary keys we own are `uuid`; natural keys (`celex_id`, `lei`, `company_number`) and LangChain-owned chunk ids are `varchar`. A foreign key always carries the type of the primary key it references.
- **Audit pattern** — `fact_ingestion_audit_trail` and `knowledge_ingest_audit_trail` share the same shape (`pipeline`, `dspy_module`, `predecessors`, `success`, `errors`, `duration`), so pipeline observability queries can union them.
- **Empty by design vs. by neglect** — `rule_facts.facts` and `langchain_pg_embedding` are drained legacy shells; `handelsregister_company_additional_data` is an unfilled placeholder; `opencorp_index_company_detail` holds a single row purely because detail fetches are API-budget limited.
- **Comment coverage** — all 53 relations carry a `COMMENT ON`; column comments are deliberately selective (108 of 461 columns), covering only those a newcomer would misread. Read one with `SELECT obj_description('celex_facts.facts'::regclass, 'pg_class');` — note this returns `NULL` for an uncommented object and only raises for a missing one.
- **Size profile** — the three external-register tables plus `corp_lei` account for roughly 8 GB of the database; all fact and knowledge data together stays under 200 MB.
