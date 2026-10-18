-- =====================================================================
-- Migration: stamp source_hash on extractions outside the remediation set
-- =====================================================================
-- Purpose
--   migrate_heideltime_source_hash.sql deliberately left source_hash NULL on
--   all 198,302 legacy rows, because NULL reads as stale and that is what
--   forces the damaged documents to be re-extracted without a backfill that
--   would have to guess which rows predate the chunk-text repair.
--
--   The side effect is that EVERY heideltime_raw run now re-scans every chunk
--   it touches, including the ~18,600 outside the damaged set that already
--   hold correct rows and were skipped forever before.  This script ends that
--   by stamping the rows whose chunk text provably has not changed since they
--   were extracted.
--
-- Why this is safe, and why a BLANKET backfill is not
--   Stamping a row asserts "these extractions describe this exact text".  For
--   a chunk whose text was rewritten by scan_dates.py that assertion is FALSE
--   and stamping it would freeze the bug permanently -- which is why the
--   exclusion below is the whole point of the script, not a nicety.
--
--   Two exclusions, and they are exhaustive for the damage we can identify:
--     1. Every CELEX in the remediation list -- the union of the documents
--        the regression probe found under-extracted, the documents left with
--        rowless date-bearing chunks by the repair, and every document
--        scan_dates.py rewrote on 2026-08-31.
--     2. Any chunk that still holds fewer rows than its text has well-formed
--        day-first dates, as a belt-and-braces catch for a document the list
--        build missed.
--
--   Measured on 2026-08-31: all 1,254 corpus chunks that carry a well-formed
--   date and no extraction row at all sit INSIDE the remediation list, so the
--   population this script stamps has no detectable staleness left in it.
--
-- Order
--   Run this AFTER the heideltime_raw re-extraction of the remediation list
--   has finished.  Running it before is still correct -- the list is excluded
--   either way -- but the re-extracted rows carry their own hash already, so
--   afterwards is the cheaper pass.
--
--   The CELEX list is the \copy path in section 0 -- edit that ONE line to
--   retarget the script.  psql performs no variable interpolation in \copy.
--
-- Apply with
--   psql "$DATABASE_URL" \
--     -f src/pipelines/rag/pgvector/migrate_heideltime_source_hash_backfill.sql
--   from the repository root.
--
-- Idempotent: only rows with source_hash IS NULL are touched.
-- =====================================================================

\set ON_ERROR_STOP on

BEGIN;

-- ---------------------------------------------------------------------
-- 0. The documents to EXCLUDE.  THE ONLY PLACE A PATH APPEARS.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _excluded_celex (celex_id text) ON COMMIT DROP;
\copy _excluded_celex FROM 'reports/heideltime_remediation_celex_20260831.txt'

CREATE INDEX ON _excluded_celex (celex_id);

-- 0a. The rows this run is allowed to change, captured BEFORE the UPDATE.
--     Needed for the post-condition: rows in the remediation set that were
--     already stamped by a re-extraction are legitimate, and only a row that
--     went from NULL to stamped *here* would be the mistake.
CREATE TEMP TABLE _null_before ON COMMIT DROP AS
SELECT id FROM celex_facts.heideltime_extractions WHERE source_hash IS NULL;

CREATE INDEX ON _null_before (id);

-- ---------------------------------------------------------------------
-- 1. Inventory before the write, so the run log records the split.
-- ---------------------------------------------------------------------
SELECT count(*)                                          AS unhashed_rows,
       count(*) FILTER (WHERE x.celex_id IS NOT NULL)    AS in_remediation_set,
       count(*) FILTER (WHERE x.celex_id IS NULL)        AS candidates
  FROM celex_facts.heideltime_extractions h
  LEFT JOIN _excluded_celex x ON x.celex_id = h.celex_id
 WHERE h.source_hash IS NULL;

-- ---------------------------------------------------------------------
-- 2. The backfill.  A chunk is stamped only when it is outside the
--    remediation set AND its row count already accounts for every
--    well-formed day-first date its text contains.
-- ---------------------------------------------------------------------
WITH candidate_chunks AS (
    SELECT h.fk_chunk_id,
           p.document,
           count(*) AS stored_rows,
           (SELECT count(*)
              FROM regexp_matches(p.document,
                   '[0-9]{1,2} (January|February|March|April|May|June|July|'
                   || 'August|September|October|November|December) [0-9]{4}',
                   'g')) AS visible_dates
      FROM celex_facts.heideltime_extractions h
      JOIN celex_facts.pg_embedding_qwen p
        ON p.id::text = h.fk_chunk_id
      LEFT JOIN _excluded_celex x
        ON x.celex_id = h.celex_id
     WHERE h.source_hash IS NULL
       AND x.celex_id IS NULL
       AND p.document IS NOT NULL
     GROUP BY h.fk_chunk_id, p.document
)
UPDATE celex_facts.heideltime_extractions h
   SET source_hash = md5(c.document)
  FROM candidate_chunks c
 WHERE h.fk_chunk_id = c.fk_chunk_id
   AND h.source_hash IS NULL
   AND c.stored_rows >= c.visible_dates;

-- ---------------------------------------------------------------------
-- 3. Post-conditions.  The first two MUST be 0.
--    A row inside the remediation set that this run moved from NULL to
--    stamped is exactly the mistake the script exists to avoid.  Rows in
--    that set stamped by an earlier re-extraction are correct and are not
--    counted, which is what _null_before is for.
-- ---------------------------------------------------------------------
SELECT 'newly_stamped_in_remediation_set' AS check_name, count(*) AS rows
  FROM celex_facts.heideltime_extractions h
  JOIN _excluded_celex x ON x.celex_id = h.celex_id
  JOIN _null_before b ON b.id = h.id
 WHERE h.source_hash IS NOT NULL
UNION ALL
SELECT 'hash_disagrees_with_text', count(*)
  FROM celex_facts.heideltime_extractions h
  JOIN celex_facts.pg_embedding_qwen p ON p.id::text = h.fk_chunk_id
 WHERE h.source_hash IS NOT NULL
   AND h.source_hash <> md5(p.document)
UNION ALL
SELECT 'still_unhashed', count(*)
  FROM celex_facts.heideltime_extractions
 WHERE source_hash IS NULL;

COMMIT;
