-- Migration: human_index_recon table
-- =========================================================================
-- Reconciliation of the human case index (the hand-maintained workbook
-- settings.HUMAN_INDEX_XLSX_PATH) against the extracted facts.
--
-- One row per (decision, target), where a target is one column family of the
-- workbook: formal decision, nature of conduct, responsible commissioner,
-- firms. Each row carries the human cell as written, the extracted value,
-- both normalised forms and the outcome of the comparison, so nobody has to
-- open the workbook again to see what the index says about a decision.
--
-- Written by src/batch/Main_recon_human_index.py, which upserts on the
-- primary key. `verdict` / `verdict_note` are the only human-written columns:
-- a re-run keeps them while both compared values are unchanged and clears
-- them otherwise, because a verdict on a value that has since changed says
-- nothing about the new value.
--
-- Idempotent and additive: safe to re-run, touches no other relation.
-- Design doc: docs/human_index2facts_recon.md
-- =========================================================================

CREATE TABLE IF NOT EXISTS celex_facts.human_index_recon (
    celex_id        text        NOT NULL,
    target          varchar     NOT NULL,

    -- Where the two values come from.
    sheet_column    varchar     NOT NULL,
    fact_names      text        NOT NULL,

    human_raw       text,
    human_norm      text,
    extracted_raw   text,
    extracted_norm  text,

    status          varchar     NOT NULL,
    detail          jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Decision context, repeated on each of a decision's rows so the table
    -- can be read and sorted on its own.
    title           text,
    decision_year   integer,
    content_tokens  integer,
    size_band       varchar,

    source_file     text        NOT NULL,
    source_row      integer     NOT NULL,

    verdict         varchar,
    verdict_note    text,

    reconciled_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT pk_human_index_recon PRIMARY KEY (celex_id, target),
    CONSTRAINT ck_human_index_recon_target CHECK (
        target IN ('formal_decision', 'nature_of_conduct',
                   'responsible_commissioner', 'firms')
    ),
    CONSTRAINT ck_human_index_recon_status CHECK (
        status IN ('agree', 'conflict', 'extraction_superset', 'human_blank',
                   'extraction_blank', 'not_comparable')
    ),
    CONSTRAINT ck_human_index_recon_size_band CHECK (size_band IN ('S', 'M', 'L')),
    CONSTRAINT ck_human_index_recon_verdict CHECK (
        verdict IN ('human_wrong', 'extraction_wrong', 'both_wrong', 'both_acceptable')
    )
);

-- "All conflicts for one target" is the query the review work runs.
CREATE INDEX IF NOT EXISTS ix_human_index_recon_target_status
    ON celex_facts.human_index_recon (target, status);
