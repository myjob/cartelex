-- ---------------------------------------------------------------------------
-- Migration: harmonize surrogate identifier types
-- Added: 2026-05-31
--
-- Rationale
--   Surrogate keys that already hold UUID values were typed inconsistently:
--   the legacy core (facts.id and its FKs) was varchar, while the newer
--   lineage/knowledge subsystem is native uuid. The two meet at foreign keys,
--   which forced ::uuid casts and DDL/ORM type clashes.
--
--   Governing rule adopted: an FK column's type equals the PK it references.
--   Surrogate PKs we own are native uuid; external natural keys
--   (celex_id, lei_id, case_id) stay text.
--
-- Changes
--   A. facts.id varchar -> uuid, propagated to every FK that references it:
--        fact_source_link.fact_id, knowledge_edges.dst_fact_id,
--        fact_ingestion_audit_trail.fact_id.
--      (question2fact2embeddings.fk_fact_id is already uuid; the view join
--       becomes type-consistent once facts.id is uuid.)
--   B. Chunk arm DOWN to varchar: source_registry.chunk_id uuid -> varchar to
--      match pg_embedding_qwen.id (a LangChain-owned varchar PK we do not
--      mutate). Chunk-source lineage capture is unchanged; only the type moves.
--
-- Safe to re-run: every step inspects current types / constraints before acting.
-- PRE-FLIGHT (run separately, must return 0):
--   SELECT count(*) FROM celex_facts.facts
--   WHERE id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
-- ---------------------------------------------------------------------------

-- ===========================================================================
-- A. facts.id varchar -> uuid (+ dependent FK columns)
-- ===========================================================================
DO $$
DECLARE
    fk record;
BEGIN
    -- Only act while facts.id is still character varying.
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'celex_facts' AND table_name = 'facts'
          AND column_name = 'id' AND data_type = 'character varying'
    ) THEN
        -- 1. Drop every FK constraint that references celex_facts.facts so the
        --    PK column can be retyped. Names are auto-generated, so discover
        --    them from the catalog rather than hard-coding.
        FOR fk IN
            SELECT con.conname, con.conrelid::regclass AS child_table
            FROM pg_constraint con
            WHERE con.contype = 'f'
              AND con.confrelid = 'celex_facts.facts'::regclass
        LOOP
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',
                           fk.child_table, fk.conname);
        END LOOP;

        -- 2. Retype the dependent FK columns that are currently varchar.
        --    fact_source_link.fact_id and knowledge_edges.dst_fact_id are
        --    already uuid in the live schema; only the audit-trail column and
        --    facts.id itself are varchar.
        ALTER TABLE celex_facts.fact_ingestion_audit_trail
            ALTER COLUMN fact_id TYPE uuid USING fact_id::uuid;

        -- 3. Retype the PK column and give it a server-side default.
        ALTER TABLE celex_facts.facts
            ALTER COLUMN id TYPE uuid USING id::uuid;
        ALTER TABLE celex_facts.facts
            ALTER COLUMN id SET DEFAULT gen_random_uuid();

        -- 4. Recreate the foreign keys (explicit, stable names).
        ALTER TABLE celex_facts.fact_source_link
            ADD CONSTRAINT fact_source_link_fact_id_fkey
            FOREIGN KEY (fact_id) REFERENCES celex_facts.facts (id)
            ON DELETE CASCADE;

        ALTER TABLE celex_facts.knowledge_edges
            ADD CONSTRAINT knowledge_edges_dst_fact_id_fkey
            FOREIGN KEY (dst_fact_id) REFERENCES celex_facts.facts (id);

        ALTER TABLE celex_facts.fact_ingestion_audit_trail
            ADD CONSTRAINT fact_ingestion_audit_trail_fact_id_fkey
            FOREIGN KEY (fact_id) REFERENCES celex_facts.facts (id);
    END IF;
END $$;

-- ===========================================================================
-- B. source_registry.chunk_id uuid -> varchar (match pg_embedding_qwen.id)
-- ===========================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'celex_facts' AND table_name = 'source_registry'
          AND column_name = 'chunk_id' AND data_type = 'uuid'
    ) THEN
        -- Drop the indexes that depend on the column type, retype, rebuild.
        DROP INDEX IF EXISTS celex_facts.uq_source_registry_chunk;
        DROP INDEX IF EXISTS celex_facts.ix_source_registry_chunk_id;

        ALTER TABLE celex_facts.source_registry
            ALTER COLUMN chunk_id TYPE varchar USING chunk_id::text;

        CREATE UNIQUE INDEX uq_source_registry_chunk
            ON celex_facts.source_registry (source_type, chunk_id)
            WHERE chunk_id IS NOT NULL;
        CREATE INDEX ix_source_registry_chunk_id
            ON celex_facts.source_registry (chunk_id)
            WHERE chunk_id IS NOT NULL;
    END IF;
END $$;
