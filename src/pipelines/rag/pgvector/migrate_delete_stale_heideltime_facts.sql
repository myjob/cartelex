-- =====================================================================
-- Migration: retire the derived heideltime facts of re-extracted documents
-- =====================================================================
-- Purpose
--   ``heideltime_raw`` is only the JAR layer.  Four facts are derived from
--   it and inherit any staleness in it:
--
--     heideltime_raw
--       -> heideltime_event
--            -> heideltime_event_agg -> eu_commisssion_decision_date
--            -> heideltime_cartel_period
--
--   Once a document's extractions have been corrected, those four rows still
--   hold values computed from the pre-repair dates.  This script deletes them
--   for a named set of CELEX ids so the pipeline recomputes them.
--
--   Run it AFTER the heideltime_raw re-extraction and BEFORE re-running the
--   derived facts, so nothing is regenerated from data that is about to move.
--
-- Scope
--   Section 0 is the only place the fact names appear, and the CELEX ids come
--   from a file, so retargeting the script is a one-line edit plus a different
--   list.  Nothing outside the (name, celex_id) intersection is touched.
--
-- Why a naive DELETE fails
--   Identical FK topology to migrate_delete_companies_fact.sql.  Seven FKs
--   reference facts(id); six cascade, but knowledge_edges.dst_fact_id is
--   NO ACTION and raises 23503.  Two further NO ACTION constraints reference
--   fact_source_registry, whose rows this delete cascades away:
--     * fact_source_link.source_id     -> fact_source_registry
--     * knowledge_edges.dst_source_id  -> fact_source_registry
--   And question2fact2embeddings carries no FK at all, so orphan fact vectors
--   would keep answering retrieval queries after the facts are gone.
--
-- MANDATORY FOLLOW-UP: rematerialize the entity graph
--   entity_edges.source_fact_id cascades, and heideltime_event is the sole
--   source of the 'dated_in' temporal relation.  This script therefore destroys
--   the temporal graph layer for every CELEX it touches.  Regenerating the
--   facts does NOT restore those edges -- only re-running the materializer
--   does.  After the derived facts have been regenerated, finish with:
--
--     for id in $(cat <the same celex list>); do \
--       PYTHONPATH=src .venv/bin/python \
--         src/batch/Main_materialize_entity_graph.py --celex-id "$id"; done
--
--   Section 0e prints the edge count this run is about to remove, and the
--   materializer's heideltime_fact_missing counter reports any CELEX rebuilt
--   before its fact came back.
--
-- Apply with
--   psql "$DATABASE_URL" \
--     -f src/pipelines/rag/pgvector/migrate_delete_stale_heideltime_facts.sql
--   from the repository root.  In DBeaver: run as a SCRIPT (Alt+X) so the
--   BEGIN/COMMIT below frame one transaction.
--
--   The CELEX list is the \copy path in section 0a -- edit that ONE line to
--   retarget the script.  It cannot be a psql variable: \copy is the single
--   meta-command in which psql performs no variable interpolation.  The path
--   is resolved on the CLIENT, relative to the working directory.
--
-- Idempotent: re-running is a no-op once the rows are gone.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

-- ---------------------------------------------------------------------
-- 0. The target fact names.  THE ONLY PLACE A FACT NAME APPEARS.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _target_names ON COMMIT DROP AS
SELECT unnest(ARRAY[
    'heideltime_event',
    'heideltime_event_agg',
    'heideltime_cartel_period',
    'eu_commisssion_decision_date'
]::text[]) AS name;

-- 0a. The documents to invalidate.  THE ONLY PLACE A PATH APPEARS.
--     Produced by scan_dates.py and the remediation-list build; one CELEX id
--     per line, the same file handed to --celex-list-file.
CREATE TEMP TABLE _target_celex (celex_id text) ON COMMIT DROP;
\copy _target_celex FROM 'reports/heideltime_remediation_celex_20260831.txt'

CREATE INDEX ON _target_celex (celex_id);

-- 0b. The doomed fact rows: the intersection of the two lists, nothing else.
CREATE TEMP TABLE _doomed_facts ON COMMIT DROP AS
SELECT f.id, f.celex_id, f.name
  FROM celex_facts.facts f
  JOIN _target_names t ON t.name = f.name
  JOIN _target_celex c ON c.celex_id = f.celex_id;

CREATE INDEX ON _doomed_facts (id);

-- 0c. Registry rows the cascade is about to remove.  Resolved BEFORE the
--     delete, because afterwards there is no way to identify them.
CREATE TEMP TABLE _doomed_sources ON COMMIT DROP AS
SELECT r.source_id
  FROM celex_facts.fact_source_registry r
  JOIN _doomed_facts d ON d.id = r.source_fact_id;

CREATE INDEX ON _doomed_sources (source_id);

-- ---------------------------------------------------------------------
-- 0d. Inventory - printed before anything is removed, so the run log
--     records exactly what this migration destroyed.
-- ---------------------------------------------------------------------
SELECT name,
       count(*)                 AS fact_rows,
       count(DISTINCT celex_id) AS celex_ids
  FROM _doomed_facts
 GROUP BY name
 ORDER BY name;

-- 0e. Entity-graph temporal edges the cascade is about to destroy.
--     entity_edges.source_fact_id cascades, and the heideltime_event fact is
--     the sole source of the 'dated_in' relation, so deleting it silently
--     removes that decision's whole temporal layer.  Regenerating the fact
--     does NOT bring the edges back: Main_materialize_entity_graph.py must be
--     re-run for these CELEX ids afterwards.  Counted here because the
--     inventory above counts facts, which would hide this entirely.
SELECT d.celex_id,
       count(*) AS dated_in_edges
  FROM celex_facts.entity_edges e
  JOIN _doomed_facts d ON d.id = e.source_fact_id
 WHERE e.relation::text = 'dated_in'
 GROUP BY d.celex_id
 ORDER BY d.celex_id;

