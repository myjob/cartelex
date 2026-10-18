
The Russo book use almost always the latest infringement article and only in the decision text the orginal number.
Reconciliation between facts retrieved with the book is therefore difficult

Example: Aubuse of Dominance

| count_82 | count_86 | count_102 | total_matching_rows |     |
| -------- | -------- | --------- | ------------------- | --- |
| 4        | 3        | 41        | 44                  |     |


```
SELECT
COUNT(*) FILTER (WHERE "document" LIKE '% 82 %') AS count_82,
COUNT(*) FILTER (WHERE "document" LIKE '% 86 %') AS count_86,
COUNT(*) FILTER (WHERE "document" LIKE '% 102 %') AS count_102,
COUNT(*) AS total_matching_rows
FROM celex_facts.pg_embedding_book_qwen
WHERE "document" LIKE '% 82 %'
OR "document" LIKE '% 86 %'
OR "document" LIKE '% 102 %';
```


This is a clean, concrete instance of the exact bug you've been chasing in the entailment/category step.

**Walking the condition against this document:**

- `eu_commisssion_decision_date` = 1988-12-21 (stated explicitly: "Date of the decision: 21 December 1988")
- `law_article_all_relevant`, taken literally from the text, = `["Article 101", "Article 102"]` (from "Infringement of Article 101 and 102 FEU Treaty")

Evaluating the three OR branches:

- `"Article 86" in law_article_all_relevant` → **False** (not in the extracted list)
- `"Article 82" in law_article_all_relevant` → **False**
- `"Article 102" in law_article_all_relevant` AND `date > 2009-11-30` → article match is **True**, but the date check is **False** (1988 ≠ post-2009)

Net result: the whole condition evaluates **False**. The task doesn't get gated into any branch and silently never fires for this document — not a misroute, a complete skip.

**Why this happens:** the date thresholds in your YAML clearly track the actual treaty renumbering history — Article 86 (EEC Treaty, pre-Amsterdam) → Article 82 (EC Treaty, Amsterdam 1999 to Lisbon 2009) → Article 102 (TFEU, post-Lisbon Dec 2009). That logic is correct. The problem is upstream: this source document is a _retrospective_ legal text describing an 1988 case, and it anachronistically labels the infringement using modern FEU Treaty numbering ("Article 101 and 102") even though the case was actually decided under the EEC Treaty — it should legally read as **Article 86** alone (Decca only had a unilateral-conduct/abuse theory here; there's no genuine Article 85/101-style horizontal agreement charge in the Commission's actual 89/113/EC decision, despite the secondary source's "101 and 102" framing — though arguably the licensing deals could _also_ be read under a market-sharing theory, the source's own econ-analysis section frames it as abuse-of-dominance/Article 86 territory).

So if your entity-resolution step extracts the literal string from text rather than mapping to the period-correct article based on decision date, you get exactly this: a document that _should_ hit the Article-86/pre-1999 branch instead fails all three branches because the literal text token is "Article 102," and 1988 doesn't satisfy that branch's date filter.

**Likely fix:** at ingestion, don't trust literal article-number mentions in narrative/secondary-source text as ground truth for `law_article_all_relevant`. Either (a) derive it deterministically from `eu_commisssion_decision_date` using the same three date thresholds your condition already encodes, or (b) keep two separate fields — `law_article_as_stated_in_source` (literal, for provenance) vs `law_article_period_correct` (derived) — and gate conditions on the latter only.

For the underlying classification task itself (if it were correctly gated): the answer would be **True** — Decca's monopoly in DNS signals, the deliberate signal-frequency changes to malfunction unlicensed competitors' receivers, and the market-partitioning licenses with AP/PE are textbook exclusionary abuse of dominance, independent of which article number labels it.


## Solution Options

### 1. specific book rules for infringments
Rather than checking the commission date, go without eu_commisssion_decision_date 

```
- target_fact_name: infringement_category_abuse_of_dominance
  question: |
    Task: Classify the following case based on the definition of "Abuse of Dominance."
    Definition: A company with significant market power unfairly exploits its position to restrict competition, eliminate rivals, or harm consumers, rather than competing fairly on the merits of its products or services.
    # Constraint: reply with 'True' or 'False' or 'NA' in no info is provided. Do not include any other text or the category name.
  output_schema: ""
  pipeline: universal
  condition: |
    exists("law_article_all_relevant") and 
    ( ( "Article 86" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or eu_commisssion_decision_date < d"1999-04-30") )
    or  ( "Article 82" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or (eu_commisssion_decision_date > d"1999-04-30" and eu_commisssion_decision_date < d"2009-11-30") ) )
    or  ( "Article 102" in law_article_all_relevant and ( eu_commisssion_decision_date == "NA" or (eu_commisssion_decision_date > d"2009-11-30" )) )
    )
  force_override: true
  run_state_active: false
```
## What's actually broken

Two coupled problems share one root cause — **`law_article_all_relevant` is a literal text surface, never canonicalized across the EU treaty renumberings**, yet every consumer treats the number as if it were period-correct.

