-- =====================================================================
-- schema_comments.sql -- authoritative COMMENT ON statements for the
-- celex_facts and rule_facts schemas.
--
-- CONTRACT
--   * Idempotent by construction: COMMENT ON overwrites unconditionally.
--     Re-run the whole file, never a subset.
--   * Loud on drift: a renamed or dropped relation raises
--     'relation ... does not exist' instead of failing silently. Running
--     this file after every migration doubles as a schema-drift canary.
--   * Cheap: sets a row in pg_description, no table rewrite, no reindex.
--
-- SCOPE
--   Table and view comments: every relation, no exceptions.
--   Column comments: only columns whose meaning a newcomer would guess
--   wrong. Self-evident columns (id, created_at, name, ...) are left
--   uncommented on purpose -- comment volume is what kills these files.
--
-- WHEN TO RUN
--   psql -h localhost -p 5433 -U celex_facts -d celex \
--        -f src/pipelines/rag/pgvector/schema_comments.sql
--   Note port 5433 (docker). A bare psql hits the host-native 5432,
--   which does not carry these schemas.
--
-- ENFORCEMENT
--   tests/test_schema_comments.py fails when a relation has no comment.
--   Prose reference: docs/Data Table Catalogue.md
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. Facts core
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.facts IS
  $$Central fact ledger: one row per (celex_id, name). Holds the markdown
    value, the structured payload and a denormalised verification verdict.
    Every downstream view and Q&A path resolves through this table.$$;
COMMENT ON COLUMN celex_facts.facts.name IS
  $$Fact name key, e.g. cartel_period. Vocabulary defined in FACT_EXTRACTOR.yaml.$$;
COMMENT ON COLUMN celex_facts.facts.value_md IS
  $$Human-readable markdown rendering of the fact -- the display value.$$;
COMMENT ON COLUMN celex_facts.facts.value_json IS
  $$Structured payload validated against the fact's pydantic output schema.$$;
COMMENT ON COLUMN celex_facts.facts.references_doc_json IS
  $$Document chunks the extraction cited. Superseded for lineage purposes by
    fact_source_link; retained because older rows predate the lineage tables.$$;
COMMENT ON COLUMN celex_facts.facts.verification_status IS
  $$Denormalised verdict from the evidence verifier (verification_status_enum).
    Written by the verifier, never by the extractor.$$;
COMMENT ON COLUMN celex_facts.facts.verification_score IS
  $$Best final_score across this fact's fact_evidence rows.$$;

COMMENT ON TABLE celex_facts.fact_source_registry IS
  $$Canonical registry of provenance anchors a fact may cite -- document chunk,
    HeidelTime event, LEI record, CSV row or another fact. Decouples what a
    source is from which facts use it.$$;
COMMENT ON COLUMN celex_facts.fact_source_registry.source_type IS
  $$Discriminator selecting which anchor column is populated
    (chunk_id, heideltime_event_id, lei_id, csv_row, source_fact_id).$$;
COMMENT ON COLUMN celex_facts.fact_source_registry.chunk_seq IS
  $$Ordinal of the chunk inside its document -- stable across re-embedding,
    unlike chunk_id.$$;
COMMENT ON COLUMN celex_facts.fact_source_registry.source_fact_id IS
  $$Set when source_type is a fact, making this registry row a derived-fact anchor.$$;

COMMENT ON TABLE celex_facts.fact_source_link IS
  $$N:M edge between facts and fact_source_registry, carrying extraction method,
    relation type, trust rank and conflict flags plus the producing pipeline.$$;
COMMENT ON COLUMN celex_facts.fact_source_link.extraction_method IS
  $$How the value was obtained: sql, vector, dspy, heideltime, manual.$$;
COMMENT ON COLUMN celex_facts.fact_source_link.relation_type IS
  $$Role the source plays: supports, derived_from, gated_by, contradicts.$$;
COMMENT ON COLUMN celex_facts.fact_source_link.trust_rank IS
  $$Precedence when several sources disagree. Lower ranks win.$$;
COMMENT ON COLUMN celex_facts.fact_source_link.conflict_flag IS
  $$True when this source disagrees with a higher-ranked one; see conflict_note.$$;
COMMENT ON COLUMN celex_facts.fact_source_link.dspy_module IS
  $$DSPy module class that produced the edge -- the unit to re-run on reprocessing.$$;

COMMENT ON TABLE celex_facts.fact_evidence IS
  $$Per-chunk verification evidence for a fact: support label, final score and
    the component sub-scores, plus the supporting quote and missing elements.$$;
COMMENT ON COLUMN celex_facts.fact_evidence.support_label IS
  $$Deterministic verdict for this one chunk, before aggregation onto facts.$$;
COMMENT ON COLUMN celex_facts.fact_evidence.final_score IS
  $$Weighted combination of the five sub-scores below.$$;
COMMENT ON COLUMN celex_facts.fact_evidence.entity_score IS
  $$Do the fact's required entities appear in the chunk.$$;
COMMENT ON COLUMN celex_facts.fact_evidence.keyword_score IS
  $$Lexical overlap between fact value and chunk.$$;
COMMENT ON COLUMN celex_facts.fact_evidence.semantic_score IS
  $$Cross-encoder relevance of chunk to the fact statement.$$;
COMMENT ON COLUMN celex_facts.fact_evidence.temporal_score IS
  $$Agreement between the fact's dates and HeidelTime expressions in the chunk.$$;
