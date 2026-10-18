# Design: Fact-to-Book Reconciliation

Reconciliation compares every fact the DSPy extractors derive from an EU Commission decision
with the human-authored analysis of the same case in Russo, Schinkel, Günster & Carree (2010).
The book is a **second, independent source**, not a ground truth: where it confirms a fact, the
fact gains a corroborating lineage link; where it contradicts one, the disagreement is recorded
for review; where it is silent, nothing is claimed either way.

This document describes the system **as it stands after the 2026-09-17/18 rework** and the
results of the two runs that followed. It is the current-state reference.
[Design Russo-Schinkel-Book vs Facts Reconciliation](<Design Russo-Schinkel-Book vs Facts Reconciliation.md>)
keeps the forensic history — findings F-1 to F-7, issues I-1 to I-9 and their dated status —
and remains the place to look for *why* a design decision was taken.
The root-cause analysis behind the rework is
[book-cross-check-root-causes-20260917](claude/plans/book-cross-check-root-causes-20260917.md).

| Component | File |
| --- | --- |
| Batch driver | [src/batch/Main_cross_check_facts_vs_book.py](../src/batch/Main_cross_check_facts_vs_book.py) |
| DSPy module | [src/pipelines/agent_dspy/book_cross_check_module.py](../src/pipelines/agent_dspy/book_cross_check_module.py) |
| Deterministic taxonomy | [src/pipelines/agent_dspy/book_taxonomy.py](../src/pipelines/agent_dspy/book_taxonomy.py) |
| Shared judge + quote gate | [src/pipelines/agent_dspy/evidence_support.py](../src/pipelines/agent_dspy/evidence_support.py) |
| Per-fact ledger DAO | [src/pipelines/rag/pgvector/fact2book_check_repository.py](../src/pipelines/rag/pgvector/fact2book_check_repository.py) |
| Book corpus DAO | [src/pipelines/rag/pgvector/book_chunk_repository.py](../src/pipelines/rag/pgvector/book_chunk_repository.py) |
| Migrations | [src/pipelines/rag/pgvector/migrate_fact2book_checks.sql](../src/pipelines/rag/pgvector/migrate_fact2book_checks.sql) |
| Book ingestion | [src/batch/Main_ingest_book2pgvector.py](../src/batch/Main_ingest_book2pgvector.py), see [Design Book](<Design Book.md>) |

Related: [Design Book](<Design Book.md>) (book ingestion and `HybridBookSearch`),
[Design Fact Verifier](<Design Fact Verifier.md>) (the document-chunk verifier this deliberately
does not write to), [Design fact_source_registry](<Design fact_source_registry.md>) (the lineage
tables), [Report Fact 2 Book Reconcilation](<Report  Fact 2 Book Reconcilation.md>) (ad-hoc
reporting queries).

---

## 1. What a reconciliation decides

One fact judged against one book case yields exactly one of three routing decisions, stored in
`fact2book_checks.action`:

| action         | Meaning                                                            | Side effects                                                                 |
| -------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| `corroborated` | The book states or files the case in a way that confirms the fact. | Ledger row **and** a `corroborated_by` lineage link to a `book_case` source. |
| `mismatch`     | The book asserts something incompatible with the fact.             | Ledger row only; any earlier lineage link for the fact is retracted.         |
| `skipped`      | The book neither confirms nor denies — the common case.            | Ledger row only; earlier lineage links retracted.                            |

The ledger is **complete**, not contradiction-only: a skipped fact is recorded with the judge's
label and reason, which is what makes "the book is silent on this" a queryable state rather than
an absence of data.

Three things are deliberately **not** written: `fact_evidence`, `facts.verification_status`, and
any edit to the fact itself. Those belong to the document-chunk verifier, whose `upsert_evidence`
is delete-then-insert; a book write there would clobber the document verdict. Whether book
corroboration should ever surface as second-rank evidence is an open product question in
ISSUES.md.

## 2. Design

### 2.1 Case resolution

The batch iterates `celex_facts.celex_case_id`, which stores every case number in the single
normalised form `IV/<digits>` — including post-1999 cases the book writes as `COMP/nn.nnn`.
`case_id_variants` therefore generates surface variants along two axes before any lookup:

- **punctuation**: the thousands dot is added and removed (`IV/26699` ↔ `IV/26.699`);
- **era prefix**: every prefix in `BOOK_CASE_ID_PREFIXES` is substituted (`IV/` ↔ `COMP/`).

