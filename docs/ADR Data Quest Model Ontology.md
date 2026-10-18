
would an explicit representation of an ontology for the entities in my cartel case domain support reasonining, Q&A or development effort? Do not plan but discuss pro/cons
-->


Short answer: yes for Q&A grounding and development consistency, mostly no for machine reasoning — and the version worth having is "ontology as code," not OWL/RDF. You in fact already have an ontology; it's just implicit and smeared across four places. Here's the breakdown.

## What you already have (worth naming, because it frames the trade-off)

Your ontology currently exists in fragments: fact families in `FACT_EXTRACTOR.yaml`, entity kinds and the five relation labels in `entity_graph_models.py`, CHECK constraints in the DDL, and a prose rendering in `KB_SCHEMA_DESCRIPTION`. The question is not "ontology vs. no ontology" but "implicit and duplicated vs. explicit and single-source." That reframing matters because the cost/benefit differs sharply by which of your three goals you look at.

## Reasoning — weakest case

**Pro:** The cartel domain has genuinely ontological structure that your current graph flattens away, and it's legally load-bearing:

- **Undertaking vs. legal entity.** EU competition law fines the _undertaking_ (economic unit), addressed to legal entities held jointly and severally. A `Company` node conflates parent, subsidiary, and undertaking. Questions like "how often was Bayer fined?" silently get this wrong without a `part-of-undertaking` / `parent-of` relation.
- **Legal succession** (company A acquired B's cartel business — liability follows), **recidivism** (needs identity across decades and renamings), **horizontal vs. vertical infringement** as a class hierarchy over infringement types.
- Transitive/hierarchical queries (liability up a corporate chain) are where explicit relation semantics pay off.

**Con:** None of that requires an ontology _formalism_. A DL reasoner (OWL + HermiT etc.) buys you subsumption and consistency checking — but your facts are noisy LLM extractions, and monotonic logical inference over noisy assertions amplifies errors rather than catching them. Your actual inference needs (precedence rules like your gleif > opencorporates > fact ranking, fuzzy identity resolution, "never downgrade" upserts) are procedural and probabilistic — exactly what OWL is bad at. And the reasoning that answers user questions is done by the LLM at query time, which reasons over prose schema descriptions and flat SQL views far better than over triples. Your own D4 decision — "do not teach the LLM the raw graph tables" — already discovered this. Also consistent with your `Graph Databases are Dead.md` / psql-CTE-over-neo4j ADR: recursive CTEs cover the transitive queries.

## Q&A — moderate, indirect case

**Pro:** Text-to-SQL quality is mostly a function of how coherent and well-documented the vocabulary is. An explicit ontology gives you:

- Stable, closed sets of relation labels and entity kinds the planner can be prompted with exhaustively (vs. discovering "what relations exist?" by sampling).
- An alias/synonym layer ("addressee", "cartel participant", "undertaking" → company; "German" → `DE`) — you're already hand-writing this into `KB_SCHEMA_DESCRIPTION` per D4/WS4. An ontology makes it data instead of prompt prose, so the same mapping serves intent routing, the enumeration leg, and section-scope filters.
- Answerability checking: the planner can decide "this question needs a relation we don't model" instead of hallucinating a join.

**Con:** The LLM already carries the legal domain ontology in its weights — it knows what a leniency applicant is. What it needs from you is your _storage_ schema, not the domain's conceptual schema, and a flat view plus two example queries (your WS4) delivers that more reliably than any ontology serialization. Extra indirection layers between question and SQL historically hurt more than help. If Q&A were the only motivation, I'd say don't bother.

## Development effort — strongest case

**Pro:** This is where you're already bleeding. The gaps your own plan documents are drift symptoms of an implicit ontology: `companies_country` extracted but parsed by nothing; `attrs_json` clobbered because the upsert didn't know attribute semantics; `KB_SCHEMA_DESCRIPTION` hand-maintained in parallel with the DDL. A single explicit model — entity kinds, relations with domain/range (`fined: company → case`), fact-family → node/edge/attribute mappings, provenance ranks — from which you _generate or validate_ the CHECK constraints, the Pydantic enums, the mapping table in `entity_graph_mapping.py`, and the schema prose, turns four sources of drift into one source of truth. Every new fact family then forces the question "node, edge, attribute, or claim-only?" at declaration time instead of being silently dropped into `EXCLUDED_FAMILIES`. This also fits your stack: it's just Pydantic models + StrEnums + a YAML registry, which DSPy signatures can reference directly.

**Con:** Schema freeze risk. You're mid-flight — kinds, relations, and the country/attrs model changed within the last three commits. Formalizing too early means every experiment pays an ontology-update tax, and an ontology nobody updates is worse than none (it becomes authoritative-looking misinformation for the LLM). There's also a real ceiling: a full ontology-engineering effort (competency questions, Protégé, imported legal ontologies like ELI or FIBO for the corporate side) is a project in itself with near-zero incremental Q&A benefit over the lightweight version.

## Bottom line

- **Formal ontology stack (OWL/RDF/triplestore/reasoner): no.** Wrong fit for noisy extracted facts, redundant with LLM world knowledge, contradicts your psql-centric architecture decisions.
- **Explicit lightweight ontology-as-code: yes, primarily for development effort, secondarily for Q&A.** One declared model of kinds/relations/fact-mappings/aliases that generates the artifacts you currently hand-synchronize. Cheap, incremental, and it directly addresses the class of bug (unparsed facts, clobbered attributes, prompt/schema drift) you've already hit twice.
- **The one genuinely ontological modeling decision worth making soon** regardless of tooling: whether to represent _undertaking vs. legal entity_ and _parent/successor_ relations. That's a domain-correctness question for fines/recidivism Q&A, not an engineering-hygiene one, and adding it later means re-materializing the graph.

# Explicit Lightweight Ontology-as-Code: `src/pipelines/graph/ontology.py`

see: [implementation plan](~/a/celex-facts/docs/claude/plans/ontology-as-code.md)

## Context

The domain vocabulary (entity kinds, edge relations, fact families, their projections and synonyms) is currently defined implicitly in at least 6 hand-synchronized places: `EntityKind`/`EdgeRelation` StrEnums (`src/pipelines/graph/entity_graph_models.py`), `FAMILY_EDGE_MAP`/`EXCLUDED_FAMILIES` (`src/pipelines/graph/entity_graph_mapping.py`), the 52-entry fact catalog (`config_main/FACT_EXTRACTOR.yaml`), the hand-written planner prose `KB_SCHEMA_DESCRIPTION` (`src/pipelines/agent_dspy/retrieval_dispatch.py:52-224`), the SQL enums duplicated in `migrate_entity_graph.sql` and `pg_celex-ddl.sql`, and the `_TOPIC_SYNONYMS` phrase->fact-name map (`src/pipelines/agent_dspy/retrieval_text.py:85-165`). Drift between them has already produced real bugs (e.g. `companies_country` extracted but parsed by nothing).

This change introduces one explicit, declared domain model — pure Python, no OWL/RDF — that (a) becomes the single source from which `FAMILY_EDGE_MAP`/`EXCLUDED_FAMILIES` are derived, (b) generates the graph-relations section of the planner prose, and (c) drift-tests the SQL enums, the YAML catalog, the synonyms, and the remaining hand-written prose. The forcing function: every fact family in FACT_EXTRACTOR.yaml must declare its projection (EDGE / NODE_ATTRIBUTE / CLAIM_ONLY with a note) or a test fails.

**Template to imitate** (established repo pattern): `ProvisionMap`/`DEFAULT_PROVISION_MAP` in `src/pipelines/legal/infringement_concept.py` — StrEnum vocabulary + typed rows + immutable registry + static default, drift-guarded against the DB mirror.

**Decision — generate vs. drift-test:** hybrid. _Generate_ the relations section (it doesn't exist yet, so no regression risk; content is purely structural). _Drift-test_ the big hand-written facts prose (it encodes hard-won LLM guidance — anti-patterns, guarded casts — a generator would flatten), the SQL files (must stay hand-editable), and `_TOPIC_SYNONYMS` (ordering is behavior; only targets are validated). The rendered section describes relations in terms of the EAV `facts` table (fact names, `value_json` payload), NOT raw graph tables — consistent with decision D4 of the pending country/LEI plan; a `view_name` parameter lets it switch to documenting `company_case_relations_vw` once that plan lands. This plan does not depend on the country/LEI plan.

## The example (core of the new module)

New file `src/pipelines/graph/ontology.py` — pure, no DB/YAML I/O at import time. Representative sketch (real file adds full docstrings per CLAUDE.md):

```python
from dataclasses import dataclass
from enum import StrEnum

from pipelines.graph.entity_graph_models import EdgeRelation, EntityKind


class FactProjection(StrEnum):
    EDGE = "edge"                      # materialized as a typed edge
    NODE_ATTRIBUTE = "node_attribute"  # enriches a node (e.g. companies_country)
    CLAIM_ONLY = "claim_only"          # stays EAV-only; note= says why


class FactValueKind(StrEnum):
    BOOLEAN = "boolean"
    DATE = "date"
    DATE_RANGE = "date_range"
    CATEGORY = "category"
    TEXT = "text"


@dataclass(frozen=True)
class RelationSpec:
    """One edge relation with domain/range and payload semantics."""
    relation: EdgeRelation
    src_kind: EntityKind
    dst_kind: EntityKind
    description: str                      # one planner-facing sentence
    materialized: bool = True             # False: reserved (parent_of, active_in)
    amount_semantics: str | None = None   # e.g. "fine amount as extracted"


@dataclass(frozen=True)
class EdgeSpec:  # moved verbatim from entity_graph_mapping (same fields)
    relation: EdgeRelation
    src_kind: EntityKind
    dst_kind: EntityKind
    endpoint_field: str
    endpoint_is_src: bool = True
    amount_field: str | None = None


@dataclass(frozen=True)
class FactFamilySpec:
    """Declared projection of one facts.name family (mandatory per family)."""
    name: str
    projection: FactProjection
    value_kind: FactValueKind = FactValueKind.TEXT
    relation: EdgeRelation | None = None  # required when projection is EDGE
    endpoint_field: str | None = None     # value_json key of the endpoint
    endpoint_is_src: bool = True
    amount_field: str | None = None
    planner_visible: bool = True          # appears in KB schema prose
    note: str = ""                        # required for CLAIM_ONLY: why


RELATIONS: tuple[RelationSpec, ...] = (
    RelationSpec(EdgeRelation.PARTICIPATED_IN, EntityKind.COMPANY, EntityKind.CASE,
                 "the company took part in the conduct decided in the case"),
    RelationSpec(EdgeRelation.FINED, EntityKind.COMPANY, EntityKind.CASE,
                 "the Commission fined the company in this decision",
                 amount_semantics="fine amount exactly as extracted, e.g. 'ECU 5 000'"),
    # ... leniency_applied / leniency_granted / leniency_rejected / violated ...
    RelationSpec(EdgeRelation.PARENT_OF, EntityKind.COMPANY, EntityKind.COMPANY,
                 "corporate parent (reserved)", materialized=False),
    RelationSpec(EdgeRelation.ACTIVE_IN, EntityKind.CASE, EntityKind.MARKET,
                 "market activity (reserved)", materialized=False),
)

FACT_FAMILIES: tuple[FactFamilySpec, ...] = (
    FactFamilySpec("companies_investigated", FactProjection.EDGE,
                   relation=EdgeRelation.PARTICIPATED_IN, endpoint_field="company"),
    FactFamilySpec("fines", FactProjection.EDGE, relation=EdgeRelation.FINED,
                   endpoint_field="company", amount_field="amount"),
    # ... the other 6 EDGE families (exemption_applicants alias; the two
    #     case->provision families with endpoint_is_src=False) ...
    FactFamilySpec("companies_country", FactProjection.NODE_ATTRIBUTE,
                   note="becomes entities.country (country/LEI plan)"),
    FactFamilySpec("companies_investigated", FactProjection.CLAIM_ONLY,
                   note="superseded by companies_investigated for participation edges"),
    FactFamilySpec("leniency_case", FactProjection.CLAIM_ONLY,
                   value_kind=FactValueKind.BOOLEAN, note="case-level flag, no endpoint"),
    # ... one explicit entry for EVERY family in FACT_EXTRACTOR.yaml (52) ...
)

# Legacy fact names present in the live facts table / prose but not in the YAML.
# Exact membership confirmed during implementation (see Risks).
_DB_ONLY_FAMILIES: frozenset[str] = frozenset({...})


class Ontology:
    """Immutable registry; validates cross-references at construction."""

    def __init__(self, relations, families) -> None:
        self._relations = {s.relation: s for s in relations}
        self._families = {s.name: s for s in families}
        self._validate()  # ValueError on: EDGE family w/o materialized relation
                          # or endpoint_field; CLAIM_ONLY w/o note; dup names;
                          # relation dict not covering all EdgeRelation members

    def family_edge_map(self) -> dict[str, EdgeSpec]:
        """Derive the mapping consumed by entity_graph_mapping/the batch.
        src/dst kinds are copied from the relation spec — domain/range is
        declared once, on RelationSpec."""
        ...

    def non_edge_families(self) -> frozenset[str]: ...   # old EXCLUDED_FAMILIES role
    def planner_visible_families(self) -> frozenset[str]: ...
    def known_fact_names(self) -> frozenset[str]: ...


ONTOLOGY = Ontology(RELATIONS, FACT_FAMILIES)

# Reserved for the country/LEI plan: attribute-name -> {source: rank}.
PROVENANCE_RANKS: dict[str, dict[str, int]] = {}


def render_graph_schema_section(ontology: Ontology = ONTOLOGY,
                                view_name: str | None = None) -> str:
    """Deterministic planner prose: one terse line per materialized relation
    (domain -> range, description, source fact names, amount location in
    value_json). view_name switches to view-based guidance later (D4)."""
    ...
```

Rendered output appended to the planner schema (deterministic order — `EdgeRelation` declaration order, family names sorted):

```
Relations encoded in celex_facts.facts (typed vocabulary):
  - participated_in (company -> case): the company took part ... Fact names: companies_investigated.
  - fined (company -> case): the Commission fined the company ... Fact names: fines.
    Amount: value_json elements carry {"company", "amount"}; amount as extracted, e.g. 'ECU 5 000'.
  - violated (case -> provision): ... Fact names: infringement_concept, law_article_all_relevant.
(parent_of and active_in are reserved and not yet populated.)
```

## Phase A — pure refactor (zero behavior change)

1. **New** `src/pipelines/graph/ontology.py` as sketched: `FactProjection`, `FactValueKind`, `RelationSpec`, `EdgeSpec` (moved from `entity_graph_mapping.py`), `FactFamilySpec`, `RELATIONS`, `FACT_FAMILIES` (all ~52 YAML names + `_DB_ONLY_FAMILIES`), `Ontology`, `ONTOLOGY`, `PROVENANCE_RANKS`. Import direction: `entity_graph_models` <- `ontology` <- `entity_graph_mapping` (no cycle).
2. **Rewire** `src/pipelines/graph/entity_graph_mapping.py`:
    
    ```python
    from pipelines.graph.ontology import ONTOLOGY, EdgeSpec  # EdgeSpec re-exported
    
    FAMILY_EDGE_MAP: dict[str, EdgeSpec] = ONTOLOGY.family_edge_map()
    EXCLUDED_FAMILIES: frozenset[str] = ONTOLOGY.non_edge_families()
    ```
    
    `parse_rows`, `ParsedRow`, `_EMPTY_JSON_MARKERS` and helpers stay unchanged (parsing, not ontology). Import surface stable — `src/batch/Main_materialize_entity_graph.py` and existing tests untouched. Note `EXCLUDED_FAMILIES` widens from 2 names to all non-edge families; the batch skips via `FAMILY_EDGE_MAP.get`, so behavior is unchanged. If `test_entity_graph_mapping.py::test_excluded_families_absent` asserts exact set equality, relax it to membership of the original two + "edge map keys and excluded set partition the declared families".
3. **New** `src/pipelines/graph/tests/test_ontology.py` (unittest, pure; `__init__.py` already exists):
    - `TestOntologyIntegrity`: `test_every_edge_relation_member_has_a_relation_spec`, `test_edge_families_reference_materialized_relations_and_endpoint_fields`, `test_claim_only_families_carry_explanatory_note`, `test_relation_domain_range_matches_derived_edge_specs`
    - `TestDerivedViewsMatchLegacyLiterals`: `test_family_edge_map_equals_previous_hand_written_map` (the 8-entry dict hardcoded in the test as regression pin), `test_company_attribute_facts_remain_non_edge` (companies_country / companies_address / companies_industry / company_recidivism)
4. **New** `src/pipelines/graph/tests/test_ontology_drift.py`, `TestSqlEnumDrift`: regex-extract quoted labels from the `CREATE TYPE ... AS ENUM` blocks of both `src/pipelines/rag/pgvector/migrate_entity_graph.sql` and `pg_celex-ddl.sql` (paths via `pathlib` relative to the module); assert **ordered** equality with `EntityKind`/`EdgeRelation` values, and that the two SQL files agree.

## Phase B — generated prose + catalog drift guards

5. **Prose wiring** in `src/pipelines/agent_dspy/retrieval_dispatch.py`: rename the existing literal (lines 52-224) to `_KB_BASE_DESCRIPTION` (content untouched), then
    
    ```python
    KB_SCHEMA_DESCRIPTION = _KB_BASE_DESCRIPTION + "\n" + render_graph_schema_section()
    ```
    
    No call-site changes: `master_planner_module.py` and `retrieval_modules.py` keep importing `KB_SCHEMA_DESCRIPTION`; existing `assertIn` drift tests keep passing (append-only). New dependency `agent_dspy -> pipelines.graph` is acyclic.
6. **Extend** `test_ontology_drift.py`:
    - `TestYamlCatalogDrift` (`yaml.safe_load` of `config_main/FACT_EXTRACTOR.yaml`): `test_every_yaml_fact_has_declared_projection` (yaml names subset of `known_fact_names()`; failure message tells the developer to add a `FactFamilySpec` — **the forcing function**); `test_declared_families_not_in_yaml_are_explicit_db_only`.
    - `TestSynonymTargetDrift`: every target in `retrieval_text._TOPIC_SYNONYMS` is a declared fact name. Known findings to resolve: targets `infringement_category` and `infringement_sub_category` (`retrieval_text.py:96-97`) are not real fact names — either retarget them to the real `infringement_category_*`/`infringement_subcategory_*` names or declare an explicit `PREFIX_ALIASES` set on the ontology; do not hide them in `_DB_ONLY_FAMILIES`.
    - `TestPlannerProseDrift`: `test_prose_mentions_every_planner_visible_family` (assertIn per name); families genuinely absent from today's prose (`infringement_concept`, the `case_*_book` family, `services`, `celex_case_id`, ...) get `planner_visible=False` with a note — an explicit decision per family instead of silence. `test_prose_contains_rendered_graph_section`.
    - In `test_ontology.py`, `TestRenderGraphSchemaSection`: `test_render_is_deterministic`, `test_render_mentions_every_materialized_relation_and_source_family`, `test_render_omits_reserved_relations_from_queryable_list`, `test_render_contains_no_emoji_or_non_ascii`.

## Phase C — deferred (not in this change)

After the country/LEI plan lands: pass `view_name="celex_facts.company_case_relations_vw"` to the renderer (generates that plan's D4 section), fill `PROVENANCE_RANKS["country"]` from `CountrySource`, optionally relocate `_TOPIC_SYNONYMS` into the ontology.

## Critical files

- `src/pipelines/graph/ontology.py` — new module (the ontology itself)
- `src/pipelines/graph/entity_graph_mapping.py` — `EdgeSpec` moves out; `FAMILY_EDGE_MAP`/`EXCLUDED_FAMILIES` become derived
- `src/pipelines/agent_dspy/retrieval_dispatch.py` — literal renamed `_KB_BASE_DESCRIPTION`, rendered section appended
- `src/pipelines/graph/tests/test_ontology.py`, `src/pipelines/graph/tests/test_ontology_drift.py` — new tests
- Read-only drift inputs: `config_main/FACT_EXTRACTOR.yaml`, `src/pipelines/rag/pgvector/migrate_entity_graph.sql`, `pg_celex-ddl.sql`, `src/pipelines/agent_dspy/retrieval_text.py`
- Reused as-is: `EntityKind`/`EdgeRelation` (`entity_graph_models.py` — they mirror DB enum labels and stay put), `FactExtractorParamVO` loader pattern, `ProvisionMap` template

## Verification

1. `.venv/bin/python -m unittest pipelines.graph.tests.test_ontology pipelines.graph.tests.test_ontology_drift pipelines.graph.tests.test_entity_graph_mapping`
2. Prose consumers: `.venv/bin/python -m unittest pipelines.agent_dspy.tests.test_fact_name_fallback pipelines.agent_dspy.tests.test_qa_retrieval_integration`
3. Behavior pin: `.venv/bin/python -c "from pipelines.graph.entity_graph_mapping import FAMILY_EDGE_MAP; print(sorted(FAMILY_EDGE_MAP))"` prints the same 8 families as before the refactor.
4. `ruff check` — no errors.
5. Spot-check: print `KB_SCHEMA_DESCRIPTION` and eyeball the appended relations section (terse, one line per relation, no non-ASCII).

## Risks / notes

- `_DB_ONLY_FAMILIES` exact membership must be confirmed during implementation against the prose free-text list (`retrieval_dispatch.py:85-97`); the runtime catalog is DB-built (`master_planner_text.py:294` `build_fact_name_catalog`), and drift tests deliberately stay file-based so they run without a DB. Optional settings-gated integration test comparing DB DISTINCT names to `known_fact_names()`.
- Appending ~25 lines grows every planner prompt — keep the rendered section terse.
- SQL enum label order is asserted ordered (Postgres enum order is meaningful; both files already follow declaration order).
- `entity_graph_repository.py:330` has one raw `kind = 'company'` literal in a read CTE — leave as-is (SQL string), noted for awareness.