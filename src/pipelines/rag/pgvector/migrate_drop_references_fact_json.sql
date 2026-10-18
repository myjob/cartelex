-- =====================================================================
-- Migration: drop celex_facts.facts.references_fact_json
-- =====================================================================
-- Purpose
--   The column was meant to hold "predecessor facts this fact was derived
--   from" (see schema_comments.sql).  Nothing has ever written it: all
--   42,298 fact rows carry SQL NULL, and no writer exists anywhere in
--   src/ -- only DDL dumps, migrations and workbench scripts mention it.
--
--   The relationship it was meant to express is held by the lineage
--   tables instead: a `fact_source_registry` row with
--   `source_type = 'fact'` and `source_fact_id` pointing at the
--   predecessor, joined through `fact_source_link` with
--   `relation_type = 'derived_from'`.  FactIngestor._derive_predecessor_lineage
--   writes those edges from the YAML `predecessors` list on every save.
--   fact_evidence_repository.get_predecessor_facts() reads that join and
--   documents it as authoritative precisely so transitive verification does
--   not depend on this column.
--
--   Keeping a column whose schema comment describes behaviour the system
--   does not implement is actively misleading to anyone reading the schema.
--
-- Dependency
--   celex_facts.question2fact2embeddings_vw selects the column in an inner
--   subquery but never projects it, so the view must be dropped and
--   recreated.  Its output columns (id, celex_id, fact_name, question,
--   value_txt, hash_q_a, embeddings) are unchanged; the pointless inner
--   subquery is collapsed into a direct join at the same time.
--
-- Safety
--   Verified before writing: 0 of 42,298 rows non-null, and
--   question2fact2embeddings_vw is the only view depending on the column.
--   No data is lost.  Rehearsed in a rolled-back transaction: the
--   recreated view returns the same 7 columns and the same 34,075 rows.
--
-- Idempotent: safe to re-run (IF EXISTS / OR REPLACE throughout).
--
-- Apply with:
--   psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/migrate_drop_references_fact_json.sql
-- =====================================================================

BEGIN;

-- 1. Drop the dependent view (it does not project the column).
DROP VIEW IF EXISTS celex_facts.question2fact2embeddings_vw;

-- 2. Drop the vestigial column.
ALTER TABLE celex_facts.facts
    DROP COLUMN IF EXISTS references_fact_json;

-- 3. Recreate the view without the redundant subquery.
CREATE OR REPLACE VIEW celex_facts.question2fact2embeddings_vw AS
SELECT f.id,
       f.celex_id,
       e.fact_name,
       e.question,
       e.value_txt,
       e.hash_q_a,
       e.embeddings
FROM celex_facts.question2fact2embeddings e
JOIN celex_facts.facts f ON f.id = e.fk_fact_id;

COMMENT ON VIEW celex_facts.question2fact2embeddings_vw IS
  'question2fact2embeddings joined back to facts for celex and fact context.';

COMMIT;
