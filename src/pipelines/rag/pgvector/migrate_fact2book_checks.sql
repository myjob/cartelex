-- Migration: fact2book_checks table (renamed from fact2book_mismatch)
-- =========================================================================
-- Full per-fact ledger of cross-checks between extracted CELEX facts and the
-- Russo-Schinkel book's analysis of the same case.
--
-- Written by BookCrossCheckModule for every fact it judges against the book.
-- The `action` column records the routing decision -- 'corroborated',
-- 'mismatch', or 'skipped' -- so the table is a complete check ledger, not a
-- contradiction-only one. One row per (fact, celex_id); re-runs upsert
-- rather than duplicate.
--
-- Idempotent: safe to re-run (guards on IF [NOT] EXISTS). Handles both a fresh
-- install (CREATE TABLE) and a live DB already renamed from fact2book_mismatch
-- (ADD COLUMN / RENAME INDEX no-ops).
-- =========================================================================

-- Fresh installs: create the table at its current name with all columns.
CREATE TABLE IF NOT EXISTS celex_facts.fact2book_checks (
    id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id       uuid        NOT NULL
        REFERENCES celex_facts.facts (id) ON DELETE CASCADE,
    celex_id      text        NOT NULL,
    case_id       text,

    -- Stable id inside pg_embedding_book_qwen (LangChain-owned varchar holding
    -- a UUID-format string). Type matches the referenced PK (varchar).
    book_chunk_id varchar     NOT NULL,
    book_path     text,

    explanation   text        NOT NULL,
    support_label text,
    final_score   numeric,

    -- Routing decision for the fact: 'corroborated' | 'mismatch' | 'skipped'.
    action         varchar     NOT NULL,
    -- How the book references the case ('landmark' | 'simple'); the provenance
    -- that justified the verdict.
    reference_type varchar,

    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Existing installs renamed from fact2book_mismatch: backfill newer columns.
ALTER TABLE celex_facts.fact2book_checks
    ADD COLUMN IF NOT EXISTS action varchar;
ALTER TABLE celex_facts.fact2book_checks
    ADD COLUMN IF NOT EXISTS reference_type varchar;

-- Carry the index / constraint names across the rename (no-op on fresh installs
-- where they were already created under the *_checks names below).
ALTER INDEX IF EXISTS celex_facts.uq_fact2book_mismatch
    RENAME TO uq_fact2book_checks;
ALTER INDEX IF EXISTS celex_facts.ix_fact2book_mismatch_fact
    RENAME TO ix_fact2book_checks_fact;
ALTER INDEX IF EXISTS celex_facts.ix_fact2book_mismatch_celex
    RENAME TO ix_fact2book_checks_celex;
ALTER INDEX IF EXISTS celex_facts.fact2book_mismatch_pkey
    RENAME TO fact2book_checks_pkey;

CREATE INDEX IF NOT EXISTS ix_fact2book_checks_fact
    ON celex_facts.fact2book_checks (fact_id);

CREATE INDEX IF NOT EXISTS ix_fact2book_checks_celex
    ON celex_facts.fact2book_checks (celex_id);

-- =========================================================================
-- 20260804: re-key the ledger from (fact_id, book_chunk_id) to
-- (fact_id, celex_id).
--
-- The citation anchor is chosen per fact by the reranker
-- (BookCrossCheckModule._book_context_for_fact, top_k=1), so it drifts between
-- runs. With book_chunk_id in the conflict target, a re-run that picked a
-- different anchor INSERTed a second row instead of overwriting the first,
-- leaving stale verdicts behind. The module writes exactly one verdict per
-- fact per case, and --skip-existing already resumes per (celex_id, fact_id),
-- so (fact_id, celex_id) is the true natural key.
-- =========================================================================

-- Legacy rows from before `action` existed: all contradiction-only rows of the
-- former fact2book_mismatch table, every one superseded by a re-judged row for
-- the same fact.
DELETE FROM celex_facts.fact2book_checks
WHERE action IS NULL;

-- Drifted duplicates: keep the most recently written verdict per (fact, celex).
DELETE FROM celex_facts.fact2book_checks AS stale
USING celex_facts.fact2book_checks AS keeper
WHERE stale.fact_id = keeper.fact_id
  AND stale.celex_id = keeper.celex_id
  AND (stale.created_at, stale.id) < (keeper.created_at, keeper.id);

DROP INDEX IF EXISTS celex_facts.uq_fact2book_checks;

-- One check row per (fact, celex); re-runs heal rather than duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fact2book_checks
    ON celex_facts.fact2book_checks (fact_id, celex_id);

-- Every judged fact carries a routing decision -- a NULL action means a row
-- predates the ledger contract.
ALTER TABLE celex_facts.fact2book_checks
    ALTER COLUMN action SET NOT NULL;

-- Pin the CrossCheckAction value set in the DB, not only in Python.
ALTER TABLE celex_facts.fact2book_checks
    DROP CONSTRAINT IF EXISTS ck_fact2book_checks_action;
ALTER TABLE celex_facts.fact2book_checks
    ADD CONSTRAINT ck_fact2book_checks_action
    CHECK (action IN ('corroborated', 'mismatch', 'skipped'));

-- =========================================================================
-- 20260913: retract stale book corroboration links.
--
-- BookCrossCheckModule linked a fact to a book_case source on every
-- corroboration but never removed the link when a later run judged the fact
-- skipped / mismatch, and each drifted reranker anchor registered another
-- source. Keep only the link whose source chunk is the fact's current
-- corroborated anchor in fact2book_checks; the module now maintains this
-- invariant itself (LineageRepository.retract_fact_links). Scoped to
-- extraction_method = 'book_cross_check' so book_case links written by other
-- producers are untouched. Registry rows are shared and left in place.
-- =========================================================================
DELETE FROM celex_facts.fact_source_link AS l
USING celex_facts.fact_source_registry AS sr
WHERE sr.source_id = l.source_id
  AND sr.source_type = 'book_case'
  AND l.relation_type = 'corroborated_by'
  AND l.extraction_method = 'book_cross_check'
  AND NOT EXISTS (
      SELECT 1
      FROM celex_facts.fact2book_checks AS c
      WHERE c.fact_id = l.fact_id
        AND c.action = 'corroborated'
        AND c.book_chunk_id = sr.chunk_id
  );

-- =========================================================================
-- 20260917: case-level run ledger for the cross-check driver.
--
-- fact2book_checks is a per-fact ledger, so it cannot say whether a case was
-- visited at all (not-in-book cases and index-only references write no fact
-- rows). One row per (celex_id, case_id) records the driver's latest pass;
-- Main_cross_check_facts_vs_book.py --run-id X --resume skips the keys that
-- finished under run X, which is what makes a crashed or thermally aborted
-- run restartable. Additive: nothing here touches fact2book_checks.
-- =========================================================================
CREATE TABLE IF NOT EXISTS celex_facts.fact2book_case_runs (
    id              uuid        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    celex_id        varchar     NOT NULL,
    case_id         varchar     NOT NULL,
    run_id          varchar     NOT NULL,
    in_book         boolean     NOT NULL,
    absence_reason  varchar     NOT NULL DEFAULT 'none',
    reference_type  varchar     NULL,
    corroborated    integer     NOT NULL DEFAULT 0,
    mismatch        integer     NOT NULL DEFAULT 0,
    skipped         integer     NOT NULL DEFAULT 0,
    already_checked integer     NOT NULL DEFAULT 0,
    started_at      timestamptz NOT NULL,
    finished_at     timestamptz NULL,
    CONSTRAINT uq_fact2book_case_runs UNIQUE (celex_id, case_id),
    CONSTRAINT ck_fact2book_case_runs_absence
        CHECK (absence_reason IN ('none', 'no_surface', 'no_anchor'))
);
CREATE INDEX IF NOT EXISTS ix_fact2book_case_runs_run
    ON celex_facts.fact2book_case_runs (run_id);
