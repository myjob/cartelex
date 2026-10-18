-- =====================================================================
-- Migration: accent-insensitive + trigram fuzzy matching for chunk text
-- =====================================================================
-- Purpose
--   Phase 1 of the knowledge-representation review replaces the fragile
--   ILIKE lexical leg of HybridSearch with Postgres full-text search, and
--   adds a trigram path for fuzzy entity-name matching (e.g. Maersk vs
--   Maersk with the ligature, AEG-Telefunken spelling variants). Both need
--   supporting extensions and an accent-folded trigram index.
--
--   The full-text-search column (document_tsv) and its GIN index already
--   exist (migrate_pg_embedding_qwen_add_chunk_fts.sql); this migration
--   only adds the extensions, the IMMUTABLE unaccent wrapper, and the
--   trigram index that ChunkRepository.trigram_search relies on.
--
-- unaccent immutability
--   The extension's unaccent(text) is only STABLE, so it cannot appear in
--   an index expression directly. celex_facts.f_unaccent pins the
--   dictionary argument, which makes the call IMMUTABLE and indexable. Both
--   the index and every query MUST use f_unaccent (not bare unaccent) so the
--   planner can match the expression against the index.
--
-- Extension schema
--   Both extensions live in the celex_facts schema on this database, so the
--   wrapper body, the trigram operator class, and every query reference them
--   schema-qualified (celex_facts.unaccent / celex_facts.gin_trgm_ops) rather
--   than relying on the connection search_path. WITH SCHEMA celex_facts keeps
--   a fresh install deterministic.
--
-- Scope note
--   document_tsv stays plain to_tsvector('english', document) (no unaccent)
--   in Phase 1 — regenerating that STORED column is a full table rewrite of
--   the hot embedding table and is deferred to the Phase 3 re-embed. FTS
--   therefore delivers stemming + multi-word matching now; accent-folding is
--   covered by the trigram path, which controls both sides of the comparison.
--
-- Idempotency
--   IF NOT EXISTS / CREATE OR REPLACE guards make this safe to re-run.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA celex_facts;
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA celex_facts;

-- IMMUTABLE, indexable wrapper around unaccent (dictionary pinned).
CREATE OR REPLACE FUNCTION celex_facts.f_unaccent(text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    STRICT
AS $$
    SELECT celex_facts.unaccent('celex_facts.unaccent', $1)
$$;

-- Accent-folded trigram GIN index accelerating ILIKE / word_similarity on
-- chunk text for fuzzy entity-name lookups.
CREATE INDEX IF NOT EXISTS pg_embedding_qwen_document_trgm_gin
    ON celex_facts.pg_embedding_qwen
    USING gin (celex_facts.f_unaccent(document) celex_facts.gin_trgm_ops);