-- ---------------------------------------------------------------------
-- 1. Record which knowledge_items this migration is about to unground.
--    Captured first: once the edges are gone we can no longer tell which
--    items THIS script affected.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _touched_knowledge_items ON COMMIT DROP AS
SELECT DISTINCT ke.src_knowledge_id AS id
  FROM celex_facts.knowledge_edges ke
  JOIN _doomed_facts d ON d.id = ke.dst_fact_id;

-- ---------------------------------------------------------------------
-- 2. NO ACTION path A: knowledge_edges grounded on a doomed fact.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.knowledge_edges ke
 USING _doomed_facts d
 WHERE ke.dst_fact_id = d.id;

-- ---------------------------------------------------------------------
-- 3. NO ACTION path B: knowledge_edges grounded on a doomed REGISTRY row.
--    Distinct from path A - an edge can cite the source entry without
--    citing the fact.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.knowledge_edges ke
 USING _doomed_sources s
 WHERE ke.dst_source_id = s.source_id;

-- ---------------------------------------------------------------------
-- 4. A knowledge_item whose ONLY grounding edge pointed at a doomed fact
--    is now unsupported.  Strictly scoped to the items step 1 recorded and
--    guarded by NOT EXISTS, so an item retaining any other grounding edge
--    is preserved.
-- ---------------------------------------------------------------------
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
-- 5. NO ACTION path C: fact_source_link rows citing a doomed registry
--    entry.  fact_source_link.fact_id is CASCADE, but .source_id is not.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.fact_source_link fsl
 USING _doomed_sources s
 WHERE fsl.source_id = s.source_id;

-- ---------------------------------------------------------------------
-- 6. question2fact2embeddings has NO foreign key, so the cascade never
--    reaches it.  Orphan fact embeddings must be removed explicitly or the
--    retired values keep answering retrieval queries.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.question2fact2embeddings e
 USING _doomed_facts d
 WHERE e.fk_fact_id = d.id;

-- ---------------------------------------------------------------------
-- 7. The facts themselves.  Cascades now clean fact_evidence,
--    fact2book_checks, fact_source_registry, fact_source_link.fact_id,
--    entity_edges and the audit-trail rows that carry a fact_id.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.facts f
 USING _doomed_facts d
 WHERE f.id = d.id;

-- ---------------------------------------------------------------------
-- 8. Audit-trail rows from FAILED runs never got a fact_id, so step 7's
--    cascade does not reach them.  Clear them by (name, celex_id).
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.fact_ingestion_audit_trail a
 USING _target_names t, _target_celex c
 WHERE a.target_fact_name = t.name
   AND a.celex_id = c.celex_id;

-- ---------------------------------------------------------------------
-- 9. Post-conditions - every row must report remaining = 0.
--    Read this output BEFORE committing.
-- ---------------------------------------------------------------------
SELECT 'facts' AS table_name, count(*) AS remaining
  FROM celex_facts.facts f
  JOIN _target_names t ON t.name = f.name
  JOIN _target_celex c ON c.celex_id = f.celex_id
UNION ALL
SELECT 'audit_trail', count(*)
  FROM celex_facts.fact_ingestion_audit_trail a
  JOIN _target_names t ON t.name = a.target_fact_name
  JOIN _target_celex c ON c.celex_id = a.celex_id
UNION ALL
-- Scoped to the targeted (name, celex_id) pairs on purpose.  A corpus-wide
-- orphan count is NOT zero to begin with -- 73 rows, 48 of them products_name,
-- predating this script (ISSUES.md 20260831) -- so a global assertion here
-- would fail for reasons this migration neither caused nor can fix.
SELECT 'orphaned_fact_embeddings', count(*)
  FROM celex_facts.question2fact2embeddings e
  JOIN _target_names t ON t.name = e.fact_name
  JOIN _target_celex c ON c.celex_id = e.celex_id
UNION ALL
SELECT 'orphaned_knowledge_edges', count(*)
  FROM celex_facts.knowledge_edges ke
  LEFT JOIN celex_facts.facts f ON f.id = ke.dst_fact_id
 WHERE ke.dst_fact_id IS NOT NULL AND f.id IS NULL
UNION ALL
SELECT 'orphaned_fact_source_link', count(*)
  FROM celex_facts.fact_source_link l
 WHERE NOT EXISTS (SELECT 1 FROM celex_facts.fact_source_registry r
                    WHERE r.source_id = l.source_id)
UNION ALL
SELECT 'ungrounded_knowledge_items', count(*)
  FROM celex_facts.knowledge_items ki
 WHERE NOT EXISTS (SELECT 1 FROM celex_facts.knowledge_edges ke
                    WHERE ke.src_knowledge_id = ki.id)
UNION ALL
-- Expected to be 0: the cascade removed every 'dated_in' edge for the targeted
-- decisions.  A non-zero value means an edge outlived its source fact, which
-- the FK should make impossible.  Zero here is NOT "nothing was lost" -- see
-- section 0e for what was destroyed and the header for the rematerialize step.
SELECT 'remaining_dated_in_edges_for_targets', count(*)
  FROM celex_facts.entity_edges e
  JOIN _target_celex t ON t.celex_id = e.celex_id
 WHERE e.relation::text = 'dated_in';

COMMIT;
