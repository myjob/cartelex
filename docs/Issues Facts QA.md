

# 1. Human-index reconciliation (wrong values measured against the index)

- #381 17 decisions have no adoption date in their text and kept their old LLM date or none. 3 have no stored date at all (e.g. Intel 37990_3581_18).
- #378 Check the reconciliation failures in docs/human_index2facts_recon.
- #377 companies_investigated loses most addressees in large decisions (Forex: 22 in the index, 3 in the roster; Trucks: 18 vs 4).
- #376 case_commissioner is empty for 192 of 577 decisions, and for 53% of those above 40k tokens.
- #375 In abuse-of-dominance decisions, complainants and third parties are listed as parties (Intel 15 entities for 1 addressee, Microsoft 11).
- #374 Translated source texts store translated company names (Comptoir belge de l'azote became "Belgian Nitrogen Counter").
- #373 The document behind an identifier is not always the decision the index describes (36756_68_16).
- #372 infringement_category_hor_vert_lic_joint disagrees with the index in 115 of 486 decisions.
- #371 The opposite case: errors in the index itself, to fix in the workbook or record as human_wrong.

# 2. Empty or NA facts where the text has the answer

- #366 When the section classifier misses, section-scoped facts come back empty. 9 decisions have no operative chunk, so case_commission_infringement and law_article_operative are [] (Teva, Deutsche Telekom, Visa).
- #311 Re-extract the operative-scoped facts for 22 decisions whose Article 1 chunk was not tagged operative.
- #160 fines is empty for 431 of 744 documents. This is an extraction problem, not a verification gap.
- #326 Every local model returns empty fines, leniency_applicants, services and exemption_applicants on the same large decisions (context or retrieval misses).
- #318 cartel_period is NA for 84 infringement decisions, but 41 of them state the start date in the text.
- #306 products came back as [] for 13 of the 41 new decisions.
- #80 companies_country is NA or [] for 53 decisions.
- #45 64 companies_investigated rows have the value NA.
- #222 companies_infringement does not load all companies in 39922_2067_2.
- #40 companies_address returns NA when the name search lands on recitals instead of the addressee block.
- #104 A stored NA or empty value counts as done, so it blocks re-extraction.

# 3. Wrong classification or values

- #380 infringement_concept stores several concepts in one row, which gives 7 provision nodes instead of 3.
- #329 NACE_group_code misclassifies services (interest-rate derivatives coded as 64.11 Central banking).
- #314 85 HeidelTime periods end on or after the decision date (exemption or agreement terms taken as the period).
- #292 DEC-AT40636-20250407 has day and month swapped.
- #304 recitals_max is marked doubtful for 281 decisions, although its placement is mostly right.
- #312 number_undertaking counts legal units, not economic undertakings.
- #18, #17, #16 The "Article 86" ambiguity fires both abuse of dominance and public undertaking. In the Decca case the source itself says "101 and 102".
- #14 Chiquita/United Brands also carries horizontal, which looks like a misextraction.
- #11 Case ids are assigned to the wrong decision in celex_case_id (33708_6_7 vs IV/26.918).
- #3 products and products_name disagree.

# 4. Company roster and country accuracy

- #86 companies_country gives one entry per group, so the group's country goes onto the lead member (Autoliv, Inc. read as JP).
- #82 companies_investigated emits polluted company names.
- #38 The companies_country schema key drifts.
- #36 The universal pipeline ignores predecessors and re-derives the companies.
- #345 Two questions invite knowledge from outside the decision: company_recidivism, and companies_country (asked without the document text).
- #76, #77 The fk_lei substring matcher assigns the wrong company's LEI to 38% of its matches.
- #166 The registry sources disagree on which company they matched for 45% of roster rows.
- #214 The alias table and fk_lei use different spellings, and where they disagree they mean different entities.
- #41 Short names with legal suffixes ("SA", "AG") produce noisy matches.
- #46 The JSON format of companies_investigated drifts.

# 5. Verification that hides inaccuracy

- #354 verified_supported overclaims: entity verification only tests that the name occurs somewhere, so a complainant passes as a party.
- #360 65% of facts score exactly 1.0, and statuses contradict their labels.
- #362 The entity graph projects facts the verifier did not accept (40% of participated_in edges).
- #159 A verdict cannot be told apart from a re-extracted value, so verdicts go stale silently.
- #356, #90 There is no gold set and no accuracy metric.
- #237 Book cross-check mismatches come from cases the book only lists, never analyses.
- #25 The chat answer said there was no fine information while the attached chunk lists EUR 400 000.
- #92 The reasoning model produces hedged speculation.

# 6. Stale or untraceable values

- #240 All 744 case_summary rows predate their input facts.
- #363 701 products values have no audit trail or lineage.
- #238 Some fact rows have no producer left in FACT_EXTRACTOR.yaml.
- #239 company_country is still in the DB although it was marked deleted.
- #91 The 1,703 llm_index_companies rows are stale.

★ Insight ─────────────────────────────────────
- Most of these come down to three causes. Retrieval or section scoping misses the operative part (#366, #311, #326, #160, #40). The extraction prompt does not separate addressees from other named parties (#375, #377). Verification accepts weak evidence, so the errors don't show (#354, #360).
- The human-index reconciliation (#371 to #381) is the only measurement of accuracy against ground truth so far. That is why #356 (gold set and evaluation entry point) would make the other fixes measurable.
  ─────────────────────────────────────────────────