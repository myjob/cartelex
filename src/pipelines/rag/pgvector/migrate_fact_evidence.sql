-- Migration: evidence-verification ledger
-- =========================================================================
-- Adds the verification-status machinery consumed by
-- ``src/pipelines/agent_dspy/evidence_verifier_module.py`` and
-- ``src/batch/Main_verify_facts_backfill.py``.
--
-- 1. ``verification_status_enum``    — the per-fact verification verdict.
-- 2. ``celex_facts.facts`` columns   — denormalized per-fact verdict + score.
-- 3. ``celex_facts.fact_evidence``   — the verification ledger: exactly one
--                                      row per non-excluded fact, never sparse.
--
-- Idempotent: safe to re-run (guards on IF NOT EXISTS / pg_type lookup).
-- =========================================================================

-- 1. Verification status enum -------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typname = 'verification_status_enum'
          AND n.nspname = 'celex_facts'
    ) THEN
        CREATE TYPE celex_facts.verification_status_enum AS ENUM (
            'verified_supported',
            'weak_support',
            'insufficient_evidence',
            'contradicted',
            'missing_required_entity',
            'missing_required_time',
            'manual_approved',
            'unverified',
            'manual_rejected'
        );
    END IF;
END$$;

-- 2. facts: denormalized verdict ---------------------------------------------
ALTER TABLE celex_facts.facts
    ADD COLUMN IF NOT EXISTS verification_status
        celex_facts.verification_status_enum NOT NULL DEFAULT 'unverified',
    ADD COLUMN IF NOT EXISTS verification_score numeric,
    ADD COLUMN IF NOT EXISTS verification_updated_at timestamptz;

-- 3. fact_evidence ledger -----------------------------------------------------
CREATE TABLE IF NOT EXISTS celex_facts.fact_evidence (
    id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    fact_id            uuid        NOT NULL
        REFERENCES celex_facts.facts (id) ON DELETE CASCADE,

    -- Stable id inside pg_embedding_qwen (LangChain-owned varchar holding a
    -- UUID-format string). NULL for human / derived / no-provenance summary
    -- rows where the verdict is not pinned to a single chunk. Type matches the
    -- referenced PK (varchar), mirroring source_registry.chunk_id.
    chunk_id           varchar,

    support_label      text,
    final_score        numeric,

    -- per-signal sub-scores (0..1)
    entity_score       numeric,
    keyword_score      numeric,
    semantic_score     numeric,
    temporal_score     numeric,
    provenance_score   numeric,

    evidence_quote     text,
    missing_elements   jsonb,

    verified_at        timestamptz NOT NULL DEFAULT now()
);

-- Natural key of a chunk-pinned row: at most one row per (fact, chunk).
-- ``FactEvidenceRepository.upsert_evidence`` writes exactly one row per fact
-- (delete-then-insert in one transaction), so this index is NOT what makes a
-- re-run idempotent and is currently never hit. It is retained as the natural
-- key guarding the multi-row shape the ledger permits — persisting more than
-- the single best candidate per fact needs no schema change, only a writer.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_evidence_chunk
    ON celex_facts.fact_evidence (fact_id, chunk_id)
    WHERE chunk_id IS NOT NULL;

-- At most one chunk-less summary row per fact — the verdict shape used for
-- human / derived / no-provenance facts, which pin to no chunk.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_evidence_summary
    ON celex_facts.fact_evidence (fact_id)
    WHERE chunk_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_fact_evidence_fact
    ON celex_facts.fact_evidence (fact_id);
