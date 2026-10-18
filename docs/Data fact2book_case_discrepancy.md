# fact2book case discrepancy report

The book names about 455 distinct Commission cases, and analyses about 68 of them in detail as landmark cases. Both are regex counts over the book chunks in the database, so they are close estimates rather than an exact total:

|Measure|Count|
|---|---|
|Distinct case numbers named anywhere|~455 (462 counting IV/ and COMP/ spellings separately)|
|Named in the E chronological lists|448|
|Own landmark write-ups (F.n headings)|~68 decisions|
|Named inside F landmark sections, own or only cited|60 case numbers|
|Named only in the index or annex|0|

**Why ~455 is a lower bound:** the book has 72 entries like "Cases IV/30.979, 31.394". My regex only caught the first number in each, so the true total could be up to about 525 if none of those extra numbers appear elsewhere. Many probably do, so the real figure is likely closer to 455–490.

**Why the two landmark numbers differ:** 68 counts decision numbers in the F.n headings. 60 counts case ids found in landmark chunks. Some landmark headings sit in chunks whose text never mentions the case id, and others cover several cases.

For comparison, the cross-check driver's run log (`fact2book_case_runs`) has 699 cases from our corpus. It found 397 of them in the book and 302 not in the book.

`★ Insight ─────────────────────────────────────`

- Nearly every case the book names (448 of ~455) is in at least one E list. The E lists work as the book's full case index, while the A–D and F sub-chapters cover only a small, selected set.
- So only about 15% of the cases in the book have a detailed analysis. That fits the finding that 50 of the 58 mismatched cases are only listed.
- 397 matches from ~455 book cases is high coverage, but some matches may be shaky. One case can map to several CELEX documents, and a case number inside a "Cases X, Y" entry is only found if the book happens to write it with the full prefix. That second point also affects the cross-check's own case lookup.`─────────────────────────────────────────────────`

Which `mismatch` verdicts of the fact-vs-book cross-check rest on a bare listing
of the case in the Russo-Schinkel book, and which rest on a real analysis.

Reporter: [fact2book_case_discrepancy_reporter.py](../src/reporting/utils/fact2book_case_discrepancy_reporter.py)

## Problem