COMMENT ON COLUMN celex_facts.fact_evidence.provenance_score IS
  $$Whether the chunk is one the extractor actually cited.$$;
COMMENT ON COLUMN celex_facts.fact_evidence.missing_elements IS
  $$Required entities or time expressions the verifier could not find --
    drives the missing_required_entity / missing_required_time verdicts.$$;

COMMENT ON TABLE celex_facts.fact2book_checks IS
  $$Complete per-fact ledger of the book cross-check: one row per (fact_id,
    celex_id) holding the cited book chunk, the judge's label and score, the
    explanation and the machine routing decision. Rewritten by every run.$$;
COMMENT ON COLUMN celex_facts.fact2book_checks.reference_type IS
  $$How the book references the case: landmark (analysed under an "Economic
    landmark cases" subsection, validated in full) or simple (named as an
    example; only infringement facts may be contradicted). Governs policy,
    not the verdict.$$;
COMMENT ON COLUMN celex_facts.fact2book_checks.action IS
  $$Machine routing decision, CHECK-constrained: corroborated (lineage link
    written), mismatch (contradiction recorded) or skipped (no verdict).$$;

COMMENT ON TABLE celex_facts.fact2book_case_runs IS
  $$Case-level ledger of the book cross-check driver: one row per (celex_id,
    case_id) with the outcome of the latest pass. Answers "was this case
    visited, and why did it produce no verdicts"; --resume skips keys
    finished under the same run_id.$$;
COMMENT ON COLUMN celex_facts.fact2book_case_runs.run_id IS
  $$Operator-chosen run label (--run-id); a resumed run reuses it.$$;
COMMENT ON COLUMN celex_facts.fact2book_case_runs.absence_reason IS
  $$none (in book), no_surface (no case-id spelling found in the corpus) or
    no_anchor (spelling found but no chunk row retrievable).$$;
COMMENT ON COLUMN celex_facts.fact2book_case_runs.already_checked IS
  $$Facts left untouched by a --skip-existing pass (neither judged nor written).$$;

COMMENT ON TABLE celex_facts.fact_ingestion_audit_trail IS
  $$Append-only run log of every fact-extraction attempt. Never updated in place;
    query the newest row per (celex_id, target_fact_name) for current state.$$;
COMMENT ON COLUMN celex_facts.fact_ingestion_audit_trail.target_fact_name IS
  $$Fact the run intended to produce -- populated even when the run failed and
    no facts row exists.$$;
COMMENT ON COLUMN celex_facts.fact_ingestion_audit_trail.output_schema IS
  $$Name of the pydantic model the run validated against.$$;
COMMENT ON COLUMN celex_facts.fact_ingestion_audit_trail.force_override IS
  $$True when the run was told to overwrite an existing fact.$$;
COMMENT ON COLUMN celex_facts.fact_ingestion_audit_trail.predecessors IS
  $$Upstream fact names this run consumed -- the replay order for reprocessing.$$;
COMMENT ON COLUMN celex_facts.fact_ingestion_audit_trail.duration IS
  $$Wall-clock seconds for the run, including LLM latency.$$;

COMMENT ON TABLE celex_facts.legal_provisions IS
  $$Reference list of treaty articles used to normalise infringement citations
    across decision vintages (Art 85 EEC becomes Art 101 TFEU).$$;
COMMENT ON COLUMN celex_facts.legal_provisions.canonical_concept IS
  $$Vintage-independent concept key that all renumberings of an article map onto.$$;
COMMENT ON COLUMN celex_facts.legal_provisions.valid_from IS
  $$First date this numbering was in force. NOT NULL -- every provision row
    must state when it took effect.$$;
COMMENT ON COLUMN celex_facts.legal_provisions.valid_to IS
  $$Last date this numbering was in force. NULL means still current.$$;

COMMENT ON TABLE rule_facts.facts IS
  $$Legacy rule-engine fact store. Drained (0 rows); retained only as the
    documented migration source for the company_standard2celex_id* tables.$$;


-- ---------------------------------------------------------------------
-- 2. Fact-derived reporting views
-- ---------------------------------------------------------------------

COMMENT ON VIEW celex_facts.celex_index_vw IS
  $$One row per decision -- case title, industry, cartel period and summary
    pivoted out of facts. The default case-picker index. Inner joins throughout,
    so a decision missing any one of those facts drops out entirely.$$;

COMMENT ON VIEW celex_facts.facts_cartel_legal_period_vw IS
  $$Pairs each decision's cartel_period with its legal_assessment_period.$$;

COMMENT ON VIEW celex_facts.facts_cartel_legal_period_category_vw IS
  $$Parses both periods into dates and classifies their overlap into a period
    category. The temporal-consistency QA lens.$$;

COMMENT ON VIEW celex_facts.fact_ingestion_audit_trail_vw IS
  $$Audit trail joined to the resulting fact value, newest first. Inner join on
    fact_id, so failed runs (no fact row) are excluded by design.$$;


