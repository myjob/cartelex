--
-- PostgreSQL database dump
--

\restrict aIDCHcpA7cDIha6Z3yadNp5z67OzbvULUsgPp1FfN1eNXbLBZPgy0WZeE84yNZr

-- Dumped from database version 18.1 (Debian 18.1-1.pgdg13+2)
-- Dumped by pg_dump version 18.1

-- Started on 2026-08-28 17:42:49 UTC

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- TOC entry 4230 (class 1262 OID 606474)
-- Name: celex; Type: DATABASE; Schema: -; Owner: job
--

CREATE DATABASE celex WITH TEMPLATE = template0 ENCODING = 'UTF8' LOCALE_PROVIDER = libc LOCALE = 'en_US.utf8';


ALTER DATABASE celex OWNER TO job;

\unrestrict aIDCHcpA7cDIha6Z3yadNp5z67OzbvULUsgPp1FfN1eNXbLBZPgy0WZeE84yNZr
\connect celex
\restrict aIDCHcpA7cDIha6Z3yadNp5z67OzbvULUsgPp1FfN1eNXbLBZPgy0WZeE84yNZr

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- TOC entry 9 (class 2615 OID 606475)
-- Name: celex_facts; Type: SCHEMA; Schema: -; Owner: job
--

CREATE SCHEMA celex_facts;


ALTER SCHEMA celex_facts OWNER TO job;

--
-- TOC entry 1072 (class 1247 OID 606894)
-- Name: edge_relation_enum; Type: TYPE; Schema: celex_facts; Owner: celex_facts
--

CREATE TYPE celex_facts.edge_relation_enum AS ENUM (
    'participated_in',
    'fined',
    'leniency_applied',
    'leniency_granted',
    'leniency_rejected',
    'parent_of',
    'active_in',
    'violated'
);


ALTER TYPE celex_facts.edge_relation_enum OWNER TO celex_facts;

--
-- TOC entry 1075 (class 1247 OID 606912)
-- Name: entity_kind_enum; Type: TYPE; Schema: celex_facts; Owner: celex_facts
--

CREATE TYPE celex_facts.entity_kind_enum AS ENUM (
    'company',
    'case',
    'market',
    'provision'
);


ALTER TYPE celex_facts.entity_kind_enum OWNER TO celex_facts;

--
-- TOC entry 1078 (class 1247 OID 606922)
-- Name: relation_type_enum; Type: TYPE; Schema: celex_facts; Owner: celex_facts
--

CREATE TYPE celex_facts.relation_type_enum AS ENUM (
    'extracted_from',
    'corroborated_by',
    'entity_resolved_via',
    'derived_from',
    'gated_by'
);


ALTER TYPE celex_facts.relation_type_enum OWNER TO celex_facts;

--
-- TOC entry 1081 (class 1247 OID 606934)
-- Name: source_type_enum; Type: TYPE; Schema: celex_facts; Owner: celex_facts
--

CREATE TYPE celex_facts.source_type_enum AS ENUM (
    'chunk',
    'csv_human',
    'book_case',
    'lei_corporate',
    'heideltime',
    'fact'
);


ALTER TYPE celex_facts.source_type_enum OWNER TO celex_facts;

--
-- TOC entry 1084 (class 1247 OID 606948)
-- Name: verification_status_enum; Type: TYPE; Schema: celex_facts; Owner: celex_facts
--

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


ALTER TYPE celex_facts.verification_status_enum OWNER TO celex_facts;

--
-- TOC entry 318 (class 1255 OID 606967)
-- Name: f_unaccent(text); Type: FUNCTION; Schema: celex_facts; Owner: celex_facts
--

CREATE FUNCTION celex_facts.f_unaccent(text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$ SELECT celex_facts.unaccent('celex_facts.unaccent', $1) $_$;


ALTER FUNCTION celex_facts.f_unaccent(text) OWNER TO celex_facts;

--
-- TOC entry 381 (class 1255 OID 606968)
-- Name: fact_provenance(uuid); Type: FUNCTION; Schema: celex_facts; Owner: celex_facts
--

CREATE FUNCTION celex_facts.fact_provenance(p_fact_id uuid) RETURNS TABLE(leaf_fact_id uuid, current_fact_id uuid, depth integer, ancestry_path uuid[], ancestry_path_names text, source_id uuid, source_type celex_facts.source_type_enum, celex_id text, chunk_id character varying, chunk_seq integer[], heideltime_event_id uuid, lei_id text, csv_row integer, source_description text, source_created_at timestamp with time zone, relation_type celex_facts.relation_type_enum, extraction_method text, trust_rank smallint, conflict_flag boolean, conflict_note text, pipeline text, dspy_module text, link_created_at timestamp with time zone)
    LANGUAGE sql STABLE
    AS $$
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


ALTER FUNCTION celex_facts.fact_provenance(p_fact_id uuid) OWNER TO celex_facts;

--
-- TOC entry 355 (class 1255 OID 606971)
-- Name: smart_to_date(text); Type: FUNCTION; Schema: celex_facts; Owner: postgres
--

CREATE FUNCTION celex_facts.smart_to_date(input_text text) RETURNS date
    LANGUAGE plpgsql IMMUTABLE
    AS $_$
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
$_$;


ALTER FUNCTION celex_facts.smart_to_date(input_text text) OWNER TO postgres;

--
-- TOC entry 404 (class 1255 OID 606972)
-- Name: trg_knowledge_must_have_edge(); Type: FUNCTION; Schema: celex_facts; Owner: celex_facts
--

CREATE FUNCTION celex_facts.trg_knowledge_must_have_edge() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM celex_facts.knowledge_edges
                WHERE src_knowledge_id = NEW.id
            ) THEN
                RAISE EXCEPTION 'knowledge_item % has no outgoing edges (ungrounded)', NEW.id;
            END IF;
            RETURN NEW;
        END;
        $$;


ALTER FUNCTION celex_facts.trg_knowledge_must_have_edge() OWNER TO celex_facts;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- TOC entry 224 (class 1259 OID 606973)
-- Name: case_celex_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.case_celex_id (
    case_id text NOT NULL,
    celex_id text NOT NULL
);


ALTER TABLE celex_facts.case_celex_id OWNER TO celex_facts;

--
-- TOC entry 225 (class 1259 OID 606980)
-- Name: celex_case_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.celex_case_id (
    celex_id text NOT NULL,
    case_x text,
    case_id text
);


ALTER TABLE celex_facts.celex_case_id OWNER TO celex_facts;

--
-- TOC entry 226 (class 1259 OID 606986)
-- Name: facts; Type: TABLE; Schema: celex_facts; Owner: postgres
--

CREATE TABLE celex_facts.facts (
    id uuid NOT NULL,
    celex_id character varying NOT NULL,
    name character varying,
    value_md character varying,
    value_json character varying,
    created_at timestamp without time zone,
    references_doc_json jsonb,
    references_fact_json jsonb,
    verification_status celex_facts.verification_status_enum DEFAULT 'unverified'::celex_facts.verification_status_enum NOT NULL,
    verification_score numeric,
    verification_updated_at timestamp with time zone
);


ALTER TABLE celex_facts.facts OWNER TO postgres;

--
-- TOC entry 227 (class 1259 OID 606995)
-- Name: celex_index_vw; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.celex_index_vw AS
 SELECT s.celex_id,
    t.value_md AS case_title,
    i.value_md AS industy,
    c.value_md AS cartel_period,
    s.value_md AS case_summary
   FROM (((celex_facts.facts s
     JOIN ( SELECT facts.celex_id,
            facts.value_md
           FROM celex_facts.facts
          WHERE ((facts.name)::text = 'case_title'::text)) t ON (((t.celex_id)::text = (s.celex_id)::text)))
     JOIN ( SELECT facts.celex_id,
            facts.value_md
           FROM celex_facts.facts
          WHERE ((facts.name)::text = 'cartel_period'::text)) c ON (((c.celex_id)::text = (s.celex_id)::text)))
     JOIN ( SELECT facts.celex_id,
            facts.value_md
           FROM celex_facts.facts
          WHERE ((facts.name)::text = 'companies_industry'::text)) i ON (((i.celex_id)::text = (s.celex_id)::text)))
  WHERE ((s.name)::text = 'case_summary'::text);


ALTER VIEW celex_facts.celex_index_vw OWNER TO celex_facts;

--
-- TOC entry 228 (class 1259 OID 607000)
-- Name: company_canonical2alias; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_canonical2alias (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_canonical text,
    company_alias text,
    fk_company_canonical uuid
)
WITH (parallel_workers='8');


ALTER TABLE celex_facts.company_canonical2alias OWNER TO celex_facts;

--
-- TOC entry 229 (class 1259 OID 607007)
-- Name: company_canonical2celex_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_canonical2celex_id (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_name text,
    celex_id text
);


ALTER TABLE celex_facts.company_canonical2celex_id OWNER TO celex_facts;

--
-- TOC entry 230 (class 1259 OID 607014)
-- Name: company_investigated2celex_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_investigated2celex_id (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_name text,
    celex_id text,
    address text,
    iso_country_code text,
    fk_lei text,
    fk_opencorp_idx text,
    fk_handelsregister text,
    jurisdication_code character varying(8)
);


ALTER TABLE celex_facts.company_investigated2celex_id OWNER TO celex_facts;

--
-- TOC entry 275 (class 1259 OID 841480)
-- Name: company_investigated2lei2corpidx2hr_vw; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.company_investigated2lei2corpidx2hr_vw AS
 SELECT DISTINCT company_name,
    address,
    iso_country_code,
    fk_lei,
    fk_opencorp_idx,
    fk_handelsregister,
    jurisdication_code
   FROM celex_facts.company_investigated2celex_id;


ALTER VIEW celex_facts.company_investigated2lei2corpidx2hr_vw OWNER TO celex_facts;

