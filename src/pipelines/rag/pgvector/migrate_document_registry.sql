-- Migration: document registry for the HTTP document downloader
-- =========================================================================
-- Three-level registry written by Main_download_documents.py:
--   competition case -> decision -> candidate document (one per PDF URL),
-- plus an append-only audit ledger of every candidate attempt and a view of
-- the decisions that ended up with no usable document.
--
--   competition_case           one row per normalised case id (IV/223, AT.35388)
--   decision_registry          one row per decision (master JSON key)
--   competition_case_decision  many-to-many case <-> decision link
--   document_registry          one row per candidate document (PDF URL)
--   document_download_audit    one row per candidate per run (no FKs)
--   document_download_failures_vw
--
-- Identifier types: every key here is a natural key (case id, decision id,
-- document id = file stem), so text; the audit row id and run_id are
-- surrogates we own, so uuid. FK columns carry the referenced PK type.
--
-- decision_registry.decision_id is the project-wide celex_id: the master
-- JSON key, CELEX- prefix optional (CELEX-31964D0233, 32450_35_6, DEC-...),
-- the same value facts.celex_id, pg_embedding_qwen.celex_id and
-- celex_case_id.celex_id carry. Join on decision_id, not on celex_number.
--
-- decision_registry.celex_number holds the real CELEX number WITH the CELEX-
-- prefix, spelled exactly like the decision_id of a CELEX decision
-- (CELEX-31964D0233, CELEX-32002D0758_01; identifiers.celex_key). It is NULL
-- for DEC- keys and for EC-case master keys (35388_9_5, iv34801_en). The
-- legacy link tables are neither written nor altered here; they stay soft
-- joins on celex_id and case_id.
--
-- document_registry.is_source marks the one document per decision that the
-- processing pipeline (MinerU, translation, fact extraction) uses; at most
-- one per decision (uq_document_registry_one_source). is_selected keeps its
-- downloader meaning. facts / pg_embedding_qwen link to it through
-- document_id columns added by migrate_document_lineage.sql.
--
-- Comments live in schema_comments.sql, not here. Apply schema_comments.sql
-- only AFTER migrate_document_lineage.sql (it also comments the lineage
-- columns that file adds), and re-apply it after any re-run of either
-- migration: the DROP VIEW below discards the view's comments.
--
-- Valid on a database where an earlier version of this file was never
-- applied; an older applied version of the tables needs a manual ALTER
-- (celex_id -> celex_number, is_source).
--
-- Apply with: psql -v ON_ERROR_STOP=1 -f migrate_document_registry.sql
--
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS, DROP VIEW IF EXISTS
-- followed by CREATE OR REPLACE VIEW). Constraints are declared inline, so they exist exactly when their
-- table does.
-- =========================================================================

