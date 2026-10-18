-- ---------------------------------------------------------------------------
-- Data Lineage Registry — normalize source_registry on natural keys
--
-- Earlier ingestion appended a fresh source_registry row per fact, so the same
-- piece of evidence (chunk, heideltime expression, LEI, CSV row) could appear
-- many times.  This migration:
--   1. collapses duplicate sources onto a single canonical row,
--   2. repoints fact_source_link rows to the canonical source,
--   3. adds partial UNIQUE indexes enforcing the natural key per source type.
--
-- Safe to re-run: dedup is idempotent and index creation is guarded.
-- Run AFTER migrate_lineage_tables.sql.
-- ---------------------------------------------------------------------------

-- Canonical-key mapping: every source row -> the keeper for its natural key.
-- Rows with no natural key (NULL natural_key) map to themselves and are skipped.
WITH keyed AS (
    SELECT
        source_id,
        CASE
            WHEN source_type IN ('chunk', 'book_case') AND chunk_id IS NOT NULL
                THEN 'chunk:' || source_type::text || ':' || chunk_id::text
            WHEN source_type = 'heideltime' AND heideltime_event_id IS NOT NULL
                THEN 'heideltime:' || heideltime_event_id::text
            WHEN source_type = 'lei_corporate' AND lei_id IS NOT NULL
                THEN 'lei:' || lei_id
            WHEN source_type = 'csv_human' AND csv_row IS NOT NULL
                THEN 'csv:' || COALESCE(celex_id, '') || ':' || csv_row::text
            ELSE NULL
        END AS natural_key,
        created_at
    FROM celex_facts.source_registry
),
keepers AS (
    SELECT DISTINCT ON (natural_key)
        natural_key,
        source_id AS canonical_id
    FROM keyed
    WHERE natural_key IS NOT NULL
    ORDER BY natural_key, created_at ASC, source_id ASC
),
remap AS (
    SELECT k.source_id AS dup_id, kp.canonical_id
    FROM keyed k
    JOIN keepers kp ON kp.natural_key = k.natural_key
    WHERE k.natural_key IS NOT NULL
      AND k.source_id <> kp.canonical_id
)
-- 1. Move links from duplicate sources onto the canonical source, ignoring
--    pairs the fact already has against the canonical source.
INSERT INTO celex_facts.fact_source_link
    (fact_id, source_id, extraction_method, relation_type, trust_rank,
     conflict_flag, conflict_note, created_at)
SELECT
    fsl.fact_id, r.canonical_id, fsl.extraction_method, fsl.relation_type,
    fsl.trust_rank, fsl.conflict_flag, fsl.conflict_note, fsl.created_at
FROM celex_facts.fact_source_link fsl
JOIN remap r ON r.dup_id = fsl.source_id
ON CONFLICT (fact_id, source_id) DO NOTHING;

