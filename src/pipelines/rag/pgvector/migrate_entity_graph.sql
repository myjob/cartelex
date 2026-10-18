-- ---------------------------------------------------------------------------
-- Typed Entity Graph (Phase 5.1) — materialized from celex_facts.facts
--
-- Creates:
--   celex_facts.entities      — typed nodes (company | case | market | provision)
--   celex_facts.entity_edges  — typed, lineage-grounded relations between nodes
--
-- Every edge carries source_fact_id (FK -> facts) so celex_facts.fact_provenance()
-- still resolves the underlying chunk evidence for any graph-derived answer.
--
-- Safe to re-run: enums guarded with a NOT EXISTS check, all DDL uses IF NOT EXISTS.
-- ---------------------------------------------------------------------------

-- Enum: entity node kinds
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'celex_facts' AND t.typname = 'entity_kind_enum'
    ) THEN
        CREATE TYPE celex_facts.entity_kind_enum AS ENUM (
            'company',    -- a legal entity, LEI-resolved when possible
            'case',       -- one Commission decision, keyed by celex_id
            'market',     -- product / NACE / geographical market (deferred materialization)
            'provision'   -- legal provision, collapsed to its canonical concept
        );
    END IF;
END $$;

-- Enum: edge relation types.
-- 'parent_of' and 'active_in' are reserved but not materialized this phase
-- (no corporate-hierarchy source family; thin market/NACE coverage).
-- NOTE: the guard below means this block is a no-op on a database that already
-- has the type, so adding a label here does NOT reach an existing deployment.
-- 'dated_in' is delivered to those by migrate_entity_graph_dated_in_enum.sql.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'celex_facts' AND t.typname = 'edge_relation_enum'
    ) THEN
        CREATE TYPE celex_facts.edge_relation_enum AS ENUM (
            'participated_in',   -- company -> case
            'fined',             -- company -> case  (payload: amount)
            'leniency_applied',  -- company -> case
            'leniency_granted',  -- company -> case  (payload: reduction amount)
            'leniency_rejected', -- company -> case
            'parent_of',         -- company -> company (reserved, unused)
            'active_in',         -- case -> market     (reserved, unused)
            'violated',          -- case -> provision
            'dated_in'           -- company -> case  (payload: actors, date bounds)
        );
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- entities: typed nodes, deduplicated per (kind, natural_key)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS celex_facts.entities (
    entity_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            celex_facts.entity_kind_enum NOT NULL,

    -- human-readable label (original casing preserved)
    canonical_name  TEXT NOT NULL,

    -- dedup identity per kind:
    --   company  -> lei  OR  'name:' || normalized_name
    --   case     -> celex_id
    --   provision-> canonical_concept (collapses Art. 85/81/101 across treaty eras)
    natural_key     TEXT NOT NULL,

    -- denormalized descriptive natural keys (nullable per kind)
    lei_id          TEXT,   -- company:   corp_lei.lei when resolved
    nace_code       TEXT,   -- market:    reserved
    celex_id        TEXT,   -- case:      the decision id
    article_number  INT,    -- provision: source article when applicable

    -- optional extra payload (country, treaty, etc.) as JSON text
    attrs_json      TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_entities_kind_natural UNIQUE (kind, natural_key)
);

CREATE INDEX IF NOT EXISTS ix_entities_lei
    ON celex_facts.entities (lei_id)
    WHERE lei_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_entities_celex
    ON celex_facts.entities (celex_id)
    WHERE celex_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_entities_kind
    ON celex_facts.entities (kind);

-- ---------------------------------------------------------------------------
-- entity_edges: typed, lineage-grounded relations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS celex_facts.entity_edges (
    edge_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    src_entity_id   UUID NOT NULL
        REFERENCES celex_facts.entities (entity_id) ON DELETE CASCADE,
    dst_entity_id   UUID NOT NULL
        REFERENCES celex_facts.entities (entity_id) ON DELETE CASCADE,

    relation        celex_facts.edge_relation_enum NOT NULL,

    -- per-case scoping: enables cheap filtering and per-celex rebuild
    celex_id        TEXT NOT NULL,

    -- edge payload, e.g. {"amount": "ECU 5 000"}
    value_json      TEXT,

    -- lineage anchor: the fact that produced this edge. ON DELETE CASCADE keeps
    -- the graph self-cleaning and every edge provenance-resolvable.
    source_fact_id  UUID NOT NULL
        REFERENCES celex_facts.facts (id) ON DELETE CASCADE,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- amount is payload, not identity: re-materialization upserts value_json in place
    CONSTRAINT uq_entity_edges UNIQUE (src_entity_id, dst_entity_id, relation, source_fact_id)
);

CREATE INDEX IF NOT EXISTS ix_edges_src_rel
    ON celex_facts.entity_edges (src_entity_id, relation);

CREATE INDEX IF NOT EXISTS ix_edges_dst_rel
    ON celex_facts.entity_edges (dst_entity_id, relation);

CREATE INDEX IF NOT EXISTS ix_edges_celex
    ON celex_facts.entity_edges (celex_id);

CREATE INDEX IF NOT EXISTS ix_edges_source_fact
    ON celex_facts.entity_edges (source_fact_id);