`BookCrossCheckModule` judges the infringement-classification facts
(`infringement_category_*`, `infringement_subcategory_*`) with the
deterministic taxonomy rule in `book_taxonomy.py`: the book chapter a case is
filed under is the classification. A fact claiming another chapter is a
`mismatch` ("Book taxonomy files the case under 5 Vertical restrictions, not
under the claimed 2.").

Every numbered book section has the same lettered sub-chapters:

| sub-chapter | content | weight as evidence |
|---|---|---|
| A Introduction | prose on the infringement type | detailed |
| B Applicable EU legislation | reference list | mention |
| C Related economics texts | reference list | mention |
| D Related legal texts | reference list | mention |
| E Commission decisions in chronological order | one short entry per decision | mention |
| F Economic landmark cases -> F.n *case* | full analysis (facts, allegation, economics, fines) | detailed |

Placement (`book_placement`) uses landmark paths when the case has any, and
otherwise **every** path naming it -- so a case with no landmark analysis is
filed wherever an `E` list happens to include it. The `E` lists also include
decisions under several sections (a decision on dealer export bans shows up in
both 5.2 and 5.5), and the list entry says nothing about why the decision sits
there. A `mismatch` built on that is weak evidence against the extracted fact.

## Design

1. **Input** -- `Fact2BookCheckRepository.list_mismatches(pattern)`: every
   `action = 'mismatch'` row joined to its fact, filtered by a fact-name
   `LIKE` pattern (default `%infringement%`, the same query used for the
   manual check).
2. **Case surface** -- the case id is resolved to the spelling the book uses
   (`IV/29146` -> `IV/29.146`, `IV/` -> `COMP/`) with `case_id_variants`, the
   exact resolver the cross-check uses, so the reporter sees the same chunks the
   verdict saw.
3. **Mentions** -- `BookChunkRepository.select_reference_chunks(surface)`
   returns `(path, text)` of every chunk naming the case. Each path is
   classified by its first lettered sub-chapter segment (`classify_mention`):
   `landmark` (F), `listing` (E), `introduction` (A), `reference` (B-D),
   `body` (section prose above the letters) or `index` (back-matter). The
   markers are `settings.BOOK_LANDMARK_SECTION_MARKERS`,
   `BOOK_LISTING_SECTION_MARKERS` and `BOOK_INTRODUCTION_SECTION_MARKERS`.
   `landmark`, `introduction` and `body` count as *detailed*.
4. **Claimed and filing chapters** -- the chapters a fact claims come from
   `book_taxonomy.claimed_numbers` (the verdict's own label-to-number rule).
   The *filing chapters* are the decisive placement chapters
   (`settings.BOOK_TAXONOMY_DECISIVE_CHAPTERS`) minus the claimed ones -- the
   chapters the verdict says the case belongs to instead.
5. **Own landmark** -- a landmark chunk is the case's own write-up when an
   `F.n ... Commission decision NN/NNN` heading carries the decision number
   derived from the CELEX id (`CELEX-31978D0155` -> `78/155/`), searched in
   the path and the chunk text (the chunker does not always split below the
   `F Economic landmark cases` heading), before the `Relevance:` paragraph. A
   landmark that only cites the decision is not its own.
6. **Classification per case**
   - `listing_only` -- no detailed mention anywhere: the book only lists it.
   - `detailed_elsewhere` -- the filing chapters only list it, another
     chapter discusses it.
   - `detailed_in_filing_chapter` -- the filing chapter discusses it (for all
     current cases: the case is that chapter's own landmark).
7. **Output** -- list 1 (`listing_only` + `detailed_elsewhere`): cases not
   discussed in the filing chapter. List 2 (`detailed_in_filing_chapter` +
   `detailed_elsewhere`): one row per detailed mention with its role
   (`filing` / `claimed` / `other`) and a text excerpt around the case id.

A search by decision number (`78/155/EEC`) in addition to the case id was
probed on the full mismatch set and found no chunk the case-id search misses,
so it is not part of the lookup; the decision number is used only for the
own-landmark test.

## Operation

```bash
# Rewrite the generated block below (the prose around it is kept):
PYTHONPATH=src .venv/bin/python src/reporting/utils/fact2book_case_discrepancy_reporter.py

# Another fact family or output file:
PYTHONPATH=src .venv/bin/python src/reporting/utils/fact2book_case_discrepancy_reporter.py \
    --fact-pattern 'infringement_category%' --out /tmp/report.md
```

The reporter is read-only (two SELECT paths, no LLM, a few seconds for ~60
cases). It replaces only the text between the
`fact2book-discrepancy-report:start` / `:end` marker comments; a file without
markers gets the block appended. Tests:
`src/reporting/utils/tests/test_fact2book_case_discrepancy_reporter.py`.

## Reading the result

- **List 1** cases are candidates for downgrading the verdict: the book
  places them by a list entry, not by analysis. Resolving them needs the
  decision text (or a rule that a listing-only placement cannot contradict).
- **List 2** cases are backed by the case's own landmark analysis. These
  mismatches deserve a reading of the landmark: either the extracted fact is
  wrong, or the book's framing differs from the decision's (e.g. BMW Belgium,
  78/155/EEC: dealers agreed among themselves not to export -- horizontal in
  form -- at the instigation of the supplier, which the book analyses as a
  vertical export ban under 5.2).

## Generated report

<!-- fact2book-discrepancy-report:start -->

Generated 2026-09-27 for fact pattern `%infringement%`: 93 mismatch rows over 58 cases.

| classification | cases |
|---|---|
| `listing_only` | 50 |
| `detailed_elsewhere` | 0 |
| `detailed_in_filing_chapter` | 8 |
| unresolved (case id not in book) | 0 |

### List 1 -- not discussed in the filing chapter, only listed

The chapter the verdict rests on names the case only in lists (`E` chronological decisions, `B`-`D` references). `detailed elsewhere` cases reappear in list 2.

| celex_id         | case     | mismatched facts                                                                                                                                                           | claimed | filed under | kinds in filing chapter | E listings         | class          |
| ---------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- | ----------- | ----------------------- | ------------------ | -------------- |
| 34579_1889_2     | IV/34579 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = price fixing collusion                                       | 2       | 5           | listing                 | 5.4                | `listing_only` |
| 38113_250_8      | IV/38113 | `infringement_category_hor_vert_lic_joint` = horizontal                                                                                                                    | 2       | 3           | listing                 | 3.2, 3.6           | `listing_only` |
| 39181_1908_8     | IV/39181 | `infringement_category_hor_vert_lic_joint` = joint venture                                                                                                                 | 6       | 2           | listing                 | 2.1, 2.2           | `listing_only` |
| 39188_2291_2     | IV/39188 | `infringement_category_hor_vert_lic_joint` = joint venture                                                                                                                 | 6       | 2           | listing                 | 2.1                | `listing_only` |
| CELEX-31973D0323 | IV/26825 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 2           | listing                 | 2.2                | `listing_only` |
| CELEX-31974D0017 | IV/795   | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 2           | listing                 | 2.2                | `listing_only` |
| CELEX-31974D0432 | IV/28374 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                       | 4       | 2, 5        | listing                 | 2.2, 5.2           | `listing_only` |
| CELEX-31975D0095 | IV/26872 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = market sharing, quota cartels, and specialization agreements | 2       | 6           | listing                 | 6.2, 8             | `listing_only` |
| CELEX-31975D0297 | IV/27879 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = market sharing, quota cartels, and specialization agreements | 2       | 4           | listing                 | 4.1                | `listing_only` |
| CELEX-31975D0482 | IV/28838 | `infringement_category_hor_vert_lic_joint` = joint venture                                                                                                                 | 6       | 2, 4        | listing                 | 2.6, 4.1           | `listing_only` |
| CELEX-31975D0781 | IV/256   | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Territorial Exclusivity / Parallel Trade Bans                    | 5       | 2           | listing                 | 2.6                | `listing_only` |
| CELEX-31978D0193 | IV/29246 | `infringement_category_hor_vert_lic_joint` = horizontal                                                                                                                    | 2       | 4           | listing                 | 4.1                | `listing_only` |
| CELEX-31980D0234 | IV/29011 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 2           | listing                 | 2.6                | `listing_only` |
| CELEX-31980D0256 | IV/29595 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = export cartels                                               | 2       | 5           | listing                 | 5.2                | `listing_only` |
| CELEX-31980D1071 | IV/25077 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 2           | listing                 | 2.6                | `listing_only` |
| CELEX-31980D1283 | IV/29702 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = export cartels                                               | 2       | 5           | listing                 | 5.2                | `listing_only` |
| CELEX-31982D0204 | IV/29971 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 4           | listing                 | 4.2                | `listing_only` |
| CELEX-31982D0896 | IV/29883 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Resale Price Maintenance (RPM)                                   | 5       | 2           | listing                 | 2.1                | `listing_only` |
| CELEX-31983D0361 | IV/30174 | `infringement_category_hor_vert_lic_joint` = vertical                                                                                                                      | 5       | 2           | listing                 | 2.1, 2.6           | `listing_only` |
| CELEX-31985D0560 | IV/30971 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 6           | listing                 | 6.1                | `listing_only` |
| CELEX-31985D0564 | IV/27591 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Resale Price Maintenance (RPM)                                   | 5       | 2           | listing                 | 2.6                | `listing_only` |
| CELEX-31987D0100 | IV/31340 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                       | 4       | 6           | listing                 | 6.2                | `listing_only` |
| CELEX-31987D0407 | IV/32034 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                       | 4       | 5           | listing                 | 5.6                | `listing_only` |
| CELEX-31988D0109 | IV/31735 | `infringement_category_hor_vert_lic_joint` = joint venture                                                                                                                 | 6       | 2           | listing, reference      | 2.1, 2.2, 2.6, 2.7 | `listing_only` |
| CELEX-31988D0555 | IV/32173 | `infringement_category_hor_vert_lic_joint` = horizontal                                                                                                                    | 2       | 6           | listing                 | 6.1                | `listing_only` |
| CELEX-31988D0568 | IV/32437 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Selective Distribution                                           | 5       | 2           | listing                 | 2.3                | `listing_only` |
| CELEX-31988D0604 | IV/32358 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                       | 4       | 5           | listing                 | 5.6                | `listing_only` |
| CELEX-31989D0044 | IV/27394 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Resale Price Maintenance (RPM)                                   | 5       | 2           | listing                 | 2.1, 2.6, 2.7      | `listing_only` |
| CELEX-31989D0094 | IV/31697 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                       | 4       | 5           | listing                 | 5.6                | `listing_only` |
| CELEX-31989D0095 | IV/31291 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 2           | listing                 | 2.8                | `listing_only` |
| CELEX-31989D0536 | IV/31734 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                       | 4       | 5           | listing                 | 5.2                | `listing_only` |
| CELEX-31990D0045 | IV/32414 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 2           | listing                 | 2.2                | `listing_only` |
| CELEX-31990D0046 | IV/32006 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = market sharing, quota cartels, and specialization agreements | 2       | 6           | listing                 | 6.1                | `listing_only` |
| CELEX-31990D0446 | IV/32688 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = market sharing, quota cartels, and specialization agreements | 2       | 6           | listing                 | 6.1                | `listing_only` |
| CELEX-31991D0335 | IV/32186 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = export cartels                                               | 2       | 5           | listing                 | 5.2                | `listing_only` |
| CELEX-31991D0532 | IV/32879 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = export cartels                                               | 2       | 5           | listing                 | 5.2                | `listing_only` |
| CELEX-31993D0126 | IV/33151 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 2           | listing                 | 2.6                | `listing_only` |
| CELEX-31994D0210 | IV/33941 | `infringement_category_hor_vert_lic_joint` = vertical                                                                                                                      | 5       | 3           | listing                 | 3.1                | `listing_only` |
| CELEX-31994D0771 | IV/34410 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                       | 4       | 6           | listing                 | 6.3, 8.4           | `listing_only` |
| CELEX-31994D0985 | IV/33218 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Resale Price Maintenance (RPM)                                   | 5       | 2           | listing                 | 2.1                | `listing_only` |
| CELEX-31995D0188 | IV/33686 | `infringement_category_hor_vert_lic_joint` = joint venture                                                                                                                 | 6       | 2, 4        | listing                 | 2.6, 4.2           | `listing_only` |
| CELEX-31995D0373 | IV/33375 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Territorial Exclusivity / Parallel Trade Bans; Exclusive Dea     | 5       | 4           | listing                 | 4.2                | `listing_only` |
| CELEX-31997D0039 | IV/35518 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Territorial Exclusivity / Parallel Trade Bans                    | 5       | 6           | listing                 | 6.1, 6.2           | `listing_only` |
| CELEX-31997D0624 | IV/34621 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Discounts and Rebates                                            | 5       | 3           | listing                 | 3.1, 3.2, 3.3, 3.6 | `listing_only` |
| CELEX-31999D0006 | IV/35280 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                       | 4       | 5           | listing                 | 5.2                | `listing_only` |
| CELEX-31999D0242 | IV/36237 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = market sharing, quota cartels, and specialization agreements | 2       | 6           | listing                 | 6.2                | `listing_only` |
| CELEX-31999D0574 | IV/36581 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 6           | listing                 | 6.2                | `listing_only` |
| CELEX-32001D0478 | IV/37576 | `infringement_category_hor_vert_lic_joint` = horizontal                                                                                                                    | 2       | 4           | listing                 | 4.2                | `listing_only` |
| CELEX-32001D0663 | IV/34950 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure                           | 5       | 4           | listing                 | 4.2                | `listing_only` |
| CELEX-32002D0914 | IV/29373 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = price fixing collusion                                       | 2       | 5           | listing                 | 5.4                | `listing_only` |

### List 2 -- detailed book content available

Each row is one detailed mention (landmark analysis, introduction or section prose). `role`: `filing` = chapter the verdict rests on, `claimed` = chapter the fact claims, `other` = neither. `own` = the landmark heading carries this case's decision number (the case is the landmark), otherwise it is only cited there.

| celex_id         | case     | mismatched facts                                                                                                                                                       | role   | kind     | own | section path                                                                                                                                                                                                        | excerpt                                                                                                                                                                                                                                                                    |
| ---------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | -------- | --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CELEX-31964D0599 | IV/71    | `infringement_category_hor_vert_lic_joint` = joint venture                                                                                                             | filing | landmark | yes | 2 Horizontal restrictions -> 2.5 Export cartels -> F Economic landmark cases -> F.1 DECA Commission decision 64/599/EEC [1964] OJ L 64/2761                                                                         | ...oduce any effect on trade between Member States it cannot be assessed under the rules laid down in Article 101 FEU Treaty. - Case IV/71 - Decision Negative Clearance - Decision addressee(s): – Dutch Engineers and Contractors Association (DECA), the Netherlands... |
| CELEX-31973D0109 | IV/26918 | `infringement_category_hor_vert_lic_joint` = vertical<br>`infringement_subcategory_vertical_restraints` = Exclusive Dealing / Market Foreclosure; Territorial Exclusiv | filing | landmark | yes | 3 Abuse of dominance -> 3.2 Loyalty discounts and fidelity rebates -> F Economic landmark cases                                                                                                                     | ...ivity contracts with customers and completely unrelated to costs and quantities, was considered abusive by the Commission. - Case IV/26.918 - Decision: Infringement of Article 101 and 102 FEU Treaty - Decision addressee(s): – Raffinerie Tirlemontoise SA (RT), ... |
| CELEX-31978D0155 | IV/29146 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = export cartels                                           | filing | landmark | yes | 5 Vertical restrictions -> 5.2 Territorial exclusivity and parallel import /export bans -> F Economic landmark cases -> F.1 BMW Belgium NV and Belgian BMW dealers Commission decision 78/155/EEC [1978] OJ L 46/33 | ... this is the first decision in which the reasoning was based on the production or otherwise of efficiencies on the market. - Case IV/29.146 - Decision: Infringement of Article 101 FEU Treaty - Decision addressee(s): – BMW Belgium NV, Belgium Autohandel O. Cocq... |
| CELEX-31979D0298 | IV/28796 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = exchange of information on prices and sales              | filing | landmark | yes | 6 Joint ventures and alliances -> 6.1 R&D joint ventures -> F Economic landmark cases                                                                                                                               | ...nce with the economic literature and concerns are expressed about the possible restriction of post-innovation competition. - Case IV/28.796 - Decision: Exemption pursuant to Article 101(3) FEU Treaty for ten years - Decision addressee(s): – Beecham Group Limit... |
| CELEX-31980D0182 | IV/29672 | `infringement_category_hor_vert_lic_joint` = joint venture                                                                                                             | filing | landmark | yes | 2 Horizontal restrictions -> 2.5 Export cartels -> F Economic landmark cases -> F.2 Floral Commission decision 80/182/EEC [1980] OJ L 39/51                                                                         | ...d derive from the agreement. The described effects are measured in relation to the market (oligopolistic) characteristics. - Case IV/29.672 - Decision: Infringement of Article 101(1) FEU Treaty, imposition of fines                                                  |
| CELEX-31983D0390 | IV/30437 | `case_infringement_type_book` = Marketing, selling, distribution, and production joint ventu                                                                           | filing | landmark | yes | 6 Joint ventures and alliances -> 6.2 Marketing, selling, distribution, and production joint ventures -> F Economic landmark cases -> F.2 Rockwell/Iveco Commission Decision 83/390/EEC [1983] OJ L 224/19          | ...ortance of analyzing the market structure and features in order to assess the effects on competition of the joint venture. - Case IV/30.437 - Decision: Exemption pursuant to Article 101(3) FEU Treaty for eleven years Decision addressee(s): – Iveco Industrial V... |
| CELEX-31989D0113 | IV/30979 | `infringement_category_hor_vert_lic_joint` = license                                                                                                                   | filing | landmark | yes | 3 Abuse of dominance -> 3.8 Preventing interoperability of standards and networks -> F Economic landmark cases                                                                                                      | ...the Commission reasoning, nonetheless the effects of abusive use of interoperability codes by a monopolist are described. - Cases IV/30.979, 31.394 - Decision: Infringement of Article 101 and 102 FEU Treaty - Decision addressee(s): Racal Electronics plc, Unite... |
| CELEX-31992D0213 | IV/33544 | `infringement_category_hor_vert_lic_joint` = horizontal<br>`infringement_subcategory_horizontal_restraints` = exchange of information on prices and sales              | filing | landmark | yes | 3 Abuse of dominance -> 3.7 Refusal to deal and essential facilities doctrine -> F Economic landmark cases -> F.2 Magill TV Guide/ITP, BBC, and RTE Commission decision 89/205/EEC [1989] OJ L 78/43                | ...nsport sector that were the object of several further Commission interventions in the frame of the liberalization process. - Case IV/33.544 - Decision: Infringement of Article 102 FEU Treaty Decision addressee(s): Aer Lingus plc, Ireland Report route: Complain... |

<!-- fact2book-discrepancy-report:end -->
