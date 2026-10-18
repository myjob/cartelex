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