-- ---------------------------------------------------------------------
-- 3. Document corpus and embeddings
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.pg_embedding_qwen IS
  $$Primary vector store (settings.EMBEDDING_TABLE): decision chunks with their
    Qwen3 embedding, text, contextual prefix, section classification and the FTS
    column feeding the hybrid lexical leg. celex_id keeps the CELEX- prefix.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_qwen.celex_id IS
  $$Decision id WITH the CELEX- prefix, e.g. CELEX-31990D0645. Stripping the
    prefix returns zero rows. Corpus also carries AT_ and bare numeric ids.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_qwen.context_prefix IS
  $$Header/subheader breadcrumb prepended to the chunk before embedding, so an
    isolated chunk still carries its position in the decision.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_qwen.section_type IS
  $$Decision section classification backfilled by Main_classify_sections.py.
    Drives the ranker structural boost and the section_scope retrieval filter.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_qwen.document_tsv IS
  $$Generated tsvector for the Postgres FTS leg of HybridSearch.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_qwen.doc_pos IS
  $$Ordinal position of the chunk within its document -- restores reading order.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_qwen.chunk_hash IS
  $$Content hash used to skip re-embedding unchanged chunks on re-ingestion.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_qwen.token_count IS
  $$Token length under the Qwen3 tokenizer; oversized passages are split and pooled.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_qwen.cmetadata IS
  $$LangChain-owned metadata blob. Project-specific fields are promoted to real
    columns instead -- do not add new keys here.$$;

COMMENT ON TABLE celex_facts.pg_embedding_book_qwen IS
  $$Same shape as pg_embedding_qwen for the reference-book corpus. The retrieval
    side of the fact-to-book cross-check.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_book_qwen.level IS
  $$Markdown heading depth of the chunk's section.$$;
COMMENT ON COLUMN celex_facts.pg_embedding_book_qwen.text_line IS
  $$Line offset in the source file -- lets a citation point at a printed page.$$;

COMMENT ON TABLE celex_facts.langchain_pg_collection IS
  $$LangChain collection registry that the embedding tables key off via
    collection_id. Four rows, one per corpus.$$;
COMMENT ON COLUMN celex_facts.langchain_pg_collection.cmetadata IS
  $$LangChain-owned collection metadata blob.$$;

COMMENT ON TABLE celex_facts.langchain_pg_embedding IS
  $$RETIRED. Default LangChain embedding table from the OpenAI
    text-embedding-3-large era, emptied when that path was pruned in Aug 2026.
    Kept as an empty shell only; pg_embedding_qwen is the live store.$$;

COMMENT ON TABLE celex_facts.query_embeddings_qwen IS
  $$Cache of Qwen3 query-side vectors keyed by query text, so a repeated search
    skips an embedding call.$$;
COMMENT ON COLUMN celex_facts.query_embeddings_qwen.query IS
  $$Verbatim query string -- the cache key. Not normalised, so casing and
    whitespace produce distinct entries.$$;

COMMENT ON TABLE celex_facts.query_embeddings IS
  $$RETIRED. Query-vector cache from the OpenAI embedder. Historical only.$$;

COMMENT ON TABLE celex_facts.heideltime_extractions IS
  $$Every temporal expression HeidelTime found in a chunk. Backs the temporal
    leg of fact verification and the event-extraction quest.$$;
COMMENT ON COLUMN celex_facts.heideltime_extractions.fk_chunk_id IS
  $$Chunk the expression was found in; references the embedding table id.$$;
COMMENT ON COLUMN celex_facts.heideltime_extractions.tid IS
  $$TIMEX3 id as emitted by HeidelTime, unique only within its document.$$;
COMMENT ON COLUMN celex_facts.heideltime_extractions.timex_text IS
  $$Surface form as written in the decision, e.g. "the end of 1994".$$;
COMMENT ON COLUMN celex_facts.heideltime_extractions.timex_type IS
  $$TIMEX3 class: DATE, TIME, DURATION or SET.$$;
COMMENT ON COLUMN celex_facts.heideltime_extractions.timex_value IS
  $$Normalised TIMEX3 value, e.g. 1994-12. Partial precision is expected and
    must not be parsed as a full date.$$;
COMMENT ON COLUMN celex_facts.heideltime_extractions.span_start IS
  $$Character offset of the expression in the chunk text.$$;
COMMENT ON COLUMN celex_facts.heideltime_extractions.source_hash IS
  $$md5 of the chunk text this row was extracted from. A chunk counts as
    processed only while every one of its rows still equals md5(document);
    NULL predates the column and reads as stale, forcing re-extraction.$$;


-- ---------------------------------------------------------------------
-- 4. Question-and-answer retrieval
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.question2fact2embeddings IS
  $$Denormalised Q&A search surface: question, fact name, answer text and its
    embedding. Deliberately duplicates facts to keep Q&A retrieval to one scan.
    Largest relation by embedding volume.$$;
COMMENT ON COLUMN celex_facts.question2fact2embeddings.hash_q_a IS
  $$Hash of question plus answer. Change key -- a differing hash means the
    underlying fact moved and the row needs re-embedding.$$;
COMMENT ON COLUMN celex_facts.question2fact2embeddings.value_txt IS
  $$Flattened answer text actually embedded, derived from facts.value_md.$$;

COMMENT ON VIEW celex_facts.question2fact2embeddings_vw IS
  $$question2fact2embeddings joined back to facts for celex and fact context.$$;

COMMENT ON VIEW celex_facts.question2fact_name IS
  $$Distinct fact-name to canonical-question mapping, taken from the most recent
    audit-trail entry per fact name. The question wording therefore drifts as
    FACT_EXTRACTOR.yaml is edited.$$;

