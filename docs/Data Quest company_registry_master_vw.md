#datamodel #companies

Consumers such as the entity graph could benefit from a view of the various company holding details (LEI, Handelsregister, `opencorp_index_companies`, `opencorp_index_companies_details`). Identify the common denominator of attributes and create the view.

-->
# `celex_facts.company_registry_master_vw`

Built 2026-08-30. Definition lives in exactly one file:
[migrate_registry_master_view.sql](../src/pipelines/rag/pgvector/migrate_registry_master_view.sql).
Lineage of the underlying tables: [Data Model Companies](<Data Model Companies.md>).
Catalogue entry: [Data Table Catalogue](<Data Table Catalogue.md>).

## Two corrections to the question

- **`opencorp_index_companies_details` does not exist.** The table is
  `celex_facts.opencorp_index_company_detail` -- singular on both words -- and it
  holds **1 row** (API budget, not a broken pipeline). It is therefore a `COALESCE`
  overlay on the OpenCorporates branch, flagged by `has_detail`, not a fifth source.
- **A fourth source was added that the question did not list.**
  `llm_index_companies` reaches 6,371 of 6,383 roster rows by exact name, against
  829 for LEI and 295 for Handelsregister. Without it the view is empty for ~87% of
  the roster. It is included at the lowest trust rank and must never supply an
  identity key.

## What the common denominator turned out to be

Twenty-three columns. The honest answer is that only **six** attributes are truly
common to all four sources -- name, identifier, identifier scheme, country, address
and an observation timestamp. Everything else is present on two or three sources and
NULL on the rest, which the tall shape makes free.

| Column | lei | handels | opencorp | llm |
| --- | --- | --- | --- | --- |
| `registry_name` | `legal_name` | `name` | `name` | `canonical_name` |
| `registry_id` | `lei` | `company_number` | `company_number` | first non-`NA` of lei/euid/bris/hr/siren |
| `registry_id_scheme` | `LEI` | `handelsregister` | `opencorporates` | whichever answered |
| `iso_country_code` | via GLEIF, else `country` | juris prefix | via GLEIF, else prefix | free text resolved by label |
| `registered_address` | legal address | registered address | registered office | **headquarters** |
| `source_observed_at` | `created_at` | `retrieved_at` | `fetched_at`/`created_at` | `updated_at` |
| `jurisdiction_code` | `legal_jurisdiction` | normalised | normalised | best effort |
| `status_text` | `entity_status` | `current_status` | `current_status` | `life_cycle_status` |
| `is_active` | ACTIVE/INACTIVE | **NULL** | `NOT inactive` | **NULL** |
| `legal_form` | **NULL** | **NULL** | `company_type` | free text |
| `incorporation_date` / `dissolution_date` | **NULL** | **NULL** | yes | **NULL** |
| `registry_url` | constructed | **NULL** | yes | **NULL** |

The **NULL**s are deliberate, not gaps:

- `legal_form` for LEI -- `entity_category` is a GLEIF category (GENERAL/BRANCH/FUND),
  not a legal form. For Handelsregister -- `register_art` (HRA/HRB) is a register
  *section*.
- `is_active` for Handelsregister -- its `current_status` vocabulary has never been
  measured anywhere in this repo, and a wrong `false` would mark live German companies
  dissolved in every downstream graph. For llm -- a free-text sentence cannot be
  reduced to a boolean without guessing.
- `incorporation_date` for LEI -- GLEIF's dates are LEI-*registration* dates. Putting
  them there would be a semantic lie. For llm -- `established_since` is free text
  ("circa 1950") that must not be cast.

Excluded entirely: previous/alternative names (three incompatible shapes -- jsonb on
OpenCorporates, a separate 1.3M-row table for Handelsregister, `abbreviation` /
`succession` on llm; needs an aggregate, not a projection) and ownership structure
(`corporate_groupings`, `ultimate_beneficial_owners` -- out of scope, and only 1 row
of source data exists).

