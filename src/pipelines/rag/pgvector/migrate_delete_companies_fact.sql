-- =====================================================================
-- Migration: delete the retired ``companies`` fact and its dependents
-- =====================================================================
-- Purpose
--   Remove the rows still carrying the retired fact name ``companies``
--   (superseded by ``companies_investigated``).  The YAML catalog and all
--   Python lookup tables were repointed earlier. This clears the data.
--
--   Scoped deliberately to ONE name.  The bundled
--   ``migrate_retire_obsolete_company_facts.sql`` deletes all four retired
--   names in a single transaction, which is unsafe today: every one of the
--   4,286 company edges in ``entity_edges`` is sourced from
--   ``companies_canoncial``, and ``companies_investigated`` currently
--   contributes none.  Retire that name only after the entity graph is
--   rematerialized.  See ISSUES.md 20260829.
--
-- Reusing this script for another name
--   Edit the ARRAY literal in section 0 -- that is the ONLY place a fact
--   name appears.  ``companies``, ``companies_all`` and ``company_country``
--   are cleared for this treatment.  ``companies_canoncial`` is NOT.
--
-- Why a naive DELETE fails
--   Seven FKs reference facts(id).  Six are ON DELETE CASCADE, but
--   ``knowledge_edges.dst_fact_id`` is NO ACTION and raises 23503.  Two
--   FURTHER NO ACTION constraints reference fact_source_registry, whose
--   rows this delete cascades away:
--     * fact_source_link.source_id     -> fact_source_registry
--     * knowledge_edges.dst_source_id  -> fact_source_registry
--   All three are cleared by hand below, before the facts go.
--
--   One more table carries NO foreign key at all, so the cascade never
--   reaches it and its rows would silently outlive the fact:
--     * question2fact2embeddings  (orphan vectors keep polluting search)
--
--   ``question2fact_name`` needs no step: it is a VIEW over facts joined to
--   fact_ingestion_audit_trail, so its row for the retired name disappears
--   once steps 7 and 8 have run.  Post-condition 9 asserts exactly that.
--
-- Apply with
--   psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/migrate_delete_companies_fact.sql
--   In DBeaver: run as a SCRIPT (Alt+X), not statement-by-statement, so the
--   BEGIN/COMMIT below actually frames one transaction.
--
-- Idempotent: re-running is a no-op once the rows are gone.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 0. The target name list.  THE ONLY PLACE A FACT NAME APPEARS.
--    Every step below reads this table (directly, or via the two derived
--    tables in 0a/0b), so retargeting the script is a one-line edit.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _target_names ON COMMIT DROP AS
SELECT unnest(ARRAY['companies']::text[]) AS name;

-- 0a. The doomed fact rows.
CREATE TEMP TABLE _doomed_facts ON COMMIT DROP AS
SELECT f.id, f.celex_id, f.name
  FROM celex_facts.facts f
  JOIN _target_names t ON t.name = f.name;

CREATE INDEX ON _doomed_facts (id);

-- 0b. Registry rows the cascade is about to remove.  Resolved BEFORE the
--     delete, because afterwards there is no way to identify them.
CREATE TEMP TABLE _doomed_sources ON COMMIT DROP AS
SELECT r.source_id
  FROM celex_facts.fact_source_registry r
  JOIN _doomed_facts d ON d.id = r.source_fact_id;

CREATE INDEX ON _doomed_sources (source_id);

-- ---------------------------------------------------------------------
-- 0c. Inventory - printed before anything is removed, so the run log
--     records exactly what this migration destroyed.
-- ---------------------------------------------------------------------
SELECT name,
       count(*)                 AS fact_rows,
       count(DISTINCT celex_id) AS celex_ids
  FROM _doomed_facts
 GROUP BY name;

