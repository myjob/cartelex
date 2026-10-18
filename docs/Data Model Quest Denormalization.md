#datamodel


I have extracted facts such as infringements per legal case into the celex_facts.facts table. but the facts are denormalized and various values are lists such as "fines" which contains companies and the fine amount. this works fine for user question & answer but fails if other references are required such as company->country->LEI code what is industry best practice in representing extracted facts? how to sort my company quest above?
-->
see: [ plan company-country-lei-entity-graph.md](claude/plans/company-country-lei-entity-graph.md)
# Company -> Country -> LEI: Entity-Graph Completion, OpenCorporates Fallback, Graph Q&A

> After approval: copy this plan into `docs/claude/plans/` (CLAUDE.md rule).

## Context

Extracted facts live in the EAV table `celex_facts.facts` — one row per (`celex_id`, `name`), with list facts (e.g. `fines`) stored as a stringified JSON array in `value_json` (`[{"company": "Roto", "amount": "100%"}]`). This is fine for per-case Q&A but cannot answer cross-entity questions like company -> country -> LEI.

**Industry best practice** for this is exactly the two-layer design the repo already started: keep the denormalized EAV/JSON layer as the _extraction claim layer_ (raw, per-document, with provenance), and materialize it into a _normalized entity layer_ (typed nodes + relations, deduplicated against an external registry — GLEIF here). The Typed Entity Graph (`celex_facts.entities` / `entity_edges`, commit 2bc5752) plus the GLEIF `corp_lei` table (3.3M rows, has `country` + `legal_jurisdiction`) already implement 80% of this. The gaps:

1. `resolve_company` finds the LEI but **country is never propagated** to entity nodes (`attrs_json` is always written `None` — in fact the upsert _clobbers_ it on every conflict).
2. The `companies_country` extracted fact is parsed by nothing (in `EXCLUDED_FAMILIES`).
3. The OpenCorporates client (`src/pipelines/corp_index/`, commit 2d93fca) is built but unwired.
4. The Q&A text-to-SQL path (`retrieval_dispatch.py`) only knows `celex_facts.facts` — it never sees the graph.

User chose the full scope: graph completion + Q&A wiring + OpenCorporates fallback.

## Design decisions

- **D1 — country is a real column** on `entities` (`country TEXT` ISO 3166-1 alpha-2 + `country_source TEXT`), not buried in `attrs_json`. Real columns are what LLM-generated SQL handles reliably. `attrs_json` (converted TEXT -> JSONB, first real writer arrives now) holds OpenCorporates identifiers.
- **D2 — provenance precedence:** `gleif` (from `corp_lei.country`, fallback prefix of `legal_jurisdiction`) > `opencorporates` (`jurisdiction_code` prefix) > `fact` (extracted `companies_country`, normalized via a static ~40-entry name->ISO map; no pycountry dependency). Upsert never downgrades: lower-rank source and NULL never overwrite higher-rank/non-NULL.
- **D3 — OpenCorporates as stage 2 of `resolve_company`**, only when GLEIF fuzzy match < `ENTITY_LEI_MATCH_THRESHOLD`, gated by new setting `ENTITY_OPENCORP_FALLBACK_ENABLED` (default **off** — no network in batch/CI unless opted in). Cache-first via existing `OpenCorpIndexRepository`. Accepted match: `natural_key = 'oc:<jurisdiction>/<company_number>'`, country from jurisdiction, OC identifiers into `attrs_json`. Any API error logs once, self-disables the fallback for the run, degrades to `name:` key — the batch never fails because of OC.
- **D4 — Q&A via one flat view** `celex_facts.company_case_relations_vw` (edges x both entity endpoints x `corp_lei`), documented in a compact appended section of `KB_SCHEMA_DESCRIPTION`. Do not teach the LLM the raw graph tables. No dispatcher changes: `_validate_read_only_sql` already admits view SELECTs.
- **D5 — one idempotent migration file** in the existing hand-written style; backfill = re-run `Main_materialize_entity_graph.py`; live-DB steps go on ISSUES.md as a run book (agent cannot mutate the live DB).

## Workstream 1 — Migration + ORM

**New file** `src/pipelines/rag/pgvector/migrate_entity_country.sql` (convention: one `migrate_*.sql` per change; safe to re-run):

