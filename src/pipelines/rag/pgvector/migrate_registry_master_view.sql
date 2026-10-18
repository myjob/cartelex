-- ---------------------------------------------------------------------------
-- Migration: celex_facts.company_registry_master_vw -- one tall view of the
-- registry master attributes every company source holds.
--
-- ORDERING: apply AFTER migrate_opencorp_relaxation_retry.sql (which adds
-- resolution_rung / match_score to the N:M link table and
-- registered_address_in_full to opencorp_index_companies) and after
-- src/pipelines/corp_index/migrate_llm_index_companies.sql. The alphabetical
-- glob in the INSTALL runbook puts 'registry' after 'opencorp', which is why
-- this file is NOT named migrate_company_registry_master_vw.sql -- that would
-- sort before its own dependencies and break a fresh-database replay. The
-- corp_index directory is not covered by that glob at all, so the DO block
-- below fails loudly rather than creating a half-view.
--
-- SHAPE: tall, one row per (roster row, source), UNION ALL over four branches
-- with a `source` discriminator and one common attribute set. Not a wide pivot
-- -- a pivot needs a new column family per source and still cannot say which
-- source a value came from.
--
-- ANCHOR: celex_facts.company_investigated2celex_id.id. It is the only key
-- common to all four sources, so this is a roster-anchored view, not a global
-- union over the 8.8M register records.
--
-- SCOPE: registry MASTER ATTRIBUTES only -- name, identifiers, jurisdiction,
-- country, status, address, dates, registry URL. Ownership and parent
-- structure are deliberately out of scope and stay in the detail jsonb.
--
-- THE THREE SENTINEL TRAPS this view exists to hide from consumers:
--   1. roster.fk_lei and roster.fk_handelsregister hold the literal 'NA' for
--      "looked up, no match". 5,554 of 6,383 rows say 'NA' on fk_lei.
--   2. roster.fk_opencorp_idx is NEVER an id. It only ever holds
--      'LINKED' / 'NA' / 'AMBIGUOUS' / ''. The real link is the N:M table.
--   3. llm_index_companies uses the literal 'NA' rather than NULL for
--      "asked, did not know", on every identifier column.
--
-- THE CANDIDATE EXPLOSION: company_investigated_celex_id2opencorp_index_company
-- is a CANDIDATE pool, not a resolved link -- 83,423 rows over 4,097 roster
-- rows, average 20.4 candidates each and 2,436 at worst. Only 5,578 rows carry
-- a match_score, and match_score IS NOT NULL is exactly equivalent to
-- resolution_rung IS NOT NULL. The OpenCorporates branch therefore restricts to
-- scored rows and picks exactly one per roster row through a LATERAL LIMIT 1.
-- Without both, this view would turn 6,383 anchors into roughly 83,000 rows.
--
-- WHY LATERAL AND NOT A CTE: a DISTINCT ON pre-aggregate over the whole link
-- table would be materialised and re-scanned on every call, including calls
-- that ask for one company. The LATERAL rides the leading column of
-- company_investigated2opencorp_unique, so a single-company query is an index
-- probe. For the same reason the roster table is repeated in each branch
-- instead of being lifted into a CTE -- a multiply-referenced CTE is
-- materialised and blocks qualifier pushdown through the UNION ALL.
--
-- Measured branch counts on 20260830 -- lei 829, handelsregister 295,
-- opencorporates 2,175, llm 6,371, total 9,670.
--
-- ALWAYS FOLLOW THIS FILE WITH schema_comments.sql. The DROP VIEW below discards
-- every COMMENT ON the view carried, so replaying this migration alone leaves the
-- view undocumented and fails test_schema_comments::test_every_relation_has_a_comment.
--
-- Safe to re-run.
-- ---------------------------------------------------------------------------

-- 0. Preconditions. A missing column would otherwise fail with a bare
-- UndefinedColumn pointing at a line of SQL rather than at the cause.
DO $$
BEGIN
    IF to_regclass('celex_facts.llm_index_companies') IS NULL THEN
        RAISE EXCEPTION
            'apply src/pipelines/corp_index/migrate_llm_index_companies.sql first';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'celex_facts'
          AND table_name = 'company_investigated_celex_id2opencorp_index_company'
          AND column_name = 'match_score'
    ) THEN
        RAISE EXCEPTION
            'apply migrate_opencorp_relaxation_retry.sql first -- match_score is absent';
    END IF;