`law_article_all_relevant` is produced by the `keyword_regex` pipeline with the pattern `Article \d+` ([FACT_EXTRACTOR.yaml:22-27](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/config_main/FACT_EXTRACTOR.yaml#L22-L27)) — it scrapes whatever digits appear in the text, with zero normalization. Then two different consumers assume those digits map cleanly to a concept:

1. **The YAML gating conditions** couple `(article-string AND date-window)` to recover the concept implicitly. When the surface number and the date disagree — retrospective text using modern numbers for an old case, the book's numbering, or a `NA` date — all three OR-branches fail and the fact **silently never fires** (your Decca walk-through).
2. **`BookCrossCheckModule`** tries to reconcile CELEX facts (period-correct numbering, "Article 82") against book prose (modern numbering, "Article 102"). The text never matches.

### The renumbering map (the domain fact everything hinges on)

|Concept|EEC (pre-1999-05-01)|EC (Amsterdam, 1999–2009)|TFEU (Lisbon, post-2009-12)|
|---|---|---|---|
|Anticompetitive agreements / cartels|**85**|**81**|**101**|
|Abuse of dominance|**86**|**82**|**102**|
|Public undertakings / member states|**90**|**86**|**106**|

The critical trap: **"Article 86" is the only ambiguous token** — abuse-of-dominance pre-1999, but public-undertakings 1999–2009. That single collision is the _entire_ reason your YAML couples article+date. Every other token (85, 81, 101, 82, 102, 90, 106) is unambiguous and needs **no date at all**.

This means your doc's Option 1 / fix (a) ("drop the date, derive from date") is **unsafe as written**: drop the date and a pre-1999 "Article 86" case will satisfy _both_ the `abuse_of_dominance` branch and the `procedural`/`member_states` branch ([FACT_EXTRACTOR.yaml:43-45](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/config_main/FACT_EXTRACTOR.yaml#L43-L45) vs [77-79](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/config_main/FACT_EXTRACTOR.yaml#L77-L79)). In fact this latent double-fire **already happens today for any `NA` date** — both branches have `date == "NA"` as a pass condition. So whatever you pick must preserve 86-disambiguation; it's load-bearing.

---

## Additional options (beyond a / b / Option-1 in your doc)

### Option C — Canonical infringement-concept fact _(recommended)_

A small **deterministic** (non-LLM) normalizer maps each `Article N` token to one of three concept enums (`anticompetitive_agreement`, `abuse_of_dominance`, `public_undertaking`), consulting the date **only for the "86" token**. Emit it as a new structured fact `infringement_concept` (a set), derived from `law_article_all_relevant` + `eu_commisssion_decision_date`.

- **Conditions become** `'abuse_of_dominance' in infringement_concept` — no date arithmetic, no 3-branch duplication, no silent skips. A `NA` date only costs you precision on the lone "86" case, not a total miss.
- **BookCrossCheck compares concept-to-concept**, so 82-vs-102 stops mattering.
- This is the principled version of your doc's (a)+(b) merged: it isolates the date dependency to the _one token that actually needs it_ instead of "always."
- **Cost:** one new normalizer module + a re-derivation pass; touches the YAML conditions. **Risk:** low — deterministic, fully unit-testable at the boundary.

### Option D — Equivalence-class membership in the condition DSL (no new data)

Keep `law_article_all_relevant` literal. Add a custom `rule_engine` function (same mechanism as the existing `exists`, [condition_checker.py:58-66](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/rule_engine/condition_checker.py#L58-L66)) so conditions read `same_concept(law_article_all_relevant, eu_commisssion_decision_date, 'abuse_of_dominance')`. The helper expands {86,82,102} and resolves the 86 collision via date internally.

- **Cost:** ~one function, no re-ingestion, no schema change. **Risk:** very low. **Limit:** fixes the YAML-gating failure only; BookCrossCheck needs the same helper wired in separately. Good as a fast first move, or as the condition-side companion to C.

### Option E — Reconcile BookCrossCheck on the book's _taxonomy_, not article text

The book's authoritative classification of a case is its **section path**, and the module _already_ reasons over that via `BookClassificationConsistencySignature` ([book_cross_check_module.py:117-142](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/src/pipelines/agent_dspy/book_cross_check_module.py#L117-L142)). So for infringement facts, derive the CELEX-side concept (Option C's `infringement_concept`, or the `infringement_category_*` facts) and compare **concept-to-concept against the section path** — stop routing infringement facts through the prose support-judge that text-matches article numbers.

- This directly kills the symptom you named ("fails to reconcile almost all infringement facts"). **Cost:** changes the routing in `_check_fact` for classification facts. **Risk:** low; it leans on machinery already present. Best paired with C/D, which give it a clean concept to compare.

### Option F — Era-tag the surface at extraction (provenance-preserving)

Instead of collapsing to a set (C), emit each mention at the regex boundary as a structured triple `(raw_token, treaty_era, canonical_class)`, stamping the era from the decision date. This is your doc's (b) done at the **ingestion layer, deterministically** rather than as two parallel LLM fields. Keeps per-mention provenance ("which surface did the source actually use") for audit, which your fact-provenance model ([[project_fact_classes_and_verification]]) tends to want.

- **Cost:** higher than C (richer schema). Pick this over C only if you need to _report_ the as-stated vs period-correct distinction, not just gate on it.

---

## One problem none of these fully solve

Your Decca example has a _second_, independent fault: the **source itself** mislabels an 1988 case as "101 and 102." No date logic fixes bad extraction. The only real defenses are (i) trust the decision's **operative article** (the "HAS ADOPTED THIS DECISION / Article 1" block) over narrative mentions, or (ii) treat the **book section path as ground truth** (Option E). Worth tracking as its own ISSUES.md item.

---

## My recommendation

**C + E**, with **D** as the quick stop-gap you can ship today: D unblocks the YAML gating in one function; C gives you the clean canonical fact; E makes the book reconciliation use it. C isolates the date to the single token that needs it, which is the only way to kill the silent-skip _and_ the NA-date double-fire without the 86 collision biting back.

Want me to write this up as a plan in [docs/claude/plans/](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/docs/claude/plans/) and add the source-mislabel and NA-date-double-fire items to [ISSUES.md](vscode-webview://0j52sitmv3flu88jv314acsl5a6f5ct662vmvi6o9rad7mt2d0ll/ISSUES.md)?


see implementation  [plans/infringement-concept-canonicalization-CDE](docs/claude/plans/infringement-concept-canonicalization-CDE)