CREATE TABLE IF NOT EXISTS celex_facts.competition_case (
    case_id     text        PRIMARY KEY,
    title       text,
    meta        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS celex_facts.decision_registry (
    decision_id         text        PRIMARY KEY,
    celex_number        text,
    decision_date       date,
    title               text        NOT NULL DEFAULT '',
    parties             text        NOT NULL DEFAULT '',
    document_class      text        NOT NULL DEFAULT '',
    selection_status    text        NOT NULL,
    language_init       text        NOT NULL DEFAULT 'NA',
    language_translated text        NOT NULL DEFAULT 'NA',
    review_reasons      text[]      NOT NULL DEFAULT '{}'::text[],
    meta                jsonb       NOT NULL DEFAULT '{}'::jsonb,
    loader_file         text        NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    -- Pin the SelectionStatus value set in the DB, not only in Python.
    CONSTRAINT ck_decision_registry_selection_status
        CHECK (selection_status IN ('selected', 'undecided', 'no_pdf_source'))
);

CREATE INDEX IF NOT EXISTS ix_decision_registry_celex_number
    ON celex_facts.decision_registry (celex_number);

CREATE TABLE IF NOT EXISTS celex_facts.competition_case_decision (
    case_id     text        NOT NULL
        REFERENCES celex_facts.competition_case (case_id),
    decision_id text        NOT NULL
        REFERENCES celex_facts.decision_registry (decision_id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT competition_case_decision_pkey PRIMARY KEY (case_id, decision_id)
);

-- Reverse lookup (decision -> cases); the PK already serves case -> decisions.
CREATE INDEX IF NOT EXISTS ix_competition_case_decision_decision
    ON celex_facts.competition_case_decision (decision_id);

CREATE TABLE IF NOT EXISTS celex_facts.document_registry (
    document_id    text        PRIMARY KEY,
    decision_id    text        NOT NULL
        REFERENCES celex_facts.decision_registry (decision_id),
    source_url     text        NOT NULL,
    file_format    text        NOT NULL DEFAULT 'pdf',
    origin         text        NOT NULL,
    language       text,
    is_selected    boolean     NOT NULL,
    is_source      boolean     NOT NULL DEFAULT false,
    local_file_pdf text,
    sha256         char(64),
    size_bytes     bigint,
    content_type   text,
    downloaded_at  timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_document_registry_source_url UNIQUE (source_url),
    CONSTRAINT ck_document_registry_sha256
        CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS ix_document_registry_decision
    ON celex_facts.document_registry (decision_id);

-- At most one processing source document per decision.
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_registry_one_source
    ON celex_facts.document_registry (decision_id)
    WHERE is_source;

-- Append-only. Intentionally no foreign keys: a failed row may reference a
-- decision or document that was never registered, and a row-level mapper
-- failure has neither (decision_id and document_id NULL).
CREATE TABLE IF NOT EXISTS celex_facts.document_download_audit (
    id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         uuid        NOT NULL,
    decision_id    text,
    document_id    text,
    source_url     text,
    outcome        text        NOT NULL,
    failure_reason text,
    http_status    integer,
    content_type   text,
    size_bytes     bigint,
    sha256         char(64),
    duration_ms    integer,
    loader_file    text        NOT NULL,
    loader_row     integer,
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- Pin the Outcome value set in the DB, not only in Python.
    CONSTRAINT ck_document_download_audit_outcome
        CHECK (outcome IN ('downloaded', 'registered_existing', 'skipped_exists',
                           'not_selected', 'no_pdf_source', 'failed'))
);

CREATE INDEX IF NOT EXISTS ix_document_download_audit_run
    ON celex_facts.document_download_audit (run_id);
CREATE INDEX IF NOT EXISTS ix_document_download_audit_decision
    ON celex_facts.document_download_audit (decision_id);
CREATE INDEX IF NOT EXISTS ix_document_download_audit_outcome
    ON celex_facts.document_download_audit (outcome);

-- =========================================================================
-- Failures view: what is still missing after all runs so far.
--
-- 1. latest   -- the newest audit row per candidate. A candidate is keyed by
--                (decision, document): decision_id, or for a row-level mapper
--                failure the loader position (loader_file, loader_row); and
--                document_id, else source_url, else '' (decision-level rows
--                such as no_pdf_source).
-- 2. usable   -- decisions with at least one candidate whose latest outcome
--                is downloaded, registered_existing or skipped_exists.
-- 3. result   -- every latest failed row, plus the latest no_pdf_source row
--                of decisions that are not usable. A failed candidate stays
--                listed even when a sibling document of its decision was
--                downloaded. not_selected rows are deliberate skips and never
--                listed. A row-level failure (decision_id NULL) drops out only
--                once a LATER RUN mapped the same loader position to a
--                decision; rows of the same run never suppress it.
--
-- A failure from an earlier run therefore disappears as soon as a later run
-- downloads or registers the same candidate. Conversely, because only the
-- LATEST outcome per candidate counts, a later failed --force re-download
-- keeps the candidate listed even though document_registry.local_file_pdf
-- still points at a file. celex_number and title come from decision_registry
-- and are NULL for decisions that were never registered.
-- Ordering is left to the reader (DownloadAuditRepository.failures).
-- =========================================================================
-- Dropped first so a re-run can change the view's columns (CREATE OR
-- REPLACE cannot rename one). No object depends on this view.
DROP VIEW IF EXISTS celex_facts.document_download_failures_vw;

CREATE OR REPLACE VIEW celex_facts.document_download_failures_vw AS
WITH keyed AS (
    SELECT a.*,
           COALESCE(
               a.decision_id,
               'row:' || a.loader_file || ':' || COALESCE(a.loader_row::text, '')
           ) AS decision_key,
           COALESCE(a.document_id, a.source_url, '') AS candidate_key
    FROM celex_facts.document_download_audit AS a
),
latest AS (
    SELECT DISTINCT ON (decision_key, candidate_key) *
    FROM keyed
    ORDER BY decision_key, candidate_key, created_at DESC, id DESC
),
usable AS (
    SELECT DISTINCT decision_key
    FROM latest
    WHERE outcome IN ('downloaded', 'registered_existing', 'skipped_exists')
)
SELECT l.run_id,
       l.decision_id,
       d.celex_number,
       d.title,
       l.document_id,
       l.source_url,
       l.outcome,
       l.failure_reason,
       l.http_status,
       l.loader_file,
       l.loader_row,
       l.created_at
FROM latest AS l
LEFT JOIN celex_facts.decision_registry AS d
       ON d.decision_id = l.decision_id
WHERE (
      l.outcome = 'failed'
      OR (
          l.outcome = 'no_pdf_source'
          AND NOT EXISTS (
              SELECT 1 FROM usable AS u WHERE u.decision_key = l.decision_key
          )
      )
  )
  AND NOT (
      l.decision_id IS NULL
      AND EXISTS (
          SELECT 1
          FROM celex_facts.document_download_audit AS later
          WHERE later.decision_id IS NOT NULL
            AND later.run_id <> l.run_id
            AND later.loader_file = l.loader_file
            AND later.loader_row = l.loader_row
            AND later.created_at > l.created_at
      )
  );