The first variant that the book corpus contains becomes the *book surface* used for every
retrieval and for the case-reference check; the canonical stored `case_id` is what gets
persisted. Without the prefix axis every COMP-era case read as absent from the book — 56 cases
and roughly 3,100 facts.

### 2.2 Reference type sets the validation policy

`classify_book_reference` reads up to `BOOK_CROSSCHECK_REFERENCE_PATH_LIMIT` distinct section
paths naming the case and classifies how the book references it:

| Reference type | Detection | Policy |
| --- | --- | --- |
| `landmark` | Any path contains `F Economic landmark case` | The book analyses the case in depth; every fact may be corroborated **or** contradicted. |
| `simple` | Default | The case is cited as an example. Any fact may be corroborated, but only infringement facts may be contradicted — a passing mention is silent about most facts rather than denying them. |
| `index` | Every path starts with `Annex` / `Index` | Back-matter listing only; no facts judged, no rows written. |

### 2.3 Evidence selection: two pools and a window

A chapter covers several cases, so the pool a fact is judged against decides whether the verdict
is about *this* case at all. Two pools are built once per case:

- **case pool** — chunks whose text literally contains the book surface (`keyword_search`);
- **chapter pool** — every chunk of the chapters those hits belong to (`fetch_chapters_for_keyword`).

Facts named by `BOOK_CASE_SPECIFIC_FACT_PREFIXES` (decision date, HeidelTime periods, cartel
period, fines, companies, commission actions) identify one specific case, so they see the **case
pool only**. Every other fact sees the case pool first and then the chapter pool, deduplicated by
chunk id, so the reranker can still reach analytical prose that does not repeat the case number.

The reranker scores the pool against the fact's latest extraction question (falling back to
`name: value`) and takes the top chunk. That chunk is **both** the judged context and the cited
`book_chunk_id`: a corroboration requires a verbatim quote from the context, so the text the
judge reads and the row the citation points at must be the same.

Two guards protect case-specific facts:

1. **Windowing.** A chunk longer than `BOOK_CROSSCHECK_CONTEXT_CHARS` is truncated to that
   budget. For a case-specific fact the window is centred on the first occurrence of the book
   surface (`_window_around`) instead of taking the head, because a 12,000-character chunk
   truncated at 8,000 can cut the case number off entirely.
2. **Case-surface check.** If the judged context still does not name the case
   (case-insensitively, after surface folding), the fact is recorded `skipped` with the reason
   `no book chunk names the case for a case-specific fact` — **without an LLM call**. Judging it
   would compare this case's date against another case's date.

### 2.4 Two judges

| Judge | Implementation | Applies to |
| --- | --- | --- |
| **Taxonomy** (deterministic, no LLM) | `book_taxonomy.judge_taxonomy` | Classification facts (`BOOK_CLASSIFICATION_FACT_PREFIXES`) that have a rule |
| **Support** (LLM) | `EvidenceSupportSignature` via `judge_support`, shared with the document verifier | Everything else, and classification facts whose taxonomy verdict is inconclusive |

**Why a classification fact cannot be judged by prose.** The book files each case under a
numbered infringement chapter, so its section path *is* the book's verdict on the category. A
fact claiming a mutually exclusive type — a horizontal-cartel label on a case the book analyses
under "3 Abuse of dominance" — is a contradiction the prose judge cannot see, because that
chapter is simply silent on "horizontal". Equally, a boolean claim such as
`infringement_category_abuse_of_dominance: True` has no verbatim surface to quote, so the prose
judge returns `insufficient` even on a perfect match. The taxonomy check decides both directions
and cites the matching section path as its quote.

The rules: a label counts as claimed when it occurs anywhere in the value (lists, `;`-joined
values and legacy table-formatted values all resolve). `supports` when a claimed chapter or
section number is in the case's placement; `contradicts` when no claimed *chapter* is in the
placement **and** the book files the case under a decisive conduct chapter
(`BOOK_TAXONOMY_DECISIVE_CHAPTERS` = 2–6); otherwise inconclusive. Chapters 7 (Art. 106) and 8
(mergers) never contradict on their own, because an Art. 106 case is frequently also an abuse.
Placement is taken from the landmark paths when the case has any, otherwise from every path
naming it, so a case discussed in several chapters is judged against all of them.

An inconclusive taxonomy falls through to the support judge with contradiction disabled: prose
may still corroborate a classification fact, but only the deterministic rule may contradict one.