COMMENT ON VIEW celex_facts.question2fact IS
  $$Every fact expressed as a question/answer pair by joining facts to
    question2fact_name. Facts whose name never appears in the audit trail
    are absent.$$;


-- ---------------------------------------------------------------------
-- 5. Knowledge layer
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.knowledge_items IS
  $$Curated, cross-decision knowledge statements distilled from agent
    conversations. Unlike facts, a knowledge item is not scoped to one celex_id.$$;
COMMENT ON COLUMN celex_facts.knowledge_items.scope IS
  $$Breadth of applicability -- global, jurisdiction-specific or case-specific.$$;
COMMENT ON COLUMN celex_facts.knowledge_items.status IS
  $$Review state; only approved items are eligible for retrieval.$$;
COMMENT ON COLUMN celex_facts.knowledge_items.agent_run_id IS
  $$Agent run that proposed the item -- ties it back to the originating session.$$;
COMMENT ON COLUMN celex_facts.knowledge_items.knowledge_tsv IS
  $$Generated tsvector for lexical retrieval of knowledge statements.$$;

COMMENT ON TABLE celex_facts.knowledge_edges IS
  $$Typed relations from a knowledge item to a fact, another knowledge item or a
    registry source, with confidence and provenance.$$;
COMMENT ON COLUMN celex_facts.knowledge_edges.relation_type IS
  $$Role of the target: supports, generalises, contradicts, derived_from.$$;
COMMENT ON COLUMN celex_facts.knowledge_edges.dst_fact_id IS
  $$Target fact. Exactly one dst_* column is populated per row.$$;

COMMENT ON TABLE celex_facts.knowledge_question_embeddings IS
  $$Embedded question variants per knowledge item, so paraphrased user questions
    still retrieve it.$$;
COMMENT ON COLUMN celex_facts.knowledge_question_embeddings.question_variant IS
  $$One generated paraphrase of the question this item answers.$$;

COMMENT ON TABLE celex_facts.knowledge_extraction_queue IS
  $$Inbox of UI prompt/answer pairs staged for knowledge extraction, with the
    supporting fact and chunk ids plus processing status.$$;
COMMENT ON COLUMN celex_facts.knowledge_extraction_queue.status IS
  $$Queue state: pending, processed or failed (see error_message).$$;
COMMENT ON COLUMN celex_facts.knowledge_extraction_queue.supporting_facts IS
  $$Fact payloads shown to the user, captured so extraction can run later
    without replaying retrieval.$$;

COMMENT ON TABLE celex_facts.knowledge_ingest_audit_trail IS
  $$Run log for knowledge ingestion. Mirrors fact_ingestion_audit_trail column
    for column, so observability queries can union the two.$$;


-- ---------------------------------------------------------------------
-- 6. Entity graph
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.entities IS
  $$Typed nodes of the graph overlay -- company, article, product, jurisdiction.
    Keeps graph-shaped queries inside PostgreSQL instead of a separate store.$$;
COMMENT ON COLUMN celex_facts.entities.kind IS
  $$Node type discriminator; decides which of the optional id columns apply.$$;
COMMENT ON COLUMN celex_facts.entities.natural_key IS
  $$Deduplication key within a kind -- how the same company found in two
    decisions collapses to one node.$$;
COMMENT ON COLUMN celex_facts.entities.attrs_json IS
  $$Kind-specific attributes that do not warrant a column.$$;

COMMENT ON TABLE celex_facts.entity_edges IS
  $$Directed, typed relations between entities, scoped to a decision and
    traceable back to the fact that produced them.$$;
COMMENT ON COLUMN celex_facts.entity_edges.relation IS
  $$Edge type. Materialized: participated_in, fined, leniency_applied,
    leniency_granted, leniency_rejected (company -> case), violated
    (case -> provision) and dated_in (company -> case, the HeidelTime temporal
    layer). parent_of and active_in are reserved and unused.$$;
COMMENT ON COLUMN celex_facts.entity_edges.value_json IS
  $$Edge payload -- fine amount, participation period and similar qualifiers.$$;
COMMENT ON COLUMN celex_facts.entity_edges.source_fact_id IS
  $$Fact the edge was derived from; deleting that fact orphans the edge.$$;

COMMENT ON VIEW celex_facts.company_case_relations_vw IS
  $$Flat company -> case relations, the read surface the Q&A planners query via
    kb_sql. Edge-derived, so company nodes with no edge never appear. Coverage
    is partial by construction: the fines fact is empty for 431 of 744
    decisions, and only 3680 of 9954 rows resolve to GLEIF.$$;
COMMENT ON COLUMN celex_facts.company_case_relations_vw.relation IS
  $$One of participated_in, fined, leniency_applied, leniency_granted,
    leniency_rejected. Not sufficient on its own -- see source_fact_name. The
    view filters to exactly these five via an allow-list: the temporal relation
    dated_in is also company -> case and would otherwise inflate this view,
    which is not what it reports.$$;
COMMENT ON COLUMN celex_facts.company_case_relations_vw.source_fact_name IS
  $$The facts.name behind the edge, and the only way to tell leniency from
    exemption: relation leniency_applied is emitted by both leniency_applicants
    and exemption_applicants, and the exemption family dominates it (1813 vs
    441 rows measured 20260830).$$;
