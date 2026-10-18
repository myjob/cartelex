-- ---------------------------------------------------------------------------
-- Migration: add the 'fact' source type to the data lineage registry
--
-- Derived facts (e.g. court_list, computed from court_list_raw) have no direct
-- chunk evidence. To record their provenance we catalogue the predecessor fact
-- itself as a source_registry row of source_type 'fact', keyed on a new
-- source_fact_id FK to celex_facts.facts.id, and link the derived fact to it
-- with relation_type 'derived_from' in fact_source_link.
--
-- Safe to re-run: every statement is guarded with IF NOT EXISTS.
--
-- NOTE: ALTER TYPE ... ADD VALUE cannot run inside a transaction block on
-- PostgreSQL < 12. Run this file outside an explicit BEGIN/COMMIT (psql -f
-- autocommits each statement, which satisfies this).
-- ---------------------------------------------------------------------------

ALTER TYPE celex_facts.source_type_enum ADD VALUE IF NOT EXISTS 'fact';

ALTER TABLE celex_facts.source_registry
    ADD COLUMN IF NOT EXISTS source_fact_id uuid
        REFERENCES celex_facts.facts (id) ON DELETE CASCADE;

-- Normalization: one source_registry row per distinct predecessor fact.
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_registry_fact
    ON celex_facts.source_registry (source_fact_id)
    WHERE source_fact_id IS NOT NULL;
