#ingestion
# Design — Source Document Decision

`batch.Main_decide_source_document` resolves the decisions the downloader left `undecided`: decisions whose candidate PDFs came from URLs without a language code (`ec.europa.eu/competition/.../dec_docs/<case>/<id>.pdf`, db-comp), so the EN-first selector kept all of them and chose no source. After MinerU, this step reads every candidate's Markdown, merges documents the corpus already holds into their existing decision, promotes one source per Commission decision, splits rows that hold several decisions, and puts everything else on a review list.

Spec: [2026-11-01-candidate-source-decision-design](claude/plans/2026-11-01-candidate-source-decision-design.md). Plan: [2026-11-01-candidate-source-decision-implementation](claude/plans/2026-11-01-candidate-source-decision-implementation.md). Upstream: [Design Downloader](Design%20Downloader.md) (run book step 7), downstream: [Design Translator](Design%20Translator.md).

---

## 1. Rules decided by the user

- A valid source is a **Commission decision**: the title page says "COMMISSION DECISION of <date>" (FR "DÉCISION DE LA COMMISSION du", DE "BESCHLUSS/ENTSCHEIDUNG DER KOMMISSION vom"). Interim-measures and amending decisions count.
- Not valid: self-declared **summaries**, **letters** (closing salutation, no decision title), and a **combined** multi-case decision when the single-case decisions of the same date exist beside it (`dbcomp-2628` for AT.40413/40414/40420/40422/40424). A multi-case decision of a joined proceeding (DSD with 16 cases, Van den Bergh, Nintendo) is valid.
- The **document's own date** counts; the registry date is corrected from it (`DEC-AT34174-20120611` holds the interim-measures decision of 11 June 1992).
- One manifest row holding **several Commission decisions** (per case, addressee or cartel) yields one decision each.
- Documents the corpus already holds are **merged** into their existing decision.
- Among versions of one decision: English first, then `ec.europa.eu` before `eur-lex` before `db-comp`, then the longest. A non-English source goes to the translator.
- A case can have several decisions: case links live in `competition_case_decision (case_id, decision_id)`; split decisions get the row's links.

## 2. Evidence per candidate (`pipelines/decide/evidence.py`)

The Markdown is stripped of images, HTML and `<small>` footnote blocks (MinerU puts footnotes after the operative part of long decisions).

| Field | How |
|---|---|
| `kind` | summary marker -> `summary`; decision title -> `decision` (or `combined` when the title names several cases); closing salutation without title -> `letter`; else `unknown` |
| `title_key` | case numbers (`AT.`, `IV/`, `COMP/E-1/`, "Affaire/Case/Sache nnnnn"), subject after the case number, document date (numeric, Roman month `5/X/2005`, written EN/FR/DE) |
| `operative` | "HAS ADOPTED THIS DECISION", "A ADOPTÉ/A ARRÊTÉ LA PRÉSENTE DÉCISION", "HAT FOLGENDEN BESCHLUSS ERLASSEN", "Done at Brussels" ... in the last `DECIDE_ENDING_FRACTION` |
| `vocabulary` | distinct words of six or more letters |
| `language` | `detect_markdown_language` (not the URL) |

## 3. Identity of a decision

Two documents are the same Commission decision when case numbers and date are equal **and** either their (known) languages differ (language versions) or the subject is equal and the vocabulary Jaccard overlap is at least `DECIDE_SAME_TEXT_MIN_OVERLAP` (0.9). Length is not identity: Commission vs Official Journal versions of one decision differ by 0.85-1.38 in length but overlap 0.95-1.00, while the two Swiss Franc decisions of 21.10.2014 (same case, subject and date) overlap 0.83.

## 4. Rule ladder (`pipelines/decide/ladder.py`)

1. **Known**: a candidate whose sha256 equals an existing source, or whose identity matches one, is re-pointed to that decision; mirrors and versions of a known candidate follow it.
2. **Valid**: `decision`/`combined` with an operative ending and a title key; covered combined decisions are dropped.
3. **Group** by identity; order inside a group as in section 1.
4. One group -> **promote**. Several -> **split**: the first group matching the row's case and date keeps the row's id, every other group gets `DEC-<case>-<yyyymmdd>` (`_2` on collision); no matching group -> review.
5. Nothing valid but known documents -> **merge** into the existing decision whose case links match the row (leftover summaries/letters follow); otherwise **review** (`letters only`, `summaries only`, `no operative commission decision`, `merged documents span other cases only`, `several decisions, none matches the row`).

## 5. Apply (`pipelines/rag/pgvector/source_decision_repository.py`)

One transaction per manifest decision, the master flush inside it:

- re-points (all verdicts) via `repoint_document` (shared with `Main_dedupe_registry`);
- promote: winner `is_source = true`, decision `selected`, `language_init`, `decision_date` from the document; a new master entry (downloader shape, `txt_md_file` = the candidate's `candidates/mineru/<id>.md`);
- split: new `decision_registry` row (`meta.split_from`), case links copied, group documents moved;
- merge: every remaining row of the manifest decision and its case links move to the owner, then the decision row is deleted;
- review: reason appended to `decision_registry.review_reasons` once.

A failure rolls back that decision (and removes the master entries it added); the run continues. The downloader's decision upsert keeps `selection_status`, `language_init` and `decision_date` of a decision resolved to `selected` (ISSUES 20261027).

## 6. Usage

```bash
# dry run: plans, log summary, CSV report under settings.REPORT_PATH
.venv/bin/python -m batch.Main_decide_source_document

# write
.venv/bin/python -m batch.Main_decide_source_document --apply

# one decision
.venv/bin/python -m batch.Main_decide_source_document --ids DEC-AT40135-20190516
```

Run it after MinerU and before the translator; run it again after any downloader run (re-created ghost rows are merged again). The report has one row per candidate: decision, document, origin, sha256, language, kind, case numbers, document date, subject, operative, length, rule outcome, verdict, target decision.

Settings: `DECIDE_TITLE_REGION_CHARS` (6000), `DECIDE_ENDING_FRACTION` (0.25), `DECIDE_LETTER_TAIL_CHARS` (1500), `DECIDE_SAME_TEXT_MIN_OVERLAP` (0.9).

## 7. First run (2026-11-01)

147 undecided decisions, 306 candidates: **merge 69** (into 73 existing decisions), **promote 45** (6 with a French source), **split 2** (`DEC-AT38589-20160629_2`, `DEC-AT40135-20190516_2`), **review 31** (17 letters only, 11 no operative Commission decision, 2 documents of another case, 1 summaries only). Applied without failure; master backup `master_doc_descriptors.json.bak-20261101-114011`; report `celex_docs/reports/decide_source_document_20261101-114011.csv`. Before applying, every plan's SQL was executed against the live database inside a rolled-back transaction.

## 8. Known limitations

- Date and marker patterns cover EN/FR/DE; other languages are mostly summaries and fall to review.
- A redacted title date (`39406_1901_3`: "ENTSCHEIDUNG DER KOMMISSION vom" without a date) is `unknown`.
- Letters are never sources; whether closure/comfort letters should be is open (ISSUES 20261101).
- `review_reasons` are overwritten by a downloader re-run; the next run of this step re-appends them.
- Legacy findings left unchanged: `AT_40414_8465464_1500_3` uses the combined decision as source; `DEC-36623-20051005` vs `DEC-AT36623-20051005`.
