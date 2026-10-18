#datamodel #gleif 

https://www.gleif.org/en/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list

if you download the official **GLEIF Accepted Legal Jurisdictions Code List** (available via the [GLEIF Data Portal](https://www.gleif.org/en/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list) in Excel, CSV, or XML format), you will find that the `"Jurisdiction Code"` column strictly utilizes standard ISO 3166 formats: [[1](https://www.gleif.org/en/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list), [2](https://www.gleif.org/de/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list), [3](https://www.gleif.org/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list/gleif_acceptedjurisdictions_v1.5.pdf), [4](https://www.gleif.org/fr/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list), [5](https://www.bfs.admin.ch/bfs/en/home/registers/enterprise-register/lei-legal-entity-identifier/faq.html)]

- **For standard national jurisdictions**, the code _is_ the **ISO 3166-1 alpha-2** country code (e.g., `AD` for Andorra or `AF` for Afghanistan).
- **For sub-national legal jurisdictions** (like states or emirates), the code _is_ the **ISO 3166-2** subdivision code (e.g., `AE-DU` for Dubai, or `US-DE` for Delaware). [[1](https://www.gleif.org/en/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list), [2](https://www.gleif.org/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list/gleif_acceptedjurisdictions_v1.5.pdf), [3](https://www.gleif.org/it/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list), [4](https://www.gleif.org/fr/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list), [5](https://www.gleif.org/organizational-identity/get-an-lei-find-lei-issuing-organizations)]

How to extract the Parent Country ISO Code

Because GLEIF maps directly to standard ISO 3166 strings, you do not need a complex third-party open-source mapping file. You can extract the parent country ISO code directly from the GLEIF jurisdiction code using simple logic or open-source libraries

turn those extracted codes into full country names, currencies, or coordinates, you can pass the extracted alpha-2 string into heavily maintained open-source packages: [[1](https://dev.co/databases/open-source/countries), [2](https://www.gleif.org/fr/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list)]

- **Python**: `pycountry`

---

## Data source

| Property | Value |
| --- | --- |
| Publisher | GLEIF (Global Legal Entity Identifier Foundation) |
| List | GLEIF Accepted Legal Jurisdictions Code List, v1.5 |
| Landing page | <https://www.gleif.org/en/lei-data/code-lists/gleif-accepted-legal-jurisdictions-code-list> |
| Local file | `~/a/celex_docs/gleif_acceptedjurisdictions_v1.5.csv` |
| Encoding | UTF-8 **with BOM**; non-ASCII jurisdiction names (`‘Ajmān (United Arab Emirates)`) |
| Header | `Jurisdiction,Code,Type` |
| Rows | 324 data rows (246 country-level, 78 subdivision) |

The file has no surrogate key, no duplicate `Code`, no duplicate `Jurisdiction`, and no NULLs — verified on load.

### `Type` values

| `Type` | Rows | Meaning |
| --- | --- | --- |
| `COUNTRY_ONLY` | 235 | Only the country as a whole is an accepted legal jurisdiction. |
| `COUNTRY_AND_SUBDIVISION` | 86 | Country plus its accepted subdivisions. The 8 countries with subdivisions (`AE`, `BE`, `CA`, `GB`, `KN`, `MY`, `PT`, `US`) each contribute one country-level row plus their 78 subdivision rows. |
| `EXCEPTIONALLY_RESERVED` | 2 | ISO 3166 exceptionally reserved codes: `EU` (European Union), `UN` (United Nations). |
| `USER_ASSIGNED_CODE` | 1 | `XX` — "Unknown states". |

### Code shapes

Every code in v1.5 matches `^[A-Z]{2}(-[A-Z0-9]{1,3})?$` (verified across the whole file):

| Length | Rows | Example |
| --- | --- | --- |
| 2 | 246 | `AT` |
| 4 | 1 | `KN-N` (Nevis) |
| 5 | 74 | `AE-DU`, `US-DE`, `PT-20` |
| 6 | 3 | `BE-VLG`, `GB-NIR`, `GB-SCT` |

Subdivision parts are 1-3 characters and are **not always alphabetic** — `PT-20`, `PT-30`, `MY-15` are numeric. Any regex or `varchar` sizing must allow digits and 3 characters.

---

## Table: `celex_facts.gleif_jurisdication2countrycode`

> The table name keeps the spelling used when it was commissioned
> (`jurisdication`, not `jurisdiction`). It is referenced by that exact name in
> the loader and in every query below.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `jurisdiction_code` | `varchar(6)` | NO | **PK.** The GLEIF/ISO code verbatim, uppercased (`AT`, `AE-DU`). Natural key from an external register, so `varchar` per the project identifier convention. |
| `jurisdiction_name` | `text` | NO | GLEIF's label (`Dubai (United Arab Emirates)`). |
| `jurisdiction_type` | `varchar(32)` | NO | GLEIF `Type` verbatim (see table above). |
| `iso_country_code` | `varchar(2)` | NO | **Derived.** ISO 3166-1 alpha-2 parent country — the part of the code before the hyphen. This is the join key to `company_investigated2celex_id.iso_country_code`. |
| `iso_subdivision_code` | `varchar(3)` | YES | **Derived.** ISO 3166-2 part after the hyphen; NULL for a country-level row. |
| `is_subdivision` | `boolean` | NO | **Derived.** `iso_subdivision_code IS NOT NULL`. Stored so filters avoid a NULL test. |
| `source_version` | `varchar(16)` | NO | GLEIF code-list version the row came from (`1.5`), for provenance across list upgrades. |
| `loaded_at` | `timestamptz` | NO | `now()` on insert, refreshed on every upsert. |

Indexes:

- `gleif_jurisdication2countrycode_pkey` — PK on `jurisdiction_code`.
- `ix_gleif_juris_country_code` — on `iso_country_code`; serves "all jurisdictions of country X" and the roster join.
- `ix_gleif_juris_type` — on `jurisdiction_type`; serves "country-level rows only" style filters.

### Derivation rules

The parent country is taken straight from the code — no external mapping table, as noted above:

```
AT      -> iso_country_code = 'AT', iso_subdivision_code = NULL, is_subdivision = false
AE-DU   -> iso_country_code = 'AE', iso_subdivision_code = 'DU', is_subdivision = true
GB-SCT  -> iso_country_code = 'GB', iso_subdivision_code = 'SCT'
KN-N    -> iso_country_code = 'KN', iso_subdivision_code = 'N'
PT-20   -> iso_country_code = 'PT', iso_subdivision_code = '20'
```

The three non-country codes are stored with `iso_country_code` equal to the code itself, so the column is never NULL: `EU -> EU`, `UN -> UN`, `XX -> XX`. They stay distinguishable by `jurisdiction_type`, so a query wanting real countries only should filter
`jurisdiction_type IN ('COUNTRY_ONLY', 'COUNTRY_AND_SUBDIVISION')`.

Because the ISO parts are derived inside the validating VO rather than read from the file, they cannot disagree with `jurisdiction_code`; supplying them explicitly is ignored.

---

## Loader: `src/pipelines/corp_index/gleif_jurisdiction_loader.py`

A plain CSV -> DB reference-data load. No LLM or DSPy pipeline is involved, so the DSPy signature rules do not apply.

Structure, mirroring `opencorp_repository.py`:

- **`GleifJurisdiction`** (`pydantic.BaseModel`, frozen) — the validation boundary for the CSV. A `model_validator(mode="before")` uppercases/trims `jurisdiction_code`, splits it on the hyphen and fills `iso_country_code` / `iso_subdivision_code`; the code is pattern-checked against `^[A-Z]{2}(-[A-Z0-9]{1,3})?$`, so a malformed row fails the load instead of silently landing a bad key. `is_subdivision` is a property.
- **`GleifJurisdictionRow`** — the ORM model, on this module's own declarative `Base`, so `ensure_tables()` creates just this reference table and never drags in the OpenCorporates cache tables.
- **`load_jurisdictions(csv_path)`** — polars `read_csv` (which strips the BOM), header check, one validated VO per row in file order.
- **`GleifJurisdictionRepository`** — Provisioner pattern, engine injected or built from `settings.DATABASE_URL` with the project search path. Methods: `ensure_tables()`, `upsert()`, `prune_absent()`, `count()`.

`upsert()` uses `INSERT ... ON CONFLICT (jurisdiction_code) DO UPDATE`, so re-running the loader — or pointing it at a newer code list — refreshes names, types and derived parts in place rather than duplicating rows. `prune_absent()` handles the other direction (a code GLEIF has retired); an empty keep-list is a deliberate no-op so it can never truncate the table.

### Configuration

Both values live in `src/config/settings.py` and are env-overridable:

| Setting | Default |
| --- | --- |
| `GLEIF_JURISDICTIONS_CSV_PATH` | `<CELEX_DOCS_DIR>/gleif_acceptedjurisdictions_v1.5.csv` |
| `GLEIF_JURISDICTIONS_VERSION` | `1.5` |

### Running it

```bash
# Validate the CSV without touching the database:
PYTHONPATH=src .venv/bin/python \
    src/pipelines/corp_index/gleif_jurisdiction_loader.py --dry-run

# Create the table (if absent) and load the code list:
PYTHONPATH=src .venv/bin/python \
    src/pipelines/corp_index/gleif_jurisdiction_loader.py

# Load a newer code-list version and drop codes GLEIF has retired:
PYTHONPATH=src .venv/bin/python \
    src/pipelines/corp_index/gleif_jurisdiction_loader.py \
    --csv-path ~/a/celex_docs/gleif_acceptedjurisdictions_v1.6.csv \
    --source-version 1.6 --prune
```

Load of 2026-07-30 (v1.5): 324 rows parsed, 324 upserted, 324 in table; 246 country-level, 78 subdivision. A second run reported 324 upserted / 0 pruned / 324 in table, confirming idempotency.

Tests: `src/pipelines/corp_index/tests/test_gleif_jurisdiction_loader.py` (14 unit tests, no DB or network — CSV/BOM parsing, derivation for each code shape, rejection of non-ISO codes, upsert column binding and rollback, prune no-op).

---

## How this table is used

### Resolving a country to its accepted jurisdictions

```sql
SELECT jurisdiction_code, jurisdiction_name
FROM celex_facts.gleif_jurisdication2countrycode
WHERE iso_country_code = 'AE'
ORDER BY jurisdiction_code;
-- AE, AE-AJ, AE-AZ, AE-DU, AE-FU, AE-RK, AE-SH, AE-UQ
```

### Validating the roster's country codes

`company_investigated2celex_id.iso_country_code` is populated from the `companies_country` fact and is *not* guaranteed to be ISO. Joining against this table finds the offenders:

```sql
SELECT DISTINCT roster.iso_country_code
FROM celex_facts.company_investigated2celex_id roster
LEFT JOIN celex_facts.gleif_jurisdication2countrycode gleif
       ON gleif.jurisdiction_code = roster.iso_country_code
WHERE roster.iso_country_code IS NOT NULL
  AND gleif.jurisdiction_code IS NULL;
```

As of 2026-07-30 this returns 11 distinct values over 21 of 5,489 roster rows: `Turkey`, `Czechoslovakia`, `Yugoslavia`, `Democratic Republic of the Congo`, `Ivory Coast`, `Swaziland`, `Tahiti`, `European Union`, `International`, `unknown`, `MQ`. Three distinct causes:

1. **Full country names instead of codes** — extraction wrote a name (`Turkey` -> `TR`, `Ivory Coast` -> `CI`, `Swaziland` -> `SZ` under its current name Eswatini).
2. **Historic or non-ISO entities** — `Czechoslovakia` and `Yugoslavia` have no current ISO 3166-1 code (`CS`/`YU` are transitionally reserved and absent from GLEIF's list); `International` and `unknown` are not jurisdictions at all. `European Union` is the name form of the `EU` code this table does carry.
3. **Valid ISO code GLEIF does not accept** — `MQ` (Martinique) is a real ISO 3166-1 alpha-2 code but not an accepted GLEIF legal jurisdiction, since entities there register under France. A GLEIF miss is therefore not proof of a bad code.

Cause 3 is why this table is the right authority for *"can I search this jurisdiction"* but not for *"is this a valid country code"*.

### Caveat: GLEIF codes are not OpenCorporates jurisdiction codes

The two vocabularies overlap but are not the same, and the difference is not just casing:

| | GLEIF | OpenCorporates |
| --- | --- | --- |
| Country | `AE` | *(no unified UAE jurisdiction)* |
| Subdivision | `AE-DU`, `AE-AZ` | `ae_du`, `ae_az` |
| Delaware | `US-DE` | `us_de` |

OpenCorporates lowercases and uses `_` as the separator, and it does **not** index every GLEIF country: a search with `jurisdiction_code="ae"` returns zero hits, while `ae_du` returns UAE companies (confirmed live — see `TestEmiratesLookup` in `src/pipelines/corp_index/tests/test_opencorp_integration.py`). Anything mapping this table onto an OpenCorporates query must lowercase and translate `-` to `_`, and must not assume a country-level jurisdiction exists at the provider.

---

## `company_investigated2celex_id.jurisdication_code`

The roster's `iso_country_code` is the wrong grain wherever a company is registered in a subdivision. `jurisdication_code` (`varchar(8)`, added by `migrate_company_investigated_jurisdiction.sql`) holds the resolved jurisdiction in the **canonical ISO 3166-2 / GLEIF form** — uppercase, hyphen-separated: `AE-DU`, `US-DE`, or a bare `GB` where the country is itself the jurisdiction. NULL means no jurisdiction could be determined.

It is exposed as the last column of `company_investigated2lei2corpidx2hr_vw` and consumed by `Main_ingest_opencorp_index_companies.py`.

### Why it exists

`Main_ingest_opencorp_index_companies.py` derived its jurisdiction filter as `iso_country_code.lower()`. OpenCorporates has **no country-level jurisdiction for AE or US at all**, so a bare `ae` search returns HTTP 200 with `total_count = 0` — indistinguishable from a genuine miss — and the batch marked those rows `fk_opencorp_idx = 'NA'`. 396 roster rows (5 AE + 391 US) were closed this way; the migration reopens them.

### Resolution precedence

Implemented in `src/pipelines/corp_index/jurisdiction_resolver.py`, populated by the `enrich_jurisdiction` post-stage of `Main_ingest_company_investigated2celex.py`. The resolver depends only on data available at roster-load time — the company facts and this GLEIF table. **It never reads OpenCorporates**, whose data is typically not available when the roster is loaded.

1. **Address** — the country's GLEIF subdivision label (`jurisdiction_name` up to `' ('`) appears in `adress` as a whole accent- and case-folded phrase. Failing that, for US/CA only, the subdivision code appears as a standalone *uppercase* token (`St Louis, MO 63188` -> `US-MO`); the casing requirement is what stops ordinary two-letter words being read as state codes. An ambiguous match (two subdivisions of the same country) is left unresolved rather than guessed.
2. **LEI legal jurisdiction** — `corp_lei.legal_jurisdiction` already stores ISO 3166-2 in exactly this format. Deliberately *after* the address, because it is both sparse (only 740 of 5,489 roster rows join to an LEI; most investigated companies have no match, often being long dissolved) and unreliable (38% of those disagree with their own row's country). Accepted only when its country part equals `iso_country_code`.
3. **Country code** — when the country has no GLEIF subdivisions at all, the country *is* the jurisdiction.
4. **NULL** — the country has subdivisions but nothing identified one.

### Measured outcome (load of 2026-07-30)

6,075 rows processed: 4,206 resolved, of which 234 to a subdivision; 1,869 left NULL.

| country | rows | resolved | to a subdivision |
| --- | --- | --- | --- |
| US | 391 | 212 | 212 |
| CA | 30 | 14 | 14 |
| GB | 711 | 6 | 6 |
| AE | 5 | 2 | 2 |
| DE | 916 | 916 | 0 (no GLEIF subdivisions) |
| BE, PT | 352 | 0 | 0 (subdivisions are rare regions) |

Most common: `US-DE` 39, `US-CA` 33, `US-NY` 31, `US-WA` 13, `CA-QC` 6.

### How the OpenCorporates batch consumes it

The roster stores *any* ISO subdivision; deciding what is searchable is the consumer's job, via two documented static constants in `Main_ingest_opencorp_index_companies.py` (transcribed from `GET /jurisdictions`, with the regeneration command in a comment — static so the batch never depends on OpenCorporates data being loaded first):

| stored | country | sent to the API | why |
| --- | --- | --- | --- |
| `AE-DU` | AE | `ae_du` | AE subdivisions are indexed |
| `US-DE` | US | `us_de` | US subdivisions are indexed |
| `GB-SCT` | GB | `gb` | OpenCorporates has no `gb_sct`; degrade to the country |
| `GB` | GB | `gb` | country-level jurisdiction exists |
| NULL | GB | `gb` | country-level jurisdiction exists |
| NULL | AE / US | *(nothing)* | **skipped, left unmarked** |

That last row is the important one: rather than send a bare `ae` and record a false `'NA'`, the batch skips the name, counts it in a `skipped_no_jurisdiction` summary key, and leaves `fk_opencorp_idx` NULL so a later run retries it once a jurisdiction is known.

Codes are handled case- and separator-insensitively throughout: `normalize_jurisdiction_code` maps `AE_DU`, `ae_du`, `AE-DU`, `ae-du`, `Ae_Du` and padded variants all to `AE-DU`, and `to_opencorp_code` converts that to `ae_du`. This applies to the `--jurisdiction-code` CLI override too.

### Known gap

The roster row `('Emirates', 'AE')` resolves to **NULL**. Its address is NULL, and its LEI (`222100BWCLXLCKXNDC15`) is *"EMIRATES NBD SICAV"* — a Luxembourg fund, whose `legal_jurisdiction = 'LU'` the country guard correctly rejects. Reaching `AE-DU` for it would require guessing a subdivision, which the current policy declines to do. Note this is a signal problem, not a jurisdiction-plumbing problem: `Emirates` under `ae_du` returns 23 live hits.