-- ---------------------------------------------------------------------------
-- Add the 'dated_in' relation to celex_facts.edge_relation_enum
--
-- 'dated_in' is the temporal layer: a company -> case edge emitted from the
-- heideltime_event fact stream, carrying aggregated date bounds as payload.
--
-- WHY THIS FILE CONTAINS EXACTLY ONE STATEMENT AND NO BEGIN/COMMIT
-- ---------------------------------------------------------------
-- PostgreSQL permits ALTER TYPE ... ADD VALUE inside a transaction block, but
-- the new label cannot be *used* until that transaction commits. Wrapping this
-- together with anything that inserts or casts to 'dated_in' therefore fails
-- with "unsafe use of new value of enum type". Run this file on its own, let it
-- commit, and only then run Main_materialize_entity_graph.py (a separate
-- process, hence a separate transaction).
--
-- In DBeaver: execute as a single statement (Ctrl+Enter), not as a script.
--
-- Safe to re-run: IF NOT EXISTS makes the ALTER a no-op once the label is there.
-- The label is also added to the guarded CREATE TYPE in migrate_entity_graph.sql
-- so a freshly built database gets it without needing this file at all.
-- ---------------------------------------------------------------------------

ALTER TYPE celex_facts.edge_relation_enum ADD VALUE IF NOT EXISTS 'dated_in';

-- Verify (expect 9 labels, 'dated_in' last):
--   SELECT enumlabel FROM pg_enum e
--     JOIN pg_type t ON t.oid = e.enumtypid
--    WHERE t.typname = 'edge_relation_enum'
--    ORDER BY enumsortorder;