## Shape and grain

**One row per `(roster row, source)`.** Tall, `UNION ALL`, with a `source`
discriminator. Anchored on `company_investigated2celex_id.id`, the only key common to
all four sources -- so this is roster-anchored, not a global union over the 8.8M
register records.

Grain is per **roster row**, not per company name. A company appearing in five
decisions yields five `company_key`s per source. Per-name is a projection you get with
`DISTINCT ON (company_name)`; per-row is not recoverable from per-name.

| Source | Rank | Joined by | Rows |
| --- | --- | --- | --- |
| `lei` | 1 | `roster.fk_lei` -> `corp_lei.lei`, primary key | 829 |
| `handelsregister` | 2 | `roster.fk_handelsregister` -> `company_number`, primary key | 295 |
| `opencorporates` | 3 | best scored candidate from the N:M pool, `LATERAL LIMIT 1` | 2,175 |
| `llm` | 4 | `llm_index_companies.company_name`, exact | 6,371 |
| | | **total** | **9,670** |

## How to read it

```sql
SELECT DISTINCT ON (company_key) *
FROM celex_facts.company_registry_master_vw
ORDER BY company_key, source_rank, link_confidence DESC NULLS LAST;
```

`source_rank` encodes **issued identifier > primary register > aggregator with a fuzzy
link > model assertion**:

1. `lei` -- a legally issued global identifier under annual re-validation, joined on
   the primary key. Already the graph's dedup key (`entities.lei_id`).
2. `handelsregister` -- a primary national register, but one jurisdiction, and the
   roster link was made by folded-*name* match, not by an issued identifier.
3. `opencorporates` -- an aggregator, not a register, and the link is a scored fuzzy
   pick from an average of 20.4 candidates at rungs as loose as `core@any` (avg 0.839).
4. `llm` -- a model assertion no registry confirmed. Use for `attrs_json`-grade
   enrichment only; **never** as `lei_id` or `natural_key`.

It is a per-source constant, not a blended score. `link_method`, `link_confidence` and
`source_observed_at` are exposed so a consumer can re-rank without editing the view.

## Coverage -- the headline finding

Only **3,008 of 6,383** roster rows (47%) reach a registry-grade row at rank <= 3. The
other **3,364** have nothing but an unconfirmed model assertion.

```sql
WITH best AS (
    SELECT DISTINCT ON (company_key) *
    FROM celex_facts.company_registry_master_vw
    ORDER BY company_key, source_rank, link_confidence DESC NULLS LAST)
SELECT source, count(*) FROM best GROUP BY 1 ORDER BY 2 DESC;
```

| source | rows |
| --- | --- |
| `llm` | 3,364 |
| `opencorporates` | 1,919 |
| `lei` | 829 |
| `handelsregister` | 260 |

The view **surfaces** what was already resolved; it resolves nothing new. Making that
shortfall countable for the first time is its real contribution. It explains the entity
graph's 1,972-of-6,531 LEI-keyed node rate directly.

## The sources disagree about which company they matched -- 45% of the time

Of the 2,399 roster rows carrying at least two sources with a country, **1,074 (44.8%)
report different `iso_country_code`s**. They matched different legal entities, not
different facts about one. It tracks name length exactly as substring/fuzzy linking
predicts: **60.0%** for names under 12 characters, 39.8% for longer ones.

Worked example -- the roster name `Phoenix`:

| source | registry_name | registry_id | country |
| --- | --- | --- | --- |
| lei | PHOENIX | `2138005AUDGMYGIO2E06` | GB |
| handelsregister | Phoenix | `de-oc-aachen-vr-3406` | DE |
| opencorporates | " Phoenix Verlagsgesellschaft m.b.H. " | `R2507_R2508_HRB1108` | DE |
| llm | Phoenix Zementwerke Krogbeumker & Co. KG | -- | DE |