-- 2. Drop the now-redundant links pointing at duplicate sources.
DELETE FROM celex_facts.fact_source_link fsl
USING (
    SELECT k.source_id AS dup_id
    FROM (
        SELECT
            source_id,
            CASE
                WHEN source_type IN ('chunk', 'book_case') AND chunk_id IS NOT NULL
                    THEN 'chunk:' || source_type::text || ':' || chunk_id::text
                WHEN source_type = 'heideltime' AND heideltime_event_id IS NOT NULL
                    THEN 'heideltime:' || heideltime_event_id::text
                WHEN source_type = 'lei_corporate' AND lei_id IS NOT NULL
                    THEN 'lei:' || lei_id
                WHEN source_type = 'csv_human' AND csv_row IS NOT NULL
                    THEN 'csv:' || COALESCE(celex_id, '') || ':' || csv_row::text
                ELSE NULL
            END AS natural_key,
            created_at
        FROM celex_facts.source_registry
    ) k
    JOIN (
        SELECT DISTINCT ON (natural_key) natural_key, source_id AS canonical_id
        FROM (
            SELECT
                source_id,
                CASE
                    WHEN source_type IN ('chunk', 'book_case') AND chunk_id IS NOT NULL
                        THEN 'chunk:' || source_type::text || ':' || chunk_id::text
                    WHEN source_type = 'heideltime' AND heideltime_event_id IS NOT NULL
                        THEN 'heideltime:' || heideltime_event_id::text
                    WHEN source_type = 'lei_corporate' AND lei_id IS NOT NULL
                        THEN 'lei:' || lei_id
                    WHEN source_type = 'csv_human' AND csv_row IS NOT NULL
                        THEN 'csv:' || COALESCE(celex_id, '') || ':' || csv_row::text
                    ELSE NULL
                END AS natural_key,
                created_at
            FROM celex_facts.source_registry
        ) k2
        WHERE natural_key IS NOT NULL
        ORDER BY natural_key, created_at ASC, source_id ASC
    ) kp ON kp.natural_key = k.natural_key
    WHERE k.natural_key IS NOT NULL AND k.source_id <> kp.canonical_id
) dups
WHERE fsl.source_id = dups.dup_id;

-- 3. Delete the duplicate source rows now that no links reference them.
DELETE FROM celex_facts.source_registry sr
USING (
    SELECT k.source_id AS dup_id
    FROM (
        SELECT
            source_id,
            CASE
                WHEN source_type IN ('chunk', 'book_case') AND chunk_id IS NOT NULL
                    THEN 'chunk:' || source_type::text || ':' || chunk_id::text
                WHEN source_type = 'heideltime' AND heideltime_event_id IS NOT NULL
                    THEN 'heideltime:' || heideltime_event_id::text
                WHEN source_type = 'lei_corporate' AND lei_id IS NOT NULL
                    THEN 'lei:' || lei_id
                WHEN source_type = 'csv_human' AND csv_row IS NOT NULL
                    THEN 'csv:' || COALESCE(celex_id, '') || ':' || csv_row::text
                ELSE NULL
            END AS natural_key,
            created_at
        FROM celex_facts.source_registry
    ) k
    JOIN (
        SELECT DISTINCT ON (natural_key) natural_key, source_id AS canonical_id
        FROM (
            SELECT
                source_id,
                CASE
                    WHEN source_type IN ('chunk', 'book_case') AND chunk_id IS NOT NULL
                        THEN 'chunk:' || source_type::text || ':' || chunk_id::text
                    WHEN source_type = 'heideltime' AND heideltime_event_id IS NOT NULL
                        THEN 'heideltime:' || heideltime_event_id::text
                    WHEN source_type = 'lei_corporate' AND lei_id IS NOT NULL
                        THEN 'lei:' || lei_id
                    WHEN source_type = 'csv_human' AND csv_row IS NOT NULL
                        THEN 'csv:' || COALESCE(celex_id, '') || ':' || csv_row::text
                    ELSE NULL
                END AS natural_key,
                created_at
            FROM celex_facts.source_registry
        ) k2
        WHERE natural_key IS NOT NULL
        ORDER BY natural_key, created_at ASC, source_id ASC
    ) kp ON kp.natural_key = k.natural_key
    WHERE k.natural_key IS NOT NULL AND k.source_id <> kp.canonical_id
) dups
WHERE sr.source_id = dups.dup_id;

-- 4. Enforce the natural key going forward.
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_registry_chunk
    ON celex_facts.source_registry (source_type, chunk_id)
    WHERE chunk_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_registry_heideltime
    ON celex_facts.source_registry (heideltime_event_id)
    WHERE heideltime_event_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_registry_lei
    ON celex_facts.source_registry (lei_id)
    WHERE lei_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_registry_csv
    ON celex_facts.source_registry (celex_id, csv_row)
    WHERE csv_row IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_registry_fact
    ON celex_facts.source_registry (source_fact_id)
    WHERE source_fact_id IS NOT NULL;
