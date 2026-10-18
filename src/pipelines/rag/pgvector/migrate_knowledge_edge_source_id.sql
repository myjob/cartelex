-- ---------------------------------------------------------------------------
-- Migration: route knowledge_edges raw-evidence grounding through source_registry
--
-- Before: knowledge_edges had a polymorphic destination of
--           {dst_chunk_id, dst_fact_id, dst_knowledge_id}
--         where dst_chunk_id pointed straight at pg_embedding_qwen — a second,
--         parallel way of referencing raw evidence alongside source_registry.
--
-- After:  the raw-evidence arm delegates to the canonical evidence catalog via
--           {dst_source_id -> source_registry, dst_fact_id, dst_knowledge_id}.
--         Facts (fact_source_link) and knowledge items (knowledge_edges) now
--         ground against ONE source catalog; adding a new source_type reaches
--         both subsystems with no schema churn. dst_fact_id / dst_knowledge_id
--         stay put — they are derived nodes, never raw sources.
--
-- Safe to re-run: every step is guarded.
-- Depends on: migrate_lineage_tables.sql (source_registry must exist).
-- ---------------------------------------------------------------------------

-- 1. Add the new destination column (FK to the canonical evidence catalog).
ALTER TABLE celex_facts.knowledge_edges
    ADD COLUMN IF NOT EXISTS dst_source_id UUID
        REFERENCES celex_facts.source_registry (source_id);

-- 2. Backfill: catalogue every chunk currently referenced by dst_chunk_id as a
--    'chunk' source (idempotent on chunk_id), then repoint the edge. Skipped
--    cleanly when dst_chunk_id has already been dropped on a prior run.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'celex_facts'
          AND table_name = 'knowledge_edges'
          AND column_name = 'dst_chunk_id'
    ) THEN
        -- 2a. Insert a chunk source for each distinct dst_chunk_id not yet
        --     present in the registry. pg_embedding_qwen.id and
        --     source_registry.chunk_id are both UUID-format varchar, so no cast.
        INSERT INTO celex_facts.source_registry (source_type, celex_id, chunk_id, description)
        SELECT DISTINCT
            'chunk'::celex_facts.source_type_enum,
            pe.celex_id,
            ke.dst_chunk_id,
            'backfill from knowledge_edges.dst_chunk_id'
        FROM celex_facts.knowledge_edges ke
        JOIN celex_facts.pg_embedding_qwen pe ON pe.id = ke.dst_chunk_id
        WHERE ke.dst_chunk_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM celex_facts.source_registry sr
              WHERE sr.source_type = 'chunk'
                AND sr.chunk_id = ke.dst_chunk_id
          );

        -- 2b. Repoint each chunk edge at its registry row.
        UPDATE celex_facts.knowledge_edges ke
        SET dst_source_id = sr.source_id
        FROM celex_facts.source_registry sr
        WHERE ke.dst_chunk_id IS NOT NULL
          AND sr.source_type = 'chunk'
          AND sr.chunk_id = ke.dst_chunk_id;

        -- 2c. Drop the legacy column. This also drops the old num_nonnulls
        --     CHECK that referenced it, plus the FK to pg_embedding_qwen.
        ALTER TABLE celex_facts.knowledge_edges DROP COLUMN dst_chunk_id;
    END IF;
END $$;

-- 3. Re-assert the "exactly one destination" invariant over the new triple.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_knowledge_edges_one_destination'
          AND conrelid = 'celex_facts.knowledge_edges'::regclass
    ) THEN
        ALTER TABLE celex_facts.knowledge_edges
            ADD CONSTRAINT ck_knowledge_edges_one_destination
            CHECK (num_nonnulls(dst_source_id, dst_fact_id, dst_knowledge_id) = 1);
    END IF;
END $$;

-- 4. Index the new FK for grounding lookups (knowledge grounded in a source).
CREATE INDEX IF NOT EXISTS ix_knowledge_edges_dst_source
    ON celex_facts.knowledge_edges (dst_source_id)
    WHERE dst_source_id IS NOT NULL;
