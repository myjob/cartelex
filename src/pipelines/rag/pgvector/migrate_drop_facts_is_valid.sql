-- ---------------------------------------------------------------------------
-- Migration: drop the vestigial celex_facts.facts.is_valid column
--
-- No production code ever assigned this column: pipeline inserts received
-- `true` only from the SQLAlchemy `default=True` on the ORM model, and the
-- column carried no DB-level default (raw-SQL inserts landed NULL).  Every
-- `false` row originated in the 2026-02/2026-03 legacy bulk imports
-- (migration_legacy/copy_facts_from_rule_engine.py) or hand-run INSERTs.
--
-- Its single production reader was the fact-embedding driver query
-- (embeddings/fact_embedding_loader_qwen.py, `WHERE is_valid IS NOT FALSE`),
-- which silently excluded 5792 facts from question2fact2embeddings, among
-- them 1407 `manual_approved` and 615 `verified_supported` ones.
--
-- Both axes the flag could have carried are already modelled elsewhere:
--   * quality    -> celex_facts.facts.verification_status
--   * provenance -> celex_facts.source_registry.source_type / fact_source_link
--
-- Two views select the column inside an inline sub-select without exposing it,
-- so they are replaced first (CREATE OR REPLACE keeps owner and grants and
-- releases the column dependency); the DROP then succeeds without CASCADE.
--
-- Safe to re-run.  Requires ownership of celex_facts.facts and of both views.
-- ---------------------------------------------------------------------------

BEGIN;

CREATE OR REPLACE VIEW celex_facts.facts_cartel_legal_period_vw AS
SELECT c.celex_id,
       c.value_md AS cartel_period,
       c.value_md AS legal_period
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

CREATE OR REPLACE VIEW celex_facts.question2fact2embeddings_vw AS
SELECT f.id,
       f.celex_id,
       e.fact_name,
       e.question,
       e.value_txt,
       e.hash_q_a,
       e.embeddings
  FROM celex_facts.question2fact2embeddings e
  JOIN (SELECT facts.id,
               facts.celex_id,
               facts.name,
               facts.value_md,
               facts.value_json,
               facts.created_at,
               facts.references_doc_json,
               facts.references_fact_json
          FROM celex_facts.facts) f
    ON f.id = e.fk_fact_id;

ALTER TABLE celex_facts.facts DROP COLUMN IF EXISTS is_valid;

COMMIT;