- `ALTER TABLE celex_facts.entities ADD COLUMN IF NOT EXISTS country TEXT;` + `country_source TEXT`.
- CHECK constraint `country_source IN ('gleif','opencorporates','fact')` guarded by `pg_constraint` lookup.
- `CREATE INDEX IF NOT EXISTS ix_entities_country ON celex_facts.entities (country) WHERE country IS NOT NULL;`
- `attrs_json` TEXT -> JSONB, guarded by `information_schema.columns` data_type check.
- `DROP VIEW IF EXISTS` + `CREATE VIEW celex_facts.company_case_relations_vw` — columns: `company_name, lei_id, companies_country (COALESCE(entities.country, corp_lei.country)), country_source, legal_jurisdiction, oc_company_number (attrs_json->>'oc_company_number'), relation, celex_id, case_name, amount ((value_json)::jsonb->>'amount'), source_fact_id`. Joins: `entity_edges` x `entities comp (kind='company', src)` x `entities cs (kind='case', dst)` LEFT JOIN `corp_lei`. The kind conditions exclude `violated` edges automatically.

**Mirror** the changes into `src/pipelines/rag/pgvector/pg_celex-ddl.sql` (established convention).

**`src/pipelines/rag/pgvector/models.py`** — `Entity` (~line 805): add `country`, `country_source` columns; `attrs_json` Text -> JSONB (already imported); add the partial index to `__table_args__`.

**`src/pipelines/graph/entity_graph_models.py`** — add `CountrySource(StrEnum)` {GLEIF, OPENCORPORATES, FACT} and `COUNTRY_SOURCE_RANK = {"gleif": 1, "opencorporates": 2, "fact": 3}`.

**`src/pipelines/rag/pgvector/entity_graph_repository.py` `upsert_entity`** (line 223): signature `attrs_json: str | None` -> `attrs: dict | None` (JSONB pitfall: pass a dict, never a pre-serialized string — double-encoding), add `country`, `country_source` params. Replace the blind `set_` with precedence-aware SQL `case()` expressions:

- `take_new = excluded.country IS NOT NULL AND (Entity.country IS NULL OR rank(excluded.country_source) <= rank(Entity.country_source))` (rank via `sqlalchemy.case` over `COUNTRY_SOURCE_RANK`, else 99);
- `country`/`country_source` = `case((take_new, excluded.x), else_=Entity.x)`;
- `lei_id` and `attrs_json` = `func.coalesce(excluded.x, Entity.x)` — fixes the existing clobber where re-materialization wipes previously written values.

## Workstream 2 — `resolve_company` OpenCorporates fallback

**`src/config/settings.py`** (next to `ENTITY_LEI_MATCH_THRESHOLD`, line 157): `ENTITY_OPENCORP_FALLBACK_ENABLED` (bool, default false) and `ENTITY_OPENCORP_MATCH_THRESHOLD` (float, default 0.85 — difflib ratio over normalized names).

**`entity_graph_repository.py`:**

- `CompanyResolution` (line 40): add slots `country`, `country_source`, `attrs` (keyword args, default None). Stays a slotted class.
- `__init__`: inject `opencorp_client: OpenCorpClient | None = None` (Provisioner pattern); build one when None and the flag is on. Track `self._opencorp_disabled = False`.
- `resolve_company` (line 157): extend the GLEIF query to also select `country, legal_jurisdiction` (keep the `legal_name % :name` / `<->` shape untouched — trgm index dependency). Flow:
    1. GLEIF hit >= threshold -> as today + `country = row.country or legal_jurisdiction prefix before '-'` (uppercase, 2-alpha only), `country_source='gleif'`.
    2. Else if fallback enabled and not self-disabled -> `_resolve_via_opencorporates(canonical_name)`: `search_companies(name, fetch_all_pages=False)` (cache-first inside client; 429 retry already in `OpenCorpClient._get`); score hits with `difflib.SequenceMatcher` over `_normalize_name`, prefer non-inactive on ties; accept best if >= `ENTITY_OPENCORP_MATCH_THRESHOLD` -> `natural_key='oc:<jurisdiction_code>/<company_number>'`, `lei_id=None`, `country=jurisdiction_code prefix before '_' upper`, `country_source='opencorporates'`, `attrs={oc_jurisdiction_code, oc_company_number, oc_name, oc_status}`. On `OpenCorpAuthError | OpenCorpApiError | requests.RequestException`: warn once, `self._opencorp_disabled = True`, fall through.
    3. Else name-based key as today, country None (materializer may fill from the fact).
- `_company_cache` keeps memoizing the full resolution.

## Workstream 3 — Materializer country enrichment

**`src/pipelines/graph/entity_graph_mapping.py`** (pure, DB-free):

- `iso_country_code(raw: str) -> str | None`: 2-letter alpha passthrough (upper); module-level `_COUNTRY_NAME_TO_ISO` dict (EU/EEA + UK/CH/US/JP + common variants: "Federal Republic of Germany", "The Netherlands", ...); else None.
- `parse_company_country(value_json: str | None) -> dict[str, str]`: reuse `_iter_elements`/empty-marker handling; `{normalized_company: iso_code}`; drop unmappable rows.
- Keep `companies_country` in `EXCLUDED_FAMILIES` (still not an edge source); update the comment: it is now a node-attribute source.

