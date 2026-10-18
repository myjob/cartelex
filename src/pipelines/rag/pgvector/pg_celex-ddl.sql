CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- celex_facts.celex_md_chunks_embeddings_large definition

-- Drop table

-- DROP TABLE celex_facts.celex_md_chunks_embeddings_large;
-- Ensure prerequisites exist
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS celex_facts;


-- celex_facts.facts definition


-- DROP FUNCTION celex_facts.smart_to_date();

-- DROP FUNCTION "celex_facts-crap".smart_to_date(text);

CREATE OR REPLACE FUNCTION "celex_facts".smart_to_date(input_text text)
 RETURNS date
 LANGUAGE plpgsql
 IMMUTABLE
AS $function$
DECLARE
    clean_str TEXT;
    result_date DATE;
BEGIN
    -- 1. Pre-cleaning: Lowercase, remove $, ^, and extra whitespace
    clean_str := LOWER(TRIM(input_text));
    clean_str := REGEXP_REPLACE(clean_str, '[\$\^]', '', 'g'); -- Remove symbols
    clean_str := REGEXP_REPLACE(clean_str, '\s+', ' ', 'g');   -- Normalize spaces
    
    -- Fix specific typos or language issues found in your list
    clean_str := REPLACE(clean_str, 'janaury', 'january');
    clean_str := REPLACE(clean_str, 'märz', 'march');
    
    -- 2. Try Standard Formats
    BEGIN
        -- Try DD Month YYYY (e.g., 20 February 2003)
        RETURN TO_DATE(clean_str, 'DD Month YYYY');
    EXCEPTION WHEN OTHERS THEN 
        -- Fall through to next attempt
    END;

    BEGIN
        -- Try Month DD, YYYY (e.g., January 1, 1991)
        RETURN TO_DATE(REGEXP_REPLACE(clean_str, ',', '', 'g'), 'Month DD YYYY');
    EXCEPTION WHEN OTHERS THEN END;

    BEGIN
        -- Try DD.MM.YYYY or DD/MM/YYYY
        IF clean_str ~ '^\d{1,2}[\./]\d{1,2}[\./]\d{2,4}$' THEN
            RETURN TO_DATE(clean_str, 'DD MM YYYY');
        END IF;
    EXCEPTION WHEN OTHERS THEN END;

    -- 3. Handle "Jammed" Text (e.g., 14May1971 or 9June1998)
    BEGIN
        IF clean_str ~ '^\d{1,2}[a-z]+\d{4}$' THEN
            RETURN TO_DATE(clean_str, 'DDMonthYYYY');
        END IF;
    EXCEPTION WHEN OTHERS THEN END;

    -- 4. Handle Year Only or Approximations (e.g., At the beginning of 1970)
    -- This extracts the first 4-digit number found
    BEGIN
        IF clean_str ~ '\d{4}' THEN
            RETURN TO_DATE(SUBSTRING(clean_str FROM '\d{4}'), 'YYYY');
        END IF;
    EXCEPTION WHEN OTHERS THEN END;

    -- Return NULL if all parsing attempts fail
    RETURN NULL;
END;
$function$
;
;



-- Drop table

-- DROP TABLE celex_facts.facts;

CREATE TABLE celex_facts.facts (
	id uuid NOT NULL DEFAULT gen_random_uuid(),
	celex_id varchar NOT NULL,
	"name" varchar NULL,
	value_md varchar NULL,
	value_json varchar NULL,
	created_at timestamp DEFAULT now() NULL,
	references_doc_json jsonb NULL,
	references_fact_json jsonb NULL,
	CONSTRAINT facts_pk PRIMARY KEY (id)
);

CREATE TABLE celex_facts.celex_md_chunks_embeddings_large (
	id varchar NOT NULL,
	celex_id varchar NULL,
	path varchar NULL,
	section varchar NULL,
	meta_data jsonb DEFAULT '{}'::jsonb NULL,
	filters jsonb DEFAULT '{}'::jsonb NULL,
	start_char bigint,
	end_char bigint,
	tokens_numof bigint,
	"content" text NULL,
    -- Added dimension (assuming 1536 for OpenAI small/large/ada, 3072 for large-3)
	embedding public.vector(3072) NULL, 
	"usage" jsonb NULL,
	created_at timestamptz DEFAULT now() NULL,
	updated_at timestamptz NULL,
	content_hash varchar NULL,
	content_id varchar NULL,
	CONSTRAINT company_knowledge_large_pkey PRIMARY KEY (id)
);
CREATE INDEX idx_company_knowledge_large_hash ON celex_facts.celex_md_chunks_embeddings_large USING btree (content_hash);
CREATE INDEX idx_company_knowledge_large_content_id ON celex_facts.celex_md_chunks_embeddings_large USING btree (content_id);
CREATE INDEX idx_company_knowledge_large_id ON celex_facts.celex_md_chunks_embeddings_large USING btree (id);
CREATE INDEX idx_company_knowledge_large_celex ON celex_facts.celex_md_chunks_embeddings_large USING btree (celex_id);
CREATE INDEX idx_company_knowledge_large_content ON celex_facts.celex_md_chunks_embeddings_large USING btree (content);

-- celex_facts.celex_case_id definition

-- Drop table

-- DROP TABLE celex_facts.celex_case_id;
CREATE TABLE celex_facts.celex_case_id (
	celex_id text NOT NULL,
	case_x text NULL,
	case_id text NULL,
	CONSTRAINT celex_case_id_pkey PRIMARY KEY (celex_id)
);



-- Loaded by src/pipelines/legacy_rule_engine/rule_fact_loader.py.
-- NOTE (2026-08-08): the live table is named "facts", not "fact" -- this
-- statement never created it, and the loader addressed the singular name until
-- it was corrected. The live column types also drift from what is declared
-- here: guid and celex_id are character varying, and celex_id is NOT NULL.
CREATE TABLE rule_facts.facts (
    guid UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    celex_id TEXT,
    fact TEXT,
    question TEXT,
    value TEXT
);

-- Forward-fix for the live database, which was created without the default.
-- rule_fact_loader inserts only (celex_id, fact, question, value) and relies on
-- the server to mint the key -- as its docstring always claimed -- so without
-- this every INSERT fails with NotNullViolation on guid. That failure is
-- swallowed by the loader's try/except and logged as "Error loading data to
-- Database", so it presents as a silent no-op rather than a crash.
-- Idempotent; gen_random_uuid() is built into PostgreSQL 13+ (no pgcrypto).
ALTER TABLE rule_facts.facts ALTER COLUMN guid SET DEFAULT gen_random_uuid();