COMMENT ON COLUMN celex_facts.company_case_relations_vw.company_country IS
  $$ISO 3166-1 alpha-2 from corp_lei, NULL where the company did not resolve.
    Becomes COALESCE(entities.country, ...) once the country/LEI plan lands.$$;
COMMENT ON COLUMN celex_facts.company_case_relations_vw.amount IS
  $$Fine or reduction verbatim as extracted (100%, ECU 5 000). Free text --
    never cast or summed.$$;

COMMENT ON VIEW celex_facts.case_provision_relations_vw IS
  $$Flat case -> provision (violated) relations for the Q&A planners. Not
    joined to legal_provisions, which holds one row per treaty era and would
    fan each edge out across them.$$;
COMMENT ON COLUMN celex_facts.case_provision_relations_vw.canonical_concept IS
  $$Provision with the treaty renumbering already collapsed (Art. 85 EEC /
    81 EC / 101 TFEU resolve to one concept). A COMMA-SEPARATED list when a
    case violated several, so match it with ILIKE, never equality. Only three
    values occur: anticompetitive_agreement, abuse_of_dominance,
    public_undertaking.$$;
COMMENT ON COLUMN celex_facts.case_provision_relations_vw.article_number IS
  $$NULL on every current row: all 665 edges come from infringement_concept,
    which carries no article. law_article_all_relevant is mapped but
    contributes nothing yet.$$;

COMMENT ON VIEW celex_facts.case_timeline_vw IS
  $$One row per HeidelTime event (57758 measured), the temporal read surface for
    the Q&A planners. Unlike the other two graph views this is fact-derived, not
    edge-derived: an event whose actor resolved to no company node is still
    reported, with actor_class unresolved. It is EVIDENCE, never a census --
    31% of events name no party at all.$$;
COMMENT ON COLUMN celex_facts.case_timeline_vw.event_date IS
  $$Typed date, NULL unless the stored value is a full and VALID ISO date.
    Year-only and month-only precisions, prose and the one corpus row holding
    2011-00-27 survive in event_date_raw instead. Always guard with
    IS NOT NULL before ordering or comparing.$$;
COMMENT ON COLUMN celex_facts.case_timeline_vw.actor_class IS
  $$company (matched to a company node of the same case), institution
    (Commission, Court, Council -- deliberately given no node), placeholder
    (redacted, parties: 31% of all events) or unresolved. Roughly half of
    company-like actors do not resolve, so absence is not evidence of absence.$$;
COMMENT ON COLUMN celex_facts.case_timeline_vw.company_entity_id IS
  $$Join key back into entities; NULL unless actor_class is company. Resolution
    is case-scoped -- an actor is only ever matched against companies this
    decision already has, and an actor matching two of them is refused rather
    than guessed (corporate-group siblings such as Jungbunzlauer AG vs
    Jungbunzlauer Holding AG account for most refusals).$$;
COMMENT ON COLUMN celex_facts.case_timeline_vw.event_seq IS
  $$Position of the event within its fact, 1-based. The extractor writes events
    in chronological order, so this orders a case timeline even where
    event_date is NULL.$$;
COMMENT ON COLUMN celex_facts.case_timeline_vw.predicate_object IS
  $$Short verb phrase describing what happened, as extracted. Free text.$$;


-- ---------------------------------------------------------------------
-- 7. CELEX / case identity
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.celex_case_id IS
  $$Authoritative CSV-loaded mapping of celex_id to Commission case number.
    One-off bulk load; narrower and cleaner than case_celex_id.$$;
COMMENT ON COLUMN celex_facts.celex_case_id.case_x IS
  $$Bare case number without prefix (e.g. 26699).$$;
COMMENT ON COLUMN celex_facts.celex_case_id.case_id IS
  $$Prefixed case id as generated from case_x (always IV/<number>); the book
    cross-check varies the prefix and punctuation to find the book's spelling.$$;

COMMENT ON TABLE celex_facts.case_celex_id IS
  $$Mappings DISCOVERED by regex-scanning document and book chunks for
    COMP/IV/AT case-ID patterns. Wider and noisier than celex_case_id;
    precedence between the two is not yet declared (see ISSUES.md).$$;
COMMENT ON COLUMN celex_facts.case_celex_id.case_id IS
  $$Case id as matched in chunk text -- not validated against celex_case_id.$$;


-- ---------------------------------------------------------------------
-- 8. Companies -- decision-derived
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.company_investigated2celex_id IS
  $$The addressees actually investigated per decision, with address, country and
    resolved foreign keys into the three external registers. The join hub of the
    company model.$$;
COMMENT ON COLUMN celex_facts.company_investigated2celex_id.fk_lei IS
  $$Resolved GLEIF LEI (corp_lei.lei). NULL when resolution found no match.$$;
COMMENT ON COLUMN celex_facts.company_investigated2celex_id.fk_opencorp_idx IS
  $$Resolved OpenCorporates entry (opencorp_index_companies.id).$$;
COMMENT ON COLUMN celex_facts.company_investigated2celex_id.fk_handelsregister IS
  $$Resolved German register entry (handelsregister_companies.company_number).$$;
COMMENT ON COLUMN celex_facts.company_investigated2celex_id.jurisdication_code IS
  $$GLEIF jurisdiction code. NOTE the misspelling is the real column name;
    joins to gleif_jurisdication2countrycode.jurisdiction_code (spelled correctly).$$;