The support judge receives the fact statement, the required elements, the extraction question as
its rubric, and the **case reference** — text about a different case is `insufficient`, never
`contradicts`. A judge exception or unstructured output becomes a `skipped` verdict and never
aborts the batch.

### 2.5 Promotion rules

A `supports` verdict becomes a corroboration only when both hold:

- `support_score >= EVIDENCE_SUPPORT_MIN_SCORE` (0.75, the same bar the document verifier uses);
- the `evidence_quote` occurs in the judged context under `quote_in_text`.

`quote_in_text` folds the surface differences a PDF-to-markdown conversion introduces — NFKC
normalisation (ligatures), typographic quotes and dashes to ASCII, soft hyphens dropped, words
rejoined across a hyphenated line break, whitespace runs collapsed — and only then tests
containment. Word choice is not folded, so a paraphrase still fails. The point of the gate is
that a corroboration must be traceable to text a reader can find.

### 2.6 Persistence and the lineage invariant

**`celex_facts.fact2book_checks`** — the per-fact ledger, natural key `(fact_id, celex_id)`, one
verdict per fact per case, upserted so re-runs heal rather than duplicate. `book_chunk_id` is
deliberately *not* part of the key: the reranker re-picks the anchor on every run, and keying on
it produced duplicate rows. `action` is `NOT NULL` with a `CHECK` constraint; `explanation` is
`NOT NULL` and falls back from judge reason to quote to a generic per-action sentence.

**`celex_facts.fact2book_case_runs`** — the case-level run ledger, one row per
`(celex_id, case_id)`: `run_id`, `in_book`, `absence_reason` (`none` / `no_surface` /
`no_anchor`), `reference_type`, the four per-fact counters, `started_at` / `finished_at`. This
answers "was this case visited at all, and why did it produce no verdicts", which a per-fact
ledger structurally cannot, and it is what `--resume` reads.

**Lineage** — a corroboration registers a `book_case` source on the cited chunk and links the
fact with `relation_type='corroborated_by'`, `extraction_method='book_cross_check'`. Every other
written verdict retracts this module's links for that fact, and a corroboration retracts links to
any earlier (drifted) anchor. The retraction is scoped by relation, method and source type, so
other producers' provenance is untouched.

> **Invariant:** every book link this module holds points at the `book_chunk_id` of a currently
> `corroborated` ledger row, and no fact carries more than one. Verified after each run by the
> query in §5.

### 2.7 Running it

```bash
PYTHONPATH=src .venv/bin/python src/batch/Main_cross_check_facts_vs_book.py \
    --run-id book-20260917            # label for the case-run ledger (default: timestamp)
    [--resume]                        # skip cases already finished under that run id
    [--celex-id CELEX-31976D0353]     # single document
    [--case-id COMP/35.587]           # single case
    [--fact-prefix infringement_]     # repeatable fact filter
    [--skip-existing]                 # incremental top-up; do not re-judge existing rows
    [--dry-run]                       # list cases only
    [--no-thermal-gate]               # deliberate, watched run
```

- **`--dry-run` is cheap.** It lists the cases and returns before the LM, the cross-encoder or any
  repository beyond the case source is constructed — no model load, no DB probe, no LLM call.
- **Thermal gate.** The DGX health and temperature check runs before every case, the same guard
  `BatchJobRunner` applies to the ingestion batches. A sustained breach stops the run with exit
  code 2 and prints the exact resume command.
- **Restart.** After a crash or a thermal abort, re-invoke with the same `--run-id` plus
  `--resume`; everything already written stays committed and finished cases are skipped.
- **Counters** distinguish `skipped` (judged, recorded) from `already_checked` (left alone by
  `--skip-existing`, nothing written), and split `not_in_book` into `no_surface` (no spelling of
  the case id exists in the corpus) and `no_anchor` (a spelling exists but no chunk was
  retrievable).

### 2.8 Configuration (`src/config/settings.py`)