END
$$;

-- 1. The one index this view wants.
--
-- 5,578 of the 83,423 link rows carry a score, so this partial index is a few
-- hundred KB. It turns the per-company LATERAL into a two-row index scan and,
-- more importantly, keeps a whole-view scan from reading the 77,845 unranked
-- candidate rows at all. The existing ix_ci2oc_resolution_rung is useless here
-- -- wrong leading column and almost no selectivity.
CREATE INDEX IF NOT EXISTS ix_ci2oc_scored_candidates
    ON celex_facts.company_investigated_celex_id2opencorp_index_company
       (fk_company_investigated, match_score DESC)
    WHERE match_score IS NOT NULL;

-- 2. The view.
--
-- DROP + CREATE, never CREATE OR REPLACE. OR REPLACE can only append columns,
-- which is how company_investigated2lei2corpidx2hr_vw lost jurisdication_code
-- once already. See docs/Data Model Quest Denormalization.md.
DROP VIEW IF EXISTS celex_facts.company_registry_master_vw;

CREATE VIEW celex_facts.company_registry_master_vw AS

-- Country and jurisdiction names, deduplicated to one row per lowercased name.
-- llm_index_companies.country and .jurisdiction are FREE TEXT ('Spain',
-- 'Netherlands') -- unlike every other source, which emits codes -- and this is
-- the only name-to-code mapping that exists in the database. Deduplicating is
-- not cosmetic: an unguarded join on jurisdiction_name would fan the llm branch
-- out if two GLEIF rows ever shared a label. is_subdivision ASC puts the
-- country-level row first, which is the right answer for a bare country name.
WITH gleif_by_name AS (
    SELECT DISTINCT ON (lower(btrim(jurisdiction_name)))
        lower(btrim(jurisdiction_name)) AS name_key,
        jurisdiction_code,
        iso_country_code
    FROM celex_facts.gleif_jurisdication2countrycode
    ORDER BY lower(btrim(jurisdiction_name)), is_subdivision, jurisdiction_code
)

-- ---------------------------------------------------------------------------
-- Branch 1 of 4 -- GLEIF LEI. Rank 1.
-- Joins corp_lei on its primary key. This view never trigram-scans the 3.3M
-- rows -- 829 roster rows carry a real LEI and every one of them is a PK probe.
-- ---------------------------------------------------------------------------
SELECT
    roster.id                                       AS company_key,
    roster.company_name                             AS company_name,
    roster.celex_id                                 AS celex_id,
    NULLIF(btrim(roster.iso_country_code), '')      AS roster_iso_country_code,
    NULLIF(btrim(roster.jurisdication_code), '')    AS roster_jurisdiction_code,

    'lei'::text                                     AS source,
    1::smallint                                     AS source_rank,
    'roster.fk_lei'::text                           AS link_method,
    NULL::numeric(4,3)                              AS link_confidence,

    NULLIF(btrim(lei.legal_name), '')               AS registry_name,
    lei.lei                                         AS registry_id,
    'LEI'::text                                     AS registry_id_scheme,
    NULLIF(btrim(lei.legal_jurisdiction), '')       AS jurisdiction_code,
    COALESCE(gj.iso_country_code,
             NULLIF(btrim(lei.country), ''))        AS iso_country_code,
    NULLIF(concat_ws(', ',
        NULLIF(btrim(lei.legal_address_first_line), ''),
        NULLIF(btrim(lei.legal_address_additional_line), ''),
        NULLIF(btrim(lei.legal_address_postal_code), ''),
        NULLIF(btrim(lei.legal_address_city), ''),
        NULLIF(btrim(lei.legal_address_region), '')), '')
                                                    AS registered_address,
    -- Deliberately NULL. corp_lei.entity_category is a GLEIF category
    -- (GENERAL / BRANCH / FUND / SOLE_PROPRIETOR), not a legal form, and
    -- surfacing it here would corrupt the column for the two sources that do
    -- carry one.
    NULL::text                                      AS legal_form,
    NULLIF(btrim(lei.entity_status), '')            AS status_text,
    CASE upper(btrim(lei.entity_status))
        WHEN 'ACTIVE'   THEN true
        WHEN 'INACTIVE' THEN false
        ELSE NULL
    END                                             AS is_active,
    -- GLEIF's dates are LEI-REGISTRATION dates, not incorporation dates.
    -- Putting them here would be a semantic lie, so the slot stays empty.
    NULL::date                                      AS incorporation_date,
    NULL::date                                      AS dissolution_date,
    ('https://search.gleif.org/#/record/' || lei.lei)
                                                    AS registry_url,
    -- corp_lei.created_at is timestamp WITHOUT time zone (models.py). Cast
    -- explicitly rather than letting UNION type resolution apply the session
    -- TimeZone.
    lei.created_at::timestamptz                     AS source_observed_at,
    false                                           AS has_detail
