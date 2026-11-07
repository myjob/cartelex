-- =====================================================================
-- Migration: retire the legacy cartel_period fact and give its name to
--            the HeidelTime-derived heideltime_cartel_period fact
-- =====================================================================
-- Purpose
--   Two facts answered the same question.  The legacy LLM ``cartel_period``
--   (2026-01-28) has no producer in FACT_EXTRACTOR.yaml any more; the
--   HeidelTime fact ``heideltime_cartel_period`` superseded it.  Most readers
--   (facts_cartel_legal_period_vw, its category view, celex_index_vw,
--   fact_repository, the case-summary report, QA synonyms) already look up
--   the bare name, so renaming the rows repoints them with no DDL change.
--
--   Only the FACT NAME changes.  The PIPELINE key heideltime_cartel_period
--   stays wherever it records lineage (fact_source_link.pipeline /
--   extraction_method, fact_ingestion_audit_trail.pipeline): those columns
--   say how a fact was made, not what it is called.
--
--   The 266 decisions that carry only the legacy fact are all gated
--   case_commission_infringement = false (exemption / negative clearance /
--   Art. 9 commitments), so their legacy ranges are wrong by design.
--
-- Why a naive DELETE fails
--   knowledge_edges.dst_fact_id is NO ACTION; its rows pointing at legacy
--   facts are removed first.  fact_evidence, fact2book_checks and
--   fact_source_link cascade.  question2fact2embeddings has no FK, and its
--   passage text embeds the fact name (so hash_q_a changes): its rows for the
--   renamed fact, plus orphans of deleted facts, are dropped for re-embedding.
--
-- MANDATORY FOLLOW-UP
--   Re-embed the renamed fact with the qwen fact embedding loader.
--
-- Run from the repo root (the backup path is relative):
--   psql "$URL" -f src/pipelines/rag/pgvector/migrate_rename_cartel_period.sql
-- Idempotent: re-running is a no-op once no row carries either old state.
-- Plan: docs/claude/plans/cartel-period-rename-20261106.md
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

-- 0. Inventory before anything changes.
SELECT name, count(*) AS fact_rows
  FROM celex_facts.facts
 WHERE name IN ('cartel_period', 'heideltime_cartel_period')
 GROUP BY name ORDER BY name;

-- 0a. Legacy rows to retire: only cartel_period facts whose celex_id has no
--     heideltime_cartel_period twin, or that predate the rename.  After a
--     first run every cartel_period row IS the renamed fact, so a re-run must
--     not delete it: the guard is that heideltime_cartel_period rows still exist.
CREATE TEMP TABLE _legacy_facts ON COMMIT DROP AS
SELECT f.id, f.celex_id, f.name, f.value_md, f.value_json, f.created_at
  FROM celex_facts.facts f
 WHERE f.name = 'cartel_period'
   AND EXISTS (SELECT 1 FROM celex_facts.facts h
                WHERE h.name = 'heideltime_cartel_period');

CREATE INDEX ON _legacy_facts (id);

-- The backup is written only when there is something to retire: an
-- unconditional \copy on a re-run would overwrite it with a header-only file.
SELECT count(*) > 0 AS has_legacy FROM _legacy_facts \gset
\if :has_legacy
\copy (SELECT celex_id, name, value_md, value_json, created_at FROM _legacy_facts ORDER BY celex_id) TO 'reports/cartel_period_legacy_backup_20261106.csv' WITH (FORMAT csv, HEADER)
\endif

-- 1. Edges with a NO ACTION reference to a legacy fact.
DELETE FROM celex_facts.knowledge_edges e
 USING _legacy_facts l
 WHERE e.dst_fact_id = l.id;

-- 2. The legacy facts (cascades fact_evidence, fact2book_checks, fact_source_link).
DELETE FROM celex_facts.facts f
 USING _legacy_facts l
 WHERE f.id = l.id;

-- 3. The rename.
UPDATE celex_facts.facts
   SET name = 'cartel_period'
 WHERE name = 'heideltime_cartel_period';

-- 4. Audit trail: the fact name only; pipeline keeps the producer's name.
UPDATE celex_facts.fact_ingestion_audit_trail
   SET target_fact_name = 'cartel_period'
 WHERE target_fact_name = 'heideltime_cartel_period';

UPDATE celex_facts.fact_ingestion_audit_trail
   SET verify_fact_name = 'cartel_period'
 WHERE verify_fact_name = 'heideltime_cartel_period';

-- 5. Fact embeddings under the old name (stale hashes, incl. orphans), and
--    cartel_period vectors whose fact is gone.  Live re-embedded
--    cartel_period vectors survive a re-run.
DELETE FROM celex_facts.question2fact2embeddings q
 WHERE q.fact_name = 'heideltime_cartel_period'
    OR (q.fact_name = 'cartel_period'
        AND NOT EXISTS (SELECT 1 FROM celex_facts.facts f WHERE f.id = q.fk_fact_id));

-- 6. Post-conditions: abort (and roll back) on any residue.
DO $$
DECLARE
    residue bigint;
BEGIN
    SELECT (SELECT count(*) FROM celex_facts.facts
             WHERE name = 'heideltime_cartel_period')
         + (SELECT count(*) FROM celex_facts.fact_ingestion_audit_trail
             WHERE target_fact_name = 'heideltime_cartel_period'
                OR verify_fact_name = 'heideltime_cartel_period')
         + (SELECT count(*) FROM celex_facts.question2fact2embeddings
             WHERE fact_name = 'heideltime_cartel_period')
      INTO residue;
    IF residue > 0 THEN
        RAISE EXCEPTION 'heideltime_cartel_period residue: % rows', residue;
    END IF;
END $$;

SELECT name, count(*) AS fact_rows
  FROM celex_facts.facts
 WHERE name IN ('cartel_period', 'heideltime_cartel_period')
 GROUP BY name ORDER BY name;

COMMIT;