| Setting | Default | Role |
| --- | --- | --- |
| `EVIDENCE_SUPPORT_MIN_SCORE` | 0.75 | Corroboration threshold, shared with the document verifier |
| `BOOK_CROSSCHECK_CONTEXT_CHARS` | 8000 | Judge context budget (windowed for case-specific facts) |
| `BOOK_CROSSCHECK_CANDIDATE_LIMIT` | 80 | Cap on each candidate pool |
| `BOOK_CROSSCHECK_REFERENCE_PATH_LIMIT` | 200 | Section paths read to classify the reference |
| `BOOK_CASE_SPECIFIC_FACT_PREFIXES` | decision date, `heideltime_`, cartel period, fines, companies, case commission | Facts restricted to the case pool |
| `BOOK_CLASSIFICATION_FACT_PREFIXES` | `infringement_category`, `infringement_subcategory` | Facts routed to the taxonomy judge |
| `BOOK_INFRINGEMENT_FACT_PREFIXES` | `infringement` | Facts contradictable on a simple reference |
| `BOOK_LANDMARK_SECTION_MARKERS` | `F Economic landmark case` | Landmark detection (singular matches both spellings) |
| `BOOK_INDEX_PATH_PREFIXES` | `Annex`, `Index` | Back-matter detection |
| `BOOK_CASE_ID_PREFIXES` | `IV/`, `COMP/` | Era prefixes for surface variants |
| `BOOK_TAXONOMY_LABEL_NUMBERS` | label → chapter/section per fact | Taxonomy rules for label facts |
| `BOOK_TAXONOMY_BOOLEAN_CHAPTERS` | `abuse_of_dominance`: 3, `member_states`: 7 | Taxonomy rules for boolean facts |
| `BOOK_TAXONOMY_DECISIVE_CHAPTERS` | 2, 3, 4, 5, 6 | Chapters whose placement can contradict a label |
| `BATCH_LLM_PROVIDER` / `BATCH_LLM_MODEL_ID` | `ollama` / blank | Shared batch LM profile (`config.llm_factory.batch_profile`) |

---

## 3. Results

Two runs, both on the code described above:

| Run | Scope | Wall clock | Cases | Outcome |
| --- | --- | --- | --- | --- |
| `book-20260917` | Full corpus, every fact, overwrite | 2026-09-17 21:20 → 2026-09-18 21:22 (24 h) | 699 | 0 errors |
| `book-20260917-window` | 32 cases, case-specific facts only | 2026-09-18 21:26 → 22:08 (41 min) | 32 | 0 errors, 480 facts re-judged |

The second run repaired a regression the first one exposed: the 8,000-character head truncation
cut the case surface out of 12,000-character chunks, so 300 case-specific facts in 32 COMP-era
cases were skipped unjudged. With windowing in place none remain.

> **Subsequent change (2026-09-19).** After these runs, 126 classification facts across 80
> documents were deleted because the operative-article gate no longer holds for them (§7.1). The
> cascade removed 81 ledger rows and 183 lineage links, and classification mismatches fell from
> 105 to 92. Those 80 documents were then re-checked under run id `gate-delete-recheck-20260919`
> (78 case rows, since 2 of the 80 have no `celex_case_id` entry; 51 in the book; 3 h 40 min;
> 0 errors). **Current ledger: 22,107 rows — 3,077 corroborated, 140 mismatch, 18,890 skipped**,
> with 3,077 book links for 3,077 corroborated rows, no fact holding more than one link and no
> orphaned ledger row. The Art. 101-vs-102 audit (horizontal or vertical claims on cases the book
> files under abuse of dominance) falls from 11 to 4, and infringement facts judged move to
> 698 supports / 92 contradicts. The tables in §3.2 to §3.5 report the two original runs as they
> completed and therefore predate the deletion.

### 3.1 Coverage

| Stage | Count |
| --- | --- |
| `celex_case_id` rows (cases attempted) | 699 |
| Present in the book | **397** (46 landmark, 351 simple) |
| Absent — no case-id spelling in the corpus | 302 |
| Absent — spelling found but no chunk retrievable | 0 |
| Index/back-matter only (no facts judged) | 0 |
| Facts belonging to the 397 in-book documents | 22,188 |
| Ledger rows written | **22,188** |

Fact coverage of the in-book corpus is complete: every fact of every in-book document carries a
verdict. The ledger spans 396 distinct documents rather than 397 because `CELEX-32002D0758`
(`IV/36264`) is in the book but has no extracted facts at all.

### 3.2 Verdicts

| action | landmark | simple | total | share |
| --- | ---: | ---: | ---: | ---: |
| corroborated | 478 | 2,588 | **3,066** | 13.8 % |
| mismatch | 62 | 91 | **153** | 0.7 % |
| skipped | 2,048 | 16,921 | **18,969** | 85.5 % |