Four different companies, and the `source_rank` ladder hands a consumer the GB trustee
entity. **`source_rank` is a preference order, not a merge rule.** This is tracked as a
`#high` item in [ISSUES](../ISSUES.md) and directly qualifies the `resolve_company`
follow-up below.

## Traps the view absorbs

1. `roster.fk_lei` and `roster.fk_handelsregister` hold the literal `'NA'` for "looked
   up, no match" -- 5,554 of 6,383 rows on `fk_lei` alone.
2. `roster.fk_opencorp_idx` is **never an id**. It only ever holds `'LINKED'` (4,098) /
   `'NA'` (1,783) / `'AMBIGUOUS'` (498) / `''` (4). The identity lives in the N:M table.
3. `llm_index_companies` writes `'NA'` rather than NULL on every identifier column.
4. **The candidate explosion.** `company_investigated_celex_id2opencorp_index_company`
   is a candidate *pool*: 83,423 rows over 4,097 roster rows, 20.4 each on average and
   2,436 at worst. Only 5,578 were ever adjudicated, and `match_score IS NOT NULL` is
   exactly equivalent to `resolution_rung IS NOT NULL`. The branch restricts to scored
   rows and takes one via `LATERAL ... LIMIT 1`; without both, 6,383 anchors become
   ~83,000 rows.

### `'NA'` is also Namibia

The sentinel collides with the real ISO 3166-1 alpha-2 code. `corp_lei` holds **107
genuinely Namibian rows**, `gleif_jurisdication2countrycode` has a legitimate
`NA -> Namibia` row, and the roster carries `Namib Air (Pty) Ltd`. The view therefore
strips `'NA'` from identifier, name, address and status columns but **never** from
`iso_country_code` or `jurisdiction_code`. Do not add sentinel-stripping to geo columns
anywhere. Tracked in [ISSUES](../ISSUES.md).

## The OpenCorporates pick

Ordered `match_score DESC`, then rung precedence, then the candidate uuid. The rung
ladder is `verbatim`, `verbatim@any`, `split`, `no-paren`, `no-role`, `core`,
`core@country`, `core@any` -- from
[opencorp_resolution_ladder.py](../src/pipelines/corp_index/opencorp_resolution_ladder.py).
`verbatim@any` sits second deliberately: it is the only rung that reaches an offshore
registration.

The uuid tiebreak is not decoration -- without it the pick is non-deterministic across
plans, and a view whose answer changes between runs is unusable as an identity source.
`array_position` returns NULL for an unknown rung, which sorts last, so a new rung
degrades to least-preferred instead of crashing.

## Performance

Every join is a primary/unique key probe. The two large tables are never scanned:
`corp_lei` (3.3M) gets 829 PK probes, `handelsregister_companies` (5.4M) gets 295. The
view never touches `ix_corp_lei_legal_name_gin_trgm` or `ix_hr_matchkey` -- the fuzzy
matching happened upstream, and re-running it here is the failure mode to avoid.

| Query | Time |
| --- | --- |
| Single company (`WHERE company_name = ...`) | **1.3 ms** |
| Full 9,670-row scan | **84 ms** |

Two structural choices that are performance decisions, not style:

- **The roster is repeated in each branch, not lifted into a CTE.** A CTE referenced
  four times is materialised, which blocks qualifier pushdown through the `UNION ALL`.
  `EXPLAIN` confirms `WHERE company_name = 'Pirelli S.p.A.'` pushes into all four
  branches as an index condition.
- **The OpenCorporates pick is `LATERAL ... LIMIT 1`, not a `DISTINCT ON` CTE.**
  Identical results, but the CTE form materialises a pre-aggregate over the whole link
  table on every call, including single-company calls.

One new partial index, created by the migration:

```sql
CREATE INDEX IF NOT EXISTS ix_ci2oc_scored_candidates
    ON celex_facts.company_investigated_celex_id2opencorp_index_company
       (fk_company_investigated, match_score DESC)
    WHERE match_score IS NOT NULL;
```