FROM celex_facts.company_investigated2celex_id AS roster
JOIN celex_facts.corp_lei AS lei
    -- The NULLIF pair is what keeps the 5,554 'NA' rows out. It also stops a
    -- pointless PK probe for the literal string 'NA'.
    ON lei.lei = NULLIF(NULLIF(btrim(roster.fk_lei), 'NA'), '')
LEFT JOIN celex_facts.gleif_jurisdication2countrycode AS gj
    ON gj.jurisdiction_code = btrim(lei.legal_jurisdiction)

UNION ALL

-- ---------------------------------------------------------------------------
-- Branch 2 of 4 -- German Handelsregister. Rank 2.
-- Joins on the primary key. The fuzzy folded-name match already happened
-- upstream in resolve_handelsregister_numbers -- ix_hr_matchkey is NOT used
-- here and must not be, or the view would re-run a 5.4M-row fuzzy match.
-- ---------------------------------------------------------------------------
SELECT
    roster.id,
    roster.company_name,
    roster.celex_id,
    NULLIF(btrim(roster.iso_country_code), ''),
    NULLIF(btrim(roster.jurisdication_code), ''),

    'handelsregister'::text,
    2::smallint,
    'roster.fk_handelsregister'::text,
    NULL::numeric(4,3),

    NULLIF(btrim(hr.name), ''),
    hr.company_number,
    'handelsregister'::text,
    upper(replace(NULLIF(btrim(hr.jurisdiction_code), ''), '_', '-')),
    COALESCE(
        upper(split_part(NULLIF(btrim(hr.jurisdiction_code), ''), '_', 1)),
        'DE'),
    -- Fills on only 1,697,250 of 5,461,351 source rows, so NULL here is normal.
    -- registered_office is 0 of 5.4M and is deliberately not projected.
    NULLIF(btrim(hr.registered_address), ''),
    -- register_art (HRA / HRB) is a register SECTION, not a legal form.
    NULL::text,
    NULLIF(btrim(hr.current_status), ''),
    -- NULL on purpose. The current_status vocabulary of the de_offeneregister
    -- dump has never been measured, and a wrong `false` would mark live German
    -- companies dissolved in every downstream graph. Measure it before adding a
    -- CASE here -- the query is in the verification section of the plan.
    NULL::boolean,
    NULL::date,
    NULL::date,
    NULL::text,
    hr.retrieved_at,
    false
FROM celex_facts.company_investigated2celex_id AS roster
JOIN celex_facts.handelsregister_companies AS hr
    ON hr.company_number
       = NULLIF(NULLIF(btrim(roster.fk_handelsregister), 'NA'), '')

UNION ALL