COMMENT ON COLUMN celex_facts.company_investigated2celex_id.iso_country_code IS
  $$ISO 3166-1 alpha-2 country, derived from jurisdication_code.$$;

COMMENT ON TABLE celex_facts.company_canonical2celex_id IS
  $$Every canonical company name mentioned in a decision -- a superset of the
    investigated set, including third parties, customers and complainants.$$;

COMMENT ON TABLE celex_facts.company_canonical2alias IS
  $$Canonical company name to its alias and spelling variants. Feeds fuzzy
    matching against the external registers.$$;
COMMENT ON COLUMN celex_facts.company_canonical2alias.fk_company_canonical IS
  $$Self-reference collapsing alias chains onto one canonical row.$$;

COMMENT ON TABLE celex_facts.company_standard2celex_id IS
  $$Standardised company names migrated out of the legacy rule engine.
    Frozen -- no pipeline writes here any more.$$;

COMMENT ON TABLE celex_facts.company_standard2celex_id2alias IS
  $$Alias variant of the same frozen legacy migration.$$;

COMMENT ON VIEW celex_facts.company_investigated2lei2corpidx2hr_vw IS
  $$Distinct company-to-external-register resolutions, stripped of the
    per-decision duplication in company_investigated2celex_id.$$;


-- ---------------------------------------------------------------------
-- 9. Companies -- external registers
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.corp_lei IS
  $$Full GLEIF LEI register, bulk-loaded. Largest relation in the database
    (~5.9 GB); always filter before joining.$$;
COMMENT ON COLUMN celex_facts.corp_lei.entity_category IS
  $$GLEIF category: GENERAL, BRANCH, FUND, SOLE_PROPRIETOR.$$;
COMMENT ON COLUMN celex_facts.corp_lei.legal_jurisdiction IS
  $$GLEIF jurisdiction code, not an ISO country code -- normalise through
    gleif_jurisdication2countrycode before comparing.$$;
COMMENT ON COLUMN celex_facts.corp_lei.record_json IS
  $$Raw GLEIF record as loaded, retained so new fields can be promoted to
    columns without a re-download.$$;

COMMENT ON TABLE celex_facts.handelsregister_companies IS
  $$German commercial-register companies, bulk-loaded. Keyed by company_number.$$;
COMMENT ON COLUMN celex_facts.handelsregister_companies.register_art IS
  $$Register section, e.g. HRB or HRA -- determines how register_nummer reads.$$;
COMMENT ON COLUMN celex_facts.handelsregister_companies.register_nummer IS
  $$Number within the register section; unique only together with registrar.$$;
COMMENT ON COLUMN celex_facts.handelsregister_companies.native_company_number IS
  $$Register-native identifier as printed, before normalisation to company_number.$$;
COMMENT ON COLUMN celex_facts.handelsregister_companies.former_registrar IS
  $$Previous court of registration; set when a company was transferred.$$;

COMMENT ON TABLE celex_facts.handelsregister_officers IS
  $$Officers per Handelsregister company -- role, name parts, term dates.$$;
COMMENT ON COLUMN celex_facts.handelsregister_officers.flag IS
  $$Register-supplied status marker carried through unparsed.$$;
COMMENT ON COLUMN celex_facts.handelsregister_officers.dismissed IS
  $$True when the officer was removed rather than having resigned or expired.$$;
COMMENT ON COLUMN celex_facts.handelsregister_officers.reference_no IS
  $$Register entry reference for the appointment or removal.$$;

COMMENT ON TABLE celex_facts.handelsregister_previous_names IS
  $$Historical company names. Essential for matching decision-era names, which
    are often decades older than the current register entry.$$;

COMMENT ON TABLE celex_facts.handelsregister_company_additional_data IS
  $$PLACEHOLDER, never populated. Intended to hold the Handelsregister document
    availability flags (AD, CD, DK, HD, SI, UT, VOE) per company.$$;

COMMENT ON TABLE celex_facts.gleif_jurisdication2countrycode IS
  $$GLEIF jurisdiction code to ISO country and subdivision code. The single
    normalisation point for jurisdictions across all three registers.
    NOTE the misspelling in the table name is real.$$;
COMMENT ON COLUMN celex_facts.gleif_jurisdication2countrycode.jurisdiction_type IS
  $$Whether the code denotes a country or a subdivision.$$;
COMMENT ON COLUMN celex_facts.gleif_jurisdication2countrycode.is_subdivision IS
  $$True for sub-national jurisdictions such as US-DE, which need the parent
    country resolved separately.$$;
COMMENT ON COLUMN celex_facts.gleif_jurisdication2countrycode.source_version IS
  $$GLEIF list version this row was loaded from -- codes are added over time.$$;


-- ---------------------------------------------------------------------
-- 10. Company resolution and matching
-- ---------------------------------------------------------------------

COMMENT ON TABLE celex_facts.company_investigated_celex_id2opencorp_index_company IS
  $$Scored candidate matches from investigated companies to OpenCorporates
    entries. This is the candidate POOL, not the decision -- 83,423 rows over
    4,097 roster rows, 20.4 candidates each on average and 2,436 at worst, and
    only 5,578 of them ever scored (match_score IS NOT NULL is exactly
    equivalent to resolution_rung IS NOT NULL). The accepted match is NOT
    fk_opencorp_idx on the roster row -- that column only ever holds the markers
    'LINKED' / 'NA' / 'AMBIGUOUS'. To resolve one company here, restrict to
    scored rows and take the best by match_score then rung, as
    company_registry_master_vw does.$$;
