-- Migration: PDF to Markdown conversion (MinerU)
-- =========================================================================
-- 1. ``celex_facts.document_registry.local_file_md``  -- target Markdown path
-- 2. ``celex_facts.document_conversion_audit``        -- append-only ledger,
--    one row per candidate per conversion run (no FKs, like
--    document_download_audit)
--
-- PREREQUISITES
--   * Apply migrate_document_registry.sql FIRST. The first statement below
--     raises 'apply migrate_document_registry.sql first' when
--     celex_facts.document_registry is absent.
--   * Apply with: psql -v ON_ERROR_STOP=1 -f migrate_document_conversion.sql
--
-- Comments live in schema_comments.sql, not here. Apply schema_comments.sql
-- AFTER this file.
--
-- Additive and idempotent: safe to re-run.
-- =========================================================================

DO $$
BEGIN
    IF to_regclass('celex_facts.document_registry') IS NULL THEN
        RAISE EXCEPTION 'celex_facts.document_registry is missing: '
            'apply migrate_document_registry.sql first';
    END IF;
END$$;

SET lock_timeout = '5s';

ALTER TABLE celex_facts.document_registry
    ADD COLUMN IF NOT EXISTS local_file_md text;

-- Append-only. Intentionally no foreign keys, as document_download_audit.
CREATE TABLE IF NOT EXISTS celex_facts.document_conversion_audit (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              uuid        NOT NULL,
    decision_id         text,
    document_id         text,
    local_file_pdf      text,
    local_file_md       text,
    outcome             text        NOT NULL,
    failure_reason      text,
    error_message       text,
    mineru_task_id      text,
    mineru_file_id      text,
    mineru_api_url      text,
    tier                text,
    parser_version      text,
    server_duration_ms  integer,
    wall_duration_ms    integer,
    pdf_size_bytes      bigint,
    pdf_sha256          char(64),
    pdf_pages           integer,
    md_chars            integer,
    image_count         integer,
    table_count         integer,
    formula_count       integer,
    language_detected   text,
    language_confidence real,
    language_stored     text,
    language_action     text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_document_conversion_audit_outcome
        CHECK (outcome IN ('converted', 'skipped_exists', 'no_target_path',
                           'pdf_missing', 'failed', 'timeout')),
    CONSTRAINT ck_document_conversion_audit_language_action
        CHECK (language_action IS NULL
               OR language_action IN ('set', 'kept_equal', 'kept_mismatch',
                                      'undetermined'))
);

CREATE INDEX IF NOT EXISTS ix_document_conversion_audit_run
    ON celex_facts.document_conversion_audit (run_id);
CREATE INDEX IF NOT EXISTS ix_document_conversion_audit_decision
    ON celex_facts.document_conversion_audit (decision_id);
CREATE INDEX IF NOT EXISTS ix_document_conversion_audit_outcome
    ON celex_facts.document_conversion_audit (outcome);