---
-- DROP view celex_facts.docs
create view celex_facts.docs
AS
SELECT id, "document", cmetadata, celex_id, doc_type, token_count, "path", chunk_hash
FROM celex_facts.langchain_pg_embedding
;




-- celex_facts.langchain_pg_embedding definition

-- Drop table

-- DROP TABLE celex_facts.langchain_pg_embedding;

CREATE TABLE celex_facts.langchain_pg_embedding (
	id varchar NOT NULL,
	collection_id uuid NULL,
	embedding public.vector NULL,
	"document" varchar NULL,
	cmetadata jsonb NULL,
	celex_id varchar NULL,
	doc_type varchar NULL,
	token_count int4 NULL,
	"path" varchar NULL,
	chunk_hash varchar NULL,
	ingestion_timestamp timestamptz DEFAULT now() NULL,
	doc_pos int4 NULL,
	CONSTRAINT langchain_pg_embedding_pkey PRIMARY KEY (id)
);
CREATE INDEX ix_langchain_pg_embedding_celex_id ON celex_facts.langchain_pg_embedding USING btree (celex_id);
CREATE INDEX ix_langchain_pg_embedding_chunk_hash ON celex_facts.langchain_pg_embedding USING btree (chunk_hash);

DROP INDEX IF EXISTS ix_langchain_pg_embedding_embedding;
CREATE INDEX ix_langchain_pg_embedding_embedding
ON celex_facts.langchain_pg_embedding
USING hnsw (embedding vector_cosine_ops);
-- celex_facts.langchain_pg_embedding foreign keys

ALTER TABLE celex_facts.langchain_pg_embedding ADD CONSTRAINT langchain_pg_embedding_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES celex_facts.langchain_pg_collection("uuid") ON DELETE CASCADE;


---
create table IF NOT EXISTS  celex_facts.fact_ingestion_audit_trail (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    celex_id text,
    fact_id	 uuid,			-- FK to the facts table id (uuid, matches facts.id)
    --- FactExtractorParamVO
    target_fact_name  text, 	-- description="The name of the fact being targeted"
    question text, 				-- description="The question to guide the extraction")
    output_schema text, 		-- description="The schema mapping for the output")
    verify_fact_name text, 		-- description="Optional fact name to verify against golden list")
    force_override text, 		-- description="Whether to bypass the existing presence check")
	---
    success bool,
	errors	text,
	duration int,				-- end to end duration in seconds of the pipeline execution	
	created_at timestamp,
	FOREIGN KEY (fact_id) REFERENCES celex_facts.facts(id)
)
;

-- drop view IF EXISTS celex_facts.fact_ingestion_audit_trail_vw ;
create view celex_facts.fact_ingestion_audit_trail_vw 
AS
SELECT a.celex_id,  a.target_fact_name, a.question,f.value_md, f.value_json, a.force_override, a.success, a.errors, a.duration, a.created_at inserted_update_at
from celex_facts.fact_ingestion_audit_trail a
inner join (select id, value_md , value_json from celex_facts.facts) as f on f.id = a.fact_id
order by created_at desc



----

CREATE TABLE IF NOT EXISTS celex_facts.company_standardized (
    id UUID DEFAULT gen_random_uuid(),
    name text,
    CONSTRAINT company_standardized_pkey PRIMARY KEY (id),
    CONSTRAINT company_standardized_name_unique UNIQUE (name)
);
CREATE INDEX ix_celex_facts_company_standardized ON celex_facts.company_standardized USING btree (name);



-----
-- DROP TABLE IF EXISTS celex_facts.company_standard2celex_id;

CREATE TABLE IF NOT EXISTS celex_facts.company_standard2celex_id (
    id UUID DEFAULT gen_random_uuid(),
    company_name text,
    celex_id text,
    CONSTRAINT company_standard2celex_id_pkey PRIMARY KEY (id),
    CONSTRAINT company_standard2celex_id_name2celexid_uniqu UNIQUE (company_name, celex_id)
);

-- drop TABLE IF  EXISTS celex_facts.company_standard2alias;
-- drop TABLE IF  EXISTS celex_facts.company_standard2celex_id2alias;
CREATE TABLE IF NOT EXISTS celex_facts.company_standard2celex_id2alias (
    id UUID DEFAULT gen_random_uuid(),
    celex_id text,
    company_name text,
    alias text,
    CONSTRAINT company_standard2alias_celex_pkey PRIMARY KEY (id),
    CONSTRAINT company_standard2alias_celex_alias_uniqu UNIQUE (celex_id,company_name, alias)
);



-----
-- DROP TABLE IF EXISTS celex_facts.company_canonical2celex_id cascade;

CREATE TABLE IF NOT EXISTS celex_facts.company_canonical2celex_id (
    id UUID DEFAULT gen_random_uuid(),
    company_name text,
    celex_id text,
    CONSTRAINT company_canonical2celex_id_pkey PRIMARY KEY (id),
    CONSTRAINT company_canonical2celex_id_name2celexid_uniqu UNIQUE (company_name, celex_id)
);

-----
-- DROP TABLE IF EXISTS celex_facts.company_canonical2alias;

CREATE TABLE IF NOT EXISTS celex_facts.company_canonical2alias (
    id UUID DEFAULT gen_random_uuid(),
    company_canonical text,
    company_alias text,
    fk_company_canonical UUID,
    CONSTRAINT company_canonical2alias_pkey PRIMARY KEY (id),
    CONSTRAINT company_canonical2alias_uniqu UNIQUE (company_canonical, company_alias),
    FOREIGN KEY (fk_company_canonical) REFERENCES celex_facts.company_canonical2celex_id(id) 
);

----------
-- drop view celex_facts.question2fact
CREATE OR REPLACE VIEW celex_facts.question2fact
AS
SELECT id, f.celex_id, fn.question, fn.fact_name, f.value_md
FROM celex_facts.facts f
inner Join(
	select   question, fact_name from celex_facts.question2fact_name
	)fn on fn.fact_name = f.name
order by celex_id,fact_name

----------
-- drop table if exists celex_facts.question2fact2embeddings
CREATE TABLE IF NOT EXISTS celex_facts.question2fact2embeddings (
    fk_fact_id                  uuid,
    celex_id            text,
    question            text,
    fact_name           text,
    value_txt           text,
    embeddings          public.vector NULL,
    hash_q_a            text,
    ingestion_timestamp timestamptz DEFAULT now(),
    CONSTRAINT idx_pk_question2fact2embeddings PRIMARY KEY (fk_fact_id)
);

