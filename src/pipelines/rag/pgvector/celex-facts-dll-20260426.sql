--
-- PostgreSQL database dump
--

\restrict CzCzGHtaw7GXWM4z9aY79ZfeKEMsIklP4WSbIQVjcYOHzcbFvjRA2L1DyRvUhj0

-- Dumped from database version 18.1 (Debian 18.1-1.pgdg13+2)
-- Dumped by pg_dump version 18.1

-- Started on 2026-04-26 19:12:14 UTC

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

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- TOC entry 252 (class 1259 OID 387300)
-- Name: case_celex_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.case_celex_id (
    case_id text NOT NULL,
    celex_id text NOT NULL
);


ALTER TABLE celex_facts.case_celex_id OWNER TO celex_facts;

--
-- TOC entry 223 (class 1259 OID 250067)
-- Name: celex_case_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.celex_case_id (
    celex_id text NOT NULL,
    case_x text,
    case_id text
);


ALTER TABLE celex_facts.celex_case_id OWNER TO celex_facts;

--
-- TOC entry 224 (class 1259 OID 250073)
-- Name: fact_ingestion_audit_trail; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.fact_ingestion_audit_trail (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
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
-- TOC entry 225 (class 1259 OID 250080)
-- Name: company_canonical2alias; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_canonical2alias (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_canonical text,
    company_alias text,
    fk_company_canonical uuid
);


ALTER TABLE celex_facts.company_canonical2alias OWNER TO celex_facts;

--
-- TOC entry 226 (class 1259 OID 250087)
-- Name: company_canonical2celex_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_canonical2celex_id (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_name text,
    celex_id text
);


ALTER TABLE celex_facts.company_canonical2celex_id OWNER TO celex_facts;

--
-- TOC entry 227 (class 1259 OID 250094)
-- Name: company_standard2celex_id; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_standard2celex_id (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    company_name text,
    celex_id text
);


ALTER TABLE celex_facts.company_standard2celex_id OWNER TO celex_facts;

--
-- TOC entry 228 (class 1259 OID 250101)
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
-- TOC entry 229 (class 1259 OID 250108)
-- Name: company_standardized; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.company_standardized (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text
);


ALTER TABLE celex_facts.company_standardized OWNER TO celex_facts;

--
-- TOC entry 253 (class 1259 OID 425535)
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
-- TOC entry 230 (class 1259 OID 250115)
-- Name: langchain_pg_embedding; Type: TABLE; Schema: celex_facts; Owner: ai
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
    ingestion_timestamp timestamp with time zone DEFAULT now(),
    doc_pos integer
);


ALTER TABLE celex_facts.langchain_pg_embedding OWNER TO ai;

--
-- TOC entry 231 (class 1259 OID 250122)
-- Name: docs; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.docs AS
 SELECT id,
    document,
    doc_pos,
    cmetadata,
    celex_id,
    doc_type,
    token_count,
    path,
    chunk_hash
   FROM celex_facts.langchain_pg_embedding;


ALTER VIEW celex_facts.docs OWNER TO celex_facts;

--
-- TOC entry 232 (class 1259 OID 250126)
-- Name: events_extraction; Type: TABLE; Schema: celex_facts; Owner: ai
--

