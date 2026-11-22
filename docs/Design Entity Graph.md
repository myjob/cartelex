#graph
# Design — Entity Graph (typed nodes and edges materialized from the fact store)

This document describes what the entity graph is, which facts feed each edge, how the
materialization batch works, and how to operate it: full rebuild, dry run, a single
decision, and a subset of decisions read from a list file.

Related plans (history, in `docs/claude/plans/`): `phase5-1-entity-graph.md` (origin),
`entity-graph-company-layer-on-companies_investigated.md`,
`entity-graph-identity-via-registry-master-view-20260909.md`, `heideltime-temporal-graph.md`,
`entity-graph-qa-wiring.md`, `graph-search-traversal-tool-20260907.md`,
`knowledge-ingest-entity-graph-evidence-20260912.md`.
Related docs: [ADR Knowledge Graph in psql CTE vs. neo4j](ADR%20Knowledge%20Graph%20in%20psql%20CTE%20vs.%20neo4j.md),
[Design Heideltime Events](Design%20Heideltime%20Events.md), [Design Fact Verifier](Design%20Fact%20Verifier.md).

Source:
- batch: [Main_materialize_entity_graph.py](file://~/a/celex-facts/src/batch/Main_materialize_entity_graph.py)
- pure mapping: [entity_graph_mapping.py](file://~/a/celex-facts/src/pipelines/graph/entity_graph_mapping.py), [entity_graph_models.py](file://~/a/celex-facts/src/pipelines/graph/entity_graph_models.py), [heideltime_actor_resolution.py](file://~/a/celex-facts/src/pipelines/graph/heideltime_actor_resolution.py)
- repository: [entity_graph_repository.py](file://~/a/celex-facts/src/pipelines/rag/pgvector/entity_graph_repository.py)
- DDL: `migrate_entity_graph.sql`, `migrate_entity_graph_dated_in_enum.sql`, `migrate_graph_qa_views.sql` (all in `src/pipelines/rag/pgvector/`)

---

## 1. Overview

The facts in `celex_facts.facts` are EAV rows: one row per (decision, fact name), with the
value as text and JSON. Cross-case questions ("which companies appear in several cartels",
"all fines of company X") are awkward over that shape. The entity graph projects the facts
into two relational tables:

- `celex_facts.entities`: typed nodes, `kind` in `company | case | market | provision`,
  unique on `(kind, natural_key)`.
- `celex_facts.entity_edges`: typed relations between two nodes, scoped to one decision
  (`celex_id`), each carrying `source_fact_id`, the fact that produced it.

Design choices:

- **Deterministic, no LLM.** The batch reads facts and writes rows. It never becomes another
  inline `dspy.LM` call site, so a rebuild needs no DGX and no LLM budget.
- **Postgres, not a graph database.** Traversals are recursive CTEs, the same pattern as
  `celex_facts.fact_provenance()` (see the ADR).
- **Lineage on every edge.** `source_fact_id` is `NOT NULL` and points into `facts.id`, so a
  graph-derived answer still resolves to chunk evidence through `fact_provenance()`.

Size on 20261119: 801 case, 7512 company and 7 provision nodes; 12663 edges.

| relation | edges | decisions |
|---|---|---|
| `participated_in` | 5782 | 746 |
| `dated_in` | 2228 | 643 |
| `leniency_applied` | 1975 | 429 |
| `fined` | 1521 | 312 |
| `violated` | 670 | 670 |
| `leniency_granted` | 377 | 130 |
| `leniency_rejected` | 110 | 62 |

`active_in` (markets) and `parent_of` exist in the enum but are not materialized: market
coverage is thin and there is no fact family for corporate hierarchy.

---

## 2. Which fact feeds which edge

`FAMILY_EDGE_MAP` (in `entity_graph_mapping.py`) declares one `EdgeSpec` per fact family.

| fact (`facts.name`) | relation | direction | payload |
|---|---|---|---|
| `companies_investigated` | `participated_in` | company to case | none |
| `fines` | `fined` | company to case | `amount` |
| `leniency_applicants` | `leniency_applied` | company to case | none |
| `exemption_applicants` | `leniency_applied` (alias) | company to case | none |
| `leniency_granted` | `leniency_granted` | company to case | `amount` (reduction) |
| `leniency_rejected` | `leniency_rejected` | company to case | none |
| `infringement_concept` | `violated` | case to provision | none |
| `law_article_all_relevant` | `violated` (fallback) | case to provision | none |
| `heideltime_event` | `dated_in` | company to case | `actors`, `first_date`, `last_date`, `event_count` |

Rules worth knowing:

- **`companies_investigated` is the only company roster.** The retired families
  (`companies`, `companies_all`, `companies_canoncial`, `company_country`) are absent from
  the map, so a re-run can never bring back an edge from a retired fact. The roster was
  written with two row keys (`companies` and `company`); both are accepted, and the
  run counts the variant (`variant_key_*`) so the data defect stays visible.
- **`violated` prefers `infringement_concept`.** Only when that fact yields no rows does the
  batch fall back to `law_article_all_relevant`, resolving each `Article N` through the
  legal-provision ontology **for the decision's treaty era**. That is the one place the
  graph reads `eu_commission_decision_date`: Article 85/86 (EEC), 81/82/86 (EC, Amsterdam
  1999) and 101/102/106 (TFEU, Lisbon 2009) mean the same concepts under different numbers.
- **`dated_in` comes from `heideltime_event`, not from the decision date.** It places a
  company inside a case because the HeidelTime event stream names it as an actor. It is
  evidence of activity, not of participation, and is kept out of
  `company_case_relations_vw`. Actors are matched only against the company nodes this
  decision already has (no node is ever created from an LLM-written actor string); an
  actor matching several companies is refused and logged, never guessed.
- **Excluded on purpose** (`EXCLUDED_FAMILIES`): per-company attribute facts
  (`companies_country`, `companies_address`, `companies_industry`, `company_recidivism`)
  and the per-case HeidelTime derivations (`heideltime_raw`, `heideltime_event_agg`,
  `cartel_period`), which have no company endpoint.

---

## 3. How one decision is materialized

`_materialize_celex`, one transaction per decision:

1. `delete_edges_for_celex`: drop every edge of this decision.
2. Upsert the case node (`natural_key = celex_id`).
3. Company families, in fact order: parse the rows, resolve each company (section 4),
   upsert the company node and the edge. Every company node touched is collected for step 5.
4. `violated`: `infringement_concept`, else the treaty-era fallback.
5. `dated_in`: project the `heideltime_event` events onto the companies of step 3, one
   aggregated edge per company. This must run last.
6. Commit, or roll back with `--dry-run`.

A decision that fails is rolled back and counted in `errors`; the loop continues with the
next one. The process exits 0 either way, so check the summary line (section 6.5).

### Idempotency and cascade

- Nodes upsert on `(kind, natural_key)` and are **never deleted**: a company collapses to
  one node across all decisions, and a decision's rebuild cannot remove a node another
  decision uses.
- Edges are delete-then-insert per decision, so a rebuild never leaves stale edges, and a
  crash never half-writes a decision.
- `entity_edges.source_fact_id` is `ON DELETE CASCADE`. Deleting a fact deletes its edges.
  **Regenerating the fact does not bring them back**: re-run the batch for the affected
  decisions as the last step of any fact remediation.
- An **upsert** of a fact keeps its `facts.id` (and `created_at`), so the edges survive but
  still show the **old** value until the batch runs again. Re-extraction therefore always
  needs a graph rebuild for the same decisions.

---

## 4. Company identity

The roster says *which* companies a decision concerns; `resolve_company` decides *who* each
one is, in tiers:

1. `registry`: the roster's own resolved `fk_lei` through
   `celex_facts.company_registry_master_vw` (`source = 'lei'`), a primary-key join.
2. `trigram`: the pg_trgm probe against `corp_lei` (threshold
   `settings.ENTITY_LEI_MATCH_THRESHOLD`) for mentions that never became a roster row.
3. `below_floor`: names shorter than `settings.ENTITY_LEI_MIN_NAME_LENGTH` are never keyed on
   an LEI; both sources fail on short names, in different ways.
4. `unresolved`: name-keyed node, counted in `unresolved_companies`.

The `lei_tier_*` counters show the mix per run; a shift between two runs means the identity
source changed.

---

## 5. Read surfaces and consumers

| surface | what it serves |
|---|---|
| `company_case_relations_vw` | the five roster-grounded company relations (no `dated_in`) |
| `case_provision_relations_vw` | `violated` edges with the source fact name |
| `case_timeline_vw` | one row per HeidelTime event; joins back to the `dated_in` payload's verbatim actors to recover the per-event grain. It is a view: nothing to rebuild |
| `EntityGraphRepository.co_participation_network` / `co_participants_by_name` | the `graph_search` tool of both UI planners (`ui/backend_server/oi_dspy.py`), bounded by `GRAPH_SEARCH_MAX_HOPS`, `_MAX_SEEDS`, `_MAX_PEERS` |
| `EntityGraphRepository.relations_for_facts` | `ValidatedRelation` evidence for knowledge ingestion: a knowledge nugget that contradicts a graph edge is flagged (`KNOWLEDGE_GRAPH_EVIDENCE_*`) |

Switches: `GRAPH_QA_ENABLED` (flat views), `GRAPH_SEARCH_ENABLED` (traversal tool).

---

## 6. Operation

All commands run from the repository root. They write to the live DB except `--dry-run`.

### 6.1 Full rebuild

```bash
.venv/bin/python -m batch.Main_materialize_entity_graph
```

Iterates over every decision in the fact store (`distinct_celex_ids`) and logs progress
every 100 decisions.

### 6.2 Dry run

```bash
.venv/bin/python -m batch.Main_materialize_entity_graph --dry-run
.venv/bin/python -m batch.Main_materialize_entity_graph --dry-run --celex-id 37396_45_3
```

Real counts, every transaction rolled back.

### 6.3 One decision

```bash
.venv/bin/python -m batch.Main_materialize_entity_graph --celex-id 37396_45_3
```

### 6.4 A subset from a list file

The batch takes one `--celex-id` per call and has no `--celex-list-file`, so a subset is a
shell loop over a file with one id per line:

```bash
list=reports/decision_date_era_changed_20261117.txt
log=logs/entity_graph_subset_$(date +%Y%m%d_%H%M).log
while read -r c; do
  [ -n "$c" ] || continue
  .venv/bin/python -m batch.Main_materialize_entity_graph --celex-id "$c" < /dev/null
done < "$list" 2>&1 | tee "$log"
```

- `read -r` keeps backslashes; `[ -n "$c" ]` skips blank lines (a trailing newline).
- `< /dev/null` matters: without it a child process that reads stdin would eat the rest of
  the list and the loop would stop early without an error.
- Add `--dry-run` after the module name to rehearse the subset first.
- Each call starts a new process (imports, DB engine, provision ontology): measured about
  22 s per decision on 20261119, so the 51 of 6.7 take about 20 minutes. For more than a
  few hundred decisions a full rebuild (6.1) is cheaper.

### 6.5 Checking a run

```bash
grep -c "materialization complete" "$log"          # one per decision in the list
grep "materialization complete" "$log" | grep -v "errors=" | wc -l   # should equal the line above
grep -E "Failed to materialize|errors=" "$log"
```

Counters in the summary line worth reading: `errors`, `roster_missing`, `roster_empty`,
`heideltime_fact_missing` (a rebuild that ran before the HeidelTime fact was regenerated),
`heideltime_no_actor_resolved`, `unresolved_companies`, `lei_tier_*`.

Stale `violated` edges, the check for a subset after `infringement_concept` was re-extracted
(read-only):

```sql
WITH cur AS (
  SELECT f.celex_id, string_agg(DISTINCT j->>'infringement_concept', ' | ' ORDER BY j->>'infringement_concept') AS fact_concepts
  FROM celex_facts.facts f
  LEFT JOIN LATERAL jsonb_array_elements(
    CASE WHEN pg_input_is_valid(f.value_json, 'jsonb') AND jsonb_typeof(f.value_json::jsonb) = 'array'
         THEN f.value_json::jsonb ELSE '[]'::jsonb END) j ON true
  WHERE f.name = 'infringement_concept' GROUP BY 1),
edge AS (
  SELECT e.celex_id, string_agg(DISTINCT p.canonical_name, ' | ' ORDER BY p.canonical_name) AS edge_concepts
  FROM celex_facts.entity_edges e JOIN celex_facts.entities p ON p.entity_id = e.dst_entity_id
  WHERE e.relation = 'violated' GROUP BY 1)
SELECT celex_id, edge_concepts, fact_concepts
FROM cur LEFT JOIN edge USING (celex_id)
WHERE coalesce(fact_concepts, '') <> coalesce(edge_concepts, '');
```

### 6.6 When to rebuild

Rebuild exactly the decisions whose input changed:

| change | rebuild |
|---|---|
| a new decision ingested | that decision |
| any fact in `FAMILY_EDGE_MAP` re-extracted or deleted | those decisions |
| `heideltime_event` regenerated | those decisions (the cascade removed their `dated_in` edges) |
| `eu_commission_decision_date` changed | only decisions whose **treaty era** changed, and only if they use the `law_article_all_relevant` fallback; in practice the decisions whose `infringement_concept` was re-run for the same reason |
| the registry master view or LEI index refreshed | full rebuild (identity can move) |
| `case_timeline_vw` | never, it is a view |

The Main pipeline planner (`batch/main_pipeline/planner.py`) applies the same rule: it
schedules the `entity_graph` step for new decisions and for decisions where a forced or
missing fact is in `GRAPH_INPUT_FACTS` (the map families, the roster, `heideltime_event`,
`eu_commission_decision_date`).

### 6.7 Worked example: ISSUES #379 (decision date, 20261118)

The date changed for 168 decisions, but the treaty era changed for 51 and only those had
`infringement_concept` and the categories re-extracted (step 6a). The graph depends on the
date only through the treaty era, so the subset is the 51:

```bash
list=reports/decision_date_era_changed_20261117.txt
log=logs/entity_graph_subset_$(date +%Y%m%d_%H%M).log
while read -r c; do
  [ -n "$c" ] || continue
  .venv/bin/python -m batch.Main_materialize_entity_graph --celex-id "$c" < /dev/null
done < "$list" 2>&1 | tee "$log"
```

Before the rebuild, the query in 6.5 found 12 of the 51 with stale `violated` edges: 4
where `public_undertaking` was added or `abuse_of_dominance` dropped, and 8 DEC-AT decisions
with no `violated` edge at all although the fact now has a concept. After the rebuild the
query must return no row for these 51.

---

## 7. Known defects

- **Combined concept nodes.** `infringement_concept` stores several concepts in one row
  (`abuse_of_dominance, anticompetitive_agreement`), and the batch keys the provision node on
  that string. So there are 7 provision nodes, 4 of them combinations, instead of 3 concepts,
  and a query for `abuse_of_dominance` misses the combined nodes. Tracked in ISSUES #380.
- **Name-keyed companies split.** Only part of the company nodes are LEI-keyed; aliases of
  one firm can sit on several nodes. `graph_search` returns the matched seeds so an answer
  shows which nodes a name stem resolved to.
- **No list-file option.** Subsets need the shell loop of 6.4.