-- celex_facts.query_embeddings definition

-- Drop table

-- DROP TABLE celex_facts.query_embeddings_qwen;

CREATE TABLE celex_facts.query_embeddings_qwen (
	query text NOT NULL,
	embedding public.vector NULL,
	created_at timestamptz DEFAULT CURRENT_TIMESTAMP NULL,
	query_hash text GENERATED ALWAYS AS (md5(query)) STORED,
	CONSTRAINT query_embeddings__qwen_pkey PRIMARY KEY (query_hash)
);

-----------

create view celex_facts.question2fact2embeddings_vw 
AS
select f.id,f.celex_id,e.fact_name , e.question, e.value_txt,e.hash_q_a , e.embeddings  from celex_facts.question2fact2embeddings e
inner join (SELECT id, celex_id, "name", value_md, value_json, created_at, references_doc_json, references_fact_json
FROM celex_facts.facts) f on f.id = e.fk_fact_id

---
select * from question2fact2embeddings_vw


SELECT query, embedding, created_at
FROM celex_facts.query_embeddings_qwen;

select * from facts where name = 'decision_remedies2' 
-- delete from facts where name = 'decision_remedies2'



------------------------------------------------------------------------
-- DROP TABLE celex_facts.pg_embedding_qwen;
CREATE TABLE celex_facts.pg_embedding_qwen (
	id varchar NOT NULL,
	collection_id uuid NULL,
	embedding public.vector NULL,
	"document" varchar NULL,
	cmetadata jsonb NULL,
	celex_id varchar NULL,
	doc_type varchar NULL,
	token_count int4 NULL,
	"path" varchar NULL,
	chunk_hash varchar NULL,
	ingestion_timestamp timestamptz DEFAULT now() NULL,
	doc_pos int4 NULL,
	-- Contextual-retrieval line (case name, section, chunk topic) written by
	-- Main_enrich_chunk_context.py; embedded together with "document".
	context_prefix text NULL,
	-- Decision section (parties | recitals_facts | legal_assessment | fines |
	-- operative | annex | other) written by Main_classify_sections.py.
	section_type varchar NULL,
	document_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(context_prefix, '') || ' ' || "document")) STORED,
	CONSTRAINT pg_embedding_qwen_pkey PRIMARY KEY (id)
);

-- CREATE INDEX idx_celex_hnsw_halfvec_qwen ON celex_facts.pg_embedding_qwen USING hnsw (((embedding)::halfvec(3072)) halfvec_cosine_ops) WITH (m='16', ef_construction='64');
CREATE INDEX ix_pg_embedding_qwen_celex_id ON celex_facts.pg_embedding_qwen USING btree (celex_id);
CREATE INDEX ix_pg_embedding_qwen_chunk_hash ON celex_facts.pg_embedding_qwen USING btree (chunk_hash);
CREATE INDEX ix_pg_embedding_qwen_section_type ON celex_facts.pg_embedding_qwen USING btree (section_type);


-- celex_facts.langchain_pg_embedding foreign keys

ALTER TABLE celex_facts.langchain_pg_embedding 
ADD CONSTRAINT langchain_pg_embedding_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES celex_facts.langchain_pg_collection("uuid") ON DELETE CASCADE;



ALTER TABLE celex_facts.langchain_pg_embedding RENAME TO celex_facts.pg_embedding_openai;


------------------------------------------------------------------------
-- Book-section embedding tables (one per embedding provider).
-- Populated by BookMarkdownIngestor via book_chunks2pgvector.py.
-- Promoted columns mirror BookNode fields written into Document.metadata.
------------------------------------------------------------------------



-- DROP TABLE IF EXISTS celex_facts.pg_embedding_book_qwen;