CREATE TABLE celex_facts.events_extraction (
    id uuid NOT NULL,
    celex_id character varying NOT NULL,
    fk_chunk_id character varying,
    extract_json jsonb NOT NULL,
    extraction_class character varying,
    extraction_text character varying,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE celex_facts.events_extraction OWNER TO ai;

--
-- TOC entry 233 (class 1259 OID 250135)
-- Name: events_extraction_attributes; Type: TABLE; Schema: celex_facts; Owner: ai
--

CREATE TABLE celex_facts.events_extraction_attributes (
    id uuid NOT NULL,
    event_id uuid,
    key character varying NOT NULL,
    value character varying
);


ALTER TABLE celex_facts.events_extraction_attributes OWNER TO ai;

--
-- TOC entry 234 (class 1259 OID 250142)
-- Name: events_extraction_attributes_vw; Type: VIEW; Schema: celex_facts; Owner: postgres
--

CREATE VIEW celex_facts.events_extraction_attributes_vw AS
 SELECT e.id AS event_extraction_id,
    e.celex_id,
    e.fk_chunk_id,
    e.extract_json,
    e.extraction_class,
    e.extraction_text,
    e.created_at,
    a.id AS attribute_id,
    a.key,
    a.value
   FROM (celex_facts.events_extraction e
     JOIN celex_facts.events_extraction_attributes a ON ((a.event_id = e.id)));


ALTER VIEW celex_facts.events_extraction_attributes_vw OWNER TO postgres;

--
-- TOC entry 235 (class 1259 OID 250146)
-- Name: events_in_chunks; Type: TABLE; Schema: celex_facts; Owner: ai
--

CREATE TABLE celex_facts.events_in_chunks (
    id uuid NOT NULL,
    celex_id character varying NOT NULL,
    fk_chunk_id character varying,
    standardized_date date,
    original_date character varying,
    is_fuzzy boolean,
    event_description text,
    is_legal_assessent boolean,
    sentence text,
    extract_json jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


ALTER TABLE celex_facts.events_in_chunks OWNER TO ai;

--
-- TOC entry 236 (class 1259 OID 250155)
-- Name: facts; Type: TABLE; Schema: celex_facts; Owner: postgres
--

CREATE TABLE celex_facts.facts (
    id uuid NOT NULL,
    celex_id character varying NOT NULL,
    name character varying,
    value_md character varying,
    value_json character varying,
    created_at timestamp without time zone,
    is_valid boolean,
    references_doc_json jsonb,
    references_fact_json jsonb
);


ALTER TABLE celex_facts.facts OWNER TO postgres;

--
-- TOC entry 237 (class 1259 OID 250162)
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
    a.duration
   FROM (celex_facts.fact_ingestion_audit_trail a
     JOIN ( SELECT facts.id,
            facts.value_md,
            facts.value_json
           FROM celex_facts.facts) f ON (((f.id)::text = (a.fact_id)::text)))
  ORDER BY a.created_at DESC;


ALTER VIEW celex_facts.fact_ingestion_audit_trail_vw OWNER TO celex_facts;

--
-- TOC entry 238 (class 1259 OID 250167)
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
            facts.created_at,
            facts.is_valid
           FROM celex_facts.facts
          WHERE ((facts.name)::text = 'legal_assessment_period'::text)) l ON (((l.celex_id)::text = (c.celex_id)::text)))
  WHERE ((c.name)::text = 'cartel_period'::text);


ALTER VIEW celex_facts.facts_cartel_legal_period_vw OWNER TO celex_facts;

--
-- TOC entry 239 (class 1259 OID 250172)
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
-- TOC entry 240 (class 1259 OID 250177)
-- Name: langchain_pg_collection; Type: TABLE; Schema: celex_facts; Owner: ai
--

CREATE TABLE celex_facts.langchain_pg_collection (
    uuid uuid NOT NULL,
    name character varying NOT NULL,
    cmetadata json
);


ALTER TABLE celex_facts.langchain_pg_collection OWNER TO ai;

