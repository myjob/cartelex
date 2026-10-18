-- ---------------------------------------------------------------------------
-- Migration: celex_facts.llm_index_companies -- the LLM-asserted company index.
--
-- OpenCorporates resolved rather more than 4,000 of the ~6,100 roster rows and
-- failed on 1,738 of them (1,704 distinct company names), which carry
-- fk_opencorp_idx = 'NA'. For those companies every deterministic path is
-- exhausted: GLEIF missed them, the German Handelsregister missed them, and the
-- OpenCorporates relaxation ladder asked under every rung it had and got
-- nothing back. Most are decades-old European undertakings that a general
-- knowledge model can still profile from the decision's own name and address.
--
-- Main_LLM_corp_index_searcher.py asks the corp_index LLM profile
-- (settings.CORP_INDEX_LLM_PROVIDER / CORP_INDEX_LLM_MODEL_ID) for a
-- structured profile per name and writes it here.
--
-- What this table is NOT: a register. Every value in it is a model assertion
-- that no registry confirmed, which is why llm_model_id is NOT NULL -- a row is
-- only interpretable together with its author, and re-asking under a newer
-- model overwrites it in place. Nothing here is written back to
-- company_investigated2celex_id: fk_lei = 'NA' there keeps meaning "GLEIF says
-- no", and an LLM-recalled LEI must never be promoted into it unverified.
--
-- Grain: one row per company_name (the functional key), upserted. Resume works
-- by absence, so an interrupted batch continues rather than restarts. Every
-- column except company_name and created_at is overwritten on conflict, so a
-- --refresh pass is what backfills columns added after the first run.
--
-- Safe to re-run: every statement is guarded with IF NOT EXISTS. The batch also
-- calls ensure_tables() at startup, so this file is the DBA path, not a
-- precondition.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS celex_facts.llm_index_companies (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Verbatim roster name, joinable onto
    -- company_investigated2celex_id.company_name.
    company_name        text NOT NULL UNIQUE,

    canonical_name      text,
    abbreviation        text,
    legal_form          text,
    -- Free text rather than a date: the honest answer is often a bare or circa
    -- year, and a date column would force a day and a month to be invented.
    established_since   text,
    life_cycle_status   text,
    -- Who holds the entity now and what event put it there. Most of these
    -- companies are unresolvable precisely because they were absorbed decades
    -- ago; without this column that answer had nowhere to go.
    succession          text,
    products_services   text,
    geo_market          text,
    headquarter_address text,
    country             text,
    jurisdiction        text,

    -- Register-issued natural keys. 'NA' when the model had none -- never NULL,
    -- so "asked and did not know" stays distinct from "no row for this company".
    lei                 varchar(64),
    euid                varchar(64),
    bris                varchar(64),
    handelsregister     varchar(64),
    siren               varchar(64),
    -- Catch-all for every registrar with no column of its own (the Belgian CBE,
    -- the Dutch KVK, VAT, Companies House, RCS, CVR, ...). A number is
    -- uninterpretable and unverifiable without the registrar that issued it, so
    -- the two travel together.
    generic_corp_id     varchar(64),
    generic_corp_registrar text,

    llm_model_id        varchar(128) NOT NULL,

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Bring an already-created table forward. CREATE TABLE IF NOT EXISTS above is a
-- no-op once the table exists, so a column added after the first deployment
-- arrives here instead. LlmCompanyIndexRepository.ensure_tables() issues the
-- same statements from the ORM model at startup; this block is the DBA path.
ALTER TABLE celex_facts.llm_index_companies
    ADD COLUMN IF NOT EXISTS established_since      text,
    ADD COLUMN IF NOT EXISTS succession             text,
    ADD COLUMN IF NOT EXISTS generic_corp_id        varchar(64),
    ADD COLUMN IF NOT EXISTS generic_corp_registrar text;

-- Retire the two columns a generic id-and-registrar pair made redundant: a KVK
-- number belongs in generic_corp_id with 'KVK Handelsregister' beside it, and a
-- VAT number likewise. Neither ever held a value -- 26 rows had been refreshed
-- when they were dropped and every one answered 'NA' -- so this loses nothing.
-- Guarded, so it is a no-op on an installation that never had them.
ALTER TABLE celex_facts.llm_index_companies
    DROP COLUMN IF EXISTS kvk,
    DROP COLUMN IF EXISTS vat_number;

CREATE INDEX IF NOT EXISTS ix_llm_index_companies_country
    ON celex_facts.llm_index_companies (country);

CREATE INDEX IF NOT EXISTS ix_llm_index_companies_lei
    ON celex_facts.llm_index_companies (lei);

-- Coverage of the residue this table exists for: how much of the
-- fk_opencorp_idx = 'NA' backlog has been profiled, and how often the model
-- actually produced an identifier rather than 'NA'.
--
-- SELECT
--     count(DISTINCT roster.company_name) AS na_names,
--     count(DISTINCT stored.company_name) AS profiled,
--     count(*) FILTER (WHERE stored.lei  <> 'NA') AS with_lei,
--     count(*) FILTER (WHERE stored.euid <> 'NA') AS with_euid,
--     count(*) FILTER (WHERE stored.bris <> 'NA') AS with_bris
-- FROM celex_facts.company_investigated2lei2corpidx2hr_vw AS roster
-- LEFT JOIN celex_facts.llm_index_companies AS stored
--        ON stored.company_name = roster.company_name
-- WHERE roster.fk_opencorp_idx = 'NA';