5,578 of 83,423 rows qualify, so it is a few hundred KB. It keeps a whole-view scan
from reading the ~77,800 unranked candidate rows at all.

## Operational notes

**Always follow the migration with `schema_comments.sql`.** `DROP VIEW` discards every
`COMMENT ON` the view carried, so replaying the migration alone leaves the view
undocumented and fails `test_schema_comments::test_every_relation_has_a_comment`.

```bash
psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/migrate_registry_master_view.sql
psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/schema_comments.sql
PYTHONPATH=src .venv/bin/python -m unittest \
    pipelines.rag.pgvector.tests.test_company_registry_master_vw \
    pipelines.rag.pgvector.tests.test_schema_comments
```

**File naming is load-bearing.** DDL is applied by an alphabetical glob replay with no
migration runner. The view needs `resolution_rung` / `match_score` /
`registered_address_in_full` from `migrate_opencorp_relaxation_retry.sql`, so
`migrate_company_...` would sort *before* its own dependencies. `migrate_registry_...`
sorts after. The `DO` block at the top turns a violated order into a readable error
instead of an `UndefinedColumn` pointing at a random line.

**Do not copy the definition anywhere.** `company_investigated2lei2corpidx2hr_vw`
already exists in three competing copies and has been broken once by replaying a stale
one. A pointer comment guarding this sits in `pg_celex-ddl.sql`.

## What it does not solve

1. **Coverage** -- it surfaces, it does not resolve. See the 47% above.
2. **Cross-decision dedup** -- grain is per roster row.
3. **Reconciliation** -- it places sources side by side; it does not vote or merge. See
   the 45% disagreement rate.
4. **Ownership / parent structure** -- out of scope.
5. **Aliases and previous names** -- so it cannot by itself fix alias-variant node
   splitting in the graph. Needs a follow-up `company_registry_names_vw`.
6. **`fk_opencorp_idx` stays a marker** -- the view routes around it, it does not repair it.
7. **Off-roster mentions** -- the graph resolves arbitrary extracted strings (third
   parties, `fines` / `leniency_*` endpoints) that never became a roster row.

## Wiring the entity graph -- done 2026-09-09

[entity_graph_repository.py](../src/pipelines/rag/pgvector/entity_graph_repository.py)
`resolve_company` used to ignore the roster's already-resolved `fk_lei` and re-derive
an LEI with a pg_trgm scan over 3.3M rows, once per mention. It is now a four-tier
ladder: `company_canonical2alias`, then a name-length floor
(`settings.ENTITY_LEI_MIN_NAME_LENGTH`, default 12), then this view at
`source = 'lei'`, then the trigram probe as the off-roster fallback. Plan:
[entity-graph-identity-via-registry-master-view-20260909](claude/plans/entity-graph-identity-via-registry-master-view-20260909.md).

```sql
SELECT registry_id FROM celex_facts.company_registry_master_vw
WHERE source = 'lei' AND company_name = :name ORDER BY registry_id LIMIT 1
```

`EXPLAIN` confirms the constant `source` qualifier prunes the other three `UNION ALL`
branches at plan time (`One-Time Filter: false`), leaving two index scans: **0.45 ms**
against **825 ms** for the trigram probe it replaces. No `company_name` on the `lei`
branch carries two distinct `registry_id` values (measured, and now pinned by
`test_lei_branch_never_maps_one_name_to_two_leis`), so the name-keyed `_company_cache`
stays correct and the `ORDER BY` is a reproducibility guarantee rather than an active
tiebreak.

**Three things the implementation learned that this document's proposal did not
anticipate.**