CREATE TABLE IF NOT EXISTS celex_facts.pg_embedding_book_qwen (
    id              varchar         NOT NULL,
    collection_id   uuid            NULL,
    "document"      text            NULL,
    cmetadata       jsonb           NULL,
    book_name       varchar         NULL,
    doc_type        varchar         NULL,
    token_count     int4            NULL,
    path            varchar         NULL,
    level           int2            NULL,
    title           varchar         NULL,
    text_line       int4            NULL,
    source          varchar         NULL,
    chunk_hash      varchar         NULL,
    ingestion_timestamp timestamptz DEFAULT now() NULL,
    doc_pos         int4            NULL,
    embedding       public.vector   NULL,
    CONSTRAINT pg_embedding_book_qwen_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_pg_embedding_book_qwen_book_name
    ON celex_facts.pg_embedding_book_qwen USING btree (book_name);
CREATE INDEX IF NOT EXISTS ix_pg_embedding_book_qwen_chunk_hash
    ON celex_facts.pg_embedding_book_qwen USING btree (chunk_hash);
CREATE INDEX IF NOT EXISTS ix_pg_embedding_book_qwen_level
    ON celex_facts.pg_embedding_book_qwen USING btree (level);

ALTER TABLE celex_facts.pg_embedding_book_qwen
    ADD CONSTRAINT pg_embedding_book_qwen_collection_fkey
    FOREIGN KEY (collection_id)
    REFERENCES celex_facts.langchain_pg_collection("uuid")
    ON DELETE CASCADE;

CREATE INDEX idx_book_path_prefix 
ON celex_facts.pg_embedding_book_qwen (path text_pattern_ops);

---------------


-- N:M
CREATE TABLE celex_facts.case_celex_id (
	
	case_id text not NULL,
	celex_id text NOT NULL
);


----------------------------------------------------------------------------------------------------
----------------------------------------------------------------------------------------------------

CREATE TABLE celex_facts.knowledge_items (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_text text NOT NULL,
    knowledge_type text NOT NULL,
    scope         text NOT NULL DEFAULT 'global',   -- global | jurisdiction | topic
    jurisdiction  text,                              -- e.g. 'EU', nullable
    status        text NOT NULL DEFAULT 'draft',     -- draft | validated | deprecated
    confidence    real,
    created_by    text NOT NULL,                     -- agent_run_id or 'human'
    agent_run_id  uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    knowledge_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', knowledge_text)) STORED
);

--  DROP TABLE if exists celex_facts.knowledge_question_embeddings 
CREATE TABLE celex_facts.knowledge_question_embeddings (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_id  uuid NOT NULL REFERENCES knowledge_items(id) ON DELETE CASCADE,
    question_variant text NOT NULL,
    embedding     public.vector NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);
--CREATE INDEX ON knowledge_question_embeddings USING hnsw (embedding vector_cosine_ops);

--  DROP TABLE celex_facts.knowledge_edges;

CREATE TABLE celex_facts.knowledge_edges (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    src_knowledge_id uuid NOT NULL REFERENCES knowledge_items(id),

    -- destination: exactly one of {raw evidence, fact, knowledge_item}.
    -- Raw evidence (chunk/csv/book/lei/heideltime) is reached through the
    -- canonical fact_source_registry, so facts and knowledge items ground against
    -- one evidence catalog. dst_fact_id / dst_knowledge_id are derived nodes.
    -- The FK to fact_source_registry is added in the dated migration block below
    -- (fact_source_registry is defined later in this file).
    dst_source_id    uuid,
    dst_fact_id      uuid REFERENCES  celex_facts.facts(id),
    dst_knowledge_id uuid REFERENCES  celex_facts.knowledge_items(id),

    relation_type    text NOT NULL,      -- supports | derived_from | contradicts
    confidence       real,
    created_by       text,
    agent_run_id     uuid,
    created_at       timestamptz NOT NULL DEFAULT now(),

    -- exactly one destination type
    CONSTRAINT ck_knowledge_edges_one_destination
        CHECK (num_nonnulls(dst_source_id, dst_fact_id, dst_knowledge_id) = 1)
);

CREATE INDEX ix_knowledge_edges_dst_source
    ON celex_facts.knowledge_edges (dst_source_id)
    WHERE dst_source_id IS NOT NULL;

CREATE INDEX ON knowledge_items USING gin (knowledge_tsv);


----------------------------------------------------------------------------------------------------
-- Knowledge extraction queue — agent writes, batch reads
----------------------------------------------------------------------------------------------------

CREATE TABLE celex_facts.knowledge_extraction_queue (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_prompt     text NOT NULL,
    agent_answer    text NOT NULL,
    supporting_facts text,                            -- serialised evidence string
    fact_ids        jsonb,                             -- e.g. ["uuid1","uuid2"]
    chunk_ids       jsonb,                             -- e.g. ["chunk-id-1","chunk-id-2"]
    status          text NOT NULL DEFAULT 'pending',   -- pending | processing | done | failed
    error_message   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz
);

CREATE INDEX ix_knowledge_extraction_queue_status
    ON celex_facts.knowledge_extraction_queue (status)
    WHERE status = 'pending';


----------------------------------------------------------------------------------------------------
-- Knowledge ingestion audit trail
----------------------------------------------------------------------------------------------------

CREATE TABLE celex_facts.knowledge_ingest_audit_trail (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id   uuid REFERENCES celex_facts.knowledge_extraction_queue(id),
    knowledge_id    uuid REFERENCES celex_facts.knowledge_items(id),
    user_prompt     text,
    agent_answer    text,
    pipeline        text DEFAULT 'knowledge_ingest',
    dspy_module     text,
    predecessors    jsonb,
    success         bool,
    errors          text,
    duration        int,           -- seconds
    created_at      timestamptz DEFAULT now()
);


----------------------------------------------------------------------------------------------------
-- Deferred grounding trigger — every knowledge_item must have ≥1 edge at commit
----------------------------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION celex_facts.trg_knowledge_must_have_edge()
RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM celex_facts.knowledge_edges
        WHERE src_knowledge_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'knowledge_item % has no outgoing edges (ungrounded)', NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_knowledge_grounding
    AFTER INSERT ON celex_facts.knowledge_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION celex_facts.trg_knowledge_must_have_edge();

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

-- NOTE (Phase 3): the expression below is the ORIGINAL Phase 1 definition.
-- migrate_section_type_context_prefix.sql redefines document_tsv to also
-- cover context_prefix; the CREATE TABLE block above carries the current
-- expression. Kept here as history; IF NOT EXISTS makes it a no-op on a
-- table built from the CREATE TABLE above.
ALTER TABLE celex_facts.pg_embedding_qwen
    ADD COLUMN IF NOT EXISTS document_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', document)) STORED;

CREATE INDEX IF NOT EXISTS pg_embedding_qwen_document_tsv_gin
    ON celex_facts.pg_embedding_qwen
    USING gin (document_tsv);

-- Accent-insensitive + trigram fuzzy matching (migrate_fts_extensions.sql).
-- f_unaccent pins the dictionary so unaccent becomes IMMUTABLE and indexable;
-- every query and the index below must use f_unaccent, not bare unaccent.
-- Extensions live in celex_facts, so references are schema-qualified.
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA celex_facts;

CREATE OR REPLACE FUNCTION celex_facts.f_unaccent(text)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    STRICT
AS $$
    SELECT celex_facts.unaccent('celex_facts.unaccent', $1)
$$;

CREATE INDEX IF NOT EXISTS pg_embedding_qwen_document_trgm_gin
    ON celex_facts.pg_embedding_qwen
    USING gin (celex_facts.f_unaccent(document) celex_facts.gin_trgm_ops);




-- ===========================================================================
-- Data Lineage Registry — Option 2 + selective Option 3 edge semantics
-- Added: 2026-05-30
-- See migrate_lineage_tables.sql for the idempotent migration script.
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'celex_facts' AND t.typname = 'source_type_enum'
    ) THEN
        CREATE TYPE celex_facts.source_type_enum AS ENUM (
            'chunk', 'csv_human', 'book_case', 'lei_corporate', 'heideltime', 'fact'
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'celex_facts' AND t.typname = 'relation_type_enum'
    ) THEN
        CREATE TYPE celex_facts.relation_type_enum AS ENUM (
            'extracted_from', 'corroborated_by', 'entity_resolved_via', 'derived_from'
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS celex_facts.fact_source_registry (
    source_id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type            celex_facts.source_type_enum NOT NULL,
    celex_id               TEXT,
    -- varchar to match pg_embedding_qwen.id / pg_embedding_book_qwen.id, which
    -- are LangChain-owned varchar PKs holding UUID-format strings.
    chunk_id               VARCHAR,
    chunk_seq              INT[],
    heideltime_event_id    UUID,
    lei_id                 TEXT,
    csv_row                INT,
    -- fact: predecessor fact a derived fact was computed from
    source_fact_id         UUID REFERENCES celex_facts.facts (id) ON DELETE CASCADE,
    description            TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_source_registry_celex    ON celex_facts.fact_source_registry (celex_id);
CREATE INDEX IF NOT EXISTS ix_source_registry_type     ON celex_facts.fact_source_registry (source_type);
CREATE INDEX IF NOT EXISTS ix_source_registry_chunk_id ON celex_facts.fact_source_registry (chunk_id) WHERE chunk_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_source_registry_heideltime ON celex_facts.fact_source_registry (heideltime_event_id) WHERE heideltime_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS celex_facts.fact_source_link (
    fact_id            UUID     NOT NULL REFERENCES celex_facts.facts (id) ON DELETE CASCADE,
    source_id          UUID     NOT NULL REFERENCES celex_facts.fact_source_registry (source_id),
    extraction_method  TEXT     NOT NULL,
    relation_type      celex_facts.relation_type_enum NOT NULL DEFAULT 'extracted_from',
    trust_rank         SMALLINT NOT NULL DEFAULT 5
        CONSTRAINT ck_fact_source_link_trust_rank CHECK (trust_rank BETWEEN 1 AND 10),
    conflict_flag      BOOLEAN  NOT NULL DEFAULT FALSE,
    conflict_note      TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fact_id, source_id)
);

CREATE INDEX IF NOT EXISTS ix_fact_source_link_fact   ON celex_facts.fact_source_link (fact_id);
CREATE INDEX IF NOT EXISTS ix_fact_source_link_source ON celex_facts.fact_source_link (source_id);


-- ===========================================================================
-- knowledge_edges raw-evidence grounding -> fact_source_registry
-- Added: 2026-05-31
-- See migrate_knowledge_edge_source_id.sql for the idempotent migration script.
-- Declared here (rather than inline above) because fact_source_registry is defined
-- later in this file; the FK is added once the catalog table exists.
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_knowledge_edges_dst_source_id'
          AND conrelid = 'celex_facts.knowledge_edges'::regclass
    ) THEN
        ALTER TABLE celex_facts.knowledge_edges
            ADD CONSTRAINT fk_knowledge_edges_dst_source_id
            FOREIGN KEY (dst_source_id)
            REFERENCES celex_facts.fact_source_registry (source_id);
    END IF;
