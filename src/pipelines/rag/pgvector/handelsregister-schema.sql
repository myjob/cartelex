-- PostgreSQL DDL for company registry data
-- Normalised from the OpenCorporates-style JSONL structure

-- ─────────────────────────────────────────────────────────────
-- 1. Core company record
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS handelsregister_companies (
    company_number        TEXT        PRIMARY KEY,
    name                  TEXT        NOT NULL,
    current_status        TEXT,
    jurisdiction_code     TEXT,
    registered_address    TEXT,
    retrieved_at          TIMESTAMPTZ,
    -- fields from all_attributes
    register_art          TEXT,                    -- _registerArt
    register_nummer       TEXT,                    -- _registerNummer
    federal_state         TEXT,
    native_company_number TEXT,
    registered_office     TEXT,
    registrar             TEXT,
    former_registrar      TEXT
);

-- ─────────────────────────────────────────────────────────────
-- 2. Boolean publication / data flags  (all_attributes.additional_data)
--    Column names use ASCII equivalents for non-ASCII keys:
--      VÖ → voe
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS handelsregister_company_additional_data (
    company_number TEXT    PRIMARY KEY
                           REFERENCES handelsregister_companies (company_number)
                           ON DELETE CASCADE,
    ad             BOOLEAN,   -- AD
    cd             BOOLEAN,   -- CD
    dk             BOOLEAN,   -- DK
    hd             BOOLEAN,   -- HD
    si             BOOLEAN,   -- SI
    ut             BOOLEAN,   -- UT
    voe            BOOLEAN    -- VÖ
);

-- ─────────────────────────────────────────────────────────────
-- 3. Officers (directors, proxies, etc.)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS handelsregister_officers (
    id             SERIAL  PRIMARY KEY,
    company_number TEXT    NOT NULL
                           REFERENCES handelsregister_companies (company_number)
                           ON DELETE CASCADE,
    name           TEXT,
    position       TEXT,       -- e.g. Geschäftsführer, Prokurist
    type           TEXT,       -- e.g. person
    start_date     DATE,
    end_date       DATE,       -- NULL = currently active
    -- from other_attributes
    firstname      TEXT,
    lastname       TEXT,
    city           TEXT,
    flag           TEXT,       -- representation authority text
    dismissed      BOOLEAN,
    reference_no   INTEGER,
    title          TEXT
);

CREATE INDEX IF NOT EXISTS idx_officers_company    ON handelsregister_officers (company_number);
CREATE INDEX IF NOT EXISTS idx_officers_name       ON handelsregister_officers (name);
CREATE INDEX IF NOT EXISTS idx_officers_end_date   ON handelsregister_officers (end_date);

-- ─────────────────────────────────────────────────────────────
-- 4. Previous / former company names
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS handelsregister_previous_names (
    id             SERIAL  PRIMARY KEY,
    company_number TEXT    NOT NULL
                           REFERENCES handelsregister_companies (company_number)
                           ON DELETE CASCADE,
    company_name   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_previous_names_company ON handelsregister_previous_names (company_number);