-- ---------------------------------------------------------------------
-- 1. Record which knowledge_items this migration is about to unground.
--    Captured first: once the edges are gone we can no longer tell which
--    items THIS script affected, and we must not touch items that were
--    already ungrounded for other reasons.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _touched_knowledge_items ON COMMIT DROP AS
SELECT DISTINCT ke.src_knowledge_id AS id
  FROM celex_facts.knowledge_edges ke
  JOIN _doomed_facts d ON d.id = ke.dst_fact_id;

-- ---------------------------------------------------------------------
-- 2. NO ACTION path A: knowledge_edges grounded on a doomed fact.
--    This is the constraint that raises 23503 on a naive DELETE.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.knowledge_edges ke
 USING _doomed_facts d
 WHERE ke.dst_fact_id = d.id;

-- ---------------------------------------------------------------------
-- 3. NO ACTION path B: knowledge_edges grounded on a doomed REGISTRY row.
--    Distinct from path A -- an edge can cite the source entry without
--    citing the fact -- and missing from the bundled migration script,
--    which therefore fails partway.  See ISSUES.md 20260829.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.knowledge_edges ke
 USING _doomed_sources s
 WHERE ke.dst_source_id = s.source_id;

-- ---------------------------------------------------------------------
-- 4. A knowledge_item whose ONLY grounding edge pointed at a doomed fact
--    is now unsupported.  trg_knowledge_grounding is AFTER INSERT so this
--    does not block the COMMIT, but leaving ungrounded items behind would
--    violate the invariant that trigger exists to enforce.  Strictly
--    scoped to the items step 1 recorded, and guarded by NOT EXISTS so an
--    item retaining any other grounding edge is preserved.
--    knowledge_edges.dst_knowledge_id -> knowledge_items is NO ACTION, so
--    inbound edges to a doomed item must go first.
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
--    reaches it.  Orphan fact embeddings must be removed explicitly or
--    they keep polluting the fact-embedding search space -- the retired
--    fact would go on answering retrieval queries after its rows are gone.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.question2fact2embeddings e
 USING _target_names t
 WHERE e.fact_name = t.name;

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
--    cascade does not reach them and fact_ingestion_audit_trail_vw (an
--    INNER JOIN onto facts) hides them.  Clear them by name.
-- ---------------------------------------------------------------------
DELETE FROM celex_facts.fact_ingestion_audit_trail a
 USING _target_names t
 WHERE a.target_fact_name = t.name;

-- ---------------------------------------------------------------------
-- 9. Post-conditions - every row must report remaining = 0.
--    Read this output BEFORE committing.
-- ---------------------------------------------------------------------
SELECT 'facts' AS table_name, count(*) AS remaining
  FROM celex_facts.facts f JOIN _target_names t ON t.name = f.name
UNION ALL
SELECT 'audit_trail', count(*)
  FROM celex_facts.fact_ingestion_audit_trail a JOIN _target_names t ON t.name = a.target_fact_name
UNION ALL
SELECT 'fact_embeddings', count(*)
  FROM celex_facts.question2fact2embeddings e JOIN _target_names t ON t.name = e.fact_name
UNION ALL
SELECT 'question_catalog', count(*)
  FROM celex_facts.question2fact_name q JOIN _target_names t ON t.name = q.fact_name
UNION ALL
SELECT 'orphaned_entity_edges', count(*)
  FROM celex_facts.entity_edges ee
  LEFT JOIN celex_facts.facts f ON f.id = ee.source_fact_id
 WHERE f.id IS NULL
UNION ALL
SELECT 'orphaned_knowledge_edges', count(*)
  FROM celex_facts.knowledge_edges ke
  LEFT JOIN celex_facts.facts f ON f.id = ke.dst_fact_id
 WHERE ke.dst_fact_id IS NOT NULL AND f.id IS NULL
UNION ALL
SELECT 'ungrounded_knowledge_items', count(*)
  FROM celex_facts.knowledge_items ki
 WHERE NOT EXISTS (SELECT 1 FROM celex_facts.knowledge_edges ke
                    WHERE ke.src_knowledge_id = ki.id);

COMMIT;
