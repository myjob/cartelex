-- =====================================================================
-- Migration: add full-text-search column + GIN index to pg_embedding_qwen
-- =====================================================================
-- Purpose
--   The enumeration retrieval strategy (EnumerationSearch) needs a BM25 /
--   PostgreSQL FTS leg over chunk text so rare named entities — courts,
--   tribunals, leniency applicants — that the bi-encoder embeds poorly are
--   still retrievable by literal surface form.
--
-- Approach
--   Add a STORED generated tsvector column maintained automatically by the
--   database on insert/update, and a GIN index on it. Mirrors the
--   established pattern on celex_facts.knowledge_items (knowledge_tsv).
--   No ingestion-side code changes are required.
--
-- Idempotency
--   IF NOT EXISTS guards make this safe to re-run.
--
-- Cost
--   Generated column triggers a one-time rewrite of the table. The GIN
--   build is the dominant cost; run during a maintenance window if the
--   table is large.
-- =====================================================================

ALTER TABLE celex_facts.pg_embedding_qwen
    ADD COLUMN IF NOT EXISTS document_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', document)) STORED;

CREATE INDEX IF NOT EXISTS pg_embedding_qwen_document_tsv_gin
    ON celex_facts.pg_embedding_qwen
    USING gin (document_tsv);
