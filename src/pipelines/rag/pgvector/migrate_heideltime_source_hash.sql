-- =====================================================================
-- Migration: tie heideltime_extractions rows to the chunk text they came from
-- =====================================================================
-- Purpose
--   ``heideltime_extractions`` had no key linking a row to the version of
--   ``pg_embedding_qwen.document`` it was extracted from.  When scan_dates.py
--   rewrote chunk text in place (2026-04-29), every stored row silently became
--   pre-repair data that no re-run could correct: the chunk filter in
--   HeidelTimeModule counted "has >= 1 row" as done, and ingest_vos()
--   ON CONFLICT DO NOTHING would have blocked a replacement anyway.
--
--   ``source_hash`` carries md5() of the chunk text at extraction time.  A
--   chunk is "done" only while every one of its rows still matches
--   md5(document); anything else is stale and gets deleted and re-extracted.
--
-- Why nullable
--   NULL means "extracted before hashing existed" and is read as STALE by
--   EventRepository.heideltime_fresh_chunk_ids.  That is deliberate: it makes
--   the 198,301 legacy rows self-invalidating and removes the need for a
--   backfill that would have to guess which rows predate the text repair.
--
--   Do NOT blanket-backfill this column with md5(document).  For the 87
--   documents damaged by the in-place text repair that would stamp
--   pre-repair rows as fresh and freeze the bug permanently.  The scoped
--   post-remediation backfill is migrate_heideltime_source_hash_backfill.sql.
--
-- Index
--   (fk_chunk_id, source_hash) serves the freshness probe, which groups by
--   fk_chunk_id and compares the hash per row.
--
-- Column comment lives in schema_comments.sql, per project convention.
--
-- Apply with
--   psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/migrate_heideltime_source_hash.sql
--   psql "$DATABASE_URL" -f src/pipelines/rag/pgvector/schema_comments.sql
--
-- Idempotent: both statements are IF NOT EXISTS.
-- =====================================================================

BEGIN;

ALTER TABLE celex_facts.heideltime_extractions
    ADD COLUMN IF NOT EXISTS source_hash character varying(32);

CREATE INDEX IF NOT EXISTS ix_heideltime_source_hash
    ON celex_facts.heideltime_extractions (fk_chunk_id, source_hash);

SELECT count(*) AS total_rows,
       count(source_hash) AS hashed_rows,
       count(*) - count(source_hash) AS stale_rows
  FROM celex_facts.heideltime_extractions;

COMMIT;