1. **The length floor turned out to be the load-bearing part**, not the view. Prepending
   the view alone swaps one set of false merges for another: over the 419 company nodes
   whose name carries an `fk_lei`, view and trigram agree on 137 of 196 names at 12+
   characters (70%) but only 63 of 223 below it (28%). The 45% cross-source disagreement
   measured above is overwhelmingly a *short-name* phenomenon, and both sides are wrong
   there -- the roster gives `Gant -> GANTE`, the trigram gives `AT&T -> AT`.
2. **The NFKC precondition was real but small** -- 26 of 5,668 roster hits. Folded in
   `_normalize_name` and on the probe name.
3. **The alias tier and `fk_lei` are keyed on different spellings.** Because
   `company_canonical2alias` must run first, the view lookup that follows misses:
   registry-tier hits fall from 682 to **249** of 7,015 mentions. Probing both spellings
   recovers 66 -- and of the 13 mentions where both carry an LEI, the two LEIs are
   different *companies* (`Nike, Inc.` vs `Nike European Operations Netherlands B.V.`;
   `Credit Suisse Securities (Europe) Limited` vs `Credit Suisse AG`). That repair was
   therefore rejected. Tracked as `#high` in [ISSUES](../ISSUES.md); the fix belongs
   upstream, in how `fk_lei` was keyed.

**The view is not the performance fix it was hoped to be.** It answers 3.5% of mentions,
because it is roster-anchored and point 3 costs most of the rest. Full-run probe time
falls ~96.5 -> ~68.5 minutes, almost all of it from the floor rather than the view.

## Verification queries

```sql
-- 1. Per-source counts. rows must equal keys on every line.
SELECT source, source_rank, count(*) AS rows, count(DISTINCT company_key) AS keys
FROM celex_facts.company_registry_master_vw GROUP BY 1, 2 ORDER BY 2;

-- 2. No fan-out. Must return zero rows -- the load-bearing invariant.
SELECT company_key, source, count(*) FROM celex_facts.company_registry_master_vw
GROUP BY 1, 2 HAVING count(*) > 1;

-- 3. Sentinel leaks and shape. Every count must be 0.
--    NOTE country/jurisdiction are excluded on purpose -- 'NA' is Namibia.
SELECT
    count(*) FILTER (WHERE registry_id = 'NA')          AS id_na,
    count(*) FILTER (WHERE registry_name = 'NA')        AS name_na,
    count(*) FILTER (WHERE registered_address = 'NA')   AS addr_na,
    count(*) FILTER (WHERE status_text = 'NA')          AS status_na,
    count(*) FILTER (WHERE iso_country_code IS NOT NULL
                       AND iso_country_code !~ '^[A-Z]{2}$') AS bad_country,
    count(*) FILTER (WHERE source = 'lei'
                       AND length(registry_id) <> 20)   AS bad_lei,
    count(*) FILTER (WHERE source = 'opencorporates'
                       AND link_confidence IS NULL)     AS oc_unscored
FROM celex_facts.company_registry_master_vw;

-- 4. Cross-source country disagreement, per the finding above.
WITH multi AS (
    SELECT company_key, count(*) AS n_sources,
           count(DISTINCT iso_country_code) AS n_countries
    FROM celex_facts.company_registry_master_vw
    WHERE iso_country_code IS NOT NULL GROUP BY 1)
SELECT count(*) FILTER (WHERE n_sources > 1) AS multi_source,
       count(*) FILTER (WHERE n_sources > 1 AND n_countries > 1) AS disagree
FROM multi;

-- 5. The regression this project has been bitten by twice.
--    Must return exactly 7 rows, jurisdication_code among them.
SELECT ordinal_position, column_name FROM information_schema.columns
WHERE table_schema = 'celex_facts'
  AND table_name = 'company_investigated2lei2corpidx2hr_vw' ORDER BY 1;
```

Structural invariants are guarded by nine tests in
[test_company_registry_master_vw.py](../src/pipelines/rag/pgvector/tests/test_company_registry_master_vw.py).
Exact row counts are deliberately **not** asserted there -- they are data facts that
drift as the OpenCorporates and LLM batches run, not invariants.
