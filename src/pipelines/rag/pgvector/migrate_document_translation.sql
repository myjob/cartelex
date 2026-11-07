-- Migration: Markdown translation to English
-- =========================================================================
-- 1. ``celex_facts.document_registry.original_file_md`` -- foreign-language
--    Markdown the English ``local_file_md`` was translated from
-- 2. ``celex_facts.document_translation_audit``        -- append-only ledger,
--    one row per queue item per translation run (no FKs, like
--    document_conversion_audit)
--
-- PREREQUISITES
--   * Apply migrate_document_conversion.sql FIRST. The first statement below
--     raises when celex_facts.document_registry.local_file_md is absent.
--   * Apply with: psql -v ON_ERROR_STOP=1 -f migrate_document_translation.sql
--
-- Comments live in schema_comments.sql, not here. Apply schema_comments.sql
-- AFTER this file.
--
-- Additive and idempotent: safe to re-run.
-- =========================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'celex_facts'
          AND table_name = 'document_registry'
          AND column_name = 'local_file_md'
    ) THEN
        RAISE EXCEPTION 'celex_facts.document_registry.local_file_md is missing: '
            'apply migrate_document_conversion.sql first';
    END IF;
END$$;

SET lock_timeout = '5s';

ALTER TABLE celex_facts.document_registry
    ADD COLUMN IF NOT EXISTS original_file_md text;

-- Append-only. Intentionally no foreign keys, as document_conversion_audit.
CREATE TABLE IF NOT EXISTS celex_facts.document_translation_audit (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              uuid        NOT NULL,
    decision_id         text,
    document_id         text,
    source_file_md      text,
    target_file_md      text,
    source_language     text,
    model_id            text,
    chunk_count         integer,
    input_tokens        integer,
    output_tokens       integer,
    duration_ms         bigint,
    outcome             text        NOT NULL,
    reason              text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_document_translation_audit_outcome
        CHECK (outcome IN ('translated', 'skipped', 'already_english', 'failed'))
);

CREATE INDEX IF NOT EXISTS ix_document_translation_audit_run
    ON celex_facts.document_translation_audit (run_id);