END $$;


---------------------------


ALTER TYPE celex_facts.source_type_enum ADD VALUE IF NOT EXISTS 'fact';

-- 'gated_by': the linked source fact is a control-flow precondition (a YAML
-- ``condition`` gate, e.g. court_list_raw is only extracted when
-- is_court_involved == true), NOT a data source the fact's value derives from.
-- Excluded from celex_facts.fact_provenance() traversal.
ALTER TYPE celex_facts.relation_type_enum ADD VALUE IF NOT EXISTS 'gated_by';

ALTER TABLE celex_facts.fact_source_registry
    ADD COLUMN IF NOT EXISTS source_fact_id uuid
        REFERENCES celex_facts.facts (id) ON DELETE CASCADE;

-- Normalization: one fact_source_registry row per distinct predecessor fact.
CREATE UNIQUE INDEX IF NOT EXISTS uq_source_registry_fact
    ON celex_facts.fact_source_registry (source_fact_id)
    WHERE source_fact_id IS NOT NULL;


-- ============================================================
-- FUNCTION: celex_facts.fact_provenance(uuid)
-- Purpose: Full lineage from any fact back to its source evidence.
-- Supports both:
--   (a) direct fact → fact_source_registry lookups
--   (b) recursive fact → fact → ... → fact_source_registry chains
--
-- This function is the ONLY supported entry point into the traversal; there is
-- deliberately no v_fact_provenance view, so no query can traverse the whole
-- corpus by accident.
--
-- Query pattern for Q&A citation:
--   SELECT p.*
--   FROM celex_facts.facts f
--   CROSS JOIN LATERAL celex_facts.fact_provenance(f.id) p
--   WHERE f.celex_id = '<CELEX-...>' AND f.name = '<fact_name>'
--   ORDER BY p.depth, p.trust_rank DESC;
-- ============================================================
--
-- DROP FUNCTION IF EXISTS celex_facts.fact_provenance(uuid);

CREATE OR REPLACE FUNCTION celex_facts.fact_provenance(p_fact_id uuid)
RETURNS TABLE (
    leaf_fact_id        uuid,
    current_fact_id     uuid,
    depth               int,
    ancestry_path       uuid[],
    ancestry_path_names text,
    source_id           uuid,
    source_type         celex_facts.source_type_enum,
    celex_id            text,
    chunk_id            varchar,
    chunk_seq           int[],
    heideltime_event_id uuid,
    lei_id              text,
    csv_row             int4,
    source_description  text,
    source_created_at   timestamptz,
    relation_type       celex_facts.relation_type_enum,
    extraction_method   text,
    trust_rank          int2,
    conflict_flag       bool,
    conflict_note       text,
    pipeline            text,
    dspy_module         text,
    link_created_at     timestamptz
)
LANGUAGE sql STABLE AS
$$
WITH RECURSIVE ancestry AS (

    -- ── Base: seed with the leaf fact, don't emit yet ─────────────
    -- current_fact_id = the fact we're currently expanding
    -- source_id       = each of its sources (fact or chunk)
    SELECT
        p_fact_id                       AS leaf_fact_id,
        p_fact_id                       AS current_fact_id,
        fsl.source_id,
        fsl.relation_type,
        fsl.extraction_method,
        fsl.trust_rank,
        fsl.conflict_flag,
        fsl.conflict_note,
        fsl.pipeline,
        fsl.dspy_module,
        fsl.created_at                  AS link_created_at,
        0                               AS depth,
        ARRAY[p_fact_id]                AS visited,
        ARRAY[p_fact_id]                AS ancestry_path
    FROM celex_facts.fact_source_link fsl
    WHERE fsl.fact_id = p_fact_id
      -- 'gated_by' edges are control-flow preconditions, not data lineage
      AND fsl.relation_type <> 'gated_by'

    UNION ALL

    -- ── Recurse: climb into any source that is itself a fact ──────
    SELECT
        anc.leaf_fact_id,
        sr_parent.source_fact_id        AS current_fact_id,
        fsl2.source_id,
        fsl2.relation_type,
        fsl2.extraction_method,
        fsl2.trust_rank,
        fsl2.conflict_flag,
        fsl2.conflict_note,
        fsl2.pipeline,
        fsl2.dspy_module,
        fsl2.created_at,
        anc.depth + 1,
        anc.visited       || sr_parent.source_fact_id,
        anc.ancestry_path || sr_parent.source_fact_id
    FROM ancestry anc
    -- the current source must be a fact
    INNER JOIN celex_facts.fact_source_registry sr_parent
           ON sr_parent.source_id   = anc.source_id
          AND sr_parent.source_type = 'fact'
    -- expand the *predecessor fact* (source_fact_id) of that fact-source,
    -- not the registry surrogate PK, which never matches a facts.id
    INNER JOIN celex_facts.fact_source_link fsl2
           ON fsl2.fact_id = sr_parent.source_fact_id
          -- do not climb through control-flow gates
          AND fsl2.relation_type <> 'gated_by'
    WHERE sr_parent.source_fact_id <> ALL(anc.visited)
      AND anc.depth < 10

)

