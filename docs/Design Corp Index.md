


# LEI

Bulk, cross-CELEX enrichment that resolves the `fk_lei` column against the GLEIF
Legal Entity Identifier register (`celex_facts.corp_lei`). It runs after the
[Loader](#loader) as a post-stage of the same pipeline (see *Orchestration*).
Unlike [Handelsregister](#handelsregister) it is **not** country-scoped — the LEI
is a global identifier, so every roster row is processed.

Source: `CompanyInvestigatedIngestor.enrich_lei` in
[Main_ingest_company_investigated2celex.py](../src/batch/Main_ingest_company_investigated2celex.py)
(orchestration) and `resolve_lei_codes` in
[company_investigated_repository.py](../src/pipelines/rag/pgvector/company_investigated_repository.py)
(matching SQL).

---

## Input

| Source | Role |
|---|---|
| `company_investigated2celex_id` (all rows) | Roster rows to enrich (`(id, company_name)` targets) |
| `celex_facts.corp_lei` (`legal_name`, `lei`) | GLEIF register; 3.28M rows, `legal_name` is **always UPPERCASE**, PK on `lei` |

## Substring matching

GLEIF legal names are usually *fuller* than the roster's short form (`"ANHEUSER-BUSCH INBEV"`
vs `"Anheuser-Busch"`), so matching is a **contains** test rather than equality:
a roster row matches a register row when the register `legal_name` **contains** the
uppercased roster name as a substring (`legal_name LIKE '%NAME%'`). This lifts
recall for short, clean names embedded in longer official names.

Details:

- **Casing** — the needle is `upper(company_name)`; `legal_name` is already upper.
- **Wildcard safety** — `%` / `_` / `!` in a name are escaped (`ESCAPE '!'`) so a
  stray wildcard can't broaden the match.
- **Minimum length** — names shorter than `MIN_LEI_NEEDLE_LENGTH` (4) are skipped
  (they match too broadly and fall below the trigram floor) and resolve to `"NA"`.
  So `"BP"`, `"3M"`, `"TU"` never match.
- **Tie-breaking** — an exact `legal_name` match is preferred; otherwise the
  **shortest** containing `legal_name` wins (closest to the needle), then the
  lowest `lei` for determinism.

It is a single set-based query: roster names are `unnest`ed into a `targets` CTE
and joined to `corp_lei` via the `LIKE` predicate, which rides the `legal_name`
trigram index (see *Performance*).

## Output

Each roster row's `fk_lei` is set to the resolved `lei`, or `"NA"` when the name
does not resolve — the same tri-state convention as [Loader → Output](#output)
(`NULL` = not processed, `"NA"` = processed / no match, value = matched). The
pass **overwrites** existing values so a re-run picks up register growth. Result
`status` is `no_rows`, `dry_run`, or `enriched`, plus `rows` / `matched` /
`unmatched`.

## Performance

`LIKE '%…%'` needs a trigram index on `legal_name`. One already exists
(`idx_legal_name_trgm_gist`, a **GiST** trigram index), but GiST is slow for
lookups (~0.25–0.4s per name → **~20 min** for the ~5.5k distinct roster names),
and `main()` runs this every invocation. For this read-heavy, static 3.28M-row
table a **GIN** trigram index is far faster for substring search:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX CONCURRENTLY ix_corp_lei_legal_name_gin_trgm
  ON celex_facts.corp_lei USING gin (legal_name gin_trgm_ops);
```

That drops the pass from ~20 min to seconds (user-gated migration; GIN is larger
and slower to write, acceptable for a static import).

## Caveats

- **Coarse matching.** Contains-matching plus a shortest-`legal_name` tiebreak is
  a heuristic — a common substring can resolve to the wrong entity. Fine for the
  hit-ratio goal, not precise.
- **No name normalization.** Only casing is handled (`upper()`); umlauts and
  punctuation are *not* folded (unlike Handelsregister), so `"Müller"` will not
  match `"MUELLER"`.
- **Long / baggy names miss.** A needle carrying extra tokens (`"Siemens
  Aktiengesellschaft"` when GLEIF stores `"SIEMENS AG"`) is not a substring and
  resolves to `"NA"`.

# Handelsregister

Bulk, cross-CELEX enrichment that resolves the `fk_handelsregister` column of the
roster table against the German commercial register
(`celex_facts.handelsregister_companies`). It runs after the [Loader](#loader) as
a post-stage of the same pipeline (see *Orchestration*), so `main()` always loads
the roster and then enriches it in one invocation.

Source: `CompanyInvestigatedIngestor.enrich_handelsregister` in
[Main_ingest_company_investigated2celex.py](../src/batch/Main_ingest_company_investigated2celex.py)
(orchestration) and `resolve_handelsregister_numbers` /
`_handelsregister_match_key` in
[company_investigated_repository.py](../src/pipelines/rag/pgvector/company_investigated_repository.py)
(matching SQL).

---

## Input

| Source | Role |
|---|---|
| `company_investigated2celex_id` where `iso_country_code = 'DE'` | Roster rows to enrich (the `(id, company_name)` targets) |
| `celex_facts.handelsregister_companies` (`name`, `company_number`) | The register; 5.4M rows, PK on `company_number` only, **`name` is not indexed** |

Only German (`DE`) rows are processed — the register is national. The country
code comes from the `companies_country` fact via the Loader, so a company with no
resolved country is never attempted here.

## Fuzzy matching

Roster names and register names rarely agree byte-for-byte (casing, umlauts,
punctuation, spacing, stray symbols). Rather than a similarity metric, both sides
are reduced to a **normalized match key** and compared for equality.
`_handelsregister_match_key` builds one SQL expression, applied identically to
both columns so the comparison is symmetric:

1. `lower(...)`
2. expand German umlauts / ligatures: `ä->ae`, `ö->oe`, `ü->ue`, `ß->ss`, `æ->ae`, `œ->oe`
3. fold remaining Latin accents to their base letter (`translate`)
4. strip every character that is not `[a-z0-9]` (drops spaces, punctuation, symbols, UTF junk)

So `"Aug. Winkhaus GmbH &Co.KG"` and `"Aug. Winkhaus GmbH & Co. KG"` both reduce
to `augwinkhausgmbhcokg` and match; `"B A S F Aktiengesellschaft"` matches
`"BASF Aktiengesellschaft"`. This is **not** a similarity function — it only makes
spelling-variant strings compare equal. It deliberately does **not** strip
legal-form tokens (`GmbH` / `AG` / `KG`), so `"Firma GmbH"` and `"Firma AG"` keep
distinct keys (no false merge) but also never cross-match.

The whole thing is a single set-based query: the roster names are `unnest`ed into
a `targets` CTE, and joined to the register on `match_key` equality (one
**hash join**, one seq scan of the register computing the key per row). A
verbatim match is preferred over a fuzzy-only match for the same input name
(`ORDER BY (register.name = target) DESC`), and among equally-ranked register
rows the lowest `company_number` wins (`DISTINCT ON`).

## Output

Each DE roster row's `fk_handelsregister` is set to the resolved `company_number`,
or the `"NA"` placeholder when the name does not resolve — the same tri-state
convention described under [Loader → Output](#output) (`NULL` = not yet processed,
`"NA"` = processed / no match, value = matched). The pass **overwrites** existing
values so a re-run picks up register growth or a later normalization improvement.

Result dict `status` is one of `no_rows`, `dry_run`, or `enriched`, plus
`rows` / `matched` / `unmatched` counts.

Current coverage (live dry run): **916 DE rows, 274 matched, 642 NA**. The
normalization lifts matches from 209 (verbatim) to 274 (+31%). Most remaining NA
are roster names carrying narrative baggage baked in by extraction — e.g.
`"Gebrüder Giulini GmbH, Ludwigshafen (Rhein), Federal Republic of Germany"` —
which needs a name-cleanup pass, not more fuzzing.

## Performance

Because `handelsregister_companies.name` is unindexed, the match key is computed
over all 5.4M rows on every run — a **~83s seq scan**. Since `main()` runs this
each invocation, that cost is paid every pipeline run. To cut it to sub-second,
add an `IMMUTABLE` functional index whose expression is **byte-identical** to
`_handelsregister_match_key` (otherwise the planner will not use it):

```sql
CREATE INDEX ix_hr_matchkey ON celex_facts.handelsregister_companies (
  regexp_replace(translate(
    replace(replace(replace(replace(replace(replace(
      lower(name),'ä','ae'),'ö','oe'),'ü','ue'),'ß','ss'),'æ','ae'),'œ','oe'),
    'àáâãåèéêëìíîïòóôõøùúûçñ','aaaaaeeeeiiiiooooouuucn'),
  '[^a-z0-9]','','g'));
```

This is a user-gated migration (multi-minute index build on 5.4M rows).

## Running

`fk_handelsregister` enrichment runs automatically as the last stage of the
full pipeline; there is no separate entry point:

```bash
# Dry run: report matched / NA counts without writing.
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_company_investigated2celex.py --dry-run

# Real run: roster load + enrichment (overwrites fk_handelsregister on DE rows).
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_company_investigated2celex.py
```

## Caveats

- **Fuzzy collisions.** Two distinct register rows can share a normalized key; the
  lowest `company_number` is picked arbitrarily (after preferring an exact raw
  match). Acceptable for the hit-ratio goal, not precise.
- **No FK constraint.** `fk_handelsregister` names a foreign key but the DDL has
  none; the `"NA"` sentinel would violate one if added, and downstream joins must
  exclude `'NA'` explicitly.

# OpenCorporates

The third corporate-index pass. It resolves roster companies that LEI and
Handelsregister could **not** identify against the OpenCorporates company-search
API, persists the hits, and records a many-to-many link between each roster row
and every matching OpenCorporates company.

GB was the **pilot** country. `--iso-country-code` still restricts a run to one
country, but by default (the flag omitted) the pass now resolves every
outstanding company across all countries in one run, each searched under its
own country's OpenCorporates jurisdiction.

Source: [Main_ingest_opencorp_index_companies.py](../src/batch/Main_ingest_opencorp_index_companies.py)
(driver / loop),
[opencorp_client.py](../src/pipelines/corp_index/opencorp_client.py) (DB-cached
REST client),
[opencorp_repository.py](../src/pipelines/corp_index/opencorp_repository.py)
(OpenCorporates cache + daily-call counter) and
[company_investigated_repository.py](../src/pipelines/rag/pgvector/company_investigated_repository.py)
(roster target selection, N:M linking, `fk_opencorp_idx` marking).




---

## Data model

Four tables, in two ownership groups. see: [Data Model Companies](<Data Model Companies.md>)

**OpenCorporates search cache** (owned by `OpenCorpIndexRepository`, written by
`OpenCorpClient.search_companies`):

| Table | Role |
|---|---|
| `opencorp_index_query` | One row per cached search, keyed by `(normalized_query, jurisdiction_code)`. `normalized_query` is the casefolded, whitespace-collapsed company name. Also carries the last-attempt error, if any (`error_class`, `error_status_code`, `error_message`) — see [Failed-query diagnostics](#failed-query-diagnostics) below. |
| `opencorp_index_companies` | The company hits of a search (FK `query_id`, `ON DELETE CASCADE`). One row per OpenCorporates company; `id` is the UUID the link table references. |

A search is served from this cache when the same `(name, jurisdiction)` was
looked up before — a cache hit makes **zero** API calls, which is what keeps
re-runs quota-free.

**Roster linkage and budget** (roster side owned by
`CompanyInvestigatedRepository`):

| Table | Role |
|---|---|
| `company_investigated_celex_id2opencorp_index_company` | N:M link: `fk_company_investigated` -> `company_investigated2celex_id.id`, `fk_opencorp_index_company` -> `opencorp_index_companies.id`. `UNIQUE (fk_company_investigated, fk_opencorp_index_company)` + `ON CONFLICT DO NOTHING` makes re-linking idempotent. |
| `opencorp_api_daily_calls` | Durable per-day API-call counter (`call_date` PK, `call_count`). Every fetched page is added here so the budget survives process restarts and cross-day resumes. |

The N:M table replaces a single-value FK: one investigated company can match
several OpenCorporates entities, and one OpenCorporates entity can back several
roster rows (the same company appears across CELEX cases), so neither side is
functionally dependent on the other.

### The `fk_opencorp_idx` marker states

Marking extends the tri-state used by `fk_lei` / `fk_handelsregister` (see the
LEI / Handelsregister discussion above) with a fourth value:

- `NULL` = not yet processed,
- `'NA'` = processed, **no rung** returned anything,
- `'LINKED'` = processed, one or more hits linked in the N:M table,
- `'AMBIGUOUS'` = processed, hits came back but none identified this company.

`'AMBIGUOUS'` is deliberately not folded into `'NA'`. "Too many, or none
convincing" is a triage bucket a human can work through; "not in any registry" is
a fact. Roster `Emirates` is the archetype: 1,447 unfiltered hits, 189 of them
clearing the match threshold, none of them the Dubai airline.

Unlike the LEI / Handelsregister passes (which overwrite every run), this pass
**skips already-processed rows**: the marker is what removes a row from the
target set, so the pass is naturally resumable. `--retry-na` and
`--retry-ambiguous` reopen the two closed states explicitly. The concrete N:M
links live in the link table; `fk_opencorp_idx` is only the processed-state
marker (a single text column cannot hold the many hit IDs).

Distribution measured 2026-08-01 over the 6,383-row roster:

| `fk_opencorp_idx` | rows |
|---|---|
| `'NA'` | 3,057 |
| `'LINKED'` | 2,167 |
| `NULL` | 1,088 |
| `'AMBIGUOUS'` | 71 |

---

## Loader (OpenCorporates pass)

Batch ETL that flattens the extracted company facts into the
`celex_facts.company_investigated2celex_id` roster table — one row per
investigated company per CELEX document, enriched with its registered address
and ISO country code. It is a pure ETL: no LLM or DSPy pipeline is involved.

Source: [Main_ingest_company_investigated2celex.py](../src/batch/Main_ingest_company_investigated2celex.py)
(driver / parsing) and
[company_investigated_repository.py](../src/pipelines/rag/pgvector/company_investigated_repository.py)
(DAO / persistence).


### Input

`select_opencorp_targets` reads
`celex_facts.company_investigated2celex_id` **directly, not the roster view**.
The view is a six-column `SELECT DISTINCT` projection that cannot supply the
per-row `address` or `celex_id` the resolution ladder scores against, and its
definition lives in a scratch script that has already been replayed once without
`jurisdication_code`, breaking every run. Selecting from the table removes both
problems.

The grain is distinct `(company_name, iso_country_code, jurisdication_code)` —
one name can appear under two jurisdictions when different CELEX documents give
different addresses, and each needs its own search. Values that vary within a
grain are aggregated: any known address, the union of the documents' industries,
and the **latest** decision year (the most permissive choice for the ladder's
temporal veto, so it cannot reject a hit some other document would have
accepted).

The WHERE clause is assembled from four conditions:

| Condition | Set by |
|---|---|
| `fk_opencorp_idx IS NULL`, widened to `... OR fk_opencorp_idx = ANY(:retry_markers)` | `--retry-na`, `--retry-ambiguous` |
| `(fk_lei = 'NA' OR fk_handelsregister = 'NA')` | dropped by `--all-corps` |
| `iso_country_code = :iso_country_code` | `--iso-country-code` |
| `iso_country_code IS NOT NULL` | **dropped** by `--include-missing-country` / `--relax` |

That last condition is worth dwelling on. It was unconditional, and
`OpenCorpTarget.iso_country_code` was a required `str`, so **829 of the 1,088
unmarked roster rows could not be represented as a target at all**, let alone
searched — the roster's largest unresolved group was invisible to the pass rather
than merely unprocessed. A country-less row is perfectly searchable, just only
unfiltered: `CEJI-Interlude` returns exactly one hit, registered `fr`. Root cause
of the NULL countries themselves is the enrichment matcher (see the 20260731
entry in `ISSUES.md`), not this pass.

Because processed rows are marked, this query is also the **resume cursor** — the
next run returns only what is still outstanding.

The search endpoint accepts only `q` (name) and `jurisdiction_code`; the roster
`address` is **not** a search parameter, though it is used by the scorer.

### Query sanitization

Before a name reaches the wire, `OpenCorpClient._sanitize_query` strips the
characters in `_QUERY_BLOCKED_CHARS` (`()[]{}<>"\|;,`), replacing each with a
space and collapsing whitespace, then applies `_SCARE_QUOTE_RE` to drop a
single quote that is *not* flanked by a letter on both sides. Only the
outgoing `q` parameter is sanitized — the cache key (`normalized_query`)
stays derived from the caller's original text, so a caller can look a result
back up by the name it holds.

This exists because the edge in front of the API answers an otherwise
legitimate name with an HTML 403 denial page (or drops the connection
outright) for certain punctuation shapes — a real block, confirmed by
probing the live endpoint directly, not an artifact of this codebase. Three
distinct patterns have been found, at varying confidence levels — full
incident writeup: [Incident OpenCorporates Query Sanitization](Incident%20OpenCorporates%20Query%20Sanitization.md).

- **Matched bracket/quoting punctuation** (the original, longer-standing
  example that first motivated sanitizing queries) — `'Chambers (W. & R.)
  Ltd'` is refused while `'Chambers W. & R. Ltd'` succeeds. A single-character
  sweep found that no lone, unmatched bracket character (including `(` or `)`
  alone) trips a rejection on its own, so the trigger is presumed to be the
  *matched pair* enclosing text rather than any one character — but unlike
  the comma case below, this has not yet been re-isolated with the same
  systematic live probing, so the exact rule (one matched pair enough? does
  it need the ampersand inside? repetition?) is still unconfirmed.
- **Repeated dotted-abbreviation-plus-comma** (fully isolated) — the trigger
  is not any single character (a lone comma is fine: `'Acme, Corp'`
  succeeds, and so does one `'N.V.,'`), and not accents. It is the literal
  substring `.,` (a period immediately followed by a comma) appearing
  **twice or more** in one query. `'André Dumont N.V., S.A., Brissel'` (two
  occurrences: `N.V.,` and `S.A.,`) is refused; the same string with either
  comma removed, or with both removed, succeeds. Stripping every comma is a
  superset guard — with no comma left, `.,` can never occur — rather than
  only blocking a second occurrence.
- **Scare-quoted nickname/trade name** (fully isolated) — four consecutive
  NL/BE roster names in one production run all 403'd, all sharing a word or
  phrase wrapped in single quotes as a nickname (`"DMV 'Campina' BV"`), not a
  contraction. Single quotes were previously untouched by sanitization
  entirely, since the only other blocklist candidate for them would also
  break genuine apostrophes (`"O'Brien"`, `"Child's Play"`, `"d'Obourg"`).
  Confirmed by live A/B curl testing outside any batch load: `'Campina'` and
  a second, unrelated pair `'Nickname'` both 403 with quotes, both 200
  without; a genuine contraction alongside a scare-quote in one query
  (`"O'Brien 'Nickname' Ltd"`) still 403s, so the delimiter quote is what
  matters, not quotes-in-general, and a real apostrophe elsewhere in the same
  query provides no protection. `_SCARE_QUOTE_RE` resolves this by keeping a
  quote only when flanked by a letter on both sides.

`&`, `+`, `-`, `/` and `.` are deliberately kept: they occur in real
registered names and are accepted by the API. The probe scripts used to
isolate these patterns are kept for reference (not run automatically)
alongside the live integration tests:
[probe_opencorp_comma_isolate.py](../src/pipelines/corp_index/tests/probe_opencorp_comma_isolate.py)
(fully run, results above); a bracket-pattern counterpart is drafted but not
yet run (see the incident writeup) — that remains the one pattern (2a) still
not independently isolated.

### The resolution ladder

A single-shot search closed a roster row permanently: a clean `HTTP 200` with
zero results was written as `'NA'` and never came back. Measurement over 3,548
such rows showed only a minority were companies genuinely absent from every
registry. The rest failed for two recoverable reasons, and
[opencorp_resolution_ladder.py](../src/pipelines/corp_index/opencorp_resolution_ladder.py)
addresses both. Design doc:
[opencorp-zero-hit-relaxation-ladder.md](claude/plans/opencorp-zero-hit-relaxation-ladder.md).

**The name.** `q` is AND-over-tokens, order-independent (confirmed live
2026-07-31), so every junk token the extractor left in a roster name is an extra
term the registry must also satisfy. Deleting tokens can only broaden.

**The jurisdiction.** `jurisdication_code` is derived from headquarters signals
but OpenCorporates indexes by *registration*. Halkis is headquartered in Athens
and stored `GR`, yet `jurisdiction_code=gr` returns nothing while `cy` returns
`HALKIS CEMENT TRANSPORTERS CO. LIMITED`. No name relaxation recovers that; only
dropping the filter does.

`build_ladder` orders the attempts most-constrained-first. Each rung slug is
persisted as `resolution_rung` on the link row, so a match stays explainable long
after the run — `verbatim` under the row's own jurisdiction is a far stronger
claim than `core@any`:

| Rung | Query | Jurisdiction | Scored? |
|---|---|---|---|
| `verbatim` | roster name as-is | the row's own | no — the filter supplies precision |
| `verbatim@any` | roster name as-is | **none** | yes |
| `split` | one member of a multi-company row | the row's own | only if unfiltered |
| `no-paren` / `no-role` / `core` | parentheticals / trailing clause / legal forms removed | the row's own | no |
| `core@country` | stripped name | country level, when the row resolved a subdivision | yes |
| `core@any` | stripped name | **none** | yes |

`verbatim@any` sits *second* rather than last on purpose: it is the only rung
that reaches an offshore registration, and a row whose jurisdiction is simply
wrong would otherwise burn every name rung before getting there. Duplicate
`(query, jurisdiction)` pairs are removed, so a clean name under a country-level
jurisdiction typically costs two rungs, not eight. A target with **no**
jurisdiction gets only the two unfiltered rungs — every filtered rung in the
table above is conditional on having one.

Scoring (`rank_and_filter`) is what makes dropping the filter safe: one veto and
three boosts, nothing geographic ever negative — a Cyprus registered office must
not count against an Athens-headquartered party. Name match is tiered at 1.0
exact / 0.8 whole-word prefix / 0.5 bare containment, and the default
`OPENCORP_MIN_MATCH_SCORE` of 0.8 is set precisely so `Halkis` reaches
`HALKIS CEMENT TRANSPORTERS CO. LIMITED` while `GESTION HALKIS INC.` is rejected.

### Per-company flow

For each target, walking the ladder and stopping at the first rung that accepts
something:

1. Throttle (`time.sleep(--sleep-seconds)`, default 1s), then
   `search_companies(rung.query_text, jurisdiction_code=rung.jurisdiction_code,
   max_hits=rung.hit_cap)`. This persists any hits into
   `opencorp_index_companies` and caches the query.
2. If the search was **not** served from cache, add its `fetched_pages` to the
   day's `opencorp_api_daily_calls` counter (each page is one API call).
3. If `total_count` exceeds the rung's cap, the result is too broad to be
   evidence — skip to the next rung (but see `--score-over-cap` below).
4. On a scored rung, run `rank_and_filter`; on an unscored one, take the
   persisted hit IDs directly, capped at `max_links`.
5. Stop the ladder as soon as a rung accepts anything.

Then, once per target: fetch the roster IDs for this name **and country**
(`roster_ids_for_company(name, iso)`, matched with `IS NOT DISTINCT FROM` so a
country-less target finds its own rows rather than nothing), insert the
cross-product into the N:M link table (`ON CONFLICT DO NOTHING`), and mark every
roster row `'LINKED'`, `'AMBIGUOUS'` (hits came back, none accepted) or `'NA'`
(no rung returned anything).

Marking happens per company, right after linking, so a mid-run quota stop or
crash preserves progress.

### Relaxation policy (`--relax`)

The ladder's defaults are tuned for precision on rows that already carry a
country and a jurisdiction. Rows that carry neither need the same code run at
different settings, not different code. `RelaxationPolicy` is a frozen Pydantic
model holding every knob, defaulted from `settings`, overridable per run from the
CLI, and passed explicitly into `build_ladder` / `rank_and_filter` — which keeps
those pure: identical inputs give identical rungs, with no hidden read of
process-wide settings. Design doc:
[opencorp-relaxed-mode-for-unmarked-rows.md](claude/plans/opencorp-relaxed-mode-for-unmarked-rows.md).

| Knob | Default | Under `--relax` | Effect |
|---|---|---|---|
| `min_query_length` | `OPENCORP_MIN_RELAXED_QUERY_LENGTH` (5) | `OPENCORP_RELAXED_MIN_QUERY_LENGTH` (3) | `STET` is four characters and produced **no rung at all**; the guard exists to keep `RS` / `PC` out, and at 3 it still does |
| `jurisdiction_hit_cap` | `OPENCORP_HIT_CAP_JURISDICTION` (200) | unchanged | linkable set / page budget, filtered rung |
| `unfiltered_hit_cap` | `OPENCORP_HIT_CAP_UNFILTERED` (50) | unchanged | linkable set / page budget, global rung |
| `min_match_score` | `OPENCORP_MIN_MATCH_SCORE` (0.8) | **unchanged** | scored-rung acceptance threshold |
| `max_links` | `OPENCORP_MAX_LINKS_PER_TARGET` (5) | **unchanged** | link fan-out ceiling |
| `strip_leading_legal_form` | off | **on** | `S.A. Cimenteries CBR` -> `Cimenteries CBR` |
| `score_over_cap` | off | **on** | an over-cap rung's already-fetched first page goes to the scorer instead of the bin |

The invariant: **relaxation widens what gets asked, never what gets accepted.**
`--relax` leaves the match threshold and the link ceiling exactly where they
were. Every knob is also settable on its own and overrides the profile in either
direction, so `--relax --min-match-score 0.9` is a relaxed run with a stricter
scorer, and `--relax --no-score-over-cap` turns one behaviour back off.
`--include-missing-country` is a *selection* flag rather than a policy field (it
changes the WHERE clause, not the ladder), and follows `--relax` unless given
explicitly.

**Why the caps do not move.** `score_over_cap` is the cheap half of the fix. The
cap doubles as the client's page budget, so raising it buys hits with API calls;
scoring the page that was fetched and charged for anyway buys some of the same
hits for free. `Wavin` is the case: 166 unfiltered hits against a cap of 50, real
registrations on page 1, previously discarded whole. Raising the cap stays
available as `--unfiltered-hit-cap` when the calls are worth spending.

**The guard that goes with it.** Scoring an over-cap page re-opens the fan-out
the cap existed to prevent. A live run linked roster `Emirates` to five arbitrary
exact-name `EMIRATES` companies (`my`, `ca_on`, `no`, `sg`, `nl`) drawn from 189
hits that cleared 0.8 out of 1,447 — none the Dubai airline. So an over-cap rung
is now evidence **only if the scorer narrows it**: when more than `max_links`
hits clear the threshold, the rung links nothing and the ladder continues,
ending in `'AMBIGUOUS'` rather than in five invented resolutions.

**Reach.** `--relax --all-corps --dry-run` plans **1,041 targets against the 252**
a default run selects. Worst-case rung usage if nothing ever resolves:
`verbatim@any` 1,037, `core@any` 585, `verbatim` 216, `core` 87, `no-paren` 29,
`no-role` 19. Four targets remain unsearchable, all genuine two-letter names.

**Non-Latin names.** `relaxation_variants` keyed its deduplication on
`fold_match_key`, which strips everything outside ASCII — so a Greek or Cyrillic
name folded to `""`, every variant was dropped, and the row was reported as "name
too short". OpenCorporates does index those names (`"ММ СТЕТ" ЕООД` is a real
hit). The key now falls back to a casefolded original when the ASCII fold is
empty.

### Quota and resume

- **Budget** — before each search the day's counter is checked; at
  `--daily-call-limit` (default 100,000) the run logs a **bold banner** and stops.
  The pre-search check can overshoot by at most one search's pages, negligible
  against the default. Set `--daily-call-limit` to the OpenCorporates **account's**
  real daily allowance so the run stops on its own budget rather than being
  rejected by the API.
- **Account-side rejection** — a mid-run `HTTP 403` (the account's daily/credit
  allowance is spent) or other API error is caught, logged with a bold banner, and
  stops the run cleanly (`stopped_on_api_error`) rather than crashing. This is
  distinct from a `429` rate-limit, which the client retries with backoff when
  retries are enabled. Processed rows are marked, so the run resumes on the next
  invocation. See *Fail fast and carry on* below: under the default
  `OPENCORP_SKIP_RETRY` nothing is retried and only a throttle or a rejected
  token can stop the run.
- **Resume** — unprocessed rows keep `fk_opencorp_idx = NULL` and reappear in the
  target view, so the next day's run continues where this one stopped. The
  per-day counter row means the budget picks up mid-day across restarts too.

### Fail fast and carry on (`OPENCORP_SKIP_RETRY`)

The original retry policy assumed a failure that survives backoff is evidence
the API side is unhealthy, so a streak of `--max-consecutive-api-errors` (3)
ended the run. That assumption breaks on `HTTP 400`: the API refuses a specific
query and will refuse it on every attempt, so retrying spends backoff on a
certain failure and three such names landing back-to-back kills a run that was
otherwise healthy (observed 2026-07-30, `'Carrefour'` then `'GFA'` after 1,490
successful calls).

`settings.OPENCORP_SKIP_RETRY` (env `OPENCORP_SKIP_RETRY`, **default on**)
changes both halves of that; set it to `false` to get the old policy back:

| Layer | `OPENCORP_SKIP_RETRY=true` (default) | `OPENCORP_SKIP_RETRY=false` |
|---|---|---|
| `OpenCorpClient._get` | Retry budget 0: one attempt, no backoff sleep, the failure is logged and raised straight away | Retries 429/5xx/transport with exponential backoff, and the HTML-403 throttle after `OPENCORP_THROTTLE_BACKOFF_SECONDS` (60s), up to `OPENCORP_MAX_RETRIES` |
| `OpenCorpIndexIngestor.run` | Only an IP throttle can stop the run; every other error is logged, recorded, skipped, and the loop moves to the next name however often it recurs | Any error streak of `--max-consecutive-api-errors` stops the run |

The two exceptions are deliberate: an `OpenCorpRateLimitError` (IP throttle) and
an `OpenCorpAuthError` (rejected token) apply to every subsequent request, so
continuing would burn the remaining roster on guaranteed failures and hammer an
API already asking the caller to slow down. `OpenCorpAuthError` stops on the
first occurrence as before; the throttle still gets the consecutive-error grace
period. Skipped names stay unmarked and resume on the next run, so nothing is
lost — the trade is more recorded errors (see below) in exchange for the run
finishing.

### Failed-query diagnostics

A search that raises `OpenCorpAuthError` or `OpenCorpApiError` (including its
`OpenCorpRateLimitError` subclass) was, until this was added, only visible in
the run's log output — nothing in the database recorded which name failed or
why. `OpenCorpIndexIngestor._record_query_error` now writes that failure back
to `opencorp_index_query` via `OpenCorpIndexRepository.save_query_error`,
keyed on the same `(normalized_query, jurisdiction_code)` a successful search
would use:

| Column | Meaning |
|---|---|
| `error_class` | The raised exception's class name, e.g. `OpenCorpRateLimitError`, `OpenCorpAuthError`, `OpenCorpApiError`. `NULL` means the last attempt for this key succeeded. |
| `error_status_code` | The HTTP status code that caused the failure (`401`, `403`, `429`, `5xx`), or `NULL` for a transport-level error with no response. |
| `error_message` | The exception's message text. |

```sql
SELECT query_text, jurisdiction_code, error_class, error_status_code,
       error_message, fetched_at
FROM celex_facts.opencorp_index_query
WHERE error_class IS NOT NULL;
```

This reuses the query row rather than a separate table, so `save_query_error`
follows the same delete-then-insert shape as `save_search` (no company rows
attached, `fetched_pages = 0`). Consequently `search()`'s cache-hit lookup
excludes rows with `error_class` set — otherwise a failed attempt would be
served back as a false "no hits" cache hit instead of being retried. The
roster row is also left unmarked on failure (see *Per-company flow* above),
so the name is retried on the next run; when that retry succeeds,
`save_search` replaces the row and the error columns disappear with it.
Migration: [migrate_opencorp_query_errors.sql](../src/pipelines/corp_index/migrate_opencorp_query_errors.sql).

### Result status

`run()` returns a summary dict:

| Field | Meaning |
|---|---|
| `targets` | Distinct outstanding targets selected (after `--limit`) |
| `processed` | Names searched and marked this run |
| `linked` | Names that resolved to >=1 OpenCorporates hit |
| `na` | Names for which **no rung** returned anything |
| `ambiguous` | Names where hits came back but none was accepted |
| `skipped` | Names left unmarked after a search error (see *Failed-query diagnostics* above); retried on the next run |
| `skipped_no_jurisdiction` | Names with no searchable jurisdiction, so every rung ran unfiltered |
| `skipped_no_ladder` | Names too short for any rung to be sent; left **unmarked**, since "we declined to ask" is not "we asked and missed" |
| `calls_today` | Day's `opencorp_api_daily_calls` count after the run |
| `stopped_on_quota` | True when the run stopped on the daily budget |
| `stopped_on_api_error` | True when the run stopped on an auth failure or too many consecutive search errors (`--max-consecutive-api-errors`); under the default `OPENCORP_SKIP_RETRY` only an auth failure or a throttle streak can set it |

### Running

This is a company-driven job (not CELEX-driven), so it does **not** use
`BatchJobRunner`; it has its own CLI.

```bash
# Dry run: list the GB companies that would be resolved (no API calls, no writes):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --iso-country-code GB --limit 3 --dry-run

# Small real run (throttled, quota-counted, resumable):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --iso-country-code GB --limit 20

# Full GB run:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py --iso-country-code GB

# Full run across every outstanding country (each searched under its own
# jurisdiction):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py
```

Relaxed runs:

```bash
# Dry run of the relaxed profile: the rungs planned for every unmarked row,
# including the ~789 country-less targets a default run never selects.
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --relax --all-corps --dry-run

# The relaxed run itself, over every unmarked row:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py --relax --all-corps

# Relaxed, and reopening the rows a single-shot search closed before the ladder
# existed. This is what recovers 'NYK Container Line, Ltd' (0 hits verbatim
# under jp, 1 exact hit once the legal form is cut) and 'PRINTEOS, S.A.'
# (0 under es, 3 unfiltered including 'Printeos GmbH'):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --relax --all-corps --retry-na --retry-ambiguous

# Relaxed asking, stricter accepting, and willing to page further globally:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --relax --all-corps --min-match-score 0.9 --unfiltered-hit-cap 200

# Just the country-less rows, at otherwise default settings:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_ingest_opencorp_index_companies.py \
    --include-missing-country --all-corps
```

### Caveat

Force-refreshing a cached search (`search_companies(..., force_refresh=True)`)
deletes and re-inserts the query's `opencorp_index_companies` rows with **new**
UUIDs; the `ON DELETE CASCADE` then drops any N:M links pointing at the old rows.
This pass never force-refreshes (it is cache-served), so hit IDs are stable — but
anyone adding a refresh path must re-link afterwards.

---


# Data 

see: [Data Model Companies](<Data Model Companies.md>)


NYK Container Line, Ltd
Company names in other languages than english such as "NEC Computer International" have no hits 

![](assets/Design%20Corp%20Index/file-20260801111952961.png)


Missing entries
Austral Líneas Aéreas   : Argentina based airline
Solomon Airlines
SABA (Schwarzwalder Apparate-Bau-Anstalt August Schwer und Söhne GmbH)
Schwarzwalder Apparate-Bau-Anstalt August Schwer und Söhne Gmbh
Schwarzwalder Apparate-Bau-Anstalt August Schwer und Söhne Gmbh
hit: Schwarzwalder Apparate-Bau-Anstalt, SABA Schwarzwälder Apparate-Bau-Anstalt
## errors

```
SELECT query_text, jurisdiction_code, error_class, error_status_code, error_message, fetched_at
FROM celex_facts.opencorp_index_query
WHERE error_class IS NOT NULL;
```


# Deepseek
Opencorporates identified >4000 of the 6000 company under investigation but failed on 1738
Deepseek API could close the gap.
Example:
A. C. Nielsen Italia SpA  --> BRIS 1513375/1996; historical record

Aviación y Comercio, SA (AVIACO) --> - **Life Cycle:** AVIACO was a Spanish airline that ceased all operations on **September 1, 1999**, and its corporate existence was subsequently ended.[](https://dic.academic.ru/dic.nsf/ruwiki%5Cdic.academic.ru/dic.nsf/ruwiki/602386) The company is widely recognized as defunct.

Brasserie de Wiltz	14 rue Joseph Simon, L-9550 Wiltz, Grand Duchy of Luxembourg -->  **EUID** 8434-4121-8815-0614;  canoncial: Brasserie De Wiltz S.E.C.S.

---

## The LLM company index

Source: `LlmCorpIndexSearcher` in
[Main_LLM_corp_index_searcher.py](../src/batch/Main_LLM_corp_index_searcher.py)
(orchestration), `LlmCompanyIndexModule` in
[corp_index_module.py](../src/pipelines/corp_index/corp_index_module.py) (the
DSPy call), `LlmCompanyIndexSignature` in
[dspy_signature.py](../src/pipelines/corp_index/dspy_signature.py) (the task
definition) and `LlmCompanyIndexRepository` in
[llm_index_repository.py](../src/pipelines/corp_index/llm_index_repository.py)
(persistence).

### Input

| Source | Role |
|---|---|
| `company_investigated2lei2corpidx2hr_vw` where `fk_opencorp_idx = 'NA'` | The residue: 1,738 rows over 1,704 distinct names (2026-08-01), of 6,109 |
| `celex_facts.llm_index_companies` | Anti-joined out, so a stored company is never re-asked |

The view is a `DISTINCT` projection in which one name can appear several times
with different addresses or countries, so the selection groups to one row per
`company_name` (1,703 targets after the junk guard drops the literal `'[...]'`
name) and aggregates with `max()` — arbitrary but deterministic, the same choice
[`select_opencorp_targets`](../src/pipelines/rag/pgvector/company_investigated_repository.py)
makes.

### Hints, and why `'NA'` is not one

Every enrichment column of the roster is tri-state: NULL = not processed,
`'NA'` = processed / no match, a value = matched. The address, country and
jurisdiction are handed to the model as context, and a *confirmed* `fk_lei` or
`fk_handelsregister` is handed over as authoritative — the signature tells the
model to reproduce it verbatim, since it was already matched against a register.
Both NULL and `'NA'` fold to an empty string first: `'NA'` means "we asked and
there was no match", which is a fact about the pipeline, not about the company,
and relaying it as a claim would invite the model to explain a nonexistent
identifier.

`jurisdication_code` is passed as an explicit *hint*, never as a constraint —
it is a headquarters signal, and an offshore or holding-company registration
legitimately disagrees with it (see the Halkis and Emirates cases above).

### Output

One row per company name in `celex_facts.llm_index_companies` (DDL:
[migrate_llm_index_companies.sql](../src/pipelines/corp_index/migrate_llm_index_companies.sql)),
upserted on `company_name`:

| Column | Content |
|---|---|
| `company_name` | The verbatim roster name; the functional key. Taken from the *request*, never from the model's answer, so it stays joinable to the roster even when the model identifies a different canonical name |
| `canonical_name`, `abbreviation`, `legal_form`, `established_since`, `life_cycle_status`, `succession` | Identity, age and fate |
| `products_services`, `geo_market`, `headquarter_address`, `country`, `jurisdiction` | Descriptive fields **no register in this pipeline supplies at all** |
| `lei`, `euid`, `bris`, `handelsregister`, `siren` | Named identifiers, `'NA'` when the model had none |
| `generic_corp_id`, `generic_corp_registrar` | Any company id the named columns have no slot for, plus the registrar that issued it — a Belgian CBE enterprise number, a Dutch KVK number, an EU VAT number, Companies House, RCS, CVR. A number without its registrar cannot be interpreted or verified, so the two are written together |
| `llm_model_id` | The resolved `corp_index` profile model — which model made the claim |

Every field defaults to `'NA'` and every spelling of "unknown" (`""`, `null`,
`not found`, `n/a`) folds onto it, so a field the model omitted is
indistinguishable from one it declined — one predicate (`<> 'NA'`) finds
everything it did answer.

`established_since` and `succession` are text, not a date and not a foreign key.
The honest answer to the first is often a bare or circa year, and a date column
would force a day and a month to be invented; the second holds *what became of
the entity* — `absorbed into DOMO in 1982, part of FrieslandCampina since 2008`
— which is the reason most of these companies are unresolvable in the first
place and previously had nowhere to go.

Field order in `LlmCompanyIndexProfile` is the order the DSPy adapter emits JSON
keys in, so the descriptive fields come first and the identifiers last: what the
model recalls about the company is written down before it is asked to commit to
a key.

### Two NA rules, not one

Until 2026-08-02 the signature carried a single instruction — *"NA is a correct
answer; a plausible-looking identifier reconstructed from the company name is
not"* — applied to every field. The justification is identifier-specific, and it
was suppressing the descriptive fields with it. The rules are now split, and
each field's own description says which it follows:

| Class | Fields | Rule |
|---|---|---|
| Descriptive | `canonical_name`, `abbreviation`, `legal_form`, `established_since`, `life_cycle_status`, `succession`, `products_services`, `geo_market`, `headquarter_address`, `country`, `jurisdiction` | Best-supported answer. A hedge, an approximate year, or a description by way of the group that absorbed the company beats `'NA'`. `'NA'` only when the company cannot be placed at all |
| Identifier | `lei`, `euid`, `bris`, `handelsregister`, `siren`, `generic_corp_id`, `generic_corp_registrar` | Report only what is actually recalled. Never derive, pattern-fill or reconstruct. `'NA'` otherwise |

The rules live on the individual `Field(description=...)` of
`LlmCompanyIndexProfile`, not only in the signature's output description,
because the DSPy JSON adapter renders that model's schema — field descriptions
included — into the prompt. Guidance a paragraph away from the decision point
was being read once; guidance on the field is read where the field is filled.

The signature also now asks for a historical name to be resolved *forward*: a
works absorbed into a larger group is describable through the group that runs it
today, and "merged into X" is an answer where "unknown" is not.

**What this bought, measured: less than hoped, and unstably.** See
[Measured behaviour (2026-08-02)](#measured-behaviour-2026-08-02-deepseek-reasoner)
below before relying on any of it.

### This is an assertion table, not a register

Nothing here is written back to `company_investigated2celex_id`. `fk_lei = 'NA'`
on the roster keeps meaning "GLEIF has no such company"; `lei` here means "a
model recalled one". Promoting the second into the first would make a
recollection indistinguishable from a register match, which is exactly the
distinction the rest of this pipeline is built to preserve — hence
`llm_model_id NOT NULL`: a row is only interpretable together with its author.

To compare the two:

```sql
SELECT r.company_name, r.fk_lei AS gleif, l.lei AS asserted, l.llm_model_id
FROM celex_facts.company_investigated2lei2corpidx2hr_vw r
JOIN celex_facts.llm_index_companies l USING (company_name)
WHERE r.fk_lei = 'NA' AND l.lei <> 'NA';
```

### Which model answers

The corp index has its own LLM profile, configured independently of the UI
planner's. `config.llm_factory` resolves both; the provider supplies the
endpoint and credential, so neither is repeated in settings.

| Setting | Default | Why this stage differs |
|---|---|---|
| `CORP_INDEX_LLM_PROVIDER` | `deepseek` | Broad world knowledge, not the local coding model |
| `CORP_INDEX_LLM_MODEL_ID` | `deepseek-reasoner` | This stage is recall-bound, not faithfulness-bound |
| `CORP_INDEX_LLM_THINKING_MODE` | `enabled` | The global `LLM_THINKING_MODE` stays `disabled` because extraction is a faithfulness task; this is the opposite case, and it opts in without moving the knob under every other pipeline |
| `CORP_INDEX_LLM_CACHE` | `false` | DSPy's LM cache is keyed by the request, so with it on a `--refresh` over unchanged prompts replays the stored answer and the run reports as renewed a set of rows it never re-asked |

Thinking mode and caching became per-profile on 2026-08-02 for the same reason
the model id already was: the two consumers want opposite answers. The UI
planner wants a fast non-reasoning reply and a cache that makes a repeated
question free. A profile that names no thinking mode follows the global
`LLM_THINKING_MODE`; a misspelled one raises rather than falling back to the
global default unseen.

They were one setting until 2026-08-01, which meant repointing this batch at a
cheaper model silently repointed the `oi_dspy` UI server too — and `oi_dspy`
built its cloud LM by importing `create_deepseek_lm` out of
`pipelines.corp_index`, a UI concern depending on a batch pipeline. Both are
gone: `create_corp_index_lm()` reads the `corp_index` profile, the UI reads
`ui_planner_profile()`, and each stamps its own answers.

Because the model is what a stored row is attributable to, `llm_model_id` is
taken from the *resolved profile* rather than from any shared setting, and the
batch reports the module's own id rather than re-reading configuration.

### Throughput: the model decides, not the loop

There is no throttle (`--sleep-seconds` defaults to `0.0`). What costs time is
the model, and the difference between model ids is the whole story — measured
2026-08-01, one call per company, cache off:

| model | per call | completion tokens | of which reasoning |
|---|---|---|---|
| `deepseek-reasoner` (default since 2026-08-02) | 94–192s | — | the point, not waste |
| `deepseek-v4-pro` / `deepseek-v4-flash` | 20–46s | 2,000–5,100 | ~97%, discarded |
| `deepseek-chat` | ~2.3s | — | none |

The `v4-*` models reason whatever they are told. Probed arms, one live call
each: `thinking={"type":"disabled"}` (what `config/llm_thinking.py` sends)
48.5s/19,518 chars of `reasoning_content`; nothing sent 40.7s/15,695;
`reasoning_effort="none"` 66.2s/24,842; `extra_body chat_template_kwargs`
83.1s/32,352; `extra_body={"thinking": false}` is **rejected** by the API. The
answer is ~430 characters in every arm. LiteLLM does forward the directive
(confirmed on `lm.kwargs`) and the API accepts it — accepted is not honoured.

Because the work is latency-bound rather than compute-bound, `--workers`
(default 8) profiles that many companies at once. Only the LLM call is
threaded: every write, counter and log line stays on the main thread, so there
is no lock and the database still sees one caller. `--workers 1` restores the
serial, roster-ordered run. Submission is bounded to
`workers * IN_FLIGHT_MULTIPLIER` at a time and topped up per delivered result,
so an error-streak stop abandons the queue instead of paying for it — cancelling
a future the pool has already started is impossible, and queueing all ~1,700 up
front would have made the stop meaningless.

Combined effect on the ~1,700-company backlog: ~16 hours serial on `v4-*`,
roughly 2 hours at 8 workers, about 8 minutes on `deepseek-chat` at 8 workers,
and **5.5–11 hours at 8 workers on the `deepseek-reasoner` default**. That last
number is the one to weigh against the yield below before committing to a full
refresh.

### Resume and cost

A full pass is ~1,700 cloud calls. Stored companies are anti-joined out, so an
interrupted run continues rather than restarts and a completed one is a no-op;
`--refresh` re-asks and overwrites **every column except `company_name` and
`created_at`** — including the ones the new answer left at `'NA'`, so a second
pass states the model's whole current answer rather than the union of it with a
stale one, and a value that disappears is visible instead of preserved by
omission. Use it after changing `CORP_INDEX_LLM_MODEL_ID`, or to fill columns
added since the last pass.

`ensure_tables()` issues `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for every
profile column at startup, driven off the ORM types. `create_all` stops at
`CREATE TABLE IF NOT EXISTS`, so without it a column added after the first
deployment never arrives and the next upsert fails on an unknown column. A
`--refresh` is therefore all a new field needs.

The set of refreshable columns is pinned in a test against
`LlmCompanyIndexProfile.model_fields`: a field present in one and absent from
the other is a silent failure — the row is written, the batch reports success,
and that one column keeps its first answer forever.

A failed call writes **nothing** — the company is not
marked "asked and unknown", it simply returns on the next run. This is the
contract the OpenCorporates pass had to be repaired to honour: writing `'NA'`
for a question that was never answered closes a row permanently. A streak of
`--max-consecutive-errors` failures (default 5) stops the run, since that shape
is an expired key or an outage rather than an awkward company.

### Measured behaviour (2026-08-01, `deepseek-v4-pro`)

- The descriptive fields are the strongest result, and the ones nothing else can
  supply: `90 Tour Italia SpA` came back as *Tour operator and travel agency
  services*, market Italy, **dissolved**; `FRUBO` as a Dutch *Vereniging*
  wholesaling imported southern fruit.
- The **identifiers are weak through the structured signature**. The manual
  DeepSeek sessions recorded above (`A. C. Nielsen Italia` → BRIS `1513375/1996`,
  `Brasserie de Wiltz` → EUID `8434-4121-8815-0614`) do **not** reproduce
  through it: both answer `NA`, and Wiltz is named `S.A.` rather than
  `S.E.C.S.`. Both are pinned as `@unittest.expectedFailure` in
  [test_corp_index_integration.py](../src/pipelines/corp_index/tests/test_corp_index_integration.py)
  so the day a model does recall them is visible rather than silent.
- `NA` is preferred over invention: a Dutch trade association is not given a
  SIREN. That is the failure mode worth guarding, since a fabricated identifier
  is indistinguishable from a recalled one once it is in the table.

### Measured behaviour (2026-08-02, `deepseek-reasoner`)

The split rules, the reasoning model, thinking on and cache off were all
introduced together to close the gap between this table and what a DeepSeek
*chat* session answers for the same company. Two reference cases:

| Company | What the chat UI returns |
|---|---|
| `Coöperatieve Zuivelfabriek Zuidelijk Westerkwartier` (`acfa38ec-…`) | Founded 1918 in Marum, Groningen; part of DOMO by 1982; today a FrieslandCampina site and the only Edammer plant in the Netherlands. Stored as `life_cycle_status = 'NA'` |
| `Ets. Abel Falisse` (`5a6d6a22-…`) | CBE enterprise number `BE 0402.398.659`; founded 23 October 1948; SA/NV; HQ Rue de la Chaudronnerie 8, 4340 Awans |

Three live calls on `Ets. Abel Falisse` through the structured signature, same
prompt, same model, cache off, over about an hour:

| field | stored (`deepseek-chat`) | call 1 | call 2 | call 3 |
|---|---|---|---|---|
| `canonical_name` | Ets. Abel Falisse | Établissements Abel Falisse | *(same)* | *(same)* |
| `geo_market` | NA | Belgium (Liège area) | *(same)* | *(same)* |
| `legal_form` | NA | **NA** | SA/NV (probable; …) | SA |
| `life_cycle_status` | NA | **NA** | not confirmed; probably inactive or absorbed | acquired |
| `products_services` | NA | **NA** | likely a Belgian trading or manufacturing concern | steel panel radiators, heating equipment |
| `established_since` | *(column absent)* | **NA** | **NA** | c. 1920 |
| every identifier | NA | **NA** | **NA** | **NA** |

Three conclusions, and they do not all point the same way.

**The identifiers never move.** `NA` in all three calls, including the new
`generic_corp_id` / `generic_corp_registrar` pair. No CBE number. The chat
session says in its own words that it is reading *"the search results"* and that
they do not provide a company id — so those answers are **web-search grounded**,
not recalled. A `dspy.ReAct` search leg or a search-enabled endpoint is the only
thing that closes this half. Prompt work on this signature has now been tried
twice; it should not be tried a third time for the identifiers before a search
leg exists. Pinned as `@unittest.expectedFailure` in
`TestLlmCompanyIndexRefreshIntegration`.

**The descriptive fields are unstable in content, not merely in fill rate.**
This is worse than the fields staying `NA`. Call 2's *"likely a Belgian trading
or manufacturing concern"* announces its own uncertainty; call 3's *"acquired"*
and *"steel panel radiators, heating equipment"* do not, and are a different
company profile. Whichever call happens to run is what lands in the table, and
nothing downstream distinguishes call 3's answer from a recalled fact. So a
fill-rate rise measured over the table is **not** evidence of better answers,
and single calls prove nothing in either direction. Characterized in
`test_descriptive_answers_are_hedged_not_recalled` so a change is visible.

**`established_since` disagrees with the reference.** Call 3 answers `c. 1920`
where the chat session says 1948. The expected-failure test on that field is
currently passing-as-expected for the wrong reason — the field is populated,
with a value that contradicts the reference.

Two further findings from the same runs:

- **`country` drifted format.** The stored row held the ISO code `BE`; the
  reasoning model answers `Belgium`. Nothing in the field description asks for
  one form over the other, so a consumer joining this column on ISO codes
  silently loses every refreshed row. Either pin the description to ISO 3166-1
  alpha-2 or normalize at the boundary; the integration test currently accepts
  both spellings and says why.
- **Upsert semantics hold across repeated refreshes.** Three real refreshes of
  the same row: still exactly one row for the name, `id` and `created_at` held,
  `updated_at` moving each time. That is the property `--refresh` depends on.

**Nothing here is measured against ground truth.** There is no gold set and no
metric for this table, so every statement above is a characterization of
behaviour, not of quality. Compiling the signature's instructions with MIPROv2
or GEPA — which is what DSPy is for, and these instructions are still
hand-written — needs ~40 labelled companies and a per-field metric where a wrong
identifier is penalized far harder than a missing descriptive field. Until that
exists, treat every prompt change here as unvalidated, including this one.

### Retrying the profiles that answered nothing (`--retry-unknown` / `--use-address`)

A row whose `life_cycle_status` **and** `established_since` are both unknown cost
a cloud call and told the fact pipeline nothing. `--retry-unknown` selects
exactly those — the inverse of the resume anti-join, `EXISTS` rather than
`NOT EXISTS`. The predicate coalesces, because for once `NULL` and `'NA'` mean
the same thing here: `NULL` is "written before the column existed" (1,550 rows
on 2026-08-02), `'NA'` is "asked and did not know" (11). Matching `'NA'` alone
would retry 11 rows where **680** are unknown.

`--use-address` changes the *question* rather than the target set. The roster
splits the address across two columns — `address` holds what the decision
printed, usually with no country, and `iso_country_code` holds the country — so
the model was being handed a street and a two-letter code in separate fields,
one of them under an instruction to use it sparingly. With the switch, the two
are composed into one postal address which goes into both the question and the
hint: `Ets. Abel Falisse, Av. Emile Digneffe 26, 4000 Liège, **Belgium**`. Of
the 680 unknown rows, 460 have an address; the rest are selected but unreachable
by the switch and simply get an ordinary re-ask.

An address that already ends in a country keeps it rather than acquiring a
second. That is not cosmetic: the roster holds rows such as
`iso_country_code = 'DE'` against `111/113, Cours Berriat, F-38019 Grenoble,
Cedex 1, France`, and the address is the more specific claim
(`tools.country_normalizer.ends_with_country` is the guard).

The two are deliberately **not** one switch. Only running the same rows with the
address off and then on tells you whether it moved anything, and this table has
no gold set — the composed question is a stronger prompt, so a fill-rate rise is
equally consistent with the model inventing from the address it was just handed.

The composed question never reaches the stored key: `company_name` is taken from
the target, so a folded address cannot fork the table.

### Running

```bash
# Dry run: the companies that would be profiled (no LLM calls, no writes):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --dry-run

# Smallest real run:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --limit 3

# Serial, roster-ordered (what to use when reading the log as it happens, or
# when the provider starts rate-limiting):
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --workers 1 --limit 3

# More overlap. Worth raising on the reasoning default, where each call is
# 94-192s; pointless past the provider's concurrency limit:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --workers 16

# One country at a time -- the cheapest way to judge answer quality before
# committing to the whole backlog:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --iso-country-code DE --limit 25

# The full backlog (~1,700 calls; re-run to resume after an interruption):
PYTHONPATH=src .venv/bin/python src/batch/Main_LLM_corp_index_searcher.py

# Re-ask everything under a different model, overwriting the stored profiles:
CORP_INDEX_LLM_MODEL_ID=deepseek-v4-flash PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --refresh

# Backfill columns added after an earlier pass (established_since, succession,
# generic_corp_id, generic_corp_registrar): ensure_tables() adds them, --refresh
# re-asks each stored company and overwrites the whole row. Read a slice before
# committing to ~1,700 reasoning-model calls:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --refresh --limit 25

# The rows that answered nothing, and the questions --use-address composes for
# them. Free -- no calls, no writes -- and the only way to eyeball the
# composition before spending reasoning-model time on it:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py \
    --retry-unknown --use-address --dry-run

# The A/B those two flags exist for: the same rows, address off, then on:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --retry-unknown --limit 25
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py \
    --retry-unknown --use-address --limit 25

# Every roster company, not only the unresolved ones -- worthwhile for the
# descriptive fields, which no register supplies:
PYTHONPATH=src .venv/bin/python \
    src/batch/Main_LLM_corp_index_searcher.py --all-markers --dry-run
```

What the descriptive fields actually closed, after a refresh:

```sql
SELECT count(*)                                          AS profiled,
       count(*) FILTER (WHERE life_cycle_status <> 'NA') AS with_status,
       count(*) FILTER (WHERE succession        <> 'NA') AS with_successor,
       count(*) FILTER (WHERE established_since <> 'NA') AS with_founding,
       count(*) FILTER (WHERE generic_corp_id   <> 'NA') AS with_generic_id
FROM celex_facts.llm_index_companies;
```

Read that as a change signal, not a quality signal — see the 2026-08-02
measurements above for why a rise in it proves nothing on its own.

Sveriges Angfartys Assurans Forening	Barlastgatan 2, S-4400 40 Göteborg, Sweden --> LEI 549300YPYVCF0JNAQ062; 557206-5265 (Swedish Corporate ID Number); **Life Cycle Status:** active


LLM company index result: {'marker': 'NA', 'iso_country_code': None, 'targets': 1561, 'processed': 1561, 'failed': 0, 'stopped_on_error': False, 'llm_model_id': 'deepseek-chat'}
