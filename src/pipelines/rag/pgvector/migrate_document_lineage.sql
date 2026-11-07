-- Migration: document lineage for facts and chunks
-- =========================================================================
-- Links every fact and every embedded chunk to the registered source document
-- it was derived from (document_registry.is_source):
--
-- 1. ``celex_facts.facts.document_id``             -- FK + index
-- 2. ``celex_facts.pg_embedding_qwen.document_id`` -- FK + index
-- 3. ``celex_facts.document_registry.sha256``     -- partial index for the
--    downloader's content safety net (DocumentRegistryRepository.
--    decision_of_content: source or selected documents by file hash)
--
-- The two lineage columns are nullable text (the referenced PK is a
-- natural-key text column) with ON DELETE SET NULL: dropping a registry row
-- never deletes a fact or a chunk, it only clears the link. Section 3 only
-- adds an index (no column, no constraint) to document_registry.
--
-- PREREQUISITES
--   * Apply migrate_document_registry.sql FIRST: the FKs reference
--     celex_facts.document_registry. The first statement below raises
--     'apply migrate_document_registry.sql first' when that table is absent.
--   * Apply with NO ingest, fact or verification batch running. Both tables
--     are written by batches; ADD COLUMN / ADD CONSTRAINT / CREATE INDEX take
--     locks that block (and are blocked by) those writers. lock_timeout makes
--     a forgotten batch fail the ALTER fast instead of queueing behind it and
--     blocking every reader (UI included); just re-run once it is idle.
--   * Apply with: psql -v ON_ERROR_STOP=1 -f migrate_document_lineage.sql
--
-- The FKs are added NOT VALID (brief ACCESS EXCLUSIVE, no scan) and then
-- validated in a separate statement, which scans under SHARE UPDATE
-- EXCLUSIVE only. Validating an already-valid constraint is a no-op.
--
-- Comments live in schema_comments.sql, not here. Apply schema_comments.sql
-- AFTER this file (it comments facts.document_id and
-- pg_embedding_qwen.document_id, which only exist from here on), and re-apply
-- it after any re-run of this file or of migrate_document_registry.sql.
--
-- Additive and idempotent: safe to re-run (ADD COLUMN IF NOT EXISTS,
-- pg_constraint lookup, CREATE INDEX IF NOT EXISTS).
-- =========================================================================

DO $$
BEGIN
    IF to_regclass('celex_facts.document_registry') IS NULL THEN
        RAISE EXCEPTION 'celex_facts.document_registry is missing: '
            'apply migrate_document_registry.sql first';
    END IF;
END$$;

SET lock_timeout = '5s';

-- 1. facts.document_id ---------------------------------------------------------
ALTER TABLE celex_facts.facts
    ADD COLUMN IF NOT EXISTS document_id text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE c.conname = 'fk_facts_document_id'
          AND n.nspname = 'celex_facts'
          AND c.conrelid = 'celex_facts.facts'::regclass
    ) THEN
        ALTER TABLE celex_facts.facts
            ADD CONSTRAINT fk_facts_document_id
            FOREIGN KEY (document_id)
            REFERENCES celex_facts.document_registry (document_id)
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END$$;

ALTER TABLE celex_facts.facts
    VALIDATE CONSTRAINT fk_facts_document_id;

CREATE INDEX IF NOT EXISTS ix_facts_document_id
    ON celex_facts.facts (document_id);

-- 2. pg_embedding_qwen.document_id ---------------------------------------------
ALTER TABLE celex_facts.pg_embedding_qwen
    ADD COLUMN IF NOT EXISTS document_id text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_namespace n ON n.oid = c.connamespace
        WHERE c.conname = 'fk_pg_embedding_qwen_document_id'
          AND n.nspname = 'celex_facts'
          AND c.conrelid = 'celex_facts.pg_embedding_qwen'::regclass
    ) THEN
        ALTER TABLE celex_facts.pg_embedding_qwen
            ADD CONSTRAINT fk_pg_embedding_qwen_document_id
            FOREIGN KEY (document_id)
            REFERENCES celex_facts.document_registry (document_id)
            ON DELETE SET NULL
            NOT VALID;
    END IF;
END$$;

ALTER TABLE celex_facts.pg_embedding_qwen
    VALIDATE CONSTRAINT fk_pg_embedding_qwen_document_id;

CREATE INDEX IF NOT EXISTS ix_pg_embedding_qwen_document_id
    ON celex_facts.pg_embedding_qwen (document_id);

-- 3. document_registry.sha256 (content safety net) -----------------------------
CREATE INDEX IF NOT EXISTS ix_document_registry_sha256
    ON celex_facts.document_registry (sha256)
    WHERE sha256 IS NOT NULL AND (is_source OR is_selected);