-- ── Emit only terminal (non-fact) sources ─────────────────────────
SELECT
    anc.leaf_fact_id,
    anc.current_fact_id,
    anc.depth,
    anc.ancestry_path,
    -- Fact-name prefix only (e.g. ``fines → fine``); the terminal source label
    -- ``[doc, pos=…, score=…]`` is appended by the caller, which alone has the
    -- verifier ``final_score`` (joined from fact_evidence) to embed.
    (
        SELECT string_agg(coalesce(fp.name, '?'), ' → ' ORDER BY ord)
        FROM unnest(anc.ancestry_path) WITH ORDINALITY AS u(fid, ord)
        LEFT JOIN celex_facts.facts fp ON fp.id = u.fid
    )                                   AS ancestry_path_names,
    sr.source_id,
    sr.source_type,
    sr.celex_id,
    sr.chunk_id,
    sr.chunk_seq,
    sr.heideltime_event_id,
    sr.lei_id,
    sr.csv_row,
    sr.description                      AS source_description,
    sr.created_at                       AS source_created_at,
    anc.relation_type,
    anc.extraction_method,
    anc.trust_rank,
    anc.conflict_flag,
    anc.conflict_note,
    anc.pipeline,
    anc.dspy_module,
    anc.link_created_at
FROM ancestry anc
INNER JOIN celex_facts.fact_source_registry sr ON sr.source_id = anc.source_id
WHERE sr.source_type != 'fact'          -- only emit chunk / heideltime / lei / csv
ORDER BY anc.depth, sr.celex_id
$$;

--- ====================================================================================


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
    action         varchar     NOT NULL
        CONSTRAINT ck_fact2book_checks_action
        CHECK (action IN ('corroborated', 'mismatch', 'skipped')),
    -- How the book references the case ('landmark' | 'simple').
    reference_type varchar,

    created_at    timestamptz NOT NULL DEFAULT now()
);

-- One check row per (fact, celex); re-runs heal rather than duplicate. The
-- citation anchor (book_chunk_id) is deliberately out of the key: the reranker
-- re-selects it per run, so keying on it would accumulate stale rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fact2book_checks
    ON celex_facts.fact2book_checks (fact_id, celex_id);

CREATE INDEX IF NOT EXISTS ix_fact2book_checks_fact
    ON celex_facts.fact2book_checks (fact_id);

CREATE INDEX IF NOT EXISTS ix_fact2book_checks_celex
    ON celex_facts.fact2book_checks (celex_id);


--- ====================================================================================

