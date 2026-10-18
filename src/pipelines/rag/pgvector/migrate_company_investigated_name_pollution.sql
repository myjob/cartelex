-- ---------------------------------------------------------------------------
-- Migration: strip registered addresses out of roster company names.
--
-- companies_investigated stored some companies with the registered address
-- concatenated onto the name, and Main_ingest_company_investigated2celex.py
-- wrote that string into company_investigated2celex_id.company_name verbatim:
--
--     company_name | Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy
--     adress       | Strada 2, Pal. F7, 20090 Assago, Milan, Italy
--
-- Two things break as a result. (a) No commercial register matches such a name,
-- so the fk_lei substring search, the Handelsregister exact-name search and the
-- OpenCorporates full-text search all miss -- indistinguishably from a company
-- that genuinely is not on the register. (b) The enrichment join in
-- _build_roster matched companies_country by exact name, and that fact names the
-- same company bare ('Praoil'), so the country was dropped to NULL on every such
-- row -- 436 rows across 109 CELEX documents had an address but no
-- iso_country_code, and therefore no jurisdication_code either.
--
-- The ingest path is fixed separately and independently (derive_clean_company_name
-- + match_enrichment in Main_ingest_company_investigated2celex.py); this
-- migration repairs the rows already loaded. Run it BEFORE re-running the batch:
-- update_missing_enrichment matches on (celex_id, company_name), so the names
-- must already be in their cleaned form for the backfill to find these rows.
--
-- The address is removed only where the row's own adress column is exactly the
-- tail of company_name -- never a guess at where a name ends. Comparing with
-- right(company_name, length(adress)) rather than LIKE '%' || adress avoids
-- having to escape % and _ occurring inside an address.
--
-- Both statements are safe to re-run: statement 1 is a no-op once the names
-- carry no address suffix, and statement 2 only reports.
-- ---------------------------------------------------------------------------

-- 1. Strip the address suffix and the separator it left behind.
--
-- company_investigated2celex_id has UNIQUE (company_name, celex_id), and two
-- rows in one document can clean to the same name -- one company listed once per
-- site, each entry naming a different plant address ('Klöckner-Werke AG,
-- Georgsmarienwerke, Osnabrück, Germany' and 'Klöckner-Werke AG, HüttenBremen,
-- Bremen, Germany'), or a company loaded twice, once polluted and once clean.
-- Those rows are left address-bearing rather than aborting the whole migration
-- on a unique violation, so both sites survive and no address is lost.
-- _resolve_clean_names applies the same rule on the ingest side, so a re-run of
-- the batch agrees with what this statement did.
--
-- Both collision classes must be excluded, and they need separate guards: a
-- clash with a row that is ALREADY clean is visible in the table, but a clash
-- between two rows that would BOTH clean to the same name is not -- the
-- subquery sees the statement's start snapshot, where both are still polluted,
-- so a guard reading only the table lets both through and the second one then
-- violates the constraint.
WITH candidate AS (
    SELECT id,
           celex_id,
           rtrim(
               left(company_name, length(company_name) - length(adress)), ' ,;'
           ) AS clean_name
    FROM celex_facts.company_investigated2celex_id
    WHERE adress IS NOT NULL
      AND company_name <> adress
      AND right(company_name, length(adress)) = adress
),
eligible AS (
    SELECT candidate.id, candidate.clean_name
    FROM candidate
    -- Never strip a name down to nothing.
    WHERE length(candidate.clean_name) > 0
      -- (a) another row in the document already carries that exact name.
      AND NOT EXISTS (
          SELECT 1
          FROM celex_facts.company_investigated2celex_id AS other
          WHERE other.celex_id = candidate.celex_id
            AND other.id <> candidate.id
            AND other.company_name = candidate.clean_name
      )
      -- (b) another row in the document would clean to the same name.
      AND NOT EXISTS (
          SELECT 1
          FROM candidate AS other
          WHERE other.celex_id = candidate.celex_id
            AND other.id <> candidate.id
            AND other.clean_name = candidate.clean_name
      )
)
UPDATE celex_facts.company_investigated2celex_id AS target
SET company_name = eligible.clean_name
FROM eligible
WHERE eligible.id = target.id;

-- 2. Report the rows deliberately left address-bearing, for manual follow-up.
--
-- Expected: the two collision groups described above (4 rows). Anything else
-- here is a name that cleans to empty or to a duplicate and wants a human look.
-- Returns no rows once nothing is left to repair.
SELECT celex_id,
       company_name,
       adress,
       rtrim(left(company_name, length(company_name) - length(adress)), ' ,;')
           AS would_become
FROM celex_facts.company_investigated2celex_id
WHERE adress IS NOT NULL
  AND company_name <> adress
  AND right(company_name, length(adress)) = adress
ORDER BY celex_id, company_name;
