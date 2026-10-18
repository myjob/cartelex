-- =====================================================================
-- Migration: legal_provisions ontology table + treaty-renumbering seed
-- =====================================================================
-- Purpose
--   Phase 3 of the knowledge-representation review materializes the EU
--   competition-provision renumbering (Art. 85/86/90 EEC -> 81/82/86 EC ->
--   101/102/106 TFEU) as a temporal-validity lookup so:
--   * era resolution in pipelines/legal/infringement_concept.py is a
--     lookup over ProvisionMap rows, not date arithmetic (closes the two
--     Article-86 ambiguity items in ISSUES.md structurally);
--   * SQL-side joins (entity graph, audits) can resolve provisions
--     directly.
--   Article 86 is the point of the table: abuse of dominance under the
--   EEC Treaty, public undertakings under the EC Treaty.
--
-- Validity dates
--   valid_from is INCLUSIVE, valid_to is EXCLUSIVE
--   (valid_from <= d < valid_to; NULL = in force). The cut-offs encode the
--   SYSTEM's era boundaries (settings.TREATY_BOUNDARY_AMSTERDAM =
--   1999-04-30, settings.TREATY_BOUNDARY_LISBON = 2009-11-30, both used
--   with "<" comparisons), deliberately NOT the real-world in-force dates
--   (1999-05-01 / 2009-12-01), so the ontology and the legacy date-coupled
--   YAML conditions agree bit-for-bit.
--
-- EEA rows
--   Article 53/54 EEA are seeded as documentation/join targets only. The
--   runtime loader (provision_repository.load_provision_map) filters to
--   treaty IN ('eec','ec','tfeu'); enabling EEA would make "Article 53/54"
--   tokens newly resolve in concepts_for() and is a deliberate follow-up
--   decision (user decision 2026-07-05).
--
-- Idempotency
--   CREATE ... IF NOT EXISTS plus ON CONFLICT DO NOTHING on the seed make
--   this safe to re-run.
--
-- Cost
--   Negligible (11 rows).
-- =====================================================================

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
