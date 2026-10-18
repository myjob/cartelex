-- ---------------------------------------------------------------------------
-- Data Lineage Registry — Option 2 + selective Option 3 edge semantics
--
-- Creates:
--   celex_facts.source_registry   — normalized catalog of all evidence sources
--   celex_facts.fact_source_link  — join table linking facts to their sources
--
-- Safe to re-run: all DDL is guarded with IF NOT EXISTS.
-- ---------------------------------------------------------------------------

-- Enum: source types that can originate a fact
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'celex_facts' AND t.typname = 'source_type_enum'
    ) THEN
        CREATE TYPE celex_facts.source_type_enum AS ENUM (
            'chunk',          -- raw document chunk from pg_embedding_qwen
            'csv_human',      -- human-curated CSV index row
            'book_case',      -- case analysis chunk from pg_embedding_book_qwen
            'lei_corporate',  -- LEI corporate registry record
            'heideltime',     -- HeidelTime temporal expression extraction
            'fact'            -- predecessor fact a derived fact was computed from
        );
    END IF;
END $$;

-- Enum: Option 3 edge relation types (used selectively on high-stakes facts)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'celex_facts' AND t.typname = 'relation_type_enum'
    ) THEN
        CREATE TYPE celex_facts.relation_type_enum AS ENUM (
            'extracted_from',       -- primary extraction: chunk or heideltime → fact
            'corroborated_by',      -- secondary confirmation from another source
            'entity_resolved_via',  -- entity resolution step (e.g. LEI lookup)
            'derived_from'          -- computed/transformed value (e.g. normalised date)
        );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- source_registry: one row per distinct piece of evidence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS celex_facts.source_registry (
    source_id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type            celex_facts.source_type_enum NOT NULL,

    -- common: document scope
    celex_id               TEXT,

    -- chunk / book_case: link to embedding table
    chunk_id               UUID,           -- stable FK to pg_embedding_qwen.id
    chunk_seq              INT[],          -- doc_pos values (for backfill / display)

    -- heideltime: link to extraction table
    heideltime_event_id    UUID,           -- FK to heideltime_extractions.id

    -- lei_corporate: LEI identifier
    lei_id                 TEXT,           -- FK to corp_lei.lei

    -- csv_human: row in the CSV export
    csv_row                INT,

    -- fact: predecessor fact a derived fact was computed from
    source_fact_id         UUID
        REFERENCES celex_facts.facts (id) ON DELETE CASCADE,

    -- fallback description for synthetic / composite sources
    description            TEXT,

    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_source_registry_celex
    ON celex_facts.source_registry (celex_id);

CREATE INDEX IF NOT EXISTS ix_source_registry_type
    ON celex_facts.source_registry (source_type);

CREATE INDEX IF NOT EXISTS ix_source_registry_chunk_id
    ON celex_facts.source_registry (chunk_id)
    WHERE chunk_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_source_registry_heideltime
    ON celex_facts.source_registry (heideltime_event_id)
    WHERE heideltime_event_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- fact_source_link: many-to-many with lineage metadata
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS celex_facts.fact_source_link (
    fact_id            UUID     NOT NULL
        REFERENCES celex_facts.facts (id) ON DELETE CASCADE,
    source_id          UUID     NOT NULL
        REFERENCES celex_facts.source_registry (source_id),

    -- which pipeline / module produced this link
    extraction_method  TEXT     NOT NULL,

    -- structured producer provenance (mirrors fact_ingestion_audit_trail)
    pipeline           TEXT,        -- producing pipeline (e.g. 'universal')
    dspy_module        TEXT,        -- producing DSPy module class name

    -- Option 3 edge semantics: relation between source and fact
    relation_type      celex_facts.relation_type_enum NOT NULL DEFAULT 'extracted_from',

    -- trust_rank: 1 (weakest) … 10 (strongest)
    -- defaults by source_type: csv_human=9, book_case=8, lei_corporate=7,
    --                           heideltime=6, chunk=5
    trust_rank         SMALLINT NOT NULL DEFAULT 5
        CONSTRAINT ck_fact_source_link_trust_rank CHECK (trust_rank BETWEEN 1 AND 10),

    -- conflict detection
    conflict_flag      BOOLEAN  NOT NULL DEFAULT FALSE,
    conflict_note      TEXT,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (fact_id, source_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_source_link_fact
    ON celex_facts.fact_source_link (fact_id);

CREATE INDEX IF NOT EXISTS ix_fact_source_link_source
    ON celex_facts.fact_source_link (source_id);

-- ---------------------------------------------------------------------------
-- Backfill for already-deployed databases: add producer provenance columns.
-- Safe to re-run.
-- ---------------------------------------------------------------------------
ALTER TABLE celex_facts.fact_source_link ADD COLUMN IF NOT EXISTS pipeline    TEXT;
ALTER TABLE celex_facts.fact_source_link ADD COLUMN IF NOT EXISTS dspy_module TEXT;
