 `2026-10-19
# Zenodo to Published Paper: A 101

Oct 19, 2026 · @Craft

The path from "files sitting in Zenodo" to "cited in a published paper."

## Part 1: Get the Zenodo record right (before you cite it)

**1. Check whether it's a draft or published.** If the upload page shows "Publish" and the record isn't public yet, it's still a draft. While it's a draft, you can still change everything.

**2. Reserve the DOI now if you haven't published.** In the draft, select "Reserve DOI" under the basic information section and save the draft. You can then put that DOI in your manuscript before the deposit goes live.

**3. Fill in the metadata properly.** This is what reviewers and future readers see:

- **Resource type:** Dataset, Software, or a mix. If you have both code and data, consider two records.
- **Title, authors with ORCID iDs, affiliations, description:** Write the description like a short abstract, covering what the files are, how they were produced, and the format.
- **License:** CC BY 4.0 is the usual choice for data. MIT or Apache-2.0 is usual for code. For CELEX/EUR-Lex-derived content, the Commission's reuse policy generally allows reuse with attribution, so credit EUR-Lex as the source in the description.
- **Version:** Use something like "1.0".
- **Related identifiers:** Use these to link the record to the paper later, for example "is supplemented by" / "is a supplement to", "is described by", and so on.
- **Communities (optional):** Submitting to a relevant Zenodo community improves discoverability.

**4. Add a README inside the deposit.** It should cover the file list, the schema or columns, how to reproduce the files, and a suggested citation.

**5. Publish.** After publishing, you can still edit metadata without creating a new version. Changing the files requires creating a new version.

## Part 2: Understand the two DOIs

When you publish for the first time, Zenodo registers two DOIs: one for the specific version of your record and one (the Concept DOI) representing all versions.

- **Cite the version DOI in your paper.** Zenodo recommends the version-specific DOI for citations, so reviewers see exactly the files you used.
- **The Concept DOI** always resolves to the latest version's landing page, so it's useful in a data availability line pointing to the evolving resource.

## Part 3: Cite it in the paper

**6. Add a reference entry.** The Zenodo record page has a "Cite as" box with ready-made formats, including BibTeX. A typical entry looks like this:

> Craft, X. (2026). *CELEX Competition-Law Fact Dataset* (Version 1.0) \[Data set\]. Zenodo. https://doi.org/10.5281/zenodo.XXXXXXX

**7. Add a Data/Code Availability statement.** Most journals require one. For example:

> The dataset and extraction code are openly available on Zenodo at https://doi.org/10.5281/zenodo.XXXXXXX under CC BY 4.0.

## Part 4: Publish the paper

**8. Pick a venue.**

- **Preprint first (fast, free, citable):** For legal and empirical-law work, SSRN is the norm. For methods-heavy work on NLP or LLM pipelines, arXiv (cs.CL or cs.IR) works well. Zenodo itself also accepts preprints and gives them a DOI.
- **Peer review:** Options include AI & law venues such as the journal *Artificial Intelligence and Law*, JURIX, and ICAIL, as well as competition-law journals if the contribution is the legal findings rather than the tooling.

**9. Submit.** Follow the journal's author guidelines on formatting, cover letter, data statement, and ORCID. Expect revisions.

## Part 5: Close the loop

**10. Link the paper back to Zenodo.** Once the paper has a DOI (preprint or journal), edit the Zenodo record's metadata and add it as a related identifier ("is supplement to"). This doesn't create a new version. It makes the dataset and paper discoverable from each other.

**11. Update files only with a new version.** If reviewers ask for changes, click "New version" on Zenodo, upload the revised files, and update the version DOI in the paper.

## Sources

- [Zenodo – DOI versioning](https://zenodo.org/help/versioning)
- [IPBES – How to Upload to and Download from Zenodo](https://ict.ipbes.net/data-management/technical-guidelines/Zenodo)