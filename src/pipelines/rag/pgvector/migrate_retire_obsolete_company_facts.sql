-- =====================================================================
-- Migration: retire the pre-July-2026 company fact names
-- =====================================================================
-- Purpose
--   Four company fact names accreted across earlier ingestion generations
--   and are superseded by the July 2026 master set:
--
--     companies            -> companies_investigated
--     companies_all        -> companies_investigated
--     companies_canoncial  -> companies_investigated   (misspelled)
--     company_country      -> companies_country
--
--   The YAML catalog (config_main/FACT_EXTRACTOR.yaml) and all Python
--   lookup tables have already been repointed.  This script removes the
--   rows still carrying the retired names so that the DB, the catalog and
--   the code agree on one vocabulary.
--
-- Prerequisite (IMPORTANT)
--   Run ``companies_investigated`` across the corpus BEFORE applying this
--   script.  Deleting the ``companies`` rows removes the DB fallback that
--   PipelineExecutor._missing_predecessors relies on; the six facts now
--   declaring ``predecessors: ["companies_investigated"]`` (fines,
--   exemption_applicants, leniency_*, company_recidivism) cannot run until
--   the roster fact exists.
--
-- Apply with
--   psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/migrate_retire_obsolete_company_facts.sql
--
-- Idempotent: re-running is a no-op once the rows are gone.
--
-- Deletion order matters.  DELETE FROM celex_facts.facts cascades into
-- fact_source_registry (source_fact_id ON DELETE CASCADE), but two FKs are
-- RESTRICT and block the cascade, so they are cleared by hand first:
--   * knowledge_edges.dst_fact_id  -> facts(id)                  (no action)
--   * fact_source_link.source_id   -> fact_source_registry(...)  (no action)
-- Everything else is CASCADE: fact_evidence, fact2book_checks,
-- fact_source_registry, fact_source_link.fact_id, entity_edges,
-- fact_ingestion_audit_trail.fact_id.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

-- ---------------------------------------------------------------------
-- 0. Inventory - printed before anything is removed, so the run log
--    records exactly what this migration destroyed.
-- ---------------------------------------------------------------------
SELECT name,
       count(*)                  AS fact_rows,
       count(DISTINCT celex_id)  AS celex_ids,
       max(created_at)::date     AS last_written
  FROM celex_facts.facts
 WHERE name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country')
 GROUP BY name
 ORDER BY fact_rows DESC;

-- ---------------------------------------------------------------------
-- 1. RESTRICT path A: knowledge_edges rows pointing at a retired fact.
--    Record the source knowledge_items first: after the edges are gone we
--    can no longer tell which items THIS migration ungrounded, and we must
--    not touch items that were already ungrounded for other reasons.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _touched_knowledge_items ON COMMIT DROP AS
SELECT DISTINCT ke.src_knowledge_id AS id
  FROM celex_facts.knowledge_edges ke
  JOIN celex_facts.facts f ON f.id = ke.dst_fact_id
 WHERE f.name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country');

DELETE FROM celex_facts.knowledge_edges
 WHERE dst_fact_id IN (
       SELECT id FROM celex_facts.facts
        WHERE name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country')
 );

-- ---------------------------------------------------------------------
-- 1b. A knowledge_item whose ONLY grounding edge pointed at a retired
--     fact is now unsupported.  trg_knowledge_grounding is AFTER INSERT,
--     so this does not block the COMMIT - but leaving ungrounded items
--     behind would violate the invariant that trigger exists to enforce.
--     Scoped strictly to the items this migration ungrounded.
-- ---------------------------------------------------------------------
-- knowledge_edges.dst_knowledge_id -> knowledge_items(id) carries no
-- ON DELETE action (RESTRICT), so any inbound edge to a doomed item must
-- go first or the delete below raises a foreign-key violation.
DELETE FROM celex_facts.knowledge_edges ke
 USING _touched_knowledge_items t
 WHERE ke.dst_knowledge_id = t.id
   AND NOT EXISTS (
       SELECT 1 FROM celex_facts.knowledge_edges inner_ke
        WHERE inner_ke.src_knowledge_id = t.id
   );

DELETE FROM celex_facts.knowledge_items ki
 USING _touched_knowledge_items t
 WHERE ki.id = t.id
   AND NOT EXISTS (
       SELECT 1 FROM celex_facts.knowledge_edges ke
        WHERE ke.src_knowledge_id = ki.id
   );

-- ---------------------------------------------------------------------
-- 2. RESTRICT path B: fact_source_link rows whose source_id points at
--    registry entries that the cascade is about to delete.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.fact_source_link
 WHERE source_id IN (
       SELECT source_id FROM celex_facts.fact_source_registry
        WHERE source_fact_id IN (
              SELECT id FROM celex_facts.facts
               WHERE name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country')
        )
 );

-- ---------------------------------------------------------------------
-- 3. question2fact2embeddings has NO foreign key on fk_fact_id, so the
--    cascade never reaches it.  Orphan fact embeddings must be removed
--    explicitly or they keep polluting the fact-embedding search space.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.question2fact2embeddings
 WHERE fact_name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country');

-- ---------------------------------------------------------------------
-- 4. The facts themselves.  Cascades clean fact_evidence,
--    fact2book_checks, fact_source_registry, fact_source_link.fact_id,
--    entity_edges and the audit-trail rows that carry a fact_id.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.facts
 WHERE name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country');

-- ---------------------------------------------------------------------
-- 5. Audit-trail rows from FAILED runs never got a fact_id, so step 4's
--    cascade does not reach them and fact_ingestion_audit_trail_vw (an
--    INNER JOIN onto facts) hides them.  Clear them by name.
--    The view itself hardcodes no fact names and needs no DDL change.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.fact_ingestion_audit_trail
 WHERE target_fact_name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country');

-- ---------------------------------------------------------------------
-- 6. qa_company_delta_vw diffed companies_investigated against
--    companies_canoncial to QA the rename.  With the canonical side gone
--    every row reports same_set = NULL, so the view is meaningless.
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS celex_facts.qa_company_delta_vw;

-- ---------------------------------------------------------------------
-- 7. Post-conditions - all four must return 0.
-- ---------------------------------------------------------------------
SELECT 'facts'             AS table_name,
       count(*)            AS remaining
  FROM celex_facts.facts
 WHERE name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country')
UNION ALL
SELECT 'audit_trail', count(*)
  FROM celex_facts.fact_ingestion_audit_trail
 WHERE target_fact_name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country')
UNION ALL
SELECT 'fact_embeddings', count(*)
  FROM celex_facts.question2fact2embeddings
 WHERE fact_name IN ('companies', 'companies_all', 'companies_canoncial', 'company_country')
UNION ALL
SELECT 'entity_edges_orphaned', count(*)
  FROM celex_facts.entity_edges ee
  LEFT JOIN celex_facts.facts f ON f.id = ee.source_fact_id
 WHERE f.id IS NULL;

COMMIT;

-- After committing, re-materialize the entity graph so the company edges
-- are rebuilt from companies_investigated (FAMILY_EDGE_MAP now keys the
-- participated_in relation on the roster fact):
--   .venv/bin/python src/batch/Main_materialize_entity_graph.py
