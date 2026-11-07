
Status: implemented 20261106. Plan: `docs/claude/plans/cartel-period-inheritance-20261106.md`.

## Problem

`cartel_period` (pipeline `heideltime_cartel_period`) reads the infringement
window from a decision's own HeidelTime events. A **follow-up decision** -- an
amending or re-adopting decision such as `DEC-AT39168-20110331`, which only
corrects the fine of the 2007 Fasteners decision -- does not restate the
conduct, so the LLM rightly answers null. The period is in the **original
decision of the same Commission case** (`39168_757_6`: `1977-01-15 : 2003-01-13`).

## Solution

A deterministic fallback leg in `HeideltimeCartelPeriodModule.forward()`:
when the event path yields no period (no conduct events, or a null range), the
injected `PeriodInheritor` (`src/pipelines/heideltime/period_inheritance.py`)
looks for a donor decision and returns its period. No LLM call.

```
heideltime_event digest -> conduct selection -> period LLM
                                                  | null
                                                  v
                         PeriodInheritor.find(celex_id)  -> donor period | NA
```

The decision's own text always wins: inheritance never overrides a period the
events produced.

## Rules

| # | Rule | Why |
|---|---|---|
| 1 | The target's own year comes from its **id** (`DEC-ATnnnnn-YYYYMMDD`, `CELEX-3YYYY...`); other id shapes do not inherit. | Stored decision dates are wrong exactly for this population: `human_index_decision_date` of 38238_249_1, 38589_4669_7 and 39914_8910_5 holds the *amending* decision's date (ISSUES #275), so "donor is older" by stored date rejects every genuine original. |
| 2 | The target must **cite an earlier Commission decision**, `C(YYYY) NNNN` with YYYY before its own year. | Identifies a follow-up. A later, separate decision of the same case (CSV `CELEX-31976D0593` vs `CELEX-31978D0732`) shares the case number but cites none, and must not inherit. |
| 3 | A donor shares a **5-digit case number**, is another document, has a parseable `START : END`, is not gated `case_commission_infringement = False`, and its conduct **ended no later than the latest cited year**. | An original decision post-dates the conduct it fines. |
| 4 | Choice: the donor whose **id year is a cited year**; else the **only** donor; else the **union** (earliest start, latest end). | EIRD: `DEC-AT39914-20160406` cites `C(2013) 8512` and has three same-case donors; the cited one is `DEC-AT39914-20131204`. |
| 5 | A period that was itself inherited (`fact_source_link.extraction_method = 'period_inheritance'`) is **never a donor**. | First live run 20261106: `DEC-AT38589-20160629`, inherited seconds earlier, became a second donor of its twin `DEC-AT38589-20160629_2` and forced a low-trust union. Filtered in `select_period_donor_rows`. |

### Case numbers

Taken from three sources and normalised to five digits (`39.168` -> `39168`):

- the celex_id: `39168_757_6`, `DEC-AT39168-...`, `AT_37990_...` (never `CELEX-3...`, whose digits are a year);
- `case_celex_id.case_id` (`IV/39168`, `AT.39914`) -- it misses `DEC-*` ids and holds prefix-only junk (`COMP/E`, `COMP/C`) that would join unrelated cases; junk carries no number and drops out;
- the `case_title` fact value (`'COMP/E-1/39.168 PO/ ...'`).

A document can hold several numbers (joined cases); any overlap matches.

## Output and provenance

- `value_json` keeps the normal shape `[{"date_range": "START : END"}]`, so
  `value_md` renders as the bare range and every reader (views, `fact_repository`,
  dspy schema hints) works unchanged. Extra keys would turn `value_md` into a table.
- `FactOutput.source = "period_inheritance"`.
- One `fact_source_link` per donor: `source_type='fact'` pointing at the donor's
  `cartel_period` row, `relation_type='derived_from'`,
  `extraction_method='period_inheritance'`. The evidence verifier routes a fact
  with fact lineage and no chunk evidence as **derived** and verifies it
  transitively from the donor, instead of searching the target's chunks for
  dates that are not there. The YAML predecessor edge (`heideltime_event`) is
  still written alongside.
- Trust rank: the `fact` source default (4) for a cited or single donor;
  `settings.PERIOD_INHERITANCE_UNION_TRUST_RANK` (default 2) for a union.

## Staleness across documents

An inherited copy goes stale when its donor is re-extracted. Two generic
rules keep it in step; both key on *cross-document* `fact` edges (a source
fact of another `celex_id`), which in the corpus are exactly the inheritance
edges:

1. **Re-extract when a source is newer.** `FactRagPipeline.process` skips an
   existing fact only while `FactRepository.select_newer_cross_document_sources`
   is empty. `upsert_fact` keeps a fact's id and moves its `created_at`, so a
   re-extracted donor is newer than the copy; the copy is then re-run with
   `force_override` (which also bypasses the module's own stored-value
   short-circuit). Same-document predecessors are not considered: the batch
   already orders those, and `--overwrite` cascades them.
2. **Drop donor edges the re-run did not link.** `FactIngestor._persist_lineage`
   calls `LineageRepository.retract_cross_document_fact_links` after every
   write, keeping only the sources just linked. `link_fact_to_source` only
   upserts, so without this a re-run that takes its own period, another donor
   or NA would keep the old `derived_from` edge, and the verifier would keep
   treating the fact as derived from it.

So a plain batch run (no `--overwrite`) refreshes every copy whose donor
changed since the copy was written. A donor re-extracted later in the same
run as its copy is picked up by the next run.

## Configuration (`src/config/settings.py`)

| Setting | Default | Meaning |
|---|---|---|
| `PERIOD_INHERITANCE_CASE_TITLE_FACT` | `case_title` | Fact whose value carries the case number |
| `PERIOD_INHERITANCE_GATE_FACT` | `case_commission_infringement` | Donors gated False are skipped |
| `PERIOD_INHERITANCE_UNION_TRUST_RANK` | `2` | Lineage trust of a unioned period |

## Data access

| Method | Reads |
|---|---|
| `FactRepository.select_case_refs(celex_id, title_fact)` | target's case ids + title value |
| `FactRepository.select_period_donor_rows(fact, gate, title)` | every non-NA period fact with gate value and case refs (one query, ~500 rows) |
| `ChunkRepository.select_regex_matches(celex_id, regex)` | captured years of `C(YYYY) NNNN` citations (`period_inheritance.CITATION_REGEX`) |

The cheap target-side checks (id year, citation, case number) run first; the
corpus-wide donor read happens only for a genuine follow-up.

## Result (dry run 20261106, read-only)

5 of 102 NA decisions not gated False inherit:

| Target | Donor | Period | Basis |
|---|---|---|---|
| DEC-AT38238-20170616 (Raw Tobacco Spain) | 38238_249_1 | 1996-03-13 : 2001-10-03 | single_sibling |
| DEC-AT38589-20160629 (Heat Stabilisers) | 38589_4669_7 | 1987-02-24 : 2000-09-26 | single_sibling |
| DEC-AT38589-20160629_2 | 38589_4669_7 | 1987-02-24 : 2000-09-26 | single_sibling |
| DEC-AT39168-20110331 (Fasteners) | 39168_757_6 | 1977-01-15 : 2003-01-13 | single_sibling |
| DEC-AT39914-20160406 (EIRD) | DEC-AT39914-20131204 | 2005-09-29 : 2008-05-30 | cited_decision |

## Applying it

The batch skips decisions that already hold a `cartel_period` row (NA rows
included), so existing NA rows are refreshed with a targeted overwrite run
(re-runs the period LLM for those decisions, then falls back):

```
.venv/bin/python -m batch.Main_ingest_generic_facts --filter-fact cartel_period \
    --celex-list-file reports/cartel_period_inheritance_targets_20261106.txt --overwrite
```

New follow-up decisions inherit on their first run, provided the donor's period
already exists; a donor processed later is picked up by the same targeted re-run.
Once inherited, copies follow donor re-extractions on their own (see Staleness).

## Limitations

- A donor fact cannot be deleted while a copy links to it: deleting the facts
  row cascades to its `fact` registry row, which `fact_source_link` still
  references (no cascade there). Re-run or delete the copy first.
- Follow-ups without `C(YYYY) NNNN` citations (pre-1990s decisions) never inherit.
- Master ids (`39168_757_6`) carry no year, so they cannot be follow-up targets;
  in the corpus they are the originals.
- Per-undertaking periods are not modelled: a re-adoption addressed to one
  party inherits the global period of the original decision.

## Tests

- `src/pipelines/heideltime/tests/test_period_inheritance.py` -- key parsing,
  citations, rules (Fasteners, CSV reject, EIRD cited pick, union, gate False,
  junk keys), `PeriodInheritor` with fake stores.
- `src/pipelines/heideltime/tests/test_heideltime_bounded_prompts.py` --
  `TestCartelPeriodInheritanceFallback`: fallback on null, lineage edge, union
  trust rank, own period wins.
- `src/pipelines/agent_dspy/tests/test_universal_fact_extraction_pipeline.py` --
  `TestCrossDocumentStaleness`: fresh copy skipped, newer donor forces a re-run.
- `src/tools/tests/test_PipelineFactIngestor.py` --
  `TestPersistLineageRetractsCrossDocumentLinks`.
- `src/pipelines/rag/pgvector/tests/test_lineage_repository.py` --
  `TestCrossDocumentFactLinksIntegration` (live DB): newer donor detected,
  newer same-document predecessor ignored, retraction scope.
