-- ---------------------------------------------------------------------------
-- Migration: partial-precision bounds in facts_cartel_legal_period_category_vw
-- (ISSUES #184).
--
-- WHY: heideltime_cartel_period stores each bound at the precision the
-- decision states ('1994', '1994-03', '1994-03-05') since 20260905. The view
-- parsed only full YYYY-MM-DD and fell back to NULL, so an honest year-only
-- period silently became periods_category 'NA'. A partial bound is now widened
-- to its span: as a start it is the first day it covers, as an end the last
-- (1994 -> 1994-01-01 .. 1994-12-31; 1996-02 -> 1996-02-01 .. 1996-02-29).
--
-- DISPLAY: cartel_period / legal_period show each bound at its stored
-- precision (a full date renders exactly as before), never the widened day,
-- so the view does not fabricate precision the fact does not carry.
--
-- LEGACY PADDING: older rows zero-pad an unknown component ('1973-00-00',
-- '1977-03-00'). The old view's lenient to_date turned these into invented
-- days (and '0000-00-00' into 0001-01-01 BC); a trailing '-00' is now read as
-- the precision it signals (year, year-month). An inner '00' ('2011-00-27')
-- is not padding and stays invalid.
--
-- VALIDITY: pg_input_is_valid guards every cast, so a shaped-but-impossible
-- value (1994-02-30, 2011-00-27) is NULL, not a statement-aborting error.
--
-- Idempotent (CREATE OR REPLACE); output columns are unchanged, and no other
-- relation depends on the view. Comments live in schema_comments.sql.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION celex_facts.partial_date_first_day(raw text)
    RETURNS date
    LANGUAGE sql
    STABLE
    PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN r ~ '^\d{4}-\d{2}-\d{2}$' AND pg_input_is_valid(r, 'date')
            THEN r::date
        WHEN r ~ '^\d{4}-\d{2}$' AND pg_input_is_valid(r || '-01', 'date')
            THEN (r || '-01')::date
        WHEN r ~ '^\d{4}$' AND pg_input_is_valid(r || '-01-01', 'date')
            THEN (r || '-01-01')::date
    END
    FROM (SELECT regexp_replace(raw, '(-00)+$', '') AS r) AS normalized
$$;

CREATE OR REPLACE FUNCTION celex_facts.partial_date_last_day(raw text)
    RETURNS date
    LANGUAGE sql
    STABLE
    PARALLEL SAFE
AS $$
    SELECT CASE
        WHEN r ~ '^\d{4}-\d{2}-\d{2}$' AND pg_input_is_valid(r, 'date')
            THEN r::date
        WHEN r ~ '^\d{4}-\d{2}$' AND pg_input_is_valid(r || '-01', 'date')
            THEN ((r || '-01')::date + interval '1 month' - interval '1 day')::date
        WHEN r ~ '^\d{4}$' AND pg_input_is_valid(r || '-12-31', 'date')
            THEN (r || '-12-31')::date
    END
    FROM (SELECT regexp_replace(raw, '(-00)+$', '') AS r) AS normalized
$$;

CREATE OR REPLACE VIEW celex_facts.facts_cartel_legal_period_category_vw AS
WITH extracted_periods AS (
    SELECT p.celex_id,
           btrim(split_part(p.cartel_period::text, ' : ', 1)) AS raw_c_start,
           btrim(split_part(p.cartel_period::text, ' : ', 2)) AS raw_c_end,
           btrim(split_part(p.legal_period::text, ' : ', 1)) AS raw_l_start,
           btrim(split_part(p.legal_period::text, ' : ', 2)) AS raw_l_end
    FROM celex_facts.facts_cartel_legal_period_vw p
), parsed_dates AS (
    SELECT e.*,
           celex_facts.partial_date_first_day(e.raw_c_start) AS c_start,
           celex_facts.partial_date_last_day(e.raw_c_end) AS c_end,
           celex_facts.partial_date_first_day(e.raw_l_start) AS l_start,
           celex_facts.partial_date_last_day(e.raw_l_end) AS l_end
    FROM extracted_periods e
)
SELECT celex_id,
       ((CASE WHEN c_start IS NULL THEN 'NA' ELSE raw_c_start END || ' : ')
        || CASE WHEN c_end IS NULL THEN 'NA' ELSE raw_c_end END) || E'\n' AS cartel_period,
       (CASE WHEN l_start IS NULL THEN 'NA' ELSE raw_l_start END || ' : ')
        || CASE WHEN l_end IS NULL THEN 'NA' ELSE raw_l_end END AS legal_period,
       CASE
           WHEN c_start IS NULL OR c_end IS NULL OR l_start IS NULL OR l_end IS NULL THEN 'NA'
           WHEN date_trunc('month', c_start) = date_trunc('month', l_start)
                AND date_trunc('month', c_end) = date_trunc('month', l_end) THEN 'exact'
           WHEN c_end < l_start THEN 'legal_post'
           WHEN l_start >= c_start AND l_end <= c_end THEN 'legal-within'
           WHEN c_start >= l_start AND c_end <= l_end THEN 'cartel-within'
           WHEN l_start <= c_end AND l_end > c_end THEN 'legal-post-overlap'
           WHEN c_start <= l_end AND c_end > l_end THEN 'cartel-post-overlap'
           ELSE 'NA'
       END AS periods_category
FROM parsed_dates;
