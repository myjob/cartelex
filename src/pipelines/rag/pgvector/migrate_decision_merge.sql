-- Migration: decision merge ledger
-- =========================================================================
-- Three steps fold a duplicate decision D into an owner K and then DELETE D's
-- decision_registry row:
--   Main_dedupe_registry              (RegistryDedupeRepository.merge_decision)
--   Main_decide_source_document MERGE (SourceDecisionRepository.apply)
--   batch.utils.repair_decided_duplicates (same apply, own merged_by)
-- Ids that still name D -- above all document_download_audit.decision_id,
-- which is append-only -- then point at nothing. decision_merge records each
-- fold D -> K, written in the merge's own transaction (record_merge).
--
-- Soft references only, no FKs: merged_decision_id is deleted by definition,
-- and merged_into may itself be merged later (K -> J), which an FK would
-- block. A chain stays as one row per hop; readers follow it to the first
-- registered decision (reporting.report_eddy_celex_facts).
--
-- Section 2 backfills the merges made before this ledger existed: every
-- audited decision id without a registry row, mapped to the current owner of
-- its audited documents (an owner holding the is_source document first, then
-- the lowest id) -- the rule report_eddy_celex_facts used before the ledger.
-- On 2026-11-02 it produced 113 rows; each was cross-checked against the run
-- reports (dedupe_registry_20261030-202058.csv: 36 deletes,
-- decide_source_document_20261101-114011.csv: 69 merges, the 8 pairs of
-- repair_decided_duplicates) and agrees with all of them. Backfilled rows
-- carry merged_by = 'backfill'. On a fresh database it inserts nothing.
--
-- Comments live in schema_comments.sql, not here; re-apply it after this file.
--
-- Additive and idempotent: safe to re-run (CREATE ... IF NOT EXISTS; the
-- backfill skips ids already in the ledger). Touches no existing table, so it
-- does not contend with running batches.
--
-- Apply with: psql -v ON_ERROR_STOP=1 -f migrate_decision_merge.sql
-- =========================================================================

DO $$
BEGIN
    IF to_regclass('celex_facts.decision_registry') IS NULL THEN
        RAISE EXCEPTION 'celex_facts.decision_registry is missing: '
            'apply migrate_document_registry.sql first';
    END IF;
END$$;

-- 1. ledger -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS celex_facts.decision_merge (
    merged_decision_id text        PRIMARY KEY,
    merged_into        text        NOT NULL,
    merged_by          text        NOT NULL,
    merged_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_decision_merge_not_self CHECK (merged_decision_id <> merged_into)
);

CREATE INDEX IF NOT EXISTS ix_decision_merge_merged_into
    ON celex_facts.decision_merge (merged_into);

-- 2. backfill of the merges made before the ledger ------------------------------
INSERT INTO celex_facts.decision_merge (merged_decision_id, merged_into, merged_by)
SELECT DISTINCT ON (a.decision_id) a.decision_id, d.decision_id, 'backfill'
FROM celex_facts.document_download_audit AS a
JOIN celex_facts.document_registry AS d ON d.document_id = a.document_id
WHERE a.decision_id IS NOT NULL
  AND a.decision_id <> d.decision_id
  AND NOT EXISTS (SELECT 1 FROM celex_facts.decision_registry AS r
                  WHERE r.decision_id = a.decision_id)
ORDER BY a.decision_id, d.is_source DESC, d.decision_id
ON CONFLICT (merged_decision_id) DO NOTHING;
