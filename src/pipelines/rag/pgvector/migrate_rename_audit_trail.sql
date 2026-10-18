-- ---------------------------------------------------------------------------
-- Migration: rename celex_facts.celex_fact_ingestion_audit_trail
--            to       celex_facts.fact_ingestion_audit_trail
--
-- Drops the redundant "celex_fact_" prefix that duplicates the schema name.
-- Dependent constraints are automatically updated by PostgreSQL on RENAME.
-- Safe to re-run: the rename is skipped when the target name already exists.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'celex_facts'
          AND table_name   = 'celex_fact_ingestion_audit_trail'
    ) THEN
        ALTER TABLE celex_facts.celex_fact_ingestion_audit_trail
            RENAME TO fact_ingestion_audit_trail;
    END IF;
END $$;
