-- ---------------------------------------------------------------------------
-- Migration: prepare the OpenCorporates resolution ladder.
--
-- 3,548 roster rows carry fk_opencorp_idx = 'NA' -- OpenCorporates returned
-- zero hits and the row was closed permanently. Measurement showed three
-- distinct causes, only one of which is a genuine absence:
--
--   A. The jurisdiction filter. 852 rows sit in jurisdictions where every
--      query ever made returned zero (us, it, at, pt, kr, eu, cn, tw, ru,
--      za, ...). The one unfiltered query in the cache (q='emirates') returned
--      25 'za' and 4 'il' companies, so OpenCorporates does index them.
--   B. Sparse jurisdictions (be, jp, pl, gr, es): 689 rows, mixed causes.
--   C. Polluted company names: 1,997 rows in demonstrably healthy
--      jurisdictions (de, gb, ch, fi). See the companies_investigated entry in
--      ISSUES.md -- that defect belongs upstream, not here.
--
-- Separately, registration jurisdiction is not HQ country: Halkis is HQ Athens
-- but registered in Cyprus, Emirates is HQ Dubai but registered in the US.
-- jurisdication_code is derived from HQ signals (address, LEI, country), so
-- for any offshore or holding-company registration it names a jurisdiction the
-- company cannot be found in. The ladder answers this with an unfiltered rung
-- whose hits are scored rather than filtered.
--
-- This migration only prepares schema. It links nothing and marks nothing.
--
-- Every statement is safe to re-run.
-- ---------------------------------------------------------------------------

-- 1. Restore jurisdication_code on the roster view.
--
-- The adress -> address rename dropped and recreated this view from the
-- six-column definition in work-script-pgvector.sql, losing the column that
-- migrate_company_investigated_jurisdiction.sql had appended. Both
-- select_opencorp_targets() and select_opencorp_targets_all() select it, so
-- every run of Main_ingest_opencorp_index_companies.py currently dies with
-- UndefinedColumn.
--
-- CREATE OR REPLACE VIEW can only append columns, so the existing six are
-- repeated verbatim and jurisdication_code goes last.
--
-- Note: the batch no longer selects its targets from this view (it reads
-- company_investigated2celex_id directly, since the ladder needs the per-row
-- address and celex_id that a DISTINCT projection cannot supply). The view is
-- repaired anyway because other consumers and ad-hoc queries use it.
CREATE OR REPLACE VIEW celex_facts.company_investigated2lei2corpidx2hr_vw AS
SELECT DISTINCT
    company_name,
    address,
    iso_country_code,
    fk_lei,
    fk_opencorp_idx,
    fk_handelsregister,
    jurisdication_code
FROM celex_facts.company_investigated2celex_id;

-- 2. Link provenance.
--
-- Which relaxation rung produced a link, under what query text and
-- jurisdiction, and how confidently. Without this a link is unauditable: the
-- roster name alone does not say whether the match came from a verbatim
-- jurisdiction-filtered search or from an unfiltered search of a stripped
-- name.
--
-- These live on the link table rather than on opencorp_index_query because the
-- rung is a property of the target-to-hit decision, not of the query: one
-- cached query row is shared by every target whose text normalizes the same
-- way. save_search() also does DELETE-then-INSERT, so a cache refresh cascades
-- company rows (and their links) away -- the link must stay self-describing.
ALTER TABLE celex_facts.company_investigated_celex_id2opencorp_index_company
    ADD COLUMN IF NOT EXISTS resolution_rung         text,
    ADD COLUMN IF NOT EXISTS resolution_query        text,
    ADD COLUMN IF NOT EXISTS resolution_jurisdiction text,
    ADD COLUMN IF NOT EXISTS match_score             numeric(4,3);

CREATE INDEX IF NOT EXISTS ix_ci2oc_resolution_rung
    ON celex_facts.company_investigated_celex_id2opencorp_index_company (resolution_rung);

-- 3. Fields the search response already sends but OpenCorpCompany discards.
--
-- /companies/search returns registered_address (with locality),
-- registered_address_in_full and industry_codes on every hit -- see
-- docs/OpenCorp API.md. OpenCorpCompany sets extra="ignore", so all three are
-- received and thrown away today. Persisting them makes town and industry
-- matching a property of the RESULTS at zero extra API cost, which is what
-- makes an unfiltered rung safe. They must never be used to reject a hit: an
-- Athens-headquartered company registered in Cyprus has a Cyprus registered
-- office, and that disagreement is expected, not disqualifying.
ALTER TABLE celex_facts.opencorp_index_companies
    ADD COLUMN IF NOT EXISTS registered_address_in_full  text,
    ADD COLUMN IF NOT EXISTS registered_address_locality text,
    ADD COLUMN IF NOT EXISTS industry_codes              jsonb;

-- 4. Report: the retry set, bucketed by the health of the jurisdiction it was
-- searched under, so the staged live runs can be sized. Bucket C is the
-- safest to run first (the jurisdiction demonstrably works, so the name is the
-- suspect); bucket A is the riskiest (only the unfiltered rung reaches it, and
-- scoring carries the entire burden).
WITH jurisdiction_health AS (
    SELECT
        jurisdiction_code,
        count(*) FILTER (WHERE error_class IS NULL) AS answered,
        count(*) FILTER (
            WHERE error_class IS NULL AND COALESCE(total_count, 0) = 0
        ) AS zero_hits
    FROM celex_facts.opencorp_index_query
    GROUP BY jurisdiction_code
), bucket AS (
    SELECT
        jurisdiction_code,
        CASE
            WHEN answered = 0 THEN 'D: never answered'
            WHEN zero_hits = answered THEN 'A: dead jurisdiction (100% zero)'
            WHEN zero_hits::numeric / answered >= 0.75 THEN 'B: sparse (>=75% zero)'
            ELSE 'C: healthy jurisdiction (name is the suspect)'
        END AS health
    FROM jurisdiction_health
)
SELECT
    COALESCE(bucket.health, 'E: never searched') AS health,
    count(*) AS na_rows,
    count(DISTINCT roster.company_name) AS na_names
FROM celex_facts.company_investigated2celex_id AS roster
LEFT JOIN bucket
    ON bucket.jurisdiction_code = lower(replace(
        COALESCE(roster.jurisdication_code, roster.iso_country_code), '-', '_'
    ))
WHERE roster.fk_opencorp_idx = 'NA'
GROUP BY 1
ORDER BY 2 DESC;