--
-- TOC entry 255 (class 1259 OID 429464)
-- Name: lei2company_canonical2alias_matches_mv; Type: MATERIALIZED VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE MATERIALIZED VIEW celex_facts.lei2company_canonical2alias_matches_mv AS
 SELECT DISTINCT ON (c2.company_alias) c2.company_alias,
    c1.legal_name,
    ((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias) AS distance
   FROM (celex_facts.company_canonical2alias c2
     CROSS JOIN LATERAL ( SELECT corp_lei.legal_name
           FROM celex_facts.corp_lei
          ORDER BY ((corp_lei.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias)
         LIMIT 3) c1)
  WHERE (((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias) < (0.3)::double precision)
  ORDER BY c2.company_alias, ((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias)
  WITH NO DATA;


ALTER MATERIALIZED VIEW celex_facts.lei2company_canonical2alias_matches_mv OWNER TO celex_facts;

--
-- TOC entry 254 (class 1259 OID 429452)
-- Name: lei2company_canonical2alias_matches_vw_sss; Type: MATERIALIZED VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE MATERIALIZED VIEW celex_facts.lei2company_canonical2alias_matches_vw_sss AS
 SELECT DISTINCT ON (c2.company_alias) c2.company_alias,
    c1.legal_name,
    ((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias) AS distance
   FROM (celex_facts.company_canonical2alias c2
     CROSS JOIN LATERAL ( SELECT corp_lei.legal_name
           FROM celex_facts.corp_lei
          ORDER BY ((corp_lei.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias)
         LIMIT 2) c1)
  WHERE ((((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias) < (0.3)::double precision) AND (c2.company_alias = 'Telefunken'::text))
  ORDER BY c2.company_alias, ((c1.legal_name)::text OPERATOR(celex_facts.<->) c2.company_alias)
  WITH NO DATA;


ALTER MATERIALIZED VIEW celex_facts.lei2company_canonical2alias_matches_vw_sss OWNER TO celex_facts;

--
-- TOC entry 256 (class 1259 OID 430939)
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
-- TOC entry 251 (class 1259 OID 386364)
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
-- TOC entry 249 (class 1259 OID 325595)
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
    doc_pos integer
);


ALTER TABLE celex_facts.pg_embedding_qwen OWNER TO celex_facts;

--
-- TOC entry 241 (class 1259 OID 250191)
-- Name: query_embeddings; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.query_embeddings (
    query text NOT NULL,
    embedding public.vector,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


ALTER TABLE celex_facts.query_embeddings OWNER TO celex_facts;

--
-- TOC entry 242 (class 1259 OID 250198)
-- Name: query_embeddings_qwen; Type: TABLE; Schema: celex_facts; Owner: celex_facts
--

CREATE TABLE celex_facts.query_embeddings_qwen (
    query text NOT NULL,
    embedding public.vector,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);


ALTER TABLE celex_facts.query_embeddings_qwen OWNER TO celex_facts;

--
-- TOC entry 243 (class 1259 OID 250205)
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
-- TOC entry 244 (class 1259 OID 250210)
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
-- TOC entry 245 (class 1259 OID 250215)
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
-- TOC entry 246 (class 1259 OID 250222)
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
            facts.is_valid,
            facts.references_doc_json,
            facts.references_fact_json
           FROM celex_facts.facts) f ON ((f.id = e.fk_fact_id)));


ALTER VIEW celex_facts.question2fact2embeddings_vw OWNER TO postgres;

--
-- TOC entry 247 (class 1259 OID 250227)
-- Name: timeline_chunks_extracted_vw; Type: VIEW; Schema: celex_facts; Owner: postgres
--

CREATE VIEW celex_facts.timeline_chunks_extracted_vw AS
 SELECT k.id,
    k.celex_id,
    k.document,
    sum(k.token_count) AS token_sum,
    json_agg(json_build_object('class', e.extraction_class, 'text', e.extraction_text)) AS extraction_data
   FROM (celex_facts.events_extraction e
     JOIN ( SELECT langchain_pg_embedding.id,
            langchain_pg_embedding.celex_id,
            langchain_pg_embedding.document,
            langchain_pg_embedding.token_count
           FROM celex_facts.langchain_pg_embedding
          ORDER BY langchain_pg_embedding.ingestion_timestamp) k ON ((((k.celex_id)::text = (e.celex_id)::text) AND ((k.id)::text = (e.fk_chunk_id)::text))))
  WHERE ((1 = 1) AND ((e.extraction_class)::text = ANY (ARRAY[('time'::character varying)::text, ('date'::character varying)::text])))
  GROUP BY k.id, k.celex_id, k.document;


ALTER VIEW celex_facts.timeline_chunks_extracted_vw OWNER TO postgres;

--
-- TOC entry 248 (class 1259 OID 250232)
-- Name: timeline_chunks_vw; Type: VIEW; Schema: celex_facts; Owner: postgres
--

CREATE VIEW celex_facts.timeline_chunks_vw AS
 SELECT e.id AS event_chunk_id,
    e.standardized_date,
    e.original_date,
    e.is_fuzzy,
    e.is_legal_assessent,
    e.event_description,
    e.sentence,
    e.fk_chunk_id,
    k.id,
    k.celex_id
   FROM (celex_facts.events_in_chunks e
     JOIN ( SELECT langchain_pg_embedding.id,
            langchain_pg_embedding.celex_id,
            langchain_pg_embedding.document,
            langchain_pg_embedding.token_count
           FROM celex_facts.langchain_pg_embedding
          ORDER BY langchain_pg_embedding.ingestion_timestamp) k ON ((((k.celex_id)::text = (e.celex_id)::text) AND ((k.id)::text = (e.fk_chunk_id)::text))))
  WHERE ((1 = 1) AND (e.standardized_date IS NOT NULL) AND (NOT (e.event_description = 'No event linked to date'::text)));


ALTER VIEW celex_facts.timeline_chunks_vw OWNER TO postgres;

--
-- TOC entry 257 (class 1259 OID 432577)
-- Name: timeline_table_vw; Type: VIEW; Schema: celex_facts; Owner: celex_facts
--

CREATE VIEW celex_facts.timeline_table_vw AS
 SELECT t.event_chunk_id,
    t.original_date,
    t.standardized_date,
    t.event_description,
    t.is_legal_assessent,
    t.fk_chunk_id,
    t.celex_id
   FROM (celex_facts.timeline_chunks_vw t
     LEFT JOIN ( SELECT events_extraction_attributes_vw.celex_id,
            celex_facts.smart_to_date((events_extraction_attributes_vw.extraction_text)::text) AS standardized_date,
            events_extraction_attributes_vw.extraction_text,
            events_extraction_attributes_vw.value,
            events_extraction_attributes_vw.fk_chunk_id
           FROM celex_facts.events_extraction_attributes_vw
          WHERE ((events_extraction_attributes_vw.extraction_class)::text = 'time'::text)) e ON ((((e.celex_id)::text = (t.celex_id)::text) AND ((e.fk_chunk_id)::text = (t.fk_chunk_id)::text) AND (e.standardized_date = t.standardized_date))))
  WHERE ((1 = 1) AND (NOT (EXTRACT(year FROM t.standardized_date) = (2026)::numeric)))
  ORDER BY t.standardized_date;


ALTER VIEW celex_facts.timeline_table_vw OWNER TO celex_facts;

--
-- TOC entry 3754 (class 2606 OID 387308)
-- Name: case_celex_id case_id_celex_case_unique; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.case_celex_id
    ADD CONSTRAINT case_id_celex_case_unique UNIQUE (case_id, celex_id);


--
-- TOC entry 3693 (class 2606 OID 325456)
-- Name: fact_ingestion_audit_trail fact_ingestion_audit_trail_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_ingestion_audit_trail
    ADD CONSTRAINT fact_ingestion_audit_trail_pkey PRIMARY KEY (id);


--
-- TOC entry 3695 (class 2606 OID 325458)
-- Name: company_canonical2alias company_canonical2alias_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2alias
    ADD CONSTRAINT company_canonical2alias_pkey PRIMARY KEY (id);


--
-- TOC entry 3697 (class 2606 OID 325460)
-- Name: company_canonical2alias company_canonical2alias_uniqu; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2alias
    ADD CONSTRAINT company_canonical2alias_uniqu UNIQUE (company_canonical, company_alias);


--
-- TOC entry 3700 (class 2606 OID 325462)
-- Name: company_canonical2celex_id company_canonical2celex_id_name2celexid_uniqu; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2celex_id
    ADD CONSTRAINT company_canonical2celex_id_name2celexid_uniqu UNIQUE (company_name, celex_id);


--
-- TOC entry 3702 (class 2606 OID 325464)
-- Name: company_canonical2celex_id company_canonical2celex_id_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2celex_id
    ADD CONSTRAINT company_canonical2celex_id_pkey PRIMARY KEY (id);


--
-- TOC entry 3708 (class 2606 OID 325466)
-- Name: company_standard2celex_id2alias company_standard2alias_celex_alias_uniqu; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standard2celex_id2alias
    ADD CONSTRAINT company_standard2alias_celex_alias_uniqu UNIQUE (celex_id, company_name, alias);


--
-- TOC entry 3710 (class 2606 OID 325468)
-- Name: company_standard2celex_id2alias company_standard2alias_celex_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standard2celex_id2alias
    ADD CONSTRAINT company_standard2alias_celex_pkey PRIMARY KEY (id);


--
-- TOC entry 3704 (class 2606 OID 325470)
-- Name: company_standard2celex_id company_standard2celex_id_name2celexid_uniqu; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standard2celex_id
    ADD CONSTRAINT company_standard2celex_id_name2celexid_uniqu UNIQUE (company_name, celex_id);


--
-- TOC entry 3706 (class 2606 OID 325472)
-- Name: company_standard2celex_id company_standard2celex_id_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standard2celex_id
    ADD CONSTRAINT company_standard2celex_id_pkey PRIMARY KEY (id);


--
-- TOC entry 3712 (class 2606 OID 325474)
-- Name: company_standardized company_standardized_name_unique; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standardized
    ADD CONSTRAINT company_standardized_name_unique UNIQUE (name);


--
-- TOC entry 3714 (class 2606 OID 325476)
-- Name: company_standardized company_standardized_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_standardized
    ADD CONSTRAINT company_standardized_pkey PRIMARY KEY (id);


--
-- TOC entry 3756 (class 2606 OID 425542)
-- Name: corp_lei corp_lei_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.corp_lei
    ADD CONSTRAINT corp_lei_pkey PRIMARY KEY (lei);


--
-- TOC entry 3726 (class 2606 OID 325478)
-- Name: events_extraction_attributes events_extraction_attributes_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.events_extraction_attributes
    ADD CONSTRAINT events_extraction_attributes_pkey PRIMARY KEY (id);


--
-- TOC entry 3722 (class 2606 OID 325480)
-- Name: events_extraction events_extraction_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.events_extraction
    ADD CONSTRAINT events_extraction_pkey PRIMARY KEY (id);


--
-- TOC entry 3729 (class 2606 OID 325482)
-- Name: events_in_chunks events_in_chunks_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.events_in_chunks
    ADD CONSTRAINT events_in_chunks_pkey PRIMARY KEY (id);


--
-- TOC entry 3732 (class 2606 OID 325484)
-- Name: facts facts_pk; Type: CONSTRAINT; Schema: celex_facts; Owner: postgres
--

ALTER TABLE ONLY celex_facts.facts
    ADD CONSTRAINT facts_pk PRIMARY KEY (id);


--
-- TOC entry 3742 (class 2606 OID 325486)
-- Name: question2fact2embeddings idx_pk_question2fact2embeddings; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.question2fact2embeddings
    ADD CONSTRAINT idx_pk_question2fact2embeddings PRIMARY KEY (fk_fact_id);


--
-- TOC entry 3734 (class 2606 OID 325488)
-- Name: langchain_pg_collection langchain_pg_collection_name_key; Type: CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.langchain_pg_collection
    ADD CONSTRAINT langchain_pg_collection_name_key UNIQUE (name);


--
-- TOC entry 3736 (class 2606 OID 325490)
-- Name: langchain_pg_collection langchain_pg_collection_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.langchain_pg_collection
    ADD CONSTRAINT langchain_pg_collection_pkey PRIMARY KEY (uuid);


--
-- TOC entry 3720 (class 2606 OID 325492)
-- Name: langchain_pg_embedding langchain_pg_embedding_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.langchain_pg_embedding
    ADD CONSTRAINT langchain_pg_embedding_pkey PRIMARY KEY (id);


--
-- TOC entry 3752 (class 2606 OID 386372)
-- Name: pg_embedding_book_qwen pg_embedding_book_qwen3_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.pg_embedding_book_qwen
    ADD CONSTRAINT pg_embedding_book_qwen3_pkey PRIMARY KEY (id);


--
-- TOC entry 3746 (class 2606 OID 325603)
-- Name: pg_embedding_qwen pg_embedding_qwen_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.pg_embedding_qwen
    ADD CONSTRAINT pg_embedding_qwen_pkey PRIMARY KEY (id);


--
-- TOC entry 3740 (class 2606 OID 325496)
-- Name: query_embeddings_qwen query_embeddings__qwen_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.query_embeddings_qwen
    ADD CONSTRAINT query_embeddings__qwen_pkey PRIMARY KEY (query);


--
-- TOC entry 3738 (class 2606 OID 325498)
-- Name: query_embeddings query_embeddings_pkey; Type: CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.query_embeddings
    ADD CONSTRAINT query_embeddings_pkey PRIMARY KEY (query);


--
-- TOC entry 3698 (class 1259 OID 427168)
-- Name: idx_alias_trgm; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_alias_trgm ON celex_facts.company_canonical2alias USING gin (company_alias celex_facts.gin_trgm_ops);


--
-- TOC entry 3747 (class 1259 OID 387280)
-- Name: idx_book_path_prefix; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_book_path_prefix ON celex_facts.pg_embedding_book_qwen USING btree (path text_pattern_ops);


--
-- TOC entry 3716 (class 1259 OID 325499)
-- Name: idx_celex_hnsw_halfvec; Type: INDEX; Schema: celex_facts; Owner: ai
--

CREATE INDEX idx_celex_hnsw_halfvec ON celex_facts.langchain_pg_embedding USING hnsw (((embedding)::public.halfvec(3072)) public.halfvec_cosine_ops) WITH (m='16', ef_construction='64');


--
-- TOC entry 3757 (class 1259 OID 429393)
-- Name: idx_legal_name_trgm_gist; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX idx_legal_name_trgm_gist ON celex_facts.corp_lei USING gist (legal_name celex_facts.gist_trgm_ops);


--
-- TOC entry 3715 (class 1259 OID 325506)
-- Name: ix_celex_facts_company_standardized; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_celex_facts_company_standardized ON celex_facts.company_standardized USING btree (name);


--
-- TOC entry 3727 (class 1259 OID 325507)
-- Name: ix_celex_facts_events_extraction_attributes_event_id; Type: INDEX; Schema: celex_facts; Owner: ai
--

CREATE INDEX ix_celex_facts_events_extraction_attributes_event_id ON celex_facts.events_extraction_attributes USING btree (event_id);


--
-- TOC entry 3723 (class 1259 OID 325508)
-- Name: ix_celex_facts_events_extraction_celex_id; Type: INDEX; Schema: celex_facts; Owner: ai
--

CREATE INDEX ix_celex_facts_events_extraction_celex_id ON celex_facts.events_extraction USING btree (celex_id);


--
-- TOC entry 3730 (class 1259 OID 325509)
-- Name: ix_celex_facts_events_in_chunks_celex_id; Type: INDEX; Schema: celex_facts; Owner: ai
--

CREATE INDEX ix_celex_facts_events_in_chunks_celex_id ON celex_facts.events_in_chunks USING btree (celex_id);


--
-- TOC entry 3724 (class 1259 OID 325510)
-- Name: ix_events_extraction_fk_chunk_id; Type: INDEX; Schema: celex_facts; Owner: ai
--

CREATE INDEX ix_events_extraction_fk_chunk_id ON celex_facts.events_extraction USING btree (fk_chunk_id);


--
-- TOC entry 3717 (class 1259 OID 325511)
-- Name: ix_langchain_pg_embedding_celex_id; Type: INDEX; Schema: celex_facts; Owner: ai
--

CREATE INDEX ix_langchain_pg_embedding_celex_id ON celex_facts.langchain_pg_embedding USING btree (celex_id);


--
-- TOC entry 3718 (class 1259 OID 325512)
-- Name: ix_langchain_pg_embedding_chunk_hash; Type: INDEX; Schema: celex_facts; Owner: ai
--

CREATE INDEX ix_langchain_pg_embedding_chunk_hash ON celex_facts.langchain_pg_embedding USING btree (chunk_hash);


--
-- TOC entry 3748 (class 1259 OID 386378)
-- Name: ix_pg_embedding_book_qwen3_book_name; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_book_qwen3_book_name ON celex_facts.pg_embedding_book_qwen USING btree (book_name);


--
-- TOC entry 3749 (class 1259 OID 386379)
-- Name: ix_pg_embedding_book_qwen3_chunk_hash; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_book_qwen3_chunk_hash ON celex_facts.pg_embedding_book_qwen USING btree (chunk_hash);


--
-- TOC entry 3750 (class 1259 OID 386380)
-- Name: ix_pg_embedding_book_qwen3_level; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_book_qwen3_level ON celex_facts.pg_embedding_book_qwen USING btree (level);


--
-- TOC entry 3743 (class 1259 OID 325605)
-- Name: ix_pg_embedding_qwen_celex_id; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_qwen_celex_id ON celex_facts.pg_embedding_qwen USING btree (celex_id);


--
-- TOC entry 3744 (class 1259 OID 325606)
-- Name: ix_pg_embedding_qwen_chunk_hash; Type: INDEX; Schema: celex_facts; Owner: celex_facts
--

CREATE INDEX ix_pg_embedding_qwen_chunk_hash ON celex_facts.pg_embedding_qwen USING btree (chunk_hash);


--
-- TOC entry 3758 (class 2606 OID 325515)
-- Name: fact_ingestion_audit_trail fact_ingestion_audit_trail_fact_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.fact_ingestion_audit_trail
    ADD CONSTRAINT fact_ingestion_audit_trail_fact_id_fkey FOREIGN KEY (fact_id) REFERENCES celex_facts.facts(id) ON DELETE CASCADE;


--
-- TOC entry 3759 (class 2606 OID 325520)
-- Name: company_canonical2alias company_canonical2alias_fk_company_canonical_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.company_canonical2alias
    ADD CONSTRAINT company_canonical2alias_fk_company_canonical_fkey FOREIGN KEY (fk_company_canonical) REFERENCES celex_facts.company_canonical2celex_id(id);


--
-- TOC entry 3760 (class 2606 OID 325525)
-- Name: langchain_pg_embedding langchain_pg_embedding_collection_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: ai
--

ALTER TABLE ONLY celex_facts.langchain_pg_embedding
    ADD CONSTRAINT langchain_pg_embedding_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES celex_facts.langchain_pg_collection(uuid) ON DELETE CASCADE;


--
-- TOC entry 3761 (class 2606 OID 386373)
-- Name: pg_embedding_book_qwen pg_embedding_book_qwen3_collection_id_fkey; Type: FK CONSTRAINT; Schema: celex_facts; Owner: celex_facts
--

ALTER TABLE ONLY celex_facts.pg_embedding_book_qwen
    ADD CONSTRAINT pg_embedding_book_qwen3_collection_id_fkey FOREIGN KEY (collection_id) REFERENCES celex_facts.langchain_pg_collection(uuid) ON DELETE CASCADE;


-- Completed on 2026-04-26 19:12:14 UTC

--
-- PostgreSQL database dump complete
--

\unrestrict CzCzGHtaw7GXWM4z9aY79ZfeKEMsIklP4WSbIQVjcYOHzcbFvjRA2L1DyRvUhj0