--
-- TOC entry 231 (class 1259 OID 607025)
-- Name: company_investigated_celex_id2opencorp_index_company; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_investigated_celex_id2opencorp_index_company (
    id uuid DEFAULT gen_random_uuid() CONSTRAINT company_investigated_celex_id2opencorp_index_compan_id_not_null NOT NULL,
    fk_company_investigated uuid CONSTRAINT company_investigated_celex_id2_fk_company_investigated_not_null NOT NULL,
    fk_opencorp_index_company uuid CONSTRAINT company_investigated_celex_i_fk_opencorp_index_company_not_null NOT NULL,
    created_at timestamp with time zone DEFAULT now() CONSTRAINT company_investigated_celex_id2opencorp_inde_created_at_not_null NOT NULL,
    resolution_rung text,
    resolution_query text,
    resolution_jurisdiction text,
    match_score numeric(4,3)
);


ALTER TABLE celex_facts.company_investigated_celex_id2opencorp_index_company OWNER TO celex_facts;

--
-- TOC entry 232 (class 1259 OID 607034)
-- Name: company_standard2celex_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_standard2celex_id (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_name text,
    celex_id text
);


ALTER TABLE celex_facts.company_standard2celex_id OWNER TO celex_facts;

--
-- TOC entry 233 (class 1259 OID 607041)
-- Name: company_standard2celex_id2alias; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_standard2celex_id2alias (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    celex_id text,
    company_name text,
    alias text
);


ALTER TABLE celex_facts.company_standard2celex_id2alias OWNER TO celex_facts;

--
-- TOC entry 234 (class 1259 OID 607048)
-- Name: corp_lei; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.corp_lei (
    lei character varying NOT NULL,
    legal_name character varying,
    entity_category character varying,
    legal_jurisdiction character varying,
    entity_status character varying,
    registration_status character varying,
    country character varying,
    legal_address_first_line character varying,
    legal_address_additional_line character varying,
    legal_address_city character varying,
    legal_address_region character varying,
    legal_address_postal_code character varying,
    headquarters_address_first_line character varying,
    headquarters_address_additional_line character varying,
    headquarters_address_city character varying,
    headquarters_address_region character varying,
    headquarters_address_postal_code character varying,
    record_json jsonb,
    created_at timestamp without time zone
);


ALTER TABLE celex_facts.corp_lei OWNER TO celex_facts;