What the skips actually are:

| Judge label | Rows | Reading |
| --- | ---: | --- |
| `insufficient` | 13,902 | The book is silent on the fact — the expected majority |
| *(null)* | 4,212 | No verifiable value, or no case-scoped context; no LLM call made |
| `supports` | 464 | Judge supported but the quote failed the containment gate (see §4) |
| `contradicts` | 347 | Contradiction downgraded by policy (non-infringement fact on a simple reference) |
| `partially_supports` | 44 | Not promoted |

### 3.3 Corroborations

Most-corroborated fact names: `eu_commisssion_decision_date` 260,
`infringement_category_hor_vert_lic_joint` 246, `infringement_concept` 211,
`companies_investigated` 169, `human_index_title` 152, `market_oligopoly` 150, `exemption_case`
130, `market_geographical` 111, `cartel_period` 107, `market_outside_eu` 98.

Case-specific facts, the population most exposed to cross-case confusion, come out at 1,045
corroborated / 16 mismatch / 4,861 skipped.

### 3.4 Mismatches

| Fact | simple | landmark | total |
| --- | ---: | ---: | ---: |
| `infringement_category_hor_vert_lic_joint` | 56 | 9 | 65 |
| `infringement_subcategory_vertical_restraints` | 23 | 1 | 24 |
| `is_court_involved` | – | 20 | 20 |
| `infringement_subcategory_horizontal_restraints` | 12 | 4 | 16 |
| `eu_commisssion_decision_date` | – | 10 | 10 |
| `market_outside_eu` | – | 7 | 7 |
| `case_commissioner`, `market_oligopoly` | – | 2 each | 4 |
| others | – | 1 each | 7 |

By book chapter (rows judged, of which mismatches): Horizontal restrictions 9,930 (56), Vertical
restrictions 5,397 (32), Joint ventures 3,014 (27), Abuse of dominance 1,990 (23), Licensing
1,346 (13), Art. 106 decisions 346 (0), Mergers 165 (2).

Classification facts specifically: 686 judged — 439 corroborated, 105 mismatch, 142 skipped.

### 3.5 Change against the 2026-08-04 baseline

| Measure | 2026-08-04 | Now | Driver of the change |
| --- | ---: | ---: | --- |
| Cases judged | 341 | **397** | `COMP/` prefix variants |
| Ledger rows | 18,453 | **22,188** | More cases; complete fact coverage |
| Corroborated | 2,507 | **3,066** | Deterministic taxonomy; case-scoped pools; folded quotes |
| Mismatch | 222 | **153** | Taxonomy replaced the LLM category judge |
| Book lineage links | 3,939 | **3,066** | Retraction; now exactly one per corroborated row |
| Facts with >1 book link | 432 | **0** | Anchor-drift retraction |
| Landmark decision-date mismatches | 17 | **10** | Case pool + case reference |
| Infringement facts supports / contradicts | 497 / 197 | **700 / 105** | Taxonomy + operative-article gating |

---

## 4. Reading the results

**Low corroboration is the expected shape, not a defect.** 88 % of ledger rows come from *simple*
references, where the book names a case as a one-line example. A case mentioned once cannot
confirm fifty extracted facts. Landmark cases corroborate at 18.5 % (478 of 2,588 rows) against
13.2 % (2,588 of 19,600) for simple references, which is the expected gradient.

**The mismatch population is now dominated by genuine classification disagreement.** Before the
deterministic taxonomy landed, 197 of 222 mismatches were classification facts, most of them
judge or path artefacts — `license` under "4 Licensing" flagged as contradictory, or a
multi-chapter case judged only against the chapter of its earliest mention. Those are gone. What
remains (105 classification mismatches) is concentrated in horizontal/vertical claims on cases
the book files elsewhere, which is exactly the Art. 101-vs-Art. 102 confusion the extraction-side
fix targets.

**The quote gate is working; the earlier diagnosis of it was wrong.** 464 `supports` verdicts are
still skipped for failing containment, and the F-6 hypothesis was that whitespace and hyphenation
in MinerU output were to blame. Re-judging a sample of ten shows otherwise: six returned
`supports` with an **empty** quote — `case_summary`, `is_court_involved`, `market_turbulence` and
similar facts have no quotable surface, and the signature forbids `supports` without a quote —
two quoted long lists with OCR spelling differences and reordered entries, and two flipped to
`insufficient` on re-ask. Surface folding recovers the small remainder. The open item is the
judge's empty-quote behaviour, not the gate.