**`src/batch/Main_materialize_entity_graph.py`:**

- `_materialize_celex` (line 174): build the fact-country map once per document and thread it into `_materialize_company_edges`.
- `_materialize_company_edges` (line 67): after `resolve_company`, if `resolution.country is None` try the fact map (keyed by normalized mention, then normalized canonical name) with `country_source=CountrySource.FACT.value`; pass `country`, `country_source`, `attrs=resolution.attrs` to `upsert_entity`.
- New stats counters: `country_gleif`, `country_opencorp`, `country_fact`, `country_missing`, `opencorp_fallback_accepted`.
- Docstring note: OC-rekeying can orphan old `name:` entities; cleanup SQL lives in the run book (no auto-delete).

## Workstream 4 — Q&A wiring

**`src/pipelines/agent_dspy/retrieval_dispatch.py`** — append a compact section to `KB_SCHEMA_DESCRIPTION` (reaches both the planner and master planner): view name, one-line purpose ("USE THIS VIEW, not facts, when a question joins a company attribute — country, LEI — with a case outcome"), column list with types and caveats (`companies_country` ISO alpha-2 nullable; `amount` is text as-extracted, never cast), the five relation labels, and two example queries ("fines for German companies", "countries by cartel participation"). Instruct: translate nationality adjectives to ISO codes ("German" -> 'DE').

## Workstream 5 — Tests (unittest, mocks at DB/API boundaries)

1. Extend `src/pipelines/graph/tests/test_entity_graph_mapping.py`: `parse_company_country` (canonical shape, "NA"/"[]", missing key, unmappable dropped); `iso_country_code` (names, passthrough, casefold, unknown->None); EXCLUDED_FAMILIES assertion still holds.
2. Extend `src/pipelines/rag/pgvector/tests/test_entity_graph_repository.py`: GLEIF row now carries country ('DE' direct; `legal_jurisdiction='US-DE'` -> 'US'); OC fallback fires only below threshold + flag on; accept/reject around 0.85; self-disable after API error (client not touched on second call); integration section (self-skipping, existing pattern): precedence upsert (fact->gleif upgrades, gleif->fact does not; NULL never clobbers; attrs coalesce), view row appears for a fined edge.
3. New `src/batch/tests/test_Main_materialize_entity_graph.py` (mocked repo): resolution country wins over fact map; fact map fills unresolved (via mention and via canonical); counters; `upsert_entity` receives dict attrs.
4. Extend `src/pipelines/agent_dspy/tests/test_retrieval_modules.py`: `KB_SCHEMA_DESCRIPTION` mentions the view, `companies_country`, relation labels; `_validate_read_only_sql` admits a view query.

Run: targeted `.venv/bin/python -m unittest ...` with `PYTHONPATH=src`, then `ruff check`.

## Workstream 6 — ISSUES.md run book (live DB — user runs these)

Prepend `- [ ] #high` entry with, in order:

1. Apply `migrate_entity_country.sql` via SQL client (idempotent; requires `migrate_entity_graph.sql` already applied).
2. Ensure OC cache tables exist (`OpenCorpIndexRepository().ensure_tables()`) and `opencorporates_api_token` is set.
3. `PYTHONPATH=src ENTITY_OPENCORP_FALLBACK_ENABLED=true .venv/bin/python src/batch/Main_materialize_entity_graph.py --dry-run` — inspect the new counters; then the real run.
4. Verify: `SELECT companies_country, count(*) FROM celex_facts.company_case_relations_vw WHERE relation='fined' GROUP BY 1 ORDER BY 2 DESC;` and ask the QA UI "Which German companies were fined?".
5. Optional orphan cleanup: delete `kind='company'` entities with no remaining edges.

Also annotate the existing "attrs_json is TEXT and never written" ISSUES.md item as resolved by this migration.

## Sequencing

WS1 -> WS2 -> WS3 (depends on WS1 signature + WS2 fields) -> WS4 (only needs the view name; can land with WS1) -> WS5 alongside each -> WS6 last.

## Risks

- JSONB conversion: `upsert_entity` must receive a dict, not a JSON string (double-encoding).
- Do not alter the GLEIF query's `legal_name % :name` / `ORDER BY <->` shape — the trgm gist index on 3.3M rows depends on it.
- `DROP VIEW IF EXISTS` + `CREATE VIEW` (not `OR REPLACE`) so future column changes stay idempotent.
- OC fallback default-off: no network in batch/CI unless opted in.