-- ---------------------------------------------------------------------------
-- Branch 3 of 4 -- OpenCorporates. Rank 3.
--
-- roster.fk_opencorp_idx is NOT joined here and never can be -- it only holds
-- 'LINKED' / 'NA' / 'AMBIGUOUS' / ''. The identity lives in the N:M table.
--
-- The LATERAL picks exactly ONE candidate per roster row. Order is
-- match_score DESC, then rung precedence, then the candidate uuid. The last
-- term is not decoration: without it the pick is non-deterministic across
-- plans, and a view whose answer changes between runs is unusable as an
-- identity source. Swap the first two terms if rung should outrank score --
-- this is the only place that decision is made, and link_method plus
-- link_confidence are exposed so a consumer can re-rank without changing it.
--
-- Rung order is verbatim, verbatim@any, split, no-paren, no-role, core,
-- core@country, core@any -- from opencorp_resolution_ladder.py. verbatim@any
-- sits second deliberately: it is the only rung that reaches an offshore
-- registration. array_position returns NULL for an unknown rung, which sorts
-- last, so a new rung degrades to least-preferred instead of crashing.
-- ---------------------------------------------------------------------------
SELECT
    roster.id,
    roster.company_name,
    roster.celex_id,
    NULLIF(btrim(roster.iso_country_code), ''),
    NULLIF(btrim(roster.jurisdication_code), ''),

    'opencorporates'::text,
    3::smallint,
    pick.resolution_rung,
    pick.match_score,

    NULLIF(btrim(COALESCE(det.name, oc.name)), ''),
    -- Unique only TOGETHER WITH jurisdiction_code. Both are in the view.
    oc.company_number,
    'opencorporates'::text,
    upper(replace(NULLIF(btrim(oc.jurisdiction_code), ''), '_', '-')),
    COALESCE(
        gjo.iso_country_code,
        upper(split_part(NULLIF(btrim(oc.jurisdiction_code), ''), '_', 1))),
    NULLIF(btrim(COALESCE(det.registered_address_in_full,
                          oc.registered_address_in_full)), ''),
    NULLIF(btrim(COALESCE(det.company_type, oc.company_type)), ''),
    NULLIF(btrim(COALESCE(det.current_status, oc.current_status)), ''),
    CASE
        WHEN COALESCE(det.inactive, oc.inactive) IS NULL THEN NULL
        ELSE NOT COALESCE(det.inactive, oc.inactive)
    END,
    COALESCE(det.incorporation_date, oc.incorporation_date),
    COALESCE(det.dissolution_date, oc.dissolution_date),
    COALESCE(det.registry_url, oc.registry_url,
             det.opencorporates_url, oc.opencorporates_url),
    COALESCE(det.fetched_at, oc.created_at),
    (det.id IS NOT NULL)
FROM celex_facts.company_investigated2celex_id AS roster
CROSS JOIN LATERAL (
    SELECT
        link.fk_opencorp_index_company,
        link.resolution_rung,
        link.match_score
    FROM celex_facts.company_investigated_celex_id2opencorp_index_company AS link
    WHERE link.fk_company_investigated = roster.id
      AND link.match_score IS NOT NULL
    ORDER BY
        link.match_score DESC,
        array_position(
            ARRAY['verbatim', 'verbatim@any', 'split', 'no-paren',
                  'no-role', 'core', 'core@country', 'core@any'],
            link.resolution_rung),
        link.fk_opencorp_index_company
    LIMIT 1
) AS pick
JOIN celex_facts.opencorp_index_companies AS oc
    ON oc.id = pick.fk_opencorp_index_company
-- Detail is an OVERLAY, not a fifth source. It is keyed on exactly the identity
-- of the search row -- same registrar, same record, richer fetch -- so a UNION
-- branch would emit two rows that cannot meaningfully disagree, and would start
-- doubling the OpenCorporates branch as the API budget fills this table. Rides
-- uq_opencorp_detail_co.
LEFT JOIN celex_facts.opencorp_index_company_detail AS det
    ON det.jurisdiction_code = oc.jurisdiction_code
   AND det.company_number    = oc.company_number
LEFT JOIN celex_facts.gleif_jurisdication2countrycode AS gjo
    ON gjo.jurisdiction_code
       = upper(replace(btrim(oc.jurisdiction_code), '_', '-'))

UNION ALL