-- ===========================================================================
-- Legal-provision ontology — treaty renumbering with temporal validity
-- Added: 2026-07-05 (Phase 3, review Weakness 4 / Article-86 ambiguity)
-- See migrate_legal_provisions.sql for the idempotent migration + seed rows.
-- valid_from inclusive, valid_to EXCLUSIVE (NULL = in force); dates encode
-- the SYSTEM era boundaries from settings (TREATY_BOUNDARY_*), not the
-- real-world in-force dates. EEA rows are data-only (excluded from the
-- runtime ProvisionMap by provision_repository.RUNTIME_TREATIES).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS celex_facts.legal_provisions (
    id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    provision         text        NOT NULL,   -- display form, e.g. 'Article 86 EEC'
    article_number    int4        NOT NULL,   -- token as parsed from text: 86
    treaty            varchar     NOT NULL,   -- 'eec' | 'ec' | 'tfeu' | 'eea'
    valid_from        date        NOT NULL,   -- inclusive
    valid_to          date        NULL,       -- EXCLUSIVE; NULL = in force
    canonical_concept varchar     NOT NULL,   -- InfringementConcept value
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_legal_provisions_article_treaty
    ON celex_facts.legal_provisions (article_number, treaty);


INSERT INTO celex_facts.legal_provisions
    (provision, article_number, treaty, valid_from, valid_to, canonical_concept)
VALUES
    ('Article 85 EEC',   85, 'eec',  DATE '1958-01-01', DATE '1999-04-30', 'anticompetitive_agreement'),
    ('Article 86 EEC',   86, 'eec',  DATE '1958-01-01', DATE '1999-04-30', 'abuse_of_dominance'),
    ('Article 90 EEC',   90, 'eec',  DATE '1958-01-01', DATE '1999-04-30', 'public_undertaking'),
    ('Article 81 EC',    81, 'ec',   DATE '1999-04-30', DATE '2009-11-30', 'anticompetitive_agreement'),
    ('Article 82 EC',    82, 'ec',   DATE '1999-04-30', DATE '2009-11-30', 'abuse_of_dominance'),
    ('Article 86 EC',    86, 'ec',   DATE '1999-04-30', DATE '2009-11-30', 'public_undertaking'),
    ('Article 101 TFEU', 101, 'tfeu', DATE '2009-11-30', NULL,              'anticompetitive_agreement'),
    ('Article 102 TFEU', 102, 'tfeu', DATE '2009-11-30', NULL,              'abuse_of_dominance'),
    ('Article 106 TFEU', 106, 'tfeu', DATE '2009-11-30', NULL,              'public_undertaking'),
    ('Article 53 EEA',   53, 'eea',  DATE '1994-01-01', NULL,              'anticompetitive_agreement'),
    ('Article 54 EEA',   54, 'eea',  DATE '1994-01-01', NULL,              'abuse_of_dominance')
ON CONFLICT (article_number, treaty) DO NOTHING;

-- ===========================================================================
-- Typed Entity Graph (Phase 5.1)
--
-- Nodes (celex_facts.entities) and lineage-grounded edges
-- (celex_facts.entity_edges) materialized from celex_facts.facts by
-- src/batch/Main_materialize_entity_graph.py. Every edge carries
-- source_fact_id so celex_facts.fact_provenance() still resolves.
-- See migrate_entity_graph.sql for the idempotent migration.
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'celex_facts' AND t.typname = 'entity_kind_enum'
    ) THEN
        CREATE TYPE celex_facts.entity_kind_enum AS ENUM (
            'company', 'case', 'market', 'provision'
        );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'celex_facts' AND t.typname = 'edge_relation_enum'
    ) THEN
        CREATE TYPE celex_facts.edge_relation_enum AS ENUM (
            'participated_in', 'fined', 'leniency_applied', 'leniency_granted',
            'leniency_rejected', 'parent_of', 'active_in', 'violated'
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS celex_facts.entities (
    entity_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            celex_facts.entity_kind_enum NOT NULL,
    canonical_name  TEXT NOT NULL,
    natural_key     TEXT NOT NULL,
    lei_id          TEXT,
    nace_code       TEXT,
    celex_id        TEXT,
    article_number  INT,
    attrs_json      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_entities_kind_natural UNIQUE (kind, natural_key)
);

CREATE INDEX IF NOT EXISTS ix_entities_lei
    ON celex_facts.entities (lei_id) WHERE lei_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_entities_celex
    ON celex_facts.entities (celex_id) WHERE celex_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_entities_kind
    ON celex_facts.entities (kind);

CREATE TABLE IF NOT EXISTS celex_facts.entity_edges (
    edge_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    src_entity_id   UUID NOT NULL REFERENCES celex_facts.entities (entity_id) ON DELETE CASCADE,
    dst_entity_id   UUID NOT NULL REFERENCES celex_facts.entities (entity_id) ON DELETE CASCADE,
    relation        celex_facts.edge_relation_enum NOT NULL,
    celex_id        TEXT NOT NULL,
    value_json      TEXT,
    source_fact_id  UUID NOT NULL REFERENCES celex_facts.facts (id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
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



-- =====================================================================
-- Migration: section_type + context_prefix columns on pg_embedding_qwen,
--            document_tsv redefined to cover the context prefix
-- =====================================================================
-- Purpose
--   Phase 3 of the knowledge-representation review models the canonical
--   structure of a Commission decision (parties -> recitals/facts ->
--   legal assessment -> fines -> operative -> annex) as chunk metadata:
--
--   * section_type    — enum-ish varchar written by the rule-first /
--                       LLM-fallback classifier batch
--                       (src/batch/Main_classify_sections.py). Enables
--                       filtered retrieval (section_types parameter on
--                       ChunkRepository / HybridSearch) and the
--                       section-type ranking boost that replaces the
--                       positional "Legal Climax" heuristic.
--   * context_prefix  — 1-2 sentence contextual-retrieval line (case
--                       name, section, chunk topic) written by
--                       src/batch/Main_enrich_chunk_context.py, which
--                       also re-embeds context_prefix + document.
--
--   document_tsv is redefined so the lexical (FTS) leg also sees the
--   prefix; the dense leg sees it via the re-embed.
--
-- Approach
--   Plain ADD COLUMN IF NOT EXISTS for the two nullable columns. Postgres
--   cannot ALTER a generated column's expression, so document_tsv is
--   dropped and re-added with the new expression inside a DO block that
--   first inspects the current expression via pg_get_expr — a re-run, or
--   a run against an already-migrated table, is a no-op.
--
--   The tsvector expression stays plain to_tsvector('english', ...)
--   (no unaccent), matching the query side (websearch_to_tsquery /
--   plainto_tsquery without unaccent); accent-folding remains the
--   trigram path's job (see migrate_fts_extensions.sql).
--
--   coalesce(context_prefix, '') preserves the pre-migration NULL
--   semantics: a NULL document still yields a NULL tsvector.
--
-- Idempotency
--   IF NOT EXISTS guards plus the pg_get_expr inspection make this safe
--   to re-run.
--
-- Cost
--   Dropping/re-adding the STORED generated column rewrites the whole
--   table (~55k rows) and rebuilds the GIN index; FTS is briefly
--   unindexed between the drop and the CREATE INDEX. Run in a quiet
--   window.
-- =====================================================================

ALTER TABLE celex_facts.pg_embedding_qwen
    ADD COLUMN IF NOT EXISTS context_prefix text NULL;

ALTER TABLE celex_facts.pg_embedding_qwen
    ADD COLUMN IF NOT EXISTS section_type varchar NULL;

DO $$
DECLARE
    tsv_expr text;
BEGIN
    SELECT pg_get_expr(d.adbin, d.adrelid)
    INTO tsv_expr
    FROM pg_attribute a
    JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE a.attrelid = 'celex_facts.pg_embedding_qwen'::regclass
      AND a.attname = 'document_tsv'
      AND NOT a.attisdropped;

    IF tsv_expr IS NULL OR tsv_expr NOT LIKE '%context_prefix%' THEN
        ALTER TABLE celex_facts.pg_embedding_qwen
            DROP COLUMN IF EXISTS document_tsv;
        ALTER TABLE celex_facts.pg_embedding_qwen
            ADD COLUMN document_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english',
                            coalesce(context_prefix, '') || ' ' || document)
            ) STORED;
    END IF;
END $$;

-- DROP COLUMN above also dropped the GIN index; recreate either way.
CREATE INDEX IF NOT EXISTS pg_embedding_qwen_document_tsv_gin
    ON celex_facts.pg_embedding_qwen
    USING gin (document_tsv);

CREATE INDEX IF NOT EXISTS ix_pg_embedding_qwen_section_type
    ON celex_facts.pg_embedding_qwen
    USING btree (section_type);


-----------


-- celex_facts.company_investigated2celex_id definition

-- Drop table

-- DROP TABLE celex_facts.company_investigated2celex_id;

CREATE TABLE celex_facts.company_investigated2celex_id (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	company_name text NULL,
	celex_id text NULL,
	address text null,
	iso_country_code text null,
	fk_lei text null,
	fk_opencorp_idx text null,
	fk_handelsregister text null,
	CONSTRAINT company_investigated2celex_id_unique UNIQUE (company_name, celex_id),
	CONSTRAINT company_investigated2celex_id_pkey PRIMARY KEY (id)
	
);





-------------

-- celex_facts.qa_company_delta_vw was dropped by
-- migrate_retire_obsolete_company_facts.sql: it diffed companies_investigated
-- against companies_canoncial to QA the rename, and the canonical side no
-- longer exists.




-----


CREATE INDEX idx_corp_names_handelsregister_companies
    ON celex_facts.handelsregister_companies USING btree
    (name ASC NULLS LAST)
    WITH (deduplicate_items=True)
    TABLESPACE pg_default;


        CREATE INDEX ix_hr_matchkey ON celex_facts.handelsregister_companies
          (regexp_replace(translate(replace(replace(replace(replace(replace(replace(
             lower(name),'ä','ae'),'ö','oe'),'ü','ue'),'ß','ss'),'æ','ae'),'œ','oe'),
             'àáâãåèéêëìíîïòóôõøùúûçñ','aaaaaeeeeiiiiooooouuucn'),'[^a-z0-9]','','g'));
        
        
        --CREATE EXTENSION IF NOT EXISTS pg_trgm;
        -- CREATE INDEX CONCURRENTLY ix_corp_lei_legal_name_gin_trgm
        --DROP index IF  exists ix_corp_lei_legal_name_gin_trgm;
        CREATE INDEX  ix_corp_lei_legal_name_gin_trgm
        ON celex_facts.corp_lei USING gin (legal_name gin_trgm_ops);
        -- then optionally DROP INDEX idx_legal_name_trgm_gist if unused.
        
 ----
        
        
-- celex_facts.company_registry_master_vw -- the tall registry-master view over
-- LEI / Handelsregister / OpenCorporates / llm_index_companies -- is defined
-- ONLY in migrate_registry_master_view.sql. Do NOT copy its definition here.
-- The roster view below already has three competing copies and has been broken
-- once by replaying a stale one.

--  DROP  view company_investigated2lei2corpidx2hr_vw;
-- jurisdication_code is part of this view. Recreating it without that column
-- breaks Main_ingest_opencorp_index_companies.py with UndefinedColumn -- which
-- is exactly how it was lost once already, when the adress -> address rename
-- replayed a stale six-column definition. Keep all seven columns.
create view company_investigated2lei2corpidx2hr_vw
AS
SELECT distinct company_name,address, iso_country_code, fk_lei, fk_opencorp_idx, fk_handelsregister, jurisdication_code
FROM celex_facts.company_investigated2celex_id;

---

-- drop  TABLE celex_facts.company_investigated_celex_id2opencorp_index_company
-- N:M link: investigated-company roster <-> OpenCorporates search hits
CREATE TABLE celex_facts.company_investigated_celex_id2opencorp_index_company (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fk_company_investigated uuid NOT NULL,
    fk_opencorp_index_company uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT company_investigated2opencorp_pkey PRIMARY KEY (id),
    CONSTRAINT company_investigated2opencorp_unique
        UNIQUE (fk_company_investigated, fk_opencorp_index_company),
    CONSTRAINT fk_ci2oc_company_investigated FOREIGN KEY (fk_company_investigated)
        REFERENCES celex_facts.company_investigated2celex_id (id) ON DELETE CASCADE,
    CONSTRAINT fk_ci2oc_opencorp_company FOREIGN KEY (fk_opencorp_index_company)
        REFERENCES celex_facts.opencorp_index_companies (id) ON DELETE CASCADE
);

-- Durable per-day OpenCorporates API-call budget counter
CREATE TABLE celex_facts.opencorp_api_daily_calls (
    call_date date NOT NULL,
    call_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT opencorp_api_daily_calls_pkey PRIMARY KEY (call_date)
);



-- DROP TABLE if exists celex_facts.lei2company_canonical2alias_matches_tbl;
CREATE TABLE celex_facts.lei2company_canonical2alias_matches_tbl 
AS
SELECT DISTINCT ON (c2.company_alias)
    c2.company_alias,
    c1.legal_name,
    c1.legal_name <-> c2.company_alias AS distance
FROM celex_facts.company_canonical2alias c2
CROSS JOIN LATERAL (
    SELECT legal_name
    FROM celex_facts.corp_lei
    ORDER BY legal_name <-> c2.company_alias  -- uses GIN/GIST index!
    LIMIT 3                                    -- top 3 candidates per alias
) c1
WHERE (c1.legal_name <-> c2.company_alias) < 0.3
--and c2.company_alias = 'Telefunken'
ORDER BY c2.company_alias, distance ASC
;

-- ---------------------------------------------------------------------------
-- Migration: track failed OpenCorporates search attempts on opencorp_index_query
--
-- Main_ingest_opencorp_index_companies.py previously only logged a failed
-- search to stdout/the log file; nothing in the database recorded which query
-- failed or why. These columns let a failure be written back to the same
-- query row that a successful search would have used, so it is queryable with
-- SQL. NULL error_class means the last attempt for that
-- (normalized_query, jurisdiction_code) key succeeded.
--
-- Safe to re-run: every statement is guarded with IF NOT EXISTS.
-- ---------------------------------------------------------------------------

ALTER TABLE celex_facts.opencorp_index_query
    ADD COLUMN IF NOT EXISTS error_class varchar(255),
    ADD COLUMN IF NOT EXISTS error_status_code integer,
    ADD COLUMN IF NOT EXISTS error_message text;

-- celex_facts.llm_index_companies definition

-- Drop table

-- DROP TABLE celex_facts.llm_index_companies;

CREATE TABLE celex_facts.llm_index_companies (
	id uuid DEFAULT gen_random_uuid() NOT NULL,
	company_name text NOT NULL,
	canonical_name text NULL,
	abbreviation text NULL,
	legal_form text NULL,
	life_cycle_status text NULL,
	products_services text NULL,
	geo_market text NULL,
	headquarter_address text NULL,
	country text NULL,
	jurisdiction text NULL,
	lei varchar(64) NULL,
	euid varchar(64) NULL,
	bris varchar(64) NULL,
	handelsregister varchar(64) NULL,
	siren varchar(64) NULL,
	llm_model_id varchar(128) NOT NULL,
	created_at timestamptz DEFAULT now() NOT NULL,
	updated_at timestamptz DEFAULT now() NOT NULL,
	established_since text NULL,
	succession text NULL,
	generic_corp_id varchar(64) NULL,
	generic_corp_registrar text NULL,
	CONSTRAINT llm_index_companies_company_name_key UNIQUE (company_name),
	CONSTRAINT llm_index_companies_pkey PRIMARY KEY (id)
);
CREATE INDEX ix_llm_index_companies_country ON celex_facts.llm_index_companies USING btree (country);
CREATE INDEX ix_llm_index_companies_lei ON celex_facts.llm_index_companies USING btree (lei);

ALTER TABLE celex_facts.llm_index_companies
    DROP COLUMN IF EXISTS kvk,
    DROP COLUMN IF EXISTS vat_number;

--
ALTER TYPE celex_facts.edge_relation_enum ADD VALUE IF NOT EXISTS 'dated_in';