COMMENT ON COLUMN celex_facts.company_investigated_celex_id2opencorp_index_company.resolution_rung IS
  $$Which step of the relaxation ladder produced this candidate. Lower rungs are
    stricter queries; a match found at a high rung is weaker evidence.$$;
COMMENT ON COLUMN celex_facts.company_investigated_celex_id2opencorp_index_company.resolution_query IS
  $$Exact query string sent to OpenCorporates, kept for reproducing the match.$$;
COMMENT ON COLUMN celex_facts.company_investigated_celex_id2opencorp_index_company.resolution_jurisdiction IS
  $$Jurisdiction filter applied at this rung, or NULL when the rung dropped it.$$;
COMMENT ON COLUMN celex_facts.company_investigated_celex_id2opencorp_index_company.match_score IS
  $$Similarity between the decision name and the candidate name.$$;

COMMENT ON TABLE celex_facts.opencorp_index_companies IS
  $$OpenCorporates search results cached per query. Rows are candidates returned
    by the API, not confirmed matches.$$;
COMMENT ON COLUMN celex_facts.opencorp_index_companies.query_id IS
  $$Query that returned this row (opencorp_index_query.id).$$;
COMMENT ON COLUMN celex_facts.opencorp_index_companies.inactive IS
  $$API-reported inactivity flag; independent of current_status wording.$$;

COMMENT ON TABLE celex_facts.opencorp_index_company_detail IS
  $$Full per-company OpenCorporates detail payload -- officers, filings,
    financials, beneficial owners. Fetched on demand only; the single row
    reflects the API budget, not a broken pipeline.$$;


COMMENT ON VIEW celex_facts.company_registry_master_vw IS
  $$Registry MASTER ATTRIBUTES for every investigated company, tall: one row per
    (roster row, source) over GLEIF, the Handelsregister, OpenCorporates and the
    LLM index, with a common attribute set and a source_rank trust ladder
    (1 = LEI, 4 = LLM). Read it with DISTINCT ON (company_key) ORDER BY
    company_key, source_rank, link_confidence DESC NULLS LAST. It hides three
    traps: the 'NA' sentinels on the roster and on llm_index_companies, the fact
    that fk_opencorp_idx is a state marker and never an id, and the 83k-row
    OpenCorporates candidate pool from which exactly one scored row is picked.
    Ownership and parent structure are out of scope. Defined ONLY in
    migrate_registry_master_view.sql -- do not copy the definition elsewhere.$$;

COMMENT ON COLUMN celex_facts.company_registry_master_vw.company_key IS
  $$company_investigated2celex_id.id -- the only key common to all four sources,
    and the DISTINCT ON key. Grain is per roster row, so a company appearing in
    five decisions yields five keys.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.company_name IS
  $$Decision-era name from the roster, identical on every branch. Compare
    against registry_name to see what each register calls the same entity.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.celex_id IS
  $$The decision this roster row belongs to. Part of the roster natural key.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.roster_iso_country_code IS
  $$The roster own ISO country, carried so a consumer can detect source-versus-
    decision disagreement rather than silently trusting one.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.roster_jurisdiction_code IS
  $$company_investigated2celex_id.jurisdication_code -- spelled CORRECTLY here on
    purpose. The base column keeps the commissioned misspelling.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.source IS
  $$Which branch produced the row: lei, handelsregister, opencorporates, llm.
    opencorp_index_company_detail is NOT a source -- it is a COALESCE overlay on
    the opencorporates branch, flagged by has_detail.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.source_rank IS
  $$Trust ladder, 1 best. Issued identifier (1 LEI) > primary register
    (2 Handelsregister) > aggregator with a fuzzy link (3 OpenCorporates) >
    unconfirmed model assertion (4 LLM). A per-source constant, not a blended
    score -- refine with link_confidence and source_observed_at.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.link_method IS
  $$How the roster row was tied to this source row: the roster fk_ column name
    for lei and handelsregister, the OpenCorporates relaxation rung verbatim for
    opencorporates, an exact company_name match for llm. Populated on every
    branch -- without it an asserted identity is unauditable.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.link_confidence IS
  $$Ladder match_score for opencorporates. NULL elsewhere, and honestly so -- the
    LEI, Handelsregister and LLM links were never scored, not scored perfectly.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.registry_name IS
  $$The name as the SOURCE holds it, not the decision-era name.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.registry_id IS
  $$The source own identifier. Uninterpretable without registry_id_scheme, and
    for opencorporates unique only together with jurisdiction_code.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.registry_id_scheme IS
  $$Which registrar issued registry_id: LEI, handelsregister, opencorporates, or
    for the llm branch whichever of LEI/EUID/BRIS/handelsregister/SIREN answered
    first, else the free-text generic_corp_registrar.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.jurisdiction_code IS
  $$Registration jurisdiction normalised to the GLEIF / ISO 3166-2 canonical form
    (uppercase, hyphenated: DE, US-DE, AE-DU) so it is directly comparable with
    roster_jurisdiction_code. Best-effort on the llm branch, whose jurisdiction
    is free text resolved through gleif_jurisdication2countrycode labels. NOTE a
    value of 'NA' here is Namibia, not a sentinel.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.iso_country_code IS
  $$ISO 3166-1 alpha-2, the one geo column comparable across all four branches.
    LEI resolves legal_jurisdiction through gleif_jurisdication2countrycode and
    falls back to its legal-address country. The llm branch resolves free text
    ('Spain') through the same table labels, so it has gaps. NOTE 'NA' here is
    Namibia, not a sentinel -- corp_lei holds 107 genuinely Namibian rows.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.registered_address IS
  $$One-line address. The sources mean DIFFERENT things by it: GLEIF gives the
    legal address, the Handelsregister the registered address, OpenCorporates
    the registered office, and the llm branch the HEADQUARTERS. Disagreement
    between them is expected for offshore registrations, not a defect.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.legal_form IS
  $$OpenCorporates company_type, or the llm branch free-text sentence. NULL for
    lei and handelsregister on purpose -- GLEIF entity_category is a category
    and register_art (HRA/HRB) is a register section, neither is a legal form.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.status_text IS
  $$The source status wording verbatim, no vocabulary imposed. GLEIF emits
    ACTIVE/INACTIVE, the llm branch emits a free-text sentence.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.is_active IS
  $$Normalised tri-state. Derived only from GLEIF entity_status and the
    OpenCorporates inactive flag. NULL for handelsregister because its
    current_status vocabulary is unmeasured, and for llm because a sentence
    cannot be reduced to a boolean without guessing.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.incorporation_date IS
  $$OpenCorporates only. GLEIF carries LEI-registration dates rather than
    incorporation dates, the Handelsregister dump carries none, and
    llm_index_companies.established_since is free text that must not be cast.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.dissolution_date IS
  $$OpenCorporates only, for the same reasons as incorporation_date.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.registry_url IS
  $$Best available URL for the record at its source. CONSTRUCTED from the LEI for
    the lei branch (search.gleif.org). NULL for handelsregister and llm.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.source_observed_at IS
  $$When the source row was captured -- GLEIF dump load, Handelsregister
    retrievedAt, OpenCorporates fetch, LLM run. The staleness signal
    source_rank alone cannot give.$$;