**`is_court_involved` is the one non-classification fact family producing many mismatches** (20,
all on landmark cases), which makes it the next candidate for a rubric review.

---

## 5. Verification queries

```sql
-- verdict distribution by reference type
SELECT action, reference_type, count(*)
FROM celex_facts.fact2book_checks GROUP BY 1, 2 ORDER BY 1, 2;

-- lineage invariant: links == corroborated rows, and no fact with more than one
SELECT (SELECT count(*) FROM celex_facts.fact_source_link l
          JOIN celex_facts.fact_source_registry sr ON sr.source_id = l.source_id
         WHERE sr.source_type = 'book_case'
           AND l.extraction_method = 'book_cross_check')            AS book_links,
       (SELECT count(*) FROM celex_facts.fact2book_checks
         WHERE action = 'corroborated')                              AS corroborated_rows;

-- coverage: which cases were visited, and why some produced nothing
SELECT run_id, in_book, absence_reason, count(*)
FROM celex_facts.fact2book_case_runs GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- mismatches for review, with the fact value and the cited chapter
SELECT c.celex_id, c.case_id, f.name, coalesce(f.value_md, f.value_json) AS value,
       c.book_path, c.explanation
FROM celex_facts.fact2book_checks c
JOIN celex_facts.facts f ON f.id = c.fact_id
WHERE c.action = 'mismatch'
ORDER BY c.celex_id, f.name;

-- supports verdicts held back by the quote gate
SELECT f.name, count(*)
FROM celex_facts.fact2book_checks c
JOIN celex_facts.facts f ON f.id = c.fact_id
WHERE c.action = 'skipped' AND c.support_label = 'supports'
  AND c.final_score >= 0.75
GROUP BY 1 ORDER BY 2 DESC;
```


## 6. Tests

| Suite | Covers |
| --- | --- |
| `agent_dspy/tests/test_book_cross_check_module.py` | Routing, case-id variants, reference policy, candidate pools, windowing, case-surface guard, taxonomy, lineage retraction, skip mode |
| `agent_dspy/tests/test_evidence_support.py` | Quote folding (whitespace, hyphenation, ligatures, punctuation), paraphrase rejection, case-reference forwarding |
| `agent_dspy/tests/test_book_taxonomy.py` | Deterministic taxonomy rules |
| `agent_dspy/tests/test_book_cross_check_integration.py` | Live run on IV/26.699 (Chiquita): abuse corroborated, horizontal claims contradicted |
| `batch/tests/test_cross_check_driver.py` | Dry run builds nothing expensive, counter separation, case-run ledger writes, `--resume`, thermal abort |
| `rag/pgvector/tests/test_fact2book_check_repository.py` | Both ledgers: upsert keys, column mapping, read filters |

## 7. Known limits and open items

Tracked in ISSUES.md; the ones that bear on how these results should be read:

1. **Classification facts whose gate no longer holds.** The extraction side now gates the
   category questions on `law_article_operative` (the articles the decision actually applies)
   rather than on every article mentioned anywhere. 126 stored facts across 80 documents no
   longer qualify, listed in `reports/facts_losing_gate_20260917.tsv`. They had to be deleted
   rather than re-extracted, because a condition that fails writes nothing at all — there is no
   "no longer applicable" state for a fact. **Done 2026-09-19**: 126 rows deleted in one guarded
   transaction, backups in `reports/backup_gate_delete_20260919/`. The cascade also removed the
   129 `fact_ingestion_audit_trail` rows recording those extractions, so that history now exists
   only in the backup.
2. **Empty-quote `supports`.** See §4; the judge's behaviour on facts with no quotable surface
   needs a decision (map to `partially_supports`, or a different rubric for boolean and summary
   facts).
3. **Book corroboration is not visible in `fact_evidence`.** The `corroborated_by` lineage link
   is currently the only record. Surfacing it as second-rank evidence needs a non-clobbering,
   multi-row evidence write.
4. **The document verifier still uses exact-substring containment.** Whether it adopts
   `quote_in_text` is undecided.
5. **Ten residual decision-date mismatches.** All now cite chunks that name the case; eight are
   year-level disagreements between the book's citation year and the decision date (e.g.
   `CELEX-31988D0087`, decided 1987-12-22, cited under 1988), two still quote a second decision's
   date from a chunk naming both cases.
