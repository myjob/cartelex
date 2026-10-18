-- =====================================================================
-- Migration: flat entity-graph views for the Q&A planners
-- =====================================================================
-- Purpose
--   The typed entity graph (celex_facts.entities / entity_edges, created by
--   migrate_entity_graph.sql) was write-only: the batch materializer filled
--   it, and nothing read it. The Q&A planners see exactly one schema string
--   (KB_SCHEMA_DESCRIPTION in src/pipelines/agent_dspy/retrieval_dispatch.py)
--   which documented only the EAV facts table, so cross-case relational
--   questions -- "which German companies were fined", "which companies appear
--   in more than one cartel", "which cases violated Article 101 or its
--   predecessors" -- fell back to ILIKE over document chunks, which cannot
--   deduplicate company aliases or resolve the treaty renumbering.
--
--   These views are the read surface. Per decision D4 of
--   docs/claude/plans/company-country-lei-entity-graph.md the LLM is taught
--   the flat views, never the raw graph tables.
--
--   company_case_relations_vw and case_provision_relations_vw are the original
--   pair; case_timeline_vw was added with the HeidelTime temporal layer and is
--   the only one of the three that is fact-derived rather than edge-derived.
--
-- Edge-derived by design
--   The first two views start from entity_edges, so the 884 zero-degree company
--   nodes left over from the retired companies_canoncial era never appear. That
--   is correct, not a defect to "fix" here -- a node with no edge has no
--   relationship to report. case_timeline_vw is deliberately the exception: it
--   starts from the heideltime_event fact so that events whose actor resolved
--   to no company are still reported, as evidence with an unresolved actor.
--
-- Provenance
--   Every row carries source_fact_id (FK -> facts.id) and source_fact_name,
--   so celex_facts.fact_provenance(), LineageRepository and the Master
--   Planner's EvidenceVerifierModule.forward(celex_id, fact_name) all resolve
--   a graph-derived answer back to its chunk evidence unchanged.
--
-- Country provenance (interim)
--   company_country comes from GLEIF (corp_lei) alone. Workstream 1 of the
--   country/LEI plan adds entities.country + entities.country_source; when it
--   lands, re-run this migration with the COALESCE extended to prefer the
--   node column. Nothing else changes.
--
-- Comments
--   Live in schema_comments.sql with every other relation comment (this
--   file carries none, matching migrate_entity_graph.sql), so the
--   test_schema_comments drift test sees both views.
--
-- Idempotency
--   DROP VIEW IF EXISTS + CREATE VIEW (never OR REPLACE) so a future column
--   change stays re-runnable. Requires migrate_entity_graph.sql applied.
-- =====================================================================

-- ---------------------------------------------------------------------------
-- company_case_relations_vw: the five company -> case relations, flattened
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS celex_facts.company_case_relations_vw;

CREATE VIEW celex_facts.company_case_relations_vw AS
SELECT
    comp.canonical_name                     AS company_name,
    comp.lei_id                             AS lei_id,
    COALESCE(lei.country, left(lei.legal_jurisdiction, 2))
                                            AS company_country,
    lei.legal_jurisdiction                  AS legal_jurisdiction,
    ed.relation::text                       AS relation,
    f.name                                  AS source_fact_name,
    ed.celex_id                             AS celex_id,
    cs.canonical_name                       AS case_name,
    (ed.value_json)::jsonb ->> 'amount'     AS amount,
    ed.source_fact_id                       AS source_fact_id
FROM celex_facts.entity_edges ed
JOIN celex_facts.entities comp
  ON comp.entity_id = ed.src_entity_id
 AND comp.kind = 'company'
JOIN celex_facts.entities cs
  ON cs.entity_id = ed.dst_entity_id
 AND cs.kind = 'case'
JOIN celex_facts.facts f
  ON f.id = ed.source_fact_id
LEFT JOIN celex_facts.corp_lei lei
  ON lei.lei = comp.lei_id
-- Explicit allow-list, NOT "<> 'dated_in'": this view's contract is the five
-- roster-grounded relations that evidence participation, and every planner
-- prompt, view comment and row-count test depends on that set staying closed.
-- The temporal relation 'dated_in' is company -> case too, so without this
-- clause it would silently join here and inflate the documented 9,954 rows.
-- Any relation added later is likewise excluded until someone decides it
-- belongs -- which is the point of an allow-list.
WHERE ed.relation IN (
    'participated_in',
    'fined',
    'leniency_applied',
    'leniency_granted',
    'leniency_rejected'
);


-- ---------------------------------------------------------------------------
-- case_provision_relations_vw: case -> provision (violated) edges
-- ---------------------------------------------------------------------------
-- Deliberately NOT joined to celex_facts.legal_provisions: that table holds one
-- row per (article_number, treaty) era, so the join would fan each edge out
-- across the EEC / EC / TFEU rows. The provision node is already the resolved
-- canonical concept -- that resolution is the point of the layer.
DROP VIEW IF EXISTS celex_facts.case_provision_relations_vw;

CREATE VIEW celex_facts.case_provision_relations_vw AS
SELECT
    ed.celex_id                             AS celex_id,
    cs.canonical_name                       AS case_name,
    prov.canonical_name                     AS canonical_concept,
    prov.article_number                     AS article_number,
    f.name                                  AS source_fact_name,
    ed.source_fact_id                       AS source_fact_id