COMMENT ON COLUMN celex_facts.company_registry_master_vw.has_detail IS
  $$True when the OpenCorporates row was overlaid with its full detail payload.
    False on every other branch by definition. The single detail row today is
    the API budget, not a broken pipeline.$$;
COMMENT ON TABLE celex_facts.opencorp_index_query IS
  $$Query log and cache key for OpenCorporates lookups. Prevents re-spending API
    quota on a query already asked, including one that already failed.$$;
COMMENT ON COLUMN celex_facts.opencorp_index_query.normalized_query IS
  $$Sanitised query text -- the real cache key. See the query-sanitization
    incident doc before changing normalisation.$$;
COMMENT ON COLUMN celex_facts.opencorp_index_query.fetched_pages IS
  $$Pages actually retrieved of total_pages; a gap means truncated results.$$;
COMMENT ON COLUMN celex_facts.opencorp_index_query.error_class IS
  $$Classified failure (rate_limit, not_found, bad_query) driving retry policy.$$;

COMMENT ON TABLE celex_facts.opencorp_api_daily_calls IS
  $$Daily API call counter enforcing the OpenCorporates quota. One row per day.$$;
COMMENT ON COLUMN celex_facts.opencorp_api_daily_calls.call_count IS
  $$Calls made on call_date; checked before every request.$$;

COMMENT ON MATERIALIZED VIEW celex_facts.lei2company_canonical_matches_mv IS
  $$Trigram nearest-neighbour match from canonical company names to
    corp_lei.legal_name, distance < 0.3. Materialised because the lateral join
    against 3.3M LEI rows is far too slow to run live. REFRESH after loading
    new canonical names.$$;
COMMENT ON COLUMN celex_facts.lei2company_canonical_matches_mv.distance IS
  $$Trigram distance -- LOWER is a better match. Zero is identical.$$;

COMMENT ON TABLE celex_facts.lei2company_canonical2alias_matches_tbl IS
  $$Frozen snapshot of alias-level LEI trigram matches. A table, not a view --
    it does not refresh with new aliases.$$;
COMMENT ON COLUMN celex_facts.lei2company_canonical2alias_matches_tbl.distance IS
  $$Trigram distance -- LOWER is a better match.$$;

COMMENT ON TABLE celex_facts.llm_index_companies IS
  $$LLM-enriched company profile: legal form, life cycle, products, geo market
    and every known register identifier. Model-generated content -- treat as a
    lead for resolution, not as a source of record.$$;
COMMENT ON COLUMN celex_facts.llm_index_companies.llm_model_id IS
  $$Model that produced the row. Required for reproducibility: rows from
    different models are not comparable.$$;
COMMENT ON COLUMN celex_facts.llm_index_companies.euid IS
  $$European Unique Identifier (BRIS). Distinct from the LEI.$$;
COMMENT ON COLUMN celex_facts.llm_index_companies.bris IS
  $$Business Registers Interconnection System reference.$$;
COMMENT ON COLUMN celex_facts.llm_index_companies.succession IS
  $$Merger, acquisition or renaming history the model reported.$$;
COMMENT ON COLUMN celex_facts.llm_index_companies.generic_corp_id IS
  $$Register id where no dedicated column exists; read with generic_corp_registrar.$$;
COMMENT ON COLUMN celex_facts.llm_index_companies.generic_corp_registrar IS
  $$Which register generic_corp_id belongs to.$$;