-- ---------------------------------------------------------------------------
-- Branch 4 of 4 -- the LLM company index. Rank 4, the LOWEST-TRUST source.
--
-- Every value here is a model assertion that no registry confirmed. It is
-- included because it is the only source that reaches the long tail -- 6,371 of
-- 6,383 roster rows -- and is excluded from any use as an identity key. See the
-- header of migrate_llm_index_companies.sql: an LLM-recalled LEI must never be
-- promoted into a register position unverified.
--
-- 'NA' is the literal this table writes for "asked, did not know", on every
-- identifier column. Every projection below strips it.
-- ---------------------------------------------------------------------------
SELECT
    roster.id,
    roster.company_name,
    roster.celex_id,
    NULLIF(btrim(roster.iso_country_code), ''),
    NULLIF(btrim(roster.jurisdication_code), ''),

    'llm'::text,
    4::smallint,
    'llm_index_companies.company_name'::text,
    NULL::numeric(4,3),

    COALESCE(NULLIF(NULLIF(btrim(llm.canonical_name), 'NA'), ''),
             roster.company_name),
    -- Precedence: the most widely resolvable identifier first. Mixing id spaces
    -- in one column is only legal because registry_id_scheme names the space,
    -- and the two ladders are kept in the same order on purpose.
    COALESCE(
        NULLIF(NULLIF(btrim(llm.lei), 'NA'), ''),
        NULLIF(NULLIF(btrim(llm.euid), 'NA'), ''),
        NULLIF(NULLIF(btrim(llm.bris), 'NA'), ''),
        NULLIF(NULLIF(btrim(llm.handelsregister), 'NA'), ''),
        NULLIF(NULLIF(btrim(llm.siren), 'NA'), ''),
        NULLIF(NULLIF(btrim(llm.generic_corp_id), 'NA'), '')),
    CASE
        WHEN NULLIF(NULLIF(btrim(llm.lei), 'NA'), '') IS NOT NULL
            THEN 'LEI'
        WHEN NULLIF(NULLIF(btrim(llm.euid), 'NA'), '') IS NOT NULL
            THEN 'EUID'
        WHEN NULLIF(NULLIF(btrim(llm.bris), 'NA'), '') IS NOT NULL
            THEN 'BRIS'
        WHEN NULLIF(NULLIF(btrim(llm.handelsregister), 'NA'), '') IS NOT NULL
            THEN 'handelsregister'
        WHEN NULLIF(NULLIF(btrim(llm.siren), 'NA'), '') IS NOT NULL
            THEN 'SIREN'
        WHEN NULLIF(NULLIF(btrim(llm.generic_corp_id), 'NA'), '') IS NOT NULL
            THEN NULLIF(NULLIF(btrim(llm.generic_corp_registrar), 'NA'), '')
        ELSE NULL
    END,
    gjl.jurisdiction_code,
    -- llm.country is free text. Accept a bare 2-letter code as already-ISO,
    -- otherwise resolve the name through the GLEIF label list. Measured
    -- 20260830 -- 787 already ISO plus 3,808 name-resolvable of 5,654 non-'NA'.
    CASE
        WHEN NULLIF(NULLIF(btrim(llm.country), 'NA'), '') ~ '^[A-Za-z]{2}$'
            THEN upper(btrim(llm.country))
        ELSE gcl.iso_country_code
    END,
    -- Headquarters, not the registered office. Documented on the column.
    NULLIF(NULLIF(btrim(llm.headquarter_address), 'NA'), ''),
    -- Long free-text sentences, not codes. Same for status_text below.
    NULLIF(NULLIF(btrim(llm.legal_form), 'NA'), ''),
    NULLIF(NULLIF(btrim(llm.life_cycle_status), 'NA'), ''),
    -- Not derivable from a free-text sentence without guessing.
    NULL::boolean,
    -- established_since is deliberately free text ('circa 1950', a bare year)
    -- and must not be cast to a date.
    NULL::date,
    NULL::date,
    NULL::text,
    COALESCE(llm.updated_at, llm.created_at),
    false
FROM celex_facts.company_investigated2celex_id AS roster
JOIN celex_facts.llm_index_companies AS llm
    ON llm.company_name = roster.company_name
LEFT JOIN gleif_by_name AS gjl
    ON gjl.name_key
       = lower(NULLIF(NULLIF(btrim(llm.jurisdiction), 'NA'), ''))
LEFT JOIN gleif_by_name AS gcl
    ON gcl.name_key
       = lower(NULLIF(NULLIF(btrim(llm.country), 'NA'), ''));
