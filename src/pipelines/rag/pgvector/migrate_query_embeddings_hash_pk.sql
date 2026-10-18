-- Migration: replace btree PK on raw query text with one on MD5 hash
-- Reason: long queries (e.g. full NACE classification lists) exceed the 2704-byte
-- btree v4 maximum for index rows.
--
-- Run once against the target database; idempotent via IF NOT EXISTS / IF EXISTS.

BEGIN;

-- 1. Drop the offending primary key on the unbounded text column.
ALTER TABLE celex_facts.query_embeddings_qwen
    DROP CONSTRAINT IF EXISTS query_embeddings__qwen_pkey;

-- 2. Add a stored generated column that holds the MD5 digest of the query.
--    MD5 hex strings are always 32 characters — well within btree limits.
ALTER TABLE celex_facts.query_embeddings_qwen
    ADD COLUMN IF NOT EXISTS query_hash text
        GENERATED ALWAYS AS (md5(query)) STORED;

-- Note: GENERATED ALWAYS AS … STORED automatically back-fills existing rows
-- when the column is added, so no explicit UPDATE is required.

-- 3. Re-create the primary key on the hash column.
ALTER TABLE celex_facts.query_embeddings_qwen
    ADD CONSTRAINT query_embeddings__qwen_pkey PRIMARY KEY (query_hash);

COMMIT;
