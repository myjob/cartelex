-- =====================================================================
-- Migration: (1) fix facts_cartel_legal_period_vw, (2) give
--            question2fact2embeddings a real FK to facts
-- =====================================================================
-- 1. facts_cartel_legal_period_vw projected c.value_md for BOTH columns, so
--    facts_cartel_legal_period_category_vw compared the cartel period with
--    itself (only 'exact' / 'NA' ever came out).  legal_period now reads the
--    joined legal_assessment_period row.  Column names and types are
--    unchanged, so CREATE OR REPLACE keeps the dependent category view.
--    ISSUES.md #317.
--
-- 2. question2fact2embeddings.fk_fact_id had no FK, so deleting a fact left
--    its embedding behind and Q&A retrieval could surface deleted values
--    (316 orphans over 51 fact names on 2026-11-06).  The orphans are
--    removed and an ON DELETE CASCADE FK added, so the next delete or
--    re-extraction cleans up after itself.  ISSUES.md #316 / #175.
--
-- Run from the repo root (the backup path is relative):
--   psql "$URL" -f src/pipelines/rag/pgvector/migrate_period_view_and_fact_embedding_fk.sql
-- Then apply schema_comments.sql (comment on the new constraint).
-- Idempotent: a re-run finds no orphans, writes no backup and keeps the FK.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

-- 1. The view.
CREATE OR REPLACE VIEW celex_facts.facts_cartel_legal_period_vw AS
SELECT c.celex_id,
       c.value_md AS cartel_period,
       l.value_md AS legal_period
  FROM celex_facts.facts c
  JOIN (SELECT facts.id,
               facts.celex_id,
               facts.name,
               facts.value_md,
               facts.value_json,
               facts.created_at
          FROM celex_facts.facts
         WHERE facts.name::text = 'legal_assessment_period'::text) l
    ON l.celex_id::text = c.celex_id::text
 WHERE c.name::text = 'cartel_period'::text;

-- 2a. Orphans (metadata only: the vectors of deleted facts have no use).
CREATE TEMP TABLE _orphans ON COMMIT DROP AS
SELECT q.fk_fact_id, q.celex_id, q.fact_name, q.value_txt, q.ingestion_timestamp
  FROM celex_facts.question2fact2embeddings q
 WHERE NOT EXISTS (SELECT 1 FROM celex_facts.facts f WHERE f.id = q.fk_fact_id);

-- Guarded: an unconditional \copy on a re-run would overwrite the backup
-- with a header-only file.
SELECT count(*) > 0 AS has_orphans FROM _orphans \gset
\if :has_orphans
\copy (SELECT * FROM _orphans ORDER BY fact_name, celex_id) TO 'reports/question2fact2embeddings_orphans_backup_20261106.csv' WITH (FORMAT csv, HEADER)
\endif

SELECT fact_name, count(*) AS orphans FROM _orphans GROUP BY fact_name ORDER BY 2 DESC;

DELETE FROM celex_facts.question2fact2embeddings q
 USING _orphans o
 WHERE q.fk_fact_id = o.fk_fact_id;

-- 2b. The FK (skipped when it already exists).
SELECT NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'fk_question2fact2embeddings_fact'
       AND conrelid = 'celex_facts.question2fact2embeddings'::regclass
) AS needs_fk \gset
\if :needs_fk
ALTER TABLE celex_facts.question2fact2embeddings
    ADD CONSTRAINT fk_question2fact2embeddings_fact
    FOREIGN KEY (fk_fact_id) REFERENCES celex_facts.facts (id) ON DELETE CASCADE;
\endif

-- 3. Post-condition.
DO $$
DECLARE
    residue bigint;
BEGIN
    SELECT count(*) INTO residue
      FROM celex_facts.question2fact2embeddings q
     WHERE NOT EXISTS (SELECT 1 FROM celex_facts.facts f WHERE f.id = q.fk_fact_id);
    IF residue > 0 THEN
        RAISE EXCEPTION 'question2fact2embeddings orphan residue: % rows', residue;
    END IF;
END $$;

COMMIT;