--
-- TOC entry 235 (class 1259 OID 607065)
-- Name: entities; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.entities (
    entity_id uuid DEFAULT gen_random_uuid() NOT NULL,
    kind celex_facts.entity_kind_enum NOT NULL,
    canonical_name text NOT NULL,
    natural_key text NOT NULL,
    lei_id text,
    nace_code text,
    celex_id text,
    article_number integer,
    attrs_json text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE celex_facts.entities OWNER TO celex_facts;

--
-- TOC entry 236 (class 1259 OID 607077)
-- Name: entity_edges; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.entity_edges (
    edge_id uuid DEFAULT gen_random_uuid() NOT NULL,
    src_entity_id uuid NOT NULL,
    dst_entity_id uuid NOT NULL,
    relation celex_facts.edge_relation_enum NOT NULL,
    celex_id text NOT NULL,
    value_json text,
    source_fact_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE celex_facts.entity_edges OWNER TO celex_facts;

--
-- TOC entry 237 (class 1259 OID 607120)
-- Name: fact2book_checks; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.fact2book_checks (
    id uuid DEFAULT gen_random_uuid() CONSTRAINT fact2book_mismatch_id_not_null NOT NULL,
    fact_id uuid CONSTRAINT fact2book_mismatch_fact_id_not_null NOT NULL,
    celex_id text CONSTRAINT fact2book_mismatch_celex_id_not_null NOT NULL,
    case_id text,
    book_chunk_id character varying CONSTRAINT fact2book_mismatch_book_chunk_id_not_null NOT NULL,
    book_path text,
    explanation text CONSTRAINT fact2book_mismatch_explanation_not_null NOT NULL,
    support_label text,
    final_score numeric,
    created_at timestamp with time zone DEFAULT now() CONSTRAINT fact2book_mismatch_created_at_not_null NOT NULL,
    reference_type character varying,
    action character varying NOT NULL,
    CONSTRAINT ck_fact2book_checks_action CHECK (((action)::text = ANY ((ARRAY['corroborated'::character varying, 'mismatch'::character varying, 'skipped'::character varying])::text[])))
);


ALTER TABLE celex_facts.fact2book_checks OWNER TO celex_facts;

--
-- TOC entry 238 (class 1259 OID 607133)
-- Name: fact_evidence; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.fact_evidence (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    fact_id uuid NOT NULL,
    chunk_id character varying,
    support_label text,
    final_score numeric,
    entity_score numeric,
    keyword_score numeric,
    semantic_score numeric,
    temporal_score numeric,
    provenance_score numeric,
    evidence_quote text,
    missing_elements jsonb,
    verified_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE celex_facts.fact_evidence OWNER TO celex_facts;

--
-- TOC entry 239 (class 1259 OID 607143)
-- Name: fact_ingestion_audit_trail; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.fact_ingestion_audit_trail (
    id uuid DEFAULT gen_random_uuid() CONSTRAINT celex_fact_ingestion_audit_trail_id_not_null NOT NULL,
    celex_id text,
    fact_id uuid,
    target_fact_name text,
    question text,
    output_schema text,
    verify_fact_name text,
    force_override text,
    success boolean,
    errors text,
    duration integer,
    created_at timestamp without time zone,
    pipeline text,
    dspy_module text,
    predecessors text
);


ALTER TABLE celex_facts.fact_ingestion_audit_trail OWNER TO celex_facts;

--
-- TOC entry 278 (class 1259 OID 1023006)
-- Name: fact_ingestion_audit_trail_vw; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.fact_ingestion_audit_trail_vw AS
 SELECT a.celex_id,
    a.target_fact_name,
    a.question,
    f.value_md,
    f.value_json,
    a.force_override,
    a.success,
    a.errors,
    a.duration,
    a.created_at AS inserted_update_at
   FROM (celex_facts.fact_ingestion_audit_trail a
     JOIN ( SELECT facts.id,
            facts.value_md,
            facts.value_json
           FROM celex_facts.facts) f ON ((f.id = a.fact_id)))
  ORDER BY a.created_at DESC;


ALTER VIEW celex_facts.fact_ingestion_audit_trail_vw OWNER TO celex_facts;

--
-- TOC entry 240 (class 1259 OID 607155)
-- Name: fact_source_link; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.fact_source_link (
    fact_id uuid NOT NULL,
    source_id uuid NOT NULL,
    extraction_method text NOT NULL,
    relation_type celex_facts.relation_type_enum DEFAULT 'extracted_from'::celex_facts.relation_type_enum NOT NULL,
    trust_rank smallint DEFAULT 5 NOT NULL,
    conflict_flag boolean DEFAULT false NOT NULL,
    conflict_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    pipeline text,
    dspy_module text,
    CONSTRAINT ck_fact_source_link_trust_rank CHECK (((trust_rank >= 1) AND (trust_rank <= 10)))
);


ALTER TABLE celex_facts.fact_source_link OWNER TO celex_facts;

--
-- TOC entry 241 (class 1259 OID 607172)
-- Name: fact_source_registry; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.fact_source_registry (
    source_id uuid DEFAULT gen_random_uuid() CONSTRAINT source_registry_source_id_not_null NOT NULL,
    source_type celex_facts.source_type_enum CONSTRAINT source_registry_source_type_not_null NOT NULL,
    celex_id text,
    chunk_id character varying,
    chunk_seq integer[],
    heideltime_event_id uuid,
    lei_id text,
    csv_row integer,
    description text,
    created_at timestamp with time zone DEFAULT now() CONSTRAINT source_registry_created_at_not_null NOT NULL,
    source_fact_id uuid
);


ALTER TABLE celex_facts.fact_source_registry OWNER TO celex_facts;

--
-- TOC entry 242 (class 1259 OID 607182)
-- Name: facts_cartel_legal_period_vw; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.facts_cartel_legal_period_vw AS
 SELECT c.celex_id,
    c.value_md AS cartel_period,
    c.value_md AS legal_period
   FROM (celex_facts.facts c
     JOIN ( SELECT facts.id,
            facts.celex_id,
            facts.name,
            facts.value_md,
            facts.value_json,
            facts.created_at
           FROM celex_facts.facts
          WHERE ((facts.name)::text = 'legal_assessment_period'::text)) l ON (((l.celex_id)::text = (c.celex_id)::text)))
  WHERE ((c.name)::text = 'cartel_period'::text);


ALTER VIEW celex_facts.facts_cartel_legal_period_vw OWNER TO celex_facts;

--
-- TOC entry 243 (class 1259 OID 607187)
-- Name: facts_cartel_legal_period_category_vw; Type: VIEW; Schema: celex_facts; Owner: postgres
--

CREATE VIEW celex_facts.facts_cartel_legal_period_category_vw AS
 WITH extracted_periods AS (
         SELECT facts_cartel_legal_period_vw.celex_id,
            split_part((facts_cartel_legal_period_vw.cartel_period)::text, ' : '::text, 1) AS raw_c_start,
            split_part((facts_cartel_legal_period_vw.cartel_period)::text, ' : '::text, 2) AS raw_c_end,
            split_part((facts_cartel_legal_period_vw.legal_period)::text, ' : '::text, 1) AS raw_l_start,
            split_part((facts_cartel_legal_period_vw.legal_period)::text, ' : '::text, 2) AS raw_l_end
           FROM celex_facts.facts_cartel_legal_period_vw
        ), parsed_dates AS (
         SELECT extracted_periods.celex_id,
                CASE
                    WHEN (extracted_periods.raw_c_start ~ '^\d{4}-\d{2}-\d{2}$'::text) THEN to_date(extracted_periods.raw_c_start, 'YYYY-MM-DD'::text)
                    ELSE NULL::date
                END AS c_start,
                CASE
                    WHEN (extracted_periods.raw_c_end ~ '^\d{4}-\d{2}-\d{2}$'::text) THEN to_date(extracted_periods.raw_c_end, 'YYYY-MM-DD'::text)
                    ELSE NULL::date
                END AS c_end,
                CASE
                    WHEN (extracted_periods.raw_l_start ~ '^\d{4}-\d{2}-\d{2}$'::text) THEN to_date(extracted_periods.raw_l_start, 'YYYY-MM-DD'::text)
                    ELSE NULL::date
                END AS l_start,
                CASE
                    WHEN (extracted_periods.raw_l_end ~ '^\d{4}-\d{2}-\d{2}$'::text) THEN to_date(extracted_periods.raw_l_end, 'YYYY-MM-DD'::text)
                    ELSE NULL::date
                END AS l_end
           FROM extracted_periods
        )
 SELECT celex_id,
    (((COALESCE(to_char((c_start)::timestamp with time zone, 'YYYY-MM-DD'::text), 'NA'::text) || ' : '::text) || COALESCE(to_char((c_end)::timestamp with time zone, 'YYYY-MM-DD'::text), 'NA'::text)) || '
'::text) AS cartel_period,
    ((COALESCE(to_char((l_start)::timestamp with time zone, 'YYYY-MM-DD'::text), 'NA'::text) || ' : '::text) || COALESCE(to_char((l_end)::timestamp with time zone, 'YYYY-MM-DD'::text), 'NA'::text)) AS legal_period,
        CASE
            WHEN ((c_start IS NULL) OR (c_end IS NULL) OR (l_start IS NULL) OR (l_end IS NULL)) THEN 'NA'::text
            WHEN ((date_trunc('month'::text, (c_start)::timestamp with time zone) = date_trunc('month'::text, (l_start)::timestamp with time zone)) AND (date_trunc('month'::text, (c_end)::timestamp with time zone) = date_trunc('month'::text, (l_end)::timestamp with time zone))) THEN 'exact'::text
            WHEN (c_end < l_start) THEN 'legal_post'::text
            WHEN ((l_start >= c_start) AND (l_end <= c_end)) THEN 'legal-within'::text
            WHEN ((c_start >= l_start) AND (c_end <= l_end)) THEN 'cartel-within'::text
            WHEN ((l_start <= c_end) AND (l_end > c_end)) THEN 'legal-post-overlap'::text
            WHEN ((c_start <= l_end) AND (c_end > l_end)) THEN 'cartel-post-overlap'::text
            ELSE 'NA'::text
        END AS periods_category
   FROM parsed_dates;


ALTER VIEW celex_facts.facts_cartel_legal_period_category_vw OWNER TO postgres;

--
-- TOC entry 274 (class 1259 OID 841358)
-- Name: gleif_jurisdication2countrycode; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.gleif_jurisdication2countrycode (
    jurisdiction_code character varying(6) NOT NULL,
    jurisdiction_name text NOT NULL,
    jurisdiction_type character varying(32) NOT NULL,
    iso_country_code character varying(2) NOT NULL,
    iso_subdivision_code character varying(3),
    is_subdivision boolean NOT NULL,
    source_version character varying(16) NOT NULL,
    loaded_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE celex_facts.gleif_jurisdication2countrycode OWNER TO celex_facts;

--
-- TOC entry 244 (class 1259 OID 607192)
-- Name: handelsregister_companies; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.handelsregister_companies (
    company_number text NOT NULL,
    name text NOT NULL,
    current_status text,
    jurisdiction_code text,
    registered_address text,
    retrieved_at timestamp with time zone,
    register_art text,
    register_nummer text,
    federal_state text,
    native_company_number text,
    registered_office text,
    registrar text,
    former_registrar text
);


ALTER TABLE celex_facts.handelsregister_companies OWNER TO celex_facts;

--
-- TOC entry 245 (class 1259 OID 607199)
-- Name: handelsregister_company_additional_data; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.handelsregister_company_additional_data (
    company_number text NOT NULL,
    ad boolean,
    cd boolean,
    dk boolean,
    hd boolean,
    si boolean,
    ut boolean,
    voe boolean
);


ALTER TABLE celex_facts.handelsregister_company_additional_data OWNER TO celex_facts;

--
-- TOC entry 246 (class 1259 OID 607205)
-- Name: handelsregister_officers; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.handelsregister_officers (
    id integer NOT NULL,
    company_number text NOT NULL,
    name text,
    "position" text,
    type text,
    start_date date,
    end_date date,
    firstname text,
    lastname text,
    city text,
    flag text,
    dismissed boolean,
    reference_no integer,
    title text
);


ALTER TABLE celex_facts.handelsregister_officers OWNER TO celex_facts;

--
-- TOC entry 247 (class 1259 OID 607212)
-- Name: handelsregister_officers_id_seq; Type: SEQUENCE; Schema: celex_facts; Owner: celex_facts
--

CREATE SEQUENCE celex_facts.handelsregister_officers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE celex_facts.handelsregister_officers_id_seq OWNER TO celex_facts;

--
-- TOC entry 4231 (class 0 OID 0)
-- Dependencies: 247
-- Name: handelsregister_officers_id_seq; Type: SEQUENCE OWNED BY; Schema: celex_facts; Owner: celex_facts
--

ALTER SEQUENCE celex_facts.handelsregister_officers_id_seq OWNED BY celex_facts.handelsregister_officers.id;


--
-- TOC entry 248 (class 1259 OID 607213)
-- Name: handelsregister_previous_names; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.handelsregister_previous_names (
    id integer NOT NULL,
    company_number text NOT NULL,
    company_name text NOT NULL
);


ALTER TABLE celex_facts.handelsregister_previous_names OWNER TO celex_facts;

--
-- TOC entry 249 (class 1259 OID 607221)
-- Name: handelsregister_previous_names_id_seq; Type: SEQUENCE; Schema: celex_facts; Owner: celex_facts
--

CREATE SEQUENCE celex_facts.handelsregister_previous_names_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE celex_facts.handelsregister_previous_names_id_seq OWNER TO celex_facts;

--
-- TOC entry 4232 (class 0 OID 0)
-- Dependencies: 249
-- Name: handelsregister_previous_names_id_seq; Type: SEQUENCE OWNED BY; Schema: celex_facts; Owner: celex_facts
--

ALTER SEQUENCE celex_facts.handelsregister_previous_names_id_seq OWNED BY celex_facts.handelsregister_previous_names.id;


--
-- TOC entry 250 (class 1259 OID 607222)
-- Name: heideltime_extractions; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.heideltime_extractions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    celex_id character varying NOT NULL,
    fk_chunk_id character varying NOT NULL,
    tid character varying NOT NULL,
    timex_text character varying NOT NULL,
    timex_type character varying NOT NULL,
    timex_value date,
    span_start integer NOT NULL,
    span_end integer NOT NULL,
    raw_jsonb jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    source_hash character varying(32)
);


ALTER TABLE celex_facts.heideltime_extractions OWNER TO celex_facts;

--
-- TOC entry 251 (class 1259 OID 607238)
-- Name: knowledge_edges; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.knowledge_edges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    src_knowledge_id uuid NOT NULL,
    dst_fact_id uuid,
    dst_knowledge_id uuid,
    relation_type text NOT NULL,
    confidence real,
    created_by text,
    agent_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    dst_source_id uuid,
    CONSTRAINT ck_knowledge_edges_one_destination CHECK ((num_nonnulls(dst_source_id, dst_fact_id, dst_knowledge_id) = 1))
);


ALTER TABLE celex_facts.knowledge_edges OWNER TO celex_facts;

--
-- TOC entry 252 (class 1259 OID 607250)
-- Name: knowledge_extraction_queue; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.knowledge_extraction_queue (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_prompt text NOT NULL,
    agent_answer text NOT NULL,
    supporting_facts text,
    fact_ids jsonb,
    chunk_ids jsonb,
    status text DEFAULT 'pending'::text NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone
);


ALTER TABLE celex_facts.knowledge_extraction_queue OWNER TO celex_facts;

--
-- TOC entry 253 (class 1259 OID 607263)
-- Name: knowledge_ingest_audit_trail; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.knowledge_ingest_audit_trail (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    queue_item_id uuid,
    knowledge_id uuid,
    user_prompt text,
    agent_answer text,
    pipeline text DEFAULT 'knowledge_ingest'::text,
    dspy_module text,
    predecessors jsonb,
    success boolean,
    errors text,
    duration integer,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE celex_facts.knowledge_ingest_audit_trail OWNER TO celex_facts;

--
-- TOC entry 254 (class 1259 OID 607272)
-- Name: knowledge_items; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.knowledge_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    knowledge_text text NOT NULL,
    knowledge_type text NOT NULL,
    scope text DEFAULT 'global'::text NOT NULL,
    jurisdiction text,
    status text DEFAULT 'draft'::text NOT NULL,
    confidence real,
    created_by text NOT NULL,
    agent_run_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    knowledge_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, knowledge_text)) STORED
);


ALTER TABLE celex_facts.knowledge_items OWNER TO celex_facts;

--
-- TOC entry 255 (class 1259 OID 607291)
-- Name: knowledge_question_embeddings; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.knowledge_question_embeddings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    knowledge_id uuid NOT NULL,
    question_variant text NOT NULL,
    embedding public.vector NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE celex_facts.knowledge_question_embeddings OWNER TO celex_facts;

--
-- TOC entry 256 (class 1259 OID 607303)
-- Name: langchain_pg_collection; Type: TABLE; Schema: celex_facts; Owner: ai
--

CREATE TABLE celex_facts.langchain_pg_collection (
    uuid uuid NOT NULL,
    name character varying NOT NULL,
    cmetadata json
);


ALTER TABLE celex_facts.langchain_pg_collection OWNER TO ai;

--
-- TOC entry 277 (class 1259 OID 843802)
-- Name: langchain_pg_embedding; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.langchain_pg_embedding (
    id character varying NOT NULL,
    collection_id uuid,
    embedding public.vector,
    document character varying,
    cmetadata jsonb,
    celex_id character varying,
    doc_type character varying,
    token_count integer,
    path character varying,
    chunk_hash character varying,
    doc_pos integer,
    ingestion_timestamp timestamp with time zone DEFAULT now()
);


ALTER TABLE celex_facts.langchain_pg_embedding OWNER TO celex_facts;

--
-- TOC entry 257 (class 1259 OID 607310)
-- Name: legal_provisions; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.legal_provisions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    provision text NOT NULL,
    article_number integer NOT NULL,
    treaty character varying NOT NULL,
    valid_from date NOT NULL,
    valid_to date,
    canonical_concept character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE celex_facts.legal_provisions OWNER TO celex_facts;

--
-- TOC entry 273 (class 1259 OID 840915)
-- Name: lei2company_canonical2alias_matches_tbl; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.lei2company_canonical2alias_matches_tbl (
    company_alias text,
    legal_name character varying,
    distance real
);


ALTER TABLE celex_facts.lei2company_canonical2alias_matches_tbl OWNER TO celex_facts;

--
-- TOC entry 258 (class 1259 OID 607338)
-- Name: lei2company_canonical_matches_mv; Type: MATERIALIZED VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE MATERIALIZED VIEW celex_facts.lei2company_canonical_matches_mv AS
 SELECT DISTINCT ON (c2.company_canonical) c2.company_canonical,
    c1.legal_name,
    ((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias) AS distance
   FROM (celex_facts.company_canonical2alias c2
     CROSS JOIN LATERAL ( SELECT corp_lei.legal_name
           FROM celex_facts.corp_lei
          ORDER BY ((corp_lei.legal_name)::text OPERATOR(celex_facts.<->) c2.company_canonical)
         LIMIT 3) c1)
  WHERE (((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_canonical) < (0.3)::double precision)
  ORDER BY c2.company_canonical, ((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias)
  WITH NO DATA;


ALTER MATERIALIZED VIEW celex_facts.lei2company_canonical_matches_mv OWNER TO celex_facts;

--
-- TOC entry 276 (class 1259 OID 842390)
-- Name: llm_index_companies; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.llm_index_companies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_name text NOT NULL,
    canonical_name text,
    abbreviation text,
    legal_form text,
    life_cycle_status text,
    products_services text,
    geo_market text,
    headquarter_address text,
    country text,
    jurisdiction text,
    lei character varying(64),
    euid character varying(64),
    bris character varying(64),
    handelsregister character varying(64),
    siren character varying(64),
    llm_model_id character varying(128) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    established_since text,
    succession text,
    generic_corp_id character varying(64),
    generic_corp_registrar text
);


ALTER TABLE celex_facts.llm_index_companies OWNER TO celex_facts;

--
-- TOC entry 259 (class 1259 OID 607345)
-- Name: opencorp_api_daily_calls; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.opencorp_api_daily_calls (
    call_date date NOT NULL,
    call_count integer DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE celex_facts.opencorp_api_daily_calls OWNER TO celex_facts;

--
-- TOC entry 260 (class 1259 OID 607353)
-- Name: opencorp_index_companies; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.opencorp_index_companies (
    id uuid NOT NULL,
    query_id uuid NOT NULL,
    name text NOT NULL,
    company_number character varying(255) NOT NULL,
    jurisdiction_code character varying(64) NOT NULL,
    incorporation_date date,
    dissolution_date date,
    company_type text,
    registry_url text,
    inactive boolean,
    current_status text,
    opencorporates_url text,
    previous_names jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    registered_address_in_full text,
    registered_address_locality text,
    industry_codes jsonb
);


ALTER TABLE celex_facts.opencorp_index_companies OWNER TO celex_facts;

--
-- TOC entry 261 (class 1259 OID 607365)
-- Name: opencorp_index_company_detail; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.opencorp_index_company_detail (
    id uuid NOT NULL,
    jurisdiction_code character varying(64) NOT NULL,
    company_number character varying(255) NOT NULL,
    name text NOT NULL,
    incorporation_date date,
    dissolution_date date,
    company_type text,
    registry_url text,
    inactive boolean,
    current_status text,
    opencorporates_url text,
    agent_name text,
    agent_address text,
    number_of_employees character varying(255),
    native_company_number character varying(255),
    registered_address_in_full text,
    previous_names jsonb,
    alternative_names jsonb,
    identifiers jsonb,
    industry_codes jsonb,
    officers jsonb,
    filings jsonb,
    financial_summary jsonb,
    corporate_groupings jsonb,
    ultimate_beneficial_owners jsonb,
    registered_address jsonb,
    source jsonb,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE celex_facts.opencorp_index_company_detail OWNER TO celex_facts;

--
-- TOC entry 262 (class 1259 OID 607376)
-- Name: opencorp_index_query; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.opencorp_index_query (
    id uuid NOT NULL,
    query_text text NOT NULL,
    normalized_query text NOT NULL,
    jurisdiction_code character varying(64) NOT NULL,
    fetched_pages integer NOT NULL,
    total_pages integer,
    total_count integer,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    error_class character varying(255),
    error_status_code integer,
    error_message text
);


ALTER TABLE celex_facts.opencorp_index_query OWNER TO celex_facts;

--
-- TOC entry 263 (class 1259 OID 607388)
-- Name: pg_embedding_book_qwen; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.pg_embedding_book_qwen (
    id character varying CONSTRAINT pg_embedding_book_qwen3_id_not_null NOT NULL,
    collection_id uuid,
    embedding public.vector,
    document character varying,
    cmetadata jsonb,
    book_name character varying,
    doc_type character varying,
    token_count integer,
    path character varying,
    level integer,
    title character varying,
    text_line integer,
    source character varying,
    chunk_hash character varying,
    ingestion_timestamp timestamp with time zone DEFAULT now(),
    doc_pos integer
);


ALTER TABLE celex_facts.pg_embedding_book_qwen OWNER TO celex_facts;

--
-- TOC entry 264 (class 1259 OID 607395)
-- Name: pg_embedding_qwen; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.pg_embedding_qwen (
    id character varying NOT NULL,
    collection_id uuid,
    embedding public.vector,
    document character varying,
    cmetadata jsonb,
    celex_id character varying,
    doc_type character varying,
    token_count integer,
    path character varying,
    chunk_hash character varying,
    ingestion_timestamp timestamp with time zone DEFAULT now(),
    doc_pos integer,
    context_prefix text,
    section_type character varying,
    document_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig, ((COALESCE(context_prefix, ''::text) || ' '::text) || (document)::text))) STORED
);


ALTER TABLE celex_facts.pg_embedding_qwen OWNER TO celex_facts;

--
-- TOC entry 265 (class 1259 OID 607403)
-- Name: qa_company_delta_vw; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.qa_company_delta_vw AS
 WITH raw AS (
         SELECT facts.celex_id,
            facts.name,
            (facts.value_json)::jsonb AS v
           FROM celex_facts.facts
          WHERE ((facts.name)::text = ANY (ARRAY[('companies_investigated'::character varying)::text, ('companies_canoncial'::character varying)::text]))
        ), items AS (
         SELECT raw.celex_id,
            raw.name,
                CASE
                    WHEN (jsonb_typeof(raw.v) = 'array'::text) THEN raw.v
                    ELSE COALESCE((raw.v -> 'companies'::text), (raw.v -> 'company'::text), (raw.v -> 'companies_investigated'::text), '[]'::jsonb)
                END AS arr
           FROM raw
        ), names AS (
         SELECT i_1.celex_id,
            i_1.name,
            COALESCE((e.value ->> 'name'::text), (e.value ->> 'company'::text), (e.value ->> 'company_name'::text), (e.value ->> 'entity'::text), (e.value #>> '{}'::text[])) AS company
           FROM (items i_1
             CROSS JOIN LATERAL jsonb_array_elements(i_1.arr) e(value))
        ), norm AS (
         SELECT names.celex_id,
            names.name,
            array_agg(DISTINCT lower(btrim(names.company)) ORDER BY (lower(btrim(names.company)))) FILTER (WHERE ((names.company IS NOT NULL) AND (btrim(names.company) <> ''::text))) AS companies
           FROM names
          GROUP BY names.celex_id, names.name
        )
 SELECT COALESCE(i.celex_id, c.celex_id) AS celex_id,
    i.companies AS investigated,
    c.companies AS canonical,
    ARRAY( SELECT unnest(i.companies) AS unnest
        EXCEPT
         SELECT unnest(c.companies) AS unnest) AS only_investigated,
    ARRAY( SELECT unnest(c.companies) AS unnest
        EXCEPT
         SELECT unnest(i.companies) AS unnest) AS only_canonical,
    ((i.companies @> c.companies) AND (c.companies @> i.companies)) AS same_set
   FROM (( SELECT norm.celex_id,
            norm.name,
            norm.companies
           FROM norm
          WHERE ((norm.name)::text = 'companies_investigated'::text)) i
     FULL JOIN ( SELECT norm.celex_id,
            norm.name,
            norm.companies
           FROM norm
          WHERE ((norm.name)::text = 'companies_canoncial'::text)) c ON (((i.celex_id)::text = (c.celex_id)::text)))
  ORDER BY ((i.companies @> c.companies) AND (c.companies @> i.companies)) NULLS FIRST, COALESCE(i.celex_id, c.celex_id);


ALTER VIEW celex_facts.qa_company_delta_vw OWNER TO celex_facts;

--
-- TOC entry 266 (class 1259 OID 607408)
-- Name: query_embeddings; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.query_embeddings (
    query text NOT NULL,
    embedding public.vector,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


ALTER TABLE celex_facts.query_embeddings OWNER TO celex_facts;

--
-- TOC entry 267 (class 1259 OID 607415)
-- Name: query_embeddings_qwen; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.query_embeddings_qwen (
    query text NOT NULL,
    embedding public.vector,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


ALTER TABLE celex_facts.query_embeddings_qwen OWNER TO celex_facts;

--
-- TOC entry 268 (class 1259 OID 607422)
-- Name: question2fact_name; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.question2fact_name AS
 SELECT DISTINCT f.name AS fact_name,
    a.question
   FROM (celex_facts.facts f
     JOIN ( SELECT DISTINCT ON (fact_ingestion_audit_trail.target_fact_name) fact_ingestion_audit_trail.fact_id,
            fact_ingestion_audit_trail.target_fact_name AS fact_name,
            fact_ingestion_audit_trail.question
           FROM celex_facts.fact_ingestion_audit_trail
          ORDER BY fact_ingestion_audit_trail.target_fact_name, fact_ingestion_audit_trail.created_at DESC) a ON (((a.fact_id = f.id) AND (a.fact_name = (f.name)::text))));


ALTER VIEW celex_facts.question2fact_name OWNER TO celex_facts;

--
-- TOC entry 269 (class 1259 OID 607427)
-- Name: question2fact; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.question2fact AS
 SELECT f.id,
    f.celex_id,
    fn.question,
    fn.fact_name,
    f.value_md
   FROM (celex_facts.facts f
     JOIN ( SELECT question2fact_name.question,
            question2fact_name.fact_name
           FROM celex_facts.question2fact_name) fn ON (((fn.fact_name)::text = (f.name)::text)))
  ORDER BY f.celex_id, fn.fact_name;


ALTER VIEW celex_facts.question2fact OWNER TO celex_facts;

--
-- TOC entry 270 (class 1259 OID 607432)
-- Name: question2fact2embeddings; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.question2fact2embeddings (
    fk_fact_id uuid NOT NULL,
    celex_id text,
    question text,
    fact_name text,
    value_txt text,
    embeddings public.vector,
    hash_q_a text,
    ingestion_timestamp timestamp with time zone DEFAULT now()
);


ALTER TABLE celex_facts.question2fact2embeddings OWNER TO celex_facts;

--
-- TOC entry 271 (class 1259 OID 607439)
-- Name: question2fact2embeddings_vw; Type: VIEW; Schema: celex_facts; Owner: postgres
--

CREATE VIEW celex_facts.question2fact2embeddings_vw AS
 SELECT f.id,
    f.celex_id,
    e.fact_name,
    e.question,
    e.value_txt,
    e.hash_q_a,
    e.embeddings
   FROM (celex_facts.question2fact2embeddings e
     JOIN ( SELECT facts.id,
            facts.celex_id,
            facts.name,
            facts.value_md,
            facts.value_json,
            facts.created_at,
            facts.references_doc_json,
            facts.references_fact_json
           FROM celex_facts.facts) f ON ((f.id = e.fk_fact_id)));


ALTER VIEW celex_facts.question2fact2embeddings_vw OWNER TO postgres;

--
-- TOC entry 3834 (class 2604 OID 607466)
-- Name: handelsregister_officers id; Type: DEFAULT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_officers ALTER COLUMN id SET DEFAULT nextval('celex_facts.handelsregister_officers_id_seq'::regclass);


--
-- TOC entry 3835 (class 2604 OID 607467)
-- Name: handelsregister_previous_names id; Type: DEFAULT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_previous_names ALTER COLUMN id SET DEFAULT nextval('celex_facts.handelsregister_previous_names_id_seq'::regclass);


--
-- TOC entry 3876 (class 2606 OID 840533)
-- Name: case_celex_id case_id_celex_case_unique; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.case_celex_id
    ADD CONSTRAINT case_id_celex_case_unique UNIQUE (case_id, celex_id);


--
-- TOC entry 3936 (class 2606 OID 840535)
-- Name: fact_ingestion_audit_trail celex_fact_ingestion_audit_trail_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_ingestion_audit_trail
    ADD CONSTRAINT celex_fact_ingestion_audit_trail_pkey PRIMARY KEY (id);


--
-- TOC entry 3880 (class 2606 OID 840537)
-- Name: company_canonical2alias company_canonical2alias_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2alias
    ADD CONSTRAINT company_canonical2alias_pkey PRIMARY KEY (id);


--
-- TOC entry 3882 (class 2606 OID 840539)
-- Name: company_canonical2alias company_canonical2alias_uniqu; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2alias
    ADD CONSTRAINT company_canonical2alias_uniqu UNIQUE (company_canonical, company_alias);


--
-- TOC entry 3885 (class 2606 OID 840541)
-- Name: company_canonical2celex_id company_canonical2celex_id_name2celexid_uniqu; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2celex_id
    ADD CONSTRAINT company_canonical2celex_id_name2celexid_uniqu UNIQUE (company_name, celex_id);


--
-- TOC entry 3887 (class 2606 OID 840543)
-- Name: company_canonical2celex_id company_canonical2celex_id_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2celex_id
    ADD CONSTRAINT company_canonical2celex_id_pkey PRIMARY KEY (id);


--
-- TOC entry 3889 (class 2606 OID 840545)
-- Name: company_investigated2celex_id company_investigated2celex_id_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_investigated2celex_id
    ADD CONSTRAINT company_investigated2celex_id_pkey PRIMARY KEY (id);


--
-- TOC entry 3891 (class 2606 OID 840547)
-- Name: company_investigated2celex_id company_investigated2celex_id_unique; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_investigated2celex_id
    ADD CONSTRAINT company_investigated2celex_id_unique UNIQUE (company_name, celex_id);


--
-- TOC entry 3894 (class 2606 OID 840549)
-- Name: company_investigated_celex_id2opencorp_index_company company_investigated2opencorp_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_investigated_celex_id2opencorp_index_company
    ADD CONSTRAINT company_investigated2opencorp_pkey PRIMARY KEY (id);


--
-- TOC entry 3896 (class 2606 OID 840551)
-- Name: company_investigated_celex_id2opencorp_index_company company_investigated2opencorp_unique; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_investigated_celex_id2opencorp_index_company
    ADD CONSTRAINT company_investigated2opencorp_unique UNIQUE (fk_company_investigated, fk_opencorp_index_company);


--
-- TOC entry 3903 (class 2606 OID 840553)
-- Name: company_standard2celex_id2alias company_standard2alias_celex_alias_uniqu; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standard2celex_id2alias
    ADD CONSTRAINT company_standard2alias_celex_alias_uniqu UNIQUE (celex_id, company_name, alias);


--
-- TOC entry 3905 (class 2606 OID 840555)
-- Name: company_standard2celex_id2alias company_standard2alias_celex_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standard2celex_id2alias
    ADD CONSTRAINT company_standard2alias_celex_pkey PRIMARY KEY (id);


--
-- TOC entry 3899 (class 2606 OID 840557)
-- Name: company_standard2celex_id company_standard2celex_id_name2celexid_uniqu; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standard2celex_id
    ADD CONSTRAINT company_standard2celex_id_name2celexid_uniqu UNIQUE (company_name, celex_id);


--
-- TOC entry 3901 (class 2606 OID 840559)
-- Name: company_standard2celex_id company_standard2celex_id_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standard2celex_id
    ADD CONSTRAINT company_standard2celex_id_pkey PRIMARY KEY (id);


--
-- TOC entry 3907 (class 2606 OID 840561)
-- Name: corp_lei corp_lei_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.corp_lei
    ADD CONSTRAINT corp_lei_pkey PRIMARY KEY (lei);


--
-- TOC entry 3911 (class 2606 OID 840569)
-- Name: entities entities_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.entities
    ADD CONSTRAINT entities_pkey PRIMARY KEY (entity_id);


--
-- TOC entry 3918 (class 2606 OID 840571)
-- Name: entity_edges entity_edges_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.entity_edges
    ADD CONSTRAINT entity_edges_pkey PRIMARY KEY (edge_id);


--
-- TOC entry 3926 (class 2606 OID 840579)
-- Name: fact2book_checks fact2book_checks_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact2book_checks
    ADD CONSTRAINT fact2book_checks_pkey PRIMARY KEY (id);


--
-- TOC entry 3931 (class 2606 OID 840581)
-- Name: fact_evidence fact_evidence_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_evidence
    ADD CONSTRAINT fact_evidence_pkey PRIMARY KEY (id);


--
-- TOC entry 3938 (class 2606 OID 840583)
-- Name: fact_source_link fact_source_link_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_source_link
    ADD CONSTRAINT fact_source_link_pkey PRIMARY KEY (fact_id, source_id);


--
-- TOC entry 3878 (class 2606 OID 840585)
-- Name: facts facts_pk; Type: CONSTRAINT; Schema: celex_facts; Owner: postgres
--

ALTER TABLE ONLY celex_facts.facts
    ADD CONSTRAINT facts_pk PRIMARY KEY (id);


--
-- TOC entry 4029 (class 2606 OID 841372)
-- Name: gleif_jurisdication2countrycode gleif_jurisdication2countrycode_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.gleif_jurisdication2countrycode
    ADD CONSTRAINT gleif_jurisdication2countrycode_pkey PRIMARY KEY (jurisdiction_code);


--
-- TOC entry 3953 (class 2606 OID 840587)
-- Name: handelsregister_companies handelsregister_companies_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_companies
    ADD CONSTRAINT handelsregister_companies_pkey PRIMARY KEY (company_number);


--
-- TOC entry 3957 (class 2606 OID 840600)
-- Name: handelsregister_company_additional_data handelsregister_company_additional_data_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_company_additional_data
    ADD CONSTRAINT handelsregister_company_additional_data_pkey PRIMARY KEY (company_number);


--
-- TOC entry 3959 (class 2606 OID 840602)
-- Name: handelsregister_officers handelsregister_officers_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_officers
    ADD CONSTRAINT handelsregister_officers_pkey PRIMARY KEY (id);


--
-- TOC entry 3964 (class 2606 OID 840612)
-- Name: handelsregister_previous_names handelsregister_previous_names_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_previous_names
    ADD CONSTRAINT handelsregister_previous_names_pkey PRIMARY KEY (id);


--
-- TOC entry 3967 (class 2606 OID 840614)
-- Name: heideltime_extractions heideltime_extractions_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.heideltime_extractions
    ADD CONSTRAINT heideltime_extractions_pkey PRIMARY KEY (id);


--
-- TOC entry 4027 (class 2606 OID 840616)
-- Name: question2fact2embeddings idx_pk_question2fact2embeddings; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.question2fact2embeddings
    ADD CONSTRAINT idx_pk_question2fact2embeddings PRIMARY KEY (fk_fact_id);


--
-- TOC entry 3975 (class 2606 OID 840618)
-- Name: knowledge_edges knowledge_edges_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_edges
    ADD CONSTRAINT knowledge_edges_pkey PRIMARY KEY (id);


--
-- TOC entry 3978 (class 2606 OID 840620)
-- Name: knowledge_extraction_queue knowledge_extraction_queue_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_extraction_queue
    ADD CONSTRAINT knowledge_extraction_queue_pkey PRIMARY KEY (id);


--
-- TOC entry 3980 (class 2606 OID 840622)
-- Name: knowledge_ingest_audit_trail knowledge_ingest_audit_trail_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_ingest_audit_trail
    ADD CONSTRAINT knowledge_ingest_audit_trail_pkey PRIMARY KEY (id);


--
-- TOC entry 3983 (class 2606 OID 840624)
-- Name: knowledge_items knowledge_items_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_items
    ADD CONSTRAINT knowledge_items_pkey PRIMARY KEY (id);


--
-- TOC entry 3985 (class 2606 OID 840626)
-- Name: knowledge_question_embeddings knowledge_question_embeddings_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_question_embeddings
    ADD CONSTRAINT knowledge_question_embeddings_pkey PRIMARY KEY (id);


--
-- TOC entry 3987 (class 2606 OID 840628)
-- Name: langchain_pg_collection langchain_pg_collection_name_key; Type: CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.langchain_pg_collection
    ADD CONSTRAINT langchain_pg_collection_name_key UNIQUE (name);


--
-- TOC entry 3989 (class 2606 OID 840630)
-- Name: langchain_pg_collection langchain_pg_collection_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.langchain_pg_collection
    ADD CONSTRAINT langchain_pg_collection_pkey PRIMARY KEY (uuid);


--
-- TOC entry 4041 (class 2606 OID 843810)
-- Name: langchain_pg_embedding langchain_pg_embedding_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.langchain_pg_embedding
    ADD CONSTRAINT langchain_pg_embedding_pkey PRIMARY KEY (id);


--
-- TOC entry 3991 (class 2606 OID 840634)
-- Name: legal_provisions legal_provisions_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.legal_provisions
    ADD CONSTRAINT legal_provisions_pkey PRIMARY KEY (id);


--
-- TOC entry 4035 (class 2606 OID 842406)
-- Name: llm_index_companies llm_index_companies_company_name_key; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.llm_index_companies
    ADD CONSTRAINT llm_index_companies_company_name_key UNIQUE (company_name);


--
-- TOC entry 4037 (class 2606 OID 842404)
-- Name: llm_index_companies llm_index_companies_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.llm_index_companies
    ADD CONSTRAINT llm_index_companies_pkey PRIMARY KEY (id);


--
-- TOC entry 3994 (class 2606 OID 840636)
-- Name: opencorp_api_daily_calls opencorp_api_daily_calls_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.opencorp_api_daily_calls
    ADD CONSTRAINT opencorp_api_daily_calls_pkey PRIMARY KEY (call_date);


--
-- TOC entry 3998 (class 2606 OID 840638)
-- Name: opencorp_index_companies opencorp_index_companies_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.opencorp_index_companies
    ADD CONSTRAINT opencorp_index_companies_pkey PRIMARY KEY (id);


--
-- TOC entry 4002 (class 2606 OID 840640)
-- Name: opencorp_index_company_detail opencorp_index_company_detail_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.opencorp_index_company_detail
    ADD CONSTRAINT opencorp_index_company_detail_pkey PRIMARY KEY (id);


--
-- TOC entry 4006 (class 2606 OID 840642)
-- Name: opencorp_index_query opencorp_index_query_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.opencorp_index_query
    ADD CONSTRAINT opencorp_index_query_pkey PRIMARY KEY (id);


--
-- TOC entry 4014 (class 2606 OID 840644)
-- Name: pg_embedding_book_qwen pg_embedding_book_qwen3_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.pg_embedding_book_qwen
    ADD CONSTRAINT pg_embedding_book_qwen3_pkey PRIMARY KEY (id);


--
-- TOC entry 4021 (class 2606 OID 840646)
-- Name: pg_embedding_qwen pg_embedding_qwen_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.pg_embedding_qwen
    ADD CONSTRAINT pg_embedding_qwen_pkey PRIMARY KEY (id);


--
-- TOC entry 4025 (class 2606 OID 840649)
-- Name: query_embeddings_qwen query_embeddings__qwen_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.query_embeddings_qwen
    ADD CONSTRAINT query_embeddings__qwen_pkey PRIMARY KEY (query);


--
-- TOC entry 4023 (class 2606 OID 840651)
-- Name: query_embeddings query_embeddings_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.query_embeddings
    ADD CONSTRAINT query_embeddings_pkey PRIMARY KEY (query);


--
-- TOC entry 3946 (class 2606 OID 840653)
-- Name: fact_source_registry source_registry_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_source_registry
    ADD CONSTRAINT source_registry_pkey PRIMARY KEY (source_id);


--
-- TOC entry 3916 (class 2606 OID 840655)
-- Name: entities uq_entities_kind_natural; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.entities
    ADD CONSTRAINT uq_entities_kind_natural UNIQUE (kind, natural_key);


--
-- TOC entry 3924 (class 2606 OID 840657)
-- Name: entity_edges uq_entity_edges; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.entity_edges
    ADD CONSTRAINT uq_entity_edges UNIQUE (src_entity_id, dst_entity_id, relation, source_fact_id);


--
-- TOC entry 3972 (class 2606 OID 840659)
-- Name: heideltime_extractions uq_heideltime_celex_chunk_tid; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.heideltime_extractions
    ADD CONSTRAINT uq_heideltime_celex_chunk_tid UNIQUE (celex_id, fk_chunk_id, tid);


--
-- TOC entry 4000 (class 2606 OID 840663)
-- Name: opencorp_index_companies uq_opencorp_company_query_co; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.opencorp_index_companies
    ADD CONSTRAINT uq_opencorp_company_query_co UNIQUE (query_id, jurisdiction_code, company_number);


--
-- TOC entry 4004 (class 2606 OID 840669)
-- Name: opencorp_index_company_detail uq_opencorp_detail_co; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.opencorp_index_company_detail
    ADD CONSTRAINT uq_opencorp_detail_co UNIQUE (jurisdiction_code, company_number);


--
-- TOC entry 4008 (class 2606 OID 840671)
-- Name: opencorp_index_query uq_opencorp_query_norm_juris; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.opencorp_index_query
    ADD CONSTRAINT uq_opencorp_query_norm_juris UNIQUE (normalized_query, jurisdiction_code);


--
-- TOC entry 3883 (class 1259 OID 840674)
-- Name: idx_alias_trgm; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_alias_trgm ON celex_facts.company_canonical2alias USING gin (company_alias celex_facts.gin_trgm_ops);


--
-- TOC entry 4009 (class 1259 OID 840675)
-- Name: idx_book_path_prefix; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_book_path_prefix ON celex_facts.pg_embedding_book_qwen USING btree (path text_pattern_ops);


--
-- TOC entry 3954 (class 1259 OID 840677)
-- Name: idx_corp_names_handelsregister_companies; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_corp_names_handelsregister_companies ON celex_facts.handelsregister_companies USING btree (name) WITH (deduplicate_items='true');


--
-- TOC entry 3908 (class 1259 OID 840678)
-- Name: idx_legal_name_trgm_gist; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_legal_name_trgm_gist ON celex_facts.corp_lei USING gist (legal_name celex_facts.gist_trgm_ops);


--
-- TOC entry 3960 (class 1259 OID 840697)
-- Name: idx_officers_company; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_officers_company ON celex_facts.handelsregister_officers USING btree (company_number);


--
-- TOC entry 3961 (class 1259 OID 840698)
-- Name: idx_officers_end_date; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_officers_end_date ON celex_facts.handelsregister_officers USING btree (end_date);


--
-- TOC entry 3962 (class 1259 OID 840699)
-- Name: idx_officers_name; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_officers_name ON celex_facts.handelsregister_officers USING btree (name);


--
-- TOC entry 3965 (class 1259 OID 840700)
-- Name: idx_previous_names_company; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_previous_names_company ON celex_facts.handelsregister_previous_names USING btree (company_number);


--
-- TOC entry 3968 (class 1259 OID 840704)
-- Name: ix_celex_facts_heideltime_extractions_celex_id; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_celex_facts_heideltime_extractions_celex_id ON celex_facts.heideltime_extractions USING btree (celex_id);


--
-- TOC entry 3969 (class 1259 OID 840705)
-- Name: ix_celex_facts_heideltime_extractions_fk_chunk_id; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_celex_facts_heideltime_extractions_fk_chunk_id ON celex_facts.heideltime_extractions USING btree (fk_chunk_id);


--
-- TOC entry 3897 (class 1259 OID 841486)
-- Name: ix_ci2oc_resolution_rung; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_ci2oc_resolution_rung ON celex_facts.company_investigated_celex_id2opencorp_index_company USING btree (resolution_rung);


--
-- TOC entry 3892 (class 1259 OID 841384)
-- Name: ix_company_investigated_jurisdication_code; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_company_investigated_jurisdication_code ON celex_facts.company_investigated2celex_id USING btree (jurisdication_code);


--
-- TOC entry 3909 (class 1259 OID 840706)
-- Name: ix_corp_lei_legal_name_gin_trgm; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_corp_lei_legal_name_gin_trgm ON celex_facts.corp_lei USING gin (legal_name celex_facts.gin_trgm_ops);


--
-- TOC entry 3919 (class 1259 OID 840707)
-- Name: ix_edges_celex; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_edges_celex ON celex_facts.entity_edges USING btree (celex_id);


--
-- TOC entry 3920 (class 1259 OID 840708)
-- Name: ix_edges_dst_rel; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_edges_dst_rel ON celex_facts.entity_edges USING btree (dst_entity_id, relation);


--
-- TOC entry 3921 (class 1259 OID 840709)
-- Name: ix_edges_source_fact; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_edges_source_fact ON celex_facts.entity_edges USING btree (source_fact_id);


--
-- TOC entry 3922 (class 1259 OID 840710)
-- Name: ix_edges_src_rel; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_edges_src_rel ON celex_facts.entity_edges USING btree (src_entity_id, relation);


--
-- TOC entry 3912 (class 1259 OID 840711)
-- Name: ix_entities_celex; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_entities_celex ON celex_facts.entities USING btree (celex_id) WHERE (celex_id IS NOT NULL);


--
-- TOC entry 3913 (class 1259 OID 840712)
-- Name: ix_entities_kind; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_entities_kind ON celex_facts.entities USING btree (kind);


--
-- TOC entry 3914 (class 1259 OID 840713)
-- Name: ix_entities_lei; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_entities_lei ON celex_facts.entities USING btree (lei_id) WHERE (lei_id IS NOT NULL);


--
-- TOC entry 3927 (class 1259 OID 840715)
-- Name: ix_fact2book_checks_celex; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_fact2book_checks_celex ON celex_facts.fact2book_checks USING btree (celex_id);


--
-- TOC entry 3928 (class 1259 OID 840716)
-- Name: ix_fact2book_checks_fact; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_fact2book_checks_fact ON celex_facts.fact2book_checks USING btree (fact_id);


--
-- TOC entry 3932 (class 1259 OID 840717)
-- Name: ix_fact_evidence_fact; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_fact_evidence_fact ON celex_facts.fact_evidence USING btree (fact_id);


--
-- TOC entry 3939 (class 1259 OID 840718)
-- Name: ix_fact_source_link_fact; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_fact_source_link_fact ON celex_facts.fact_source_link USING btree (fact_id);


--
-- TOC entry 3940 (class 1259 OID 840719)
-- Name: ix_fact_source_link_source; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_fact_source_link_source ON celex_facts.fact_source_link USING btree (source_id);


--
-- TOC entry 4030 (class 1259 OID 841374)
-- Name: ix_gleif_juris_country_code; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_gleif_juris_country_code ON celex_facts.gleif_jurisdication2countrycode USING btree (iso_country_code);


--
-- TOC entry 4031 (class 1259 OID 841373)
-- Name: ix_gleif_juris_type; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_gleif_juris_type ON celex_facts.gleif_jurisdication2countrycode USING btree (jurisdiction_type);


--
-- TOC entry 3970 (class 1259 OID 840720)
-- Name: ix_heideltime_celex_chunk; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_heideltime_celex_chunk ON celex_facts.heideltime_extractions USING btree (celex_id, fk_chunk_id);


--
-- Name: ix_heideltime_source_hash; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_heideltime_source_hash ON celex_facts.heideltime_extractions USING btree (fk_chunk_id, source_hash);


--
-- TOC entry 3955 (class 1259 OID 840721)
-- Name: ix_hr_matchkey; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_hr_matchkey ON celex_facts.handelsregister_companies USING btree (regexp_replace(translate(replace(replace(replace(replace(replace(replace(lower(name), 'ä'::text, 'ae'::text), 'ö'::text, 'oe'::text), 'ü'::text, 'ue'::text), 'ß'::text, 'ss'::text), 'æ'::text, 'ae'::text), 'œ'::text, 'oe'::text), 'àáâãåèéêëìíîïòóôõøùúûçñ'::text, 'aaaaaeeeeiiiiooooouuucn'::text), '[^a-z0-9]'::text, ''::text, 'g'::text));


--
-- TOC entry 3973 (class 1259 OID 840722)
-- Name: ix_knowledge_edges_dst_source; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_knowledge_edges_dst_source ON celex_facts.knowledge_edges USING btree (dst_source_id) WHERE (dst_source_id IS NOT NULL);


--
-- TOC entry 3976 (class 1259 OID 840723)
-- Name: ix_knowledge_extraction_queue_status; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_knowledge_extraction_queue_status ON celex_facts.knowledge_extraction_queue USING btree (status) WHERE (status = 'pending'::text);


--
-- TOC entry 4038 (class 1259 OID 843817)
-- Name: ix_langchain_pg_embedding_celex_id; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_langchain_pg_embedding_celex_id ON celex_facts.langchain_pg_embedding USING btree (celex_id);


--
-- TOC entry 4039 (class 1259 OID 843816)
-- Name: ix_langchain_pg_embedding_chunk_hash; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_langchain_pg_embedding_chunk_hash ON celex_facts.langchain_pg_embedding USING btree (chunk_hash);


--
-- TOC entry 4032 (class 1259 OID 842407)
-- Name: ix_llm_index_companies_country; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_llm_index_companies_country ON celex_facts.llm_index_companies USING btree (country);


--
-- TOC entry 4033 (class 1259 OID 842408)
-- Name: ix_llm_index_companies_lei; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_llm_index_companies_lei ON celex_facts.llm_index_companies USING btree (lei);


--
-- TOC entry 3995 (class 1259 OID 840726)
-- Name: ix_opencorp_company_juris_number; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_opencorp_company_juris_number ON celex_facts.opencorp_index_companies USING btree (jurisdiction_code, company_number);


--
-- TOC entry 3996 (class 1259 OID 840727)
-- Name: ix_opencorp_company_name; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_opencorp_company_name ON celex_facts.opencorp_index_companies USING btree (name);


--
-- TOC entry 4010 (class 1259 OID 840728)
-- Name: ix_pg_embedding_book_qwen3_book_name; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_book_qwen3_book_name ON celex_facts.pg_embedding_book_qwen USING btree (book_name);


--
-- TOC entry 4011 (class 1259 OID 840729)
-- Name: ix_pg_embedding_book_qwen3_chunk_hash; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_book_qwen3_chunk_hash ON celex_facts.pg_embedding_book_qwen USING btree (chunk_hash);


--
-- TOC entry 4012 (class 1259 OID 840730)
-- Name: ix_pg_embedding_book_qwen3_level; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_book_qwen3_level ON celex_facts.pg_embedding_book_qwen USING btree (level);


--
-- TOC entry 4015 (class 1259 OID 840731)
-- Name: ix_pg_embedding_qwen_celex_id; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_qwen_celex_id ON celex_facts.pg_embedding_qwen USING btree (celex_id);


--
-- TOC entry 4016 (class 1259 OID 840732)
-- Name: ix_pg_embedding_qwen_chunk_hash; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_qwen_chunk_hash ON celex_facts.pg_embedding_qwen USING btree (chunk_hash);


--
-- TOC entry 4017 (class 1259 OID 840733)
-- Name: ix_pg_embedding_qwen_section_type; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_qwen_section_type ON celex_facts.pg_embedding_qwen USING btree (section_type);


--
-- TOC entry 3941 (class 1259 OID 840734)
-- Name: ix_source_registry_celex; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_source_registry_celex ON celex_facts.fact_source_registry USING btree (celex_id);


--
-- TOC entry 3942 (class 1259 OID 840735)
-- Name: ix_source_registry_chunk_id; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_source_registry_chunk_id ON celex_facts.fact_source_registry USING btree (chunk_id) WHERE (chunk_id IS NOT NULL);


--
-- TOC entry 3943 (class 1259 OID 840736)
-- Name: ix_source_registry_heideltime; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_source_registry_heideltime ON celex_facts.fact_source_registry USING btree (heideltime_event_id) WHERE (heideltime_event_id IS NOT NULL);


--
-- TOC entry 3944 (class 1259 OID 840737)
-- Name: ix_source_registry_type; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_source_registry_type ON celex_facts.fact_source_registry USING btree (source_type);


--
-- TOC entry 3981 (class 1259 OID 840738)
-- Name: knowledge_items_knowledge_tsv_idx; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX knowledge_items_knowledge_tsv_idx ON celex_facts.knowledge_items USING gin (knowledge_tsv);


--
-- TOC entry 4018 (class 1259 OID 840739)
-- Name: pg_embedding_qwen_document_trgm_gin; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX pg_embedding_qwen_document_trgm_gin ON celex_facts.pg_embedding_qwen USING gin (celex_facts.f_unaccent((document)::text) celex_facts.gin_trgm_ops);


--
-- TOC entry 4019 (class 1259 OID 840740)
-- Name: pg_embedding_qwen_document_tsv_gin; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX pg_embedding_qwen_document_tsv_gin ON celex_facts.pg_embedding_qwen USING gin (document_tsv);


--
-- TOC entry 3929 (class 1259 OID 843799)
-- Name: uq_fact2book_checks; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_fact2book_checks ON celex_facts.fact2book_checks USING btree (fact_id, celex_id);


--
-- TOC entry 3933 (class 1259 OID 840742)
-- Name: uq_fact_evidence_chunk; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_fact_evidence_chunk ON celex_facts.fact_evidence USING btree (fact_id, chunk_id) WHERE (chunk_id IS NOT NULL);


--
-- TOC entry 3934 (class 1259 OID 840743)
-- Name: uq_fact_evidence_summary; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_fact_evidence_summary ON celex_facts.fact_evidence USING btree (fact_id) WHERE (chunk_id IS NULL);


--
-- TOC entry 3992 (class 1259 OID 840744)
-- Name: uq_legal_provisions_article_treaty; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_legal_provisions_article_treaty ON celex_facts.legal_provisions USING btree (article_number, treaty);


--
-- TOC entry 3947 (class 1259 OID 840745)
-- Name: uq_source_registry_chunk; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_source_registry_chunk ON celex_facts.fact_source_registry USING btree (source_type, chunk_id) WHERE (chunk_id IS NOT NULL);


--
-- TOC entry 3948 (class 1259 OID 840746)
-- Name: uq_source_registry_csv; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_source_registry_csv ON celex_facts.fact_source_registry USING btree (celex_id, csv_row) WHERE (csv_row IS NOT NULL);


--
-- TOC entry 3949 (class 1259 OID 840747)
-- Name: uq_source_registry_fact; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_source_registry_fact ON celex_facts.fact_source_registry USING btree (source_fact_id) WHERE (source_fact_id IS NOT NULL);


--
-- TOC entry 3950 (class 1259 OID 840748)
-- Name: uq_source_registry_heideltime; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_source_registry_heideltime ON celex_facts.fact_source_registry USING btree (heideltime_event_id) WHERE (heideltime_event_id IS NOT NULL);


--
-- TOC entry 3951 (class 1259 OID 840749)
-- Name: uq_source_registry_lei; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE UNIQUE INDEX uq_source_registry_lei ON celex_facts.fact_source_registry USING btree (lei_id) WHERE (lei_id IS NOT NULL);


--
-- TOC entry 4067 (class 2620 OID 840750)
-- Name: knowledge_items trg_knowledge_grounding; Type: TRIGGER; Schema: celex_facts; Owner: celex_facts
--

CREATE CONSTRAINT TRIGGER trg_knowledge_grounding AFTER INSERT ON celex_facts.knowledge_items DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION celex_facts.trg_knowledge_must_have_edge();


--
-- TOC entry 4050 (class 2606 OID 840752)
-- Name: fact_ingestion_audit_trail celex_fact_ingestion_audit_trail_fact_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_ingestion_audit_trail
    ADD CONSTRAINT celex_fact_ingestion_audit_trail_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES celex_facts.facts(id) ON DELETE CASCADE;


--
-- TOC entry 4042 (class 2606 OID 840757)
-- Name: company_canonical2alias company_canonical2alias_fk_company_canonical_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2alias
    ADD CONSTRAINT company_canonical2alias_fk_company_canonical_fkey FOREIGN KEY (fk_company_canonical) REFERENCES celex_facts.company_canonical2celex_id(id);


--
-- TOC entry 4045 (class 2606 OID 840762)
-- Name: entity_edges entity_edges_dst_entity_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.entity_edges
    ADD CONSTRAINT entity_edges_dst_entity_id_fkey FOREIGN KEY (dst_entity_id) REFERENCES celex_facts.entities(entity_id) ON DELETE CASCADE;


--
-- TOC entry 4046 (class 2606 OID 840767)
-- Name: entity_edges entity_edges_source_fact_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.entity_edges
    ADD CONSTRAINT entity_edges_source_fact_id_fkey FOREIGN KEY (source_fact_id) REFERENCES celex_facts.facts(id) ON DELETE CASCADE;


--
-- TOC entry 4047 (class 2606 OID 840772)
-- Name: entity_edges entity_edges_src_entity_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.entity_edges
    ADD CONSTRAINT entity_edges_src_entity_id_fkey FOREIGN KEY (src_entity_id) REFERENCES celex_facts.entities(entity_id) ON DELETE CASCADE;


--
-- TOC entry 4048 (class 2606 OID 840777)
-- Name: fact2book_checks fact2book_mismatch_fact_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact2book_checks
    ADD CONSTRAINT fact2book_mismatch_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES celex_facts.facts(id) ON DELETE CASCADE;


--
-- TOC entry 4049 (class 2606 OID 840782)
-- Name: fact_evidence fact_evidence_fact_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_evidence
    ADD CONSTRAINT fact_evidence_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES celex_facts.facts(id) ON DELETE CASCADE;


--
-- TOC entry 4051 (class 2606 OID 840787)
-- Name: fact_source_link fact_source_link_fact_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_source_link
    ADD CONSTRAINT fact_source_link_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES celex_facts.facts(id) ON DELETE CASCADE;


--
-- TOC entry 4052 (class 2606 OID 840792)
-- Name: fact_source_link fact_source_link_source_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_source_link
    ADD CONSTRAINT fact_source_link_source_id_fkey FOREIGN KEY (source_id) REFERENCES celex_facts.fact_source_registry(source_id);


--
-- TOC entry 4043 (class 2606 OID 840797)
-- Name: company_investigated_celex_id2opencorp_index_company fk_ci2oc_company_investigated; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_investigated_celex_id2opencorp_index_company
    ADD CONSTRAINT fk_ci2oc_company_investigated FOREIGN KEY (fk_company_investigated) REFERENCES celex_facts.company_investigated2celex_id(id) ON DELETE CASCADE;


--
-- TOC entry 4044 (class 2606 OID 840802)
-- Name: company_investigated_celex_id2opencorp_index_company fk_ci2oc_opencorp_company; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_investigated_celex_id2opencorp_index_company
    ADD CONSTRAINT fk_ci2oc_opencorp_company FOREIGN KEY (fk_opencorp_index_company) REFERENCES celex_facts.opencorp_index_companies(id) ON DELETE CASCADE;


--
-- TOC entry 4054 (class 2606 OID 840807)
-- Name: handelsregister_company_additional_data handelsregister_company_additional_data_company_number_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_company_additional_data
    ADD CONSTRAINT handelsregister_company_additional_data_company_number_fkey FOREIGN KEY (company_number) REFERENCES celex_facts.handelsregister_companies(company_number) ON DELETE CASCADE;


--
-- TOC entry 4055 (class 2606 OID 840812)
-- Name: handelsregister_officers handelsregister_officers_company_number_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_officers
    ADD CONSTRAINT handelsregister_officers_company_number_fkey FOREIGN KEY (company_number) REFERENCES celex_facts.handelsregister_companies(company_number) ON DELETE CASCADE;


--
-- TOC entry 4056 (class 2606 OID 840817)
-- Name: handelsregister_previous_names handelsregister_previous_names_company_number_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.handelsregister_previous_names
    ADD CONSTRAINT handelsregister_previous_names_company_number_fkey FOREIGN KEY (company_number) REFERENCES celex_facts.handelsregister_companies(company_number) ON DELETE CASCADE;


--
-- TOC entry 4057 (class 2606 OID 840822)
-- Name: knowledge_edges knowledge_edges_dst_fact_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_edges
    ADD CONSTRAINT knowledge_edges_dst_fact_id_fkey FOREIGN KEY (dst_fact_id) REFERENCES celex_facts.facts(id);


--
-- TOC entry 4058 (class 2606 OID 840827)
-- Name: knowledge_edges knowledge_edges_dst_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_edges
    ADD CONSTRAINT knowledge_edges_dst_knowledge_id_fkey FOREIGN KEY (dst_knowledge_id) REFERENCES celex_facts.knowledge_items(id);


--
-- TOC entry 4059 (class 2606 OID 840832)
-- Name: knowledge_edges knowledge_edges_dst_source_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_edges
    ADD CONSTRAINT knowledge_edges_dst_source_id_fkey FOREIGN KEY (dst_source_id) REFERENCES celex_facts.fact_source_registry(source_id);


--
-- TOC entry 4060 (class 2606 OID 840837)
-- Name: knowledge_edges knowledge_edges_src_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_edges
    ADD CONSTRAINT knowledge_edges_src_knowledge_id_fkey FOREIGN KEY (src_knowledge_id) REFERENCES celex_facts.knowledge_items(id);


--
-- TOC entry 4061 (class 2606 OID 840842)
-- Name: knowledge_ingest_audit_trail knowledge_ingest_audit_trail_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_ingest_audit_trail
    ADD CONSTRAINT knowledge_ingest_audit_trail_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES celex_facts.knowledge_items(id);


--
-- TOC entry 4062 (class 2606 OID 840847)
-- Name: knowledge_ingest_audit_trail knowledge_ingest_audit_trail_queue_item_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_ingest_audit_trail
    ADD CONSTRAINT knowledge_ingest_audit_trail_queue_item_id_fkey FOREIGN KEY (queue_item_id) REFERENCES celex_facts.knowledge_extraction_queue(id);


--
-- TOC entry 4063 (class 2606 OID 840852)
-- Name: knowledge_question_embeddings knowledge_question_embeddings_knowledge_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.knowledge_question_embeddings
    ADD CONSTRAINT knowledge_question_embeddings_knowledge_id_fkey FOREIGN KEY (knowledge_id) REFERENCES celex_facts.knowledge_items(id) ON DELETE CASCADE;


--
-- TOC entry 4066 (class 2606 OID 843811)
-- Name: langchain_pg_embedding langchain_pg_embedding_collection_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.langchain_pg_embedding
    ADD CONSTRAINT langchain_pg_embedding_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES celex_facts.langchain_pg_collection(uuid) ON DELETE CASCADE;


--
-- TOC entry 4064 (class 2606 OID 840862)
-- Name: opencorp_index_companies opencorp_index_companies_query_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.opencorp_index_companies
    ADD CONSTRAINT opencorp_index_companies_query_id_fkey FOREIGN KEY (query_id) REFERENCES celex_facts.opencorp_index_query(id) ON DELETE CASCADE;


--
-- TOC entry 4065 (class 2606 OID 840867)
-- Name: pg_embedding_book_qwen pg_embedding_book_qwen3_collection_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.pg_embedding_book_qwen
    ADD CONSTRAINT pg_embedding_book_qwen3_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES celex_facts.langchain_pg_collection(uuid) ON DELETE CASCADE;


--
-- TOC entry 4053 (class 2606 OID 840872)
-- Name: fact_source_registry source_registry_source_fact_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_source_registry
    ADD CONSTRAINT source_registry_source_fact_id_fkey FOREIGN KEY (source_fact_id) REFERENCES celex_facts.facts(id) ON DELETE CASCADE;


-- Completed on 2026-08-28 17:42:49 UTC

--
-- PostgreSQL database dump complete
--

\unrestrict aIDCHcpA7cDIha6Z3yadNp5z67OzbvULUsgPp1FfN1eNXbLBZPgy0WZeE84yNZr

