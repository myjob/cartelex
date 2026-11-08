-- =====================================================================
-- Migration: fix the fact-name typo eu_commisssion_decision_date
--            -> eu_commission_decision_date
-- =====================================================================
-- Purpose
--   The decision-date fact was named with a triple "s".  Code, YAML and docs
--   switch to the correct spelling in the same change; this migration moves
--   the stored data with them.  Run it together with the code change: a batch
--   run in between treats the fact as missing and re-extracts all of it.
--
-- What moves (live counts 20261106)
--   facts.name                                  782
--   facts.value_json object key                 691  (other rows use the
--       drifted keys extracted_fact / extracted_value -- left as they are)
--   fact_ingestion_audit_trail.target_fact_name 2115 (question2fact* views
--       derive from it and follow automatically)
--   fact_source_link.extraction_method          780  (holds the fact name for
--       predecessor lineage; pipeline stays 'fact_context')
--   fact_source_registry.description            0 today, handled anyway
--   question2fact2embeddings                    781 rows DELETED: the
--       passage text embeds the fact name, so hash_q_a and the vector change.
--
-- Not touched: rendered text snapshots (case_summary value_md mentioning the
-- old name), historic migrations, reports/ backups.
--
-- MANDATORY FOLLOW-UP
--   Re-embed the renamed fact (782 Ollama calls on the Spark) with
--   FactEmbeddingLoaderQwen.ingest_for_celex_and_fact; the exact command is
--   step 4 of docs/claude/plans/eu-commission-decision-date-rename-20261106.md.
--
-- Reversal: every step except the embedding delete is a rename; run the same
-- UPDATEs with the two names swapped, then re-embed.
--
-- Run from the repo root:
--   psql "$URL" -f src/pipelines/rag/pgvector/migrate_rename_eu_commission_decision_date.sql
-- Idempotent: a re-run finds no old-name rows and changes nothing.
-- Plan: docs/claude/plans/eu-commission-decision-date-rename-20261106.md
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

-- 0. Inventory before anything changes.
SELECT name, count(*) AS fact_rows
  FROM celex_facts.facts
 WHERE name IN ('eu_commisssion_decision_date', 'eu_commission_decision_date')
 GROUP BY name ORDER BY name;

-- 0a. A document holding both names would violate uq_facts_celex_name.
DO $$
DECLARE
    twins bigint;
BEGIN
    SELECT count(*) INTO twins
      FROM celex_facts.facts o
      JOIN celex_facts.facts n
        ON n.celex_id = o.celex_id AND n.name = 'eu_commission_decision_date'
     WHERE o.name = 'eu_commisssion_decision_date';
    IF twins > 0 THEN
        RAISE EXCEPTION '% documents hold both names; resolve them first', twins;
    END IF;
END $$;

-- 1. The fact name.
UPDATE celex_facts.facts
   SET name = 'eu_commission_decision_date'
 WHERE name = 'eu_commisssion_decision_date';

-- 2. The value_json object key ([{"eu_commisssion_decision_date": "..."}]).
--    Textual replace keeps the stored JSON formatting byte for byte.
UPDATE celex_facts.facts
   SET value_json = replace(value_json,
                            '"eu_commisssion_decision_date":',
                            '"eu_commission_decision_date":')
 WHERE name = 'eu_commission_decision_date'
   AND value_json LIKE '%"eu_commisssion_decision_date":%';

-- 3. Audit trail fact names.
UPDATE celex_facts.fact_ingestion_audit_trail
   SET target_fact_name = 'eu_commission_decision_date'
 WHERE target_fact_name = 'eu_commisssion_decision_date';

UPDATE celex_facts.fact_ingestion_audit_trail
   SET verify_fact_name = 'eu_commission_decision_date'
 WHERE verify_fact_name = 'eu_commisssion_decision_date';

-- 4. Lineage labels carrying the fact name.
UPDATE celex_facts.fact_source_link
   SET extraction_method = 'eu_commission_decision_date'
 WHERE extraction_method = 'eu_commisssion_decision_date';

UPDATE celex_facts.fact_source_registry
   SET description = 'fact:eu_commission_decision_date'
 WHERE description = 'fact:eu_commisssion_decision_date';

-- 5. Fact embeddings under the old name (stale passage text and hash).
DELETE FROM celex_facts.question2fact2embeddings
 WHERE fact_name = 'eu_commisssion_decision_date';

-- 6. Post-conditions: abort (and roll back) on any residue.
DO $$
DECLARE
    residue bigint;
BEGIN
    SELECT (SELECT count(*) FROM celex_facts.facts
             WHERE name = 'eu_commisssion_decision_date'
                OR (name = 'eu_commission_decision_date'
                    AND value_json LIKE '%"eu_commisssion_decision_date":%'))
         + (SELECT count(*) FROM celex_facts.fact_ingestion_audit_trail
             WHERE target_fact_name = 'eu_commisssion_decision_date'
                OR verify_fact_name = 'eu_commisssion_decision_date')
         + (SELECT count(*) FROM celex_facts.fact_source_link
             WHERE extraction_method = 'eu_commisssion_decision_date')
         + (SELECT count(*) FROM celex_facts.fact_source_registry
             WHERE description = 'fact:eu_commisssion_decision_date')
         + (SELECT count(*) FROM celex_facts.question2fact2embeddings
             WHERE fact_name = 'eu_commisssion_decision_date')
      INTO residue;
    IF residue > 0 THEN
        RAISE EXCEPTION 'eu_commisssion_decision_date residue: % rows', residue;
    END IF;
END $$;

SELECT name, count(*) AS fact_rows
  FROM celex_facts.facts
 WHERE name IN ('eu_commisssion_decision_date', 'eu_commission_decision_date')
 GROUP BY name ORDER BY name;

COMMIT;
