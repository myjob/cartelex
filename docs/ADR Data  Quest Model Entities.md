
Would a ontology help with the entity type in table? the design of this table is odd, because the "kind" enum is disjunct to the attributes. 
Example: if kind == case, the lei_id and nace dont make sense at all. I would prefer a data model where the entities stay meta level and is not referencing certain attributes of certain attribute types. Give me options how to improve this 

CREATE TABLE celex_facts.entities ( entity_id uuid DEFAULT gen_random_uuid() NOT NULL, kind celex_facts."entity_kind_enum" NOT NULL, canonical_name text NOT NULL, natural_key text NOT NULL, lei_id text NULL, nace_code text NULL, celex_id text NULL, article_number int4 NULL, attrs_json text NULL, created_at timestamptz DEFAULT now() NOT NULL, CONSTRAINT entities_pkey PRIMARY KEY (entity_id), CONSTRAINT uq_entities_kind_natural UNIQUE (kind, natural_key) );

-->

Yes — this is exactly the kind of thing the ontology should own. What you're describing is the classic _single-table inheritance_ smell: one table for a polymorphic concept, with the union of all subtype attributes as nullable columns. Nothing stops a `case` row from carrying an `lei_id` today; the "which attribute belongs to which kind" rule exists only in the materializer's head. The ontology fix is to declare attribute applicability explicitly (`AttributeSpec(name="lei_id", applies_to={COMPANY})`) and then _derive_ whichever physical model you choose — constraints, extension tables, views, or validators. Here are the realistic options, roughly in ascending order of migration effort.

## Option 1 — Keep the table, add ontology-generated CHECK constraints

Keep the shape, close the integrity hole:

```sql
ALTER TABLE celex_facts.entities
  ADD CONSTRAINT ck_entities_lei_company_only
    CHECK (lei_id IS NULL OR kind = 'company'),
  ADD CONSTRAINT ck_entities_celex_case_only
    CHECK (celex_id IS NULL OR kind = 'case'),
  ADD CONSTRAINT ck_entities_article_provision_only
    CHECK (article_number IS NULL OR kind = 'provision');
```

The ontology declares `applies_to` per attribute; a drift test (same pattern as the SQL-enum test we planned) asserts the DDL's CHECKs match the declarations, and the repository validates before upsert with a Pydantic discriminated union on `kind`.

**Pros:** cheapest by far; no writer or reader changes; keeps the flat real columns that your own D1 decision says LLM-generated SQL needs. 
**Cons:** doesn't address your aesthetic objection — the table still _names_ subtype attributes; every new attribute (country is arriving via the pending plan) is another nullable column plus another CHECK. This is a patch, not the model you asked for.

## Option 2 — Slim meta core + typed JSONB `attrs`, validated by the ontology

`entities` shrinks to the truly universal part — `entity_id, kind, canonical_name, natural_key, created_at` — and everything kind-specific moves into a JSONB `attrs` column, validated at the write boundary by per-kind Pydantic models (`CompanyAttrs(lei_id, nace_code, country)`, `CaseAttrs(celex_id)`, `ProvisionAttrs(article_number)`) that the ontology registers as a discriminated union on `kind`.

**Pros:** maximally meta; adding an attribute is a Pydantic field, not a migration. 
**Cons:** this directly contradicts your own hard-won D1 decision ("real columns are what LLM-generated SQL handles reliably") — every Q&A query grows `attrs->>'lei_id'` casts; typed indexing needs expression indexes; NULL/type enforcement lives only in Python. You'd have to compensate with generated per-kind views, at which point you've built most of Option 3 anyway but with weaker guarantees. I'd argue against this one for your stack.

## Option 3 — Class-table inheritance: meta core + per-kind extension tables

The textbook normalization. Same slim core as Option 2, plus:

```sql
CREATE TABLE celex_facts.entity_company (
    entity_id uuid PRIMARY KEY REFERENCES celex_facts.entities,
    lei_id text, nace_code text, country text, country_source text);
CREATE TABLE celex_facts.entity_case (
    entity_id uuid PRIMARY KEY REFERENCES celex_facts.entities,
    celex_id text NOT NULL);
CREATE TABLE celex_facts.entity_provision (
    entity_id uuid PRIMARY KEY REFERENCES celex_facts.entities,
    article_number int4 NOT NULL);
```

The ontology declares each kind's attribute set; the extension DDL and one flat convenience view per kind (`company_entities_vw` = core JOIN extension) are generated or drift-tested from it. The LLM only ever sees the flat views — consistent with D4 ("don't teach the LLM the raw graph tables").

**Pros:** real columns with real types and NOT NULLs _where they're meaningful_; entities is genuinely meta-level; extension tables evolve independently (the country/LEI plan's `country` columns land in `entity_company` instead of widening the core). 
**Cons:** the upsert becomes a two-table transaction (your precedence-aware `upsert_entity` gets a bit hairier); the pending country/LEI plan is written against the wide table and would need rebasing; reads need the views. Migration itself is easy — single writer, data is re-materializable by rerunning `Main_materialize_entity_graph.py`.

## Option 4 — Recognize that most of these are _identifiers_, not attributes

Look at the actual columns: `lei_id`, `nace_code`, the OpenCorporates numbers headed for `attrs_json` — these are all **codes from external identification schemes**. That generalizes into one genuinely meta-level table:

```sql
CREATE TABLE celex_facts.entity_identifiers (
    entity_id uuid REFERENCES celex_facts.entities,
    scheme text NOT NULL,      -- 'lei' | 'nace' | 'oc' | ... (ontology-declared)
    value text NOT NULL,
    source text,               -- provenance: 'gleif' | 'opencorporates' | 'fact'
    UNIQUE (entity_id, scheme));
```

The ontology declares `IdentifierScheme(name, applies_to, description, provenance_ranks)` — which folds your D2 precedence logic into the same registry. Meanwhile `celex_id` and `article_number` aren't attributes at all: they're the _identity_ of case/provision entities and are already encoded in `natural_key` (`celex:...`, article-based keys) — so they can simply be dropped from the core, re-derivable in a view. This is how registry-style systems (GLEIF itself, OpenCorporates, Wikidata) model it, and it handles the future for free: a company with an LEI _and_ an OC number _and_ a national register number needs no schema change, and per-identifier provenance comes built in.

**Pros:** the most honest data model for what you're actually storing; provenance per identifier; zero migrations for new schemes. 
**Cons:** it's a key-value shape, so direct LLM SQL against it is poor — the flat per-kind view (core + crosstab of the schemes the ontology declares) is mandatory, not optional; the precedence upsert moves from column-level `CASE` expressions to row-level logic.

## Recommendation

Options 3 and 4 compose, and that combination is what I'd aim for: **slim meta core + `entity_identifiers` for external codes (LEI, NACE, OC) + drop `celex_id`/`article_number` into the natural-key convention + ontology-generated flat per-kind views for the Q&A layer.** True attributes that aren't identifiers (like `country`) go on a small `entity_company` extension per Option 3. The ontology is what makes this safe rather than clever: `applies_to`, scheme vocabulary, and provenance ranks are declared once, and the views, constraints, and Pydantic write-validators are derived and drift-tested from that single declaration — the same pattern as `FAMILY_EDGE_MAP`.

Two practical caveats. First, if you want the quick fix now and the redesign later, Option 1's CHECK constraints are one small migration and immediately stop bad rows. Second, whichever you pick should be decided **before** implementing the country/LEI plan — it's written against the wide table (country as a column on `entities`), and rebasing it onto the slim-core model afterward means redoing its migration, upsert, and view work.