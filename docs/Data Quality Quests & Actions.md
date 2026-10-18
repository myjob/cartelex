#data_quality
documentation of data cleansing & fixation actions applied


## fact companies

various company related facts are ingested over time. the following are the master (July 2026):

| target_fact_name       | question                                                                                                                                                                                                                                                 |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| companies_investigated | List the undertakings, companies, or associations that are named as parties or addressees of this decision (the entities whose conduct or agreements the<br>Commission assessed). Include trade or professional associations. Report each name verbatim. |
| companies_country      | for all companies, what is their ISO country code ?                                                                                                                                                                                                      |
| companies_address      | What is the registered address of this company ?                                                                                                                                                                                                         |
| companies_industry     | list the industries                                                                                                                                                                                                                                      |
| company_recidivism     |                                                                                                                                                                                                                                                          |
the following facts are obsolete  and need to be replaces:
* companies
* companies_investigated
* companies_investigated
* companies_country

Impact:
1) cleans FACT_EXTRACTOR.yaml
	* delete target_fact names
	* change references of condition & predecessors
2) cleans audit trail fact_ingestion_audit_trail_vw fed by fact_ingestion_audit_trail


see plan: [Retire obsolete company facts](<docs/claude/plans/retire-obsolete-company-facts.md>)

## fact product vs product_names

|                                                   |                                                                                                                  |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| **[out of scope]** phantom `products` predecessor | L212, L227, L236 depend on `products`; the real fact is `products_name` (L312). Same bug class, different family |
## case duplications 39452_3336_8 and  39452_3384_13
 
     The full-corpus language audit (`src/batch/Main_audit_chunk_language.py`) flags it
      as the corpus's only wrong-language document: 314 of 324 chunks German, 99.2% of
      scored characters. Root cause is in `config_main/master_doc_descriptors.json` —
      both `txt_md_file` and `local_file_pdf` for this id point at **another case
      document's** German original, `39452_3384_13.pdf` (descriptor comment: "issue with
      org PDF containing HTML error text fixed by manually download", so the manual
      re-download grabbed the wrong doc number). Meanwhile `39452_3384_13` is a separate
      corpus entry that correctly resolves to `39452_3384_13_en.md` and is already
      ingested in English (219 chunks). So there is no English source for 39452_3336_8
      itself, and it carries its own 101 KB `facts-39452_3336_8.json` ledger. Needs a
      sourcing decision, not a re-ingest: either obtain the real 39452_3336_8 PDF from
      the Commission case page, or retire the id and remap its fact ledger onto
      39452_3384_13. Until then its 324 German chunks pollute retrieval.
		--> 20260728:  39452_3336_8 deleted
	## Decision: 
		* delete the german chunks in pg_embedding_qwen of 39452_3336_8 to keep the hybrid search free of doublets
		* take 39452_3336_8 out of master_doc_descriptors.json
		* keep the facts of 39452_3336_8 for the moment: the facts derived from german are surprisingly close to the english chunks
```
 "39452_3336_8": {
 "gs_public_url": "https:storage.googleapis.com/niki-phd-pdf-bucket/39452_3336_8.pdf",
 "txt_md_file": "~/a/celex_docs/39452_3336_8/mineru/39452_3384_13/auto/39452_3384_13.md",
 "facts": "~/a/celex_docs/39452_3336_8/facts-39452_3336_8.json",
 "local_file_pdf": "~/a/celex_docs/39452_3336_8/39452_3384_13.pdf",
 "language_translated": "NA",
 "CELEX": "39452_3336_8",
 "comment": "issue with org PDF containing HTML error text fixed by manually download",
 "language_init": "de",
 "webDocDescriptor": {
 "case": "39452",
 "url_pdf": "https:ec.europa.eu/competition/antitrust/cases/dec_docs/39452/39452_3384_13.pdf",
 "url_html": "",
 "year": "2012",
 "text": "",
 "comment": "",
 "title": "Mountings for windows and window-doors",
 "decision_date": "2012-03-28 00:00:00",
 "firms": "Alban Giacomo SpA; Aug. Winkhaus GmbH & Co KG; Carl Fuhr GmbH & Co KG; Gretsch-Unitas Baubeschlaege; Gretsch-Unitas GmbH; Hautau GmbH; Heinrich Strenger GmbH & Co. KG; Mayer & Co Beschläge; NORAA GmbH; Roto Frank AG; Siegenia-Aubi KG",
 "nace": "Manufacture of locks and hinges"
 }
 },
```