FROM celex_facts.entity_edges ed
JOIN celex_facts.entities cs
  ON cs.entity_id = ed.src_entity_id
 AND cs.kind = 'case'
JOIN celex_facts.entities prov
  ON prov.entity_id = ed.dst_entity_id
 AND prov.kind = 'provision'
JOIN celex_facts.facts f
  ON f.id = ed.source_fact_id
WHERE ed.relation = 'violated';



-- ---------------------------------------------------------------------------
-- case_timeline_vw: one row per HeidelTime event, actor resolved where possible
-- ---------------------------------------------------------------------------
-- The temporal read surface. entity_edges cannot hold per-event rows -- its
-- unique key (src, dst, relation, source_fact_id) collapses every event of a
-- decision onto one 'dated_in' edge per company, because there is exactly one
-- heideltime_event fact per CELEX. This view recovers the per-event grain by
-- unnesting the fact itself and joining back to the edge payload's verbatim
-- actor list, so the fuzzy matching stays in tested Python and the SQL is a
-- plain string equi-join.
--
-- Degrades gracefully: with zero 'dated_in' edges every event still appears,
-- with actor_class in (institution, placeholder, unresolved) and NULL company
-- columns. That is what makes it deployable before the rematerialization.
--
-- Two casts that must never be done blind:
--   * facts.value_json is TEXT and is not always JSON -- pg_input_is_valid
--     (PostgreSQL 16+) is the only guard that survives predicate reordering.
--   * 'NA'::date aborts the whole query, not the row, so event_date is only
--     cast behind a full-ISO regex; partial precisions ('1994', '1994-03')
--     survive in event_date_raw.
--
-- The placeholder / institution literals below mirror _PLACEHOLDER_ACTORS and
-- _INSTITUTION_MARKERS in pipelines/graph/heideltime_actor_resolution.py; a
-- drift test asserts every Python member appears in this file.
DROP VIEW IF EXISTS celex_facts.case_timeline_vw;

CREATE VIEW celex_facts.case_timeline_vw AS
SELECT
    f.celex_id                              AS celex_id,
    cs.canonical_name                       AS case_name,
    ev.seq::int                             AS event_seq,
    -- The regex fixes the SHAPE (full ISO only, so '1994' and '1994-03' stay
    -- out of the typed column); pg_input_is_valid fixes the VALIDITY. Both are
    -- required: the corpus contains '2011-00-27', which is correctly shaped and
    -- still not a date, and a bad cast aborts the entire query rather than the
    -- offending row.
    CASE
        WHEN ev.elem ->> 'event_date' ~ '^\d{4}-\d{2}-\d{2}$'
         AND pg_input_is_valid(ev.elem ->> 'event_date', 'date')
        THEN (ev.elem ->> 'event_date')::date
    END                                     AS event_date,
    ev.elem ->> 'event_date'                AS event_date_raw,
    ev.elem ->> 'actor'                     AS actor,
    CASE
        WHEN m.entity_id IS NOT NULL THEN 'company'
        WHEN lower(btrim(ev.elem ->> 'actor')) IN (
            'redacted', '[redacted]', 'unknown', 'na', 'n/a', 'none',
            'not specified', 'unspecified', 'parties', 'the parties',
            'addressees', 'the addressees', 'third party', 'third parties',
            'customers', 'competitors', 'employee of non-addressee',
            'employees of non-addressee'
        ) THEN 'placeholder'
        WHEN lower(ev.elem ->> 'actor') ~
             '(commission|court|council|parliament|authority|ombudsman|bundeskartellamt|office of fair trading|dg comp|ecj|cfi)'
        THEN 'institution'
        ELSE 'unresolved'
    END                                     AS actor_class,
    m.canonical_name                        AS company_name,
    m.entity_id                             AS company_entity_id,
    m.lei_id                                AS lei_id,
    ev.elem ->> 'predicate_object'          AS predicate_object,
    jsonb_array_length(f.value_json::jsonb) AS celex_event_count,
    f.name                                  AS source_fact_name,
    f.id                                    AS source_fact_id
FROM celex_facts.facts f
CROSS JOIN LATERAL jsonb_array_elements(f.value_json::jsonb)
    WITH ORDINALITY AS ev(elem, seq)
JOIN celex_facts.entities cs
  ON cs.kind = 'case'
 AND cs.natural_key = f.celex_id
LEFT JOIN LATERAL (
    SELECT comp.entity_id, comp.canonical_name, comp.lei_id
      FROM celex_facts.entity_edges ed
      JOIN celex_facts.entities comp
        ON comp.entity_id = ed.src_entity_id
     CROSS JOIN LATERAL jsonb_array_elements_text(
              (ed.value_json)::jsonb -> 'actors') AS a(actor)
     -- Compared as text, not as the enum literal, so this view can be created
     -- BEFORE migrate_entity_graph_dated_in_enum.sql runs (an unknown enum
     -- label in a view body is a hard parse error). The predicate is a filter
     -- on top of the source_fact_id index lookup either way, so nothing is lost.
     WHERE ed.relation::text = 'dated_in'
       AND ed.source_fact_id = f.id
       AND a.actor = ev.elem ->> 'actor'
     LIMIT 1
) m ON TRUE
WHERE f.name = 'heideltime_event'
  AND pg_input_is_valid(f.value_json, 'jsonb')
  AND jsonb_typeof(f.value_json::jsonb) = 'array';
