-- ---------------------------------------------------------------------------
-- Migration: store a resolved legal jurisdiction on the investigated-company
-- roster.
--
-- company_investigated2celex_id only carried iso_country_code (ISO 3166-1
-- alpha-2), which is the wrong grain wherever a company is registered in a
-- subdivision. Main_ingest_opencorp_index_companies.py derived its
-- OpenCorporates jurisdiction filter as iso_country_code.lower(), and
-- OpenCorporates has no country-level jurisdiction for AE or US at all -- a
-- bare 'ae' search returns HTTP 200 with zero companies, which the batch could
-- not distinguish from a genuine miss, so those rows were marked
-- fk_opencorp_idx = 'NA' and left the roster view as if unfindable.
--
-- jurisdication_code holds the ISO 3166-2 / GLEIF canonical form (uppercase,
-- hyphen-separated: 'AE-DU', or plain 'GB' where the country is itself the
-- jurisdiction). It is populated by the enrich_jurisdiction stage of
-- Main_ingest_company_investigated2celex.py; NULL means no jurisdiction could
-- be determined from the row's address, LEI or country, and consumers should
-- decline to search rather than guess.
--
-- The column name keeps the spelling used when it was commissioned
-- ('jurisdication'), matching celex_facts.gleif_jurisdication2countrycode.
--
-- Statements 1 and 2 are safe to re-run. Statement 3 is a one-off data repair;
-- it is idempotent but only meaningful once, and is kept separate so it can be
-- reviewed or skipped independently.
-- ---------------------------------------------------------------------------

-- 1. The column. varchar(8) fits the longest canonical code (e.g. 'BE-VLG').
ALTER TABLE celex_facts.company_investigated2celex_id
    ADD COLUMN IF NOT EXISTS jurisdication_code varchar(8);

CREATE INDEX IF NOT EXISTS ix_company_investigated_jurisdication_code
    ON celex_facts.company_investigated2celex_id (jurisdication_code);

-- 2. Expose it on the roster view the OpenCorporates batch selects targets
-- from. CREATE OR REPLACE VIEW can only append columns, so jurisdication_code
-- goes last and the existing column list is repeated verbatim.
CREATE OR REPLACE VIEW celex_facts.company_investigated2lei2corpidx2hr_vw AS
SELECT DISTINCT
    company_name,
    adress,
    iso_country_code,
    fk_lei,
    fk_opencorp_idx,
    fk_handelsregister,
    jurisdication_code
FROM celex_facts.company_investigated2celex_id;

-- 3. One-off repair: reopen the false negatives.
--
-- Every AE and US row marked 'NA' was marked on the strength of a search sent
-- to a jurisdiction that does not exist at OpenCorporates ('ae' / 'us'), so the
-- zero-hit result carried no information about the company. Clearing the marker
-- returns these rows to the roster view so a later run retries them under a
-- real jurisdiction. Rows that genuinely resolved (fk_opencorp_idx = 'LINKED')
-- are untouched.
UPDATE celex_facts.company_investigated2celex_id
SET fk_opencorp_idx = NULL
WHERE fk_opencorp_idx = 'NA'
  AND iso_country_code IN ('AE', 'US');
