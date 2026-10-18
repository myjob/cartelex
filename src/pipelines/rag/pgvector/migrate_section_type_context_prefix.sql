-- =====================================================================
-- Migration: section_type + context_prefix columns on pg_embedding_qwen,
--            document_tsv redefined to cover the context prefix
-- =====================================================================
-- Purpose
--   Phase 3 of the knowledge-representation review models the canonical
--   structure of a Commission decision (parties -> recitals/facts ->
--   legal assessment -> fines -> operative -> annex) as chunk metadata:
--
--   * section_type    — enum-ish varchar written by the rule-first /
--                       LLM-fallback classifier batch
--                       (src/batch/Main_classify_sections.py). Enables
--                       filtered retrieval (section_types parameter on
--                       ChunkRepository / HybridSearch) and the
--                       section-type ranking boost that replaces the
--                       positional "Legal Climax" heuristic.
--   * context_prefix  — 1-2 sentence contextual-retrieval line (case
--                       name, section, chunk topic) written by
--                       src/batch/Main_enrich_chunk_context.py, which
--                       also re-embeds context_prefix + document.
--
--   document_tsv is redefined so the lexical (FTS) leg also sees the
--   prefix; the dense leg sees it via the re-embed.
--
-- Approach
--   Plain ADD COLUMN IF NOT EXISTS for the two nullable columns. Postgres
--   cannot ALTER a generated column's expression, so document_tsv is
--   dropped and re-added with the new expression inside a DO block that
--   first inspects the current expression via pg_get_expr — a re-run, or
--   a run against an already-migrated table, is a no-op.
--
--   The tsvector expression stays plain to_tsvector('english', ...)
--   (no unaccent), matching the query side (websearch_to_tsquery /
--   plainto_tsquery without unaccent); accent-folding remains the
--   trigram path's job (see migrate_fts_extensions.sql).
--
--   coalesce(context_prefix, '') preserves the pre-migration NULL
--   semantics: a NULL document still yields a NULL tsvector.
--
-- Idempotency
--   IF NOT EXISTS guards plus the pg_get_expr inspection make this safe
--   to re-run.
--
-- Cost
--   Dropping/re-adding the STORED generated column rewrites the whole
--   table (~55k rows) and rebuilds the GIN index; FTS is briefly
--   unindexed between the drop and the CREATE INDEX. Run in a quiet
--   window.
-- =====================================================================

ALTER TABLE celex_facts.pg_embedding_qwen
    ADD COLUMN IF NOT EXISTS context_prefix text NULL;

ALTER TABLE celex_facts.pg_embedding_qwen
    ADD COLUMN IF NOT EXISTS section_type varchar NULL;

DO $$
DECLARE
    tsv_expr text;
BEGIN
    SELECT pg_get_expr(d.adbin, d.adrelid)
    INTO tsv_expr
    FROM pg_attribute a
    JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attrelid = 'celex_facts.pg_embedding_qwen'::regclass
      AND a.attname = 'document_tsv'
      AND NOT a.attisdropped;

    IF tsv_expr IS NULL OR tsv_expr NOT LIKE '%context_prefix%' THEN
        ALTER TABLE celex_facts.pg_embedding_qwen
            DROP COLUMN IF EXISTS document_tsv;
        ALTER TABLE celex_facts.pg_embedding_qwen
            ADD COLUMN document_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english',
                            coalesce(context_prefix, '') || ' ' || document)
            ) STORED;
    END IF;
END $$;

-- DROP COLUMN above also dropped the GIN index; recreate either way.
CREATE INDEX IF NOT EXISTS pg_embedding_qwen_document_tsv_gin
    ON celex_facts.pg_embedding_qwen
    USING gin (document_tsv);

CREATE INDEX IF NOT EXISTS ix_pg_embedding_qwen_section_type
    ON celex_facts.pg_embedding_qwen
    USING btree (section_type);
