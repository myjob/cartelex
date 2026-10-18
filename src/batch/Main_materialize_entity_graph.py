"""Materialize the typed entity graph from ``celex_facts.facts`` (Phase 5.1).

Projects the EAV fact store into typed nodes (``celex_facts.entities``) and
lineage-grounded relations (``celex_facts.entity_edges``).  Deterministic — no
LLM — so it never becomes another inline ``dspy.LM`` call site.  Company mentions
resolve to LEIs through ``celex_facts.company_registry_master_vw`` (the roster's
own ``fk_lei``), with the pg_trgm index as the off-roster fallback; provisions
resolve to canonical concepts via the legal-provision ontology; every edge
carries ``source_fact_id`` so ``celex_facts.fact_provenance()`` still resolves
the underlying chunk evidence.

Relations materialized: ``participated_in`` (companies_investigated), ``fined``
(fines), ``leniency_applied`` (leniency/exemption applicants), ``leniency_granted``,
``leniency_rejected``, ``violated`` (infringement_concept preferred, else
law_article_all_relevant resolved through the ontology), and ``dated_in`` (the
temporal layer, from ``heideltime_event``).  ``active_in`` (markets) and
``parent_of`` are reserved but not materialized this phase.

Temporal layer
--------------
``dated_in`` places a company inside a case on the strength of the HeidelTime
event stream, carrying aggregated date bounds as payload.  It is evidence of
*activity*, not of participation, and is deliberately excluded from
``company_case_relations_vw``.  Actors are matched only against the company
nodes this decision already has, so the pass runs after the company layer is
complete and never invents a node from an LLM-written actor string; see
``pipelines.graph.heideltime_actor_resolution`` for the matching rules.

Because ``entity_edges.source_fact_id`` cascades, deleting a ``heideltime_event``
fact destroys that decision's temporal edges.  Regenerating the fact does *not*
restore them - a re-run of this batch for the affected CELEX ids is the final
step of any heideltime remediation, and ``heideltime_fact_missing`` in the run
summary is what makes a rebuild-before-regeneration visible.

Company source
--------------
``companies_investigated`` is the *only* source of the company layer.  The
graph's first materialization drew ``participated_in`` from ``companies_canoncial``,
which has since been retired from ``FACT_EXTRACTOR.yaml`` and deleted from the
fact store together with ``companies``, ``companies_all`` and ``company_country``;
those names are absent from ``FAMILY_EDGE_MAP`` and any fact still carrying one
is skipped (``FAMILY_EDGE_MAP.get`` returns ``None``), so a re-run can never
resurrect an edge from a retired fact.  Because the roster fact was written with
two different row keys, the spec accepts both (``companies`` then ``company``);
the run counts the variant so the underlying data defect stays visible, and it
counts documents whose roster yields no company at all rather than letting them
disappear silently.

Rerunning this batch is what restores the company layer after the retirement
migrations - the ``entities`` company nodes survive those deletes (no FK to
``facts``), but every ``participated_in`` edge is cascaded away with them.

Company identity
----------------
The roster names *which* companies a decision concerns; ``resolve_company``
decides *who* each one is.  Since 20260909 that runs through
``celex_facts.company_registry_master_vw`` at ``source = 'lei'`` - the roster's
already-resolved ``fk_lei``, a primary-key join - before falling back to the
pg_trgm probe against ``corp_lei`` for mentions that never became a roster row.
Below ``settings.ENTITY_LEI_MIN_NAME_LENGTH`` no tier may key a node on an LEI
at all: both sources fail on short names and fail differently, so a short
mention stays name-keyed.  The ``lei_tier_*`` counters in the run summary report
the mix (``registry`` / ``trigram`` / ``below_floor`` / ``unresolved``), which is
what makes a change in identity source visible between two runs.

Idempotency
-----------
Entities upsert on ``(kind, natural_key)`` and are never deleted, so a company
collapses to one node across every decision it appears in.  Edges are rebuilt per
CELEX inside one transaction (delete-then-insert) so a re-extracted fact does not
leave stale edges.  Re-running is safe; a crash never half-writes a document.

Usage
-----
    PYTHONPATH=src .venv/bin/python src/batch/Main_materialize_entity_graph.py
    PYTHONPATH=src .venv/bin/python src/batch/Main_materialize_entity_graph.py --dry-run
    PYTHONPATH=src .venv/bin/python src/batch/Main_materialize_entity_graph.py --celex-id 32021R0001

The migration ``migrate_entity_graph.sql`` must be applied first (see ISSUES.md).
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from config.settings import settings
from pipelines.graph.entity_graph_mapping import (
    COMPANY_ROSTER_FAMILY,
    FAMILY_EDGE_MAP,
    TEMPORAL_EDGE_FAMILY,
    parse_rows,
)
from pipelines.graph.entity_graph_models import EdgeRelation, EntityKind
from pipelines.graph.heideltime_actor_resolution import (
    CompanyCandidate,
    parse_heideltime_events,
    project_case_events,
)
from pipelines.legal.infringement_concept import ProvisionMap, concepts_for
from pipelines.rag.pgvector.entity_graph_repository import EntityGraphRepository
from pipelines.rag.pgvector.provision_repository import load_provision_map
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("materialize_entity_graph")

_LOG_INTERVAL = 100  # log progress every N documents


def _decision_date_value(facts_by_name: dict[str, dict[str, Any]]) -> str | None:
    """Return the raw decision-date value for treaty-era resolution, if present."""
    fact = facts_by_name.get("eu_commisssion_decision_date")
    if fact is None:
        return None
    return fact.get("value_md") or fact.get("value_json")


def _materialize_company_edges(
    repo: EntityGraphRepository,
    fact: dict[str, Any],
    case_entity_id: Any,
    celex_id: str,
    session: Session,
    stats: dict[str, int],
    case_companies: dict[Any, str],
) -> int:
    """Emit company -> case edges (participated_in / fined / leniency_*) for a fact.

    Parameters
    ----------
    case_companies:
        Accumulator mapping every company node touched for this decision to its
        canonical name.  It becomes the candidate set for temporal actor
        matching, which is why the temporal pass must run after every company
        family has been processed.

    Returns
    -------
    int
        The number of edges emitted, so the caller can detect a family that
        parsed to nothing (an empty roster is reported, never silently dropped).
    """
    spec = FAMILY_EDGE_MAP[fact["name"]]
    emitted = 0
    for row in parse_rows(fact["value_json"], spec):
        if row.source_field is not None and row.source_field != spec.endpoint_fields[0]:
            # Row-key variance in the stored fact (companies_investigated writes
            # both "companies" and "company"); counted so it stays visible.
            stats[f"variant_key_{fact['name']}_{row.source_field}"] += 1
        resolution = repo.resolve_company(row.endpoint_token, session)
        company_id = repo.upsert_entity(
            session,
            kind=EntityKind.COMPANY,
            natural_key=resolution.natural_key,
            canonical_name=resolution.canonical_name,
            lei_id=resolution.lei_id,
        )
        case_companies.setdefault(company_id, resolution.canonical_name)
        repo.upsert_edge(
            session,
            src_entity_id=company_id,
            dst_entity_id=case_entity_id,
            relation=spec.relation,
            celex_id=celex_id,
            source_fact_id=fact["id"],
            value_json=row.payload.to_json(),
        )
        emitted += 1
        stats[f"edge_{spec.relation.value}"] += 1
        # Per-edge, like unresolved_companies below: a firm appearing in both
        # the roster and the fines list is counted once per emission, not once
        # per company. The tier mix is what makes a shift in identity source
        # visible between runs.
        stats[f"lei_tier_{resolution.source}"] += 1
        if resolution.lei_id is None:
            stats["unresolved_companies"] += 1
    return emitted


def _materialize_violated_edges(
    repo: EntityGraphRepository,
    provision_map: ProvisionMap,
    celex_id: str,
    case_entity_id: Any,
    facts_by_name: dict[str, dict[str, Any]],
    session: Session,
    stats: dict[str, int],
) -> None:
    """Emit case -> provision ``violated`` edges.

    Prefers the ``infringement_concept`` fact (already canonical); falls back to
    ``law_article_all_relevant`` resolved through the ontology for the case's
    treaty era so Article-86 ambiguity is handled structurally, not by guessing.
    """
    concept_fact = facts_by_name.get("infringement_concept")
    if concept_fact is not None:
        spec = FAMILY_EDGE_MAP["infringement_concept"]
        rows = parse_rows(concept_fact["value_json"], spec)
        for row in rows:
            _emit_provision_edge(
                repo, celex_id, case_entity_id, row.endpoint_token,
                concept_fact["id"], session, stats,
            )
        if rows:
            return  # infringement_concept is authoritative; do not double-count

    article_fact = facts_by_name.get("law_article_all_relevant")
    if article_fact is None:
        return
    spec = FAMILY_EDGE_MAP["law_article_all_relevant"]
    tokens = [row.endpoint_token for row in parse_rows(article_fact["value_json"], spec)]
    if not tokens:
        return
    concepts = concepts_for(
        " ".join(tokens),
        _decision_date_value(facts_by_name),
        celex_id,
        provision_map,
    )
    for concept in sorted(concepts):
        _emit_provision_edge(
            repo, celex_id, case_entity_id, concept.value,
            article_fact["id"], session, stats,
        )


def _emit_provision_edge(
    repo: EntityGraphRepository,
    celex_id: str,
    case_entity_id: Any,
    concept: str,
    source_fact_id: Any,
    session: Session,
    stats: dict[str, int],
) -> None:
    """Upsert a provision entity and the case -> provision ``violated`` edge."""
    provision_id = repo.upsert_entity(
        session,
        kind=EntityKind.PROVISION,
        natural_key=concept,
        canonical_name=concept,
    )
    repo.upsert_edge(
        session,
        src_entity_id=case_entity_id,
        dst_entity_id=provision_id,
        relation=EdgeRelation.VIOLATED,
        celex_id=celex_id,
        source_fact_id=source_fact_id,
    )
    stats["edge_violated"] += 1


def _materialize_temporal_edges(
    repo: EntityGraphRepository,
    celex_id: str,
    case_entity_id: Any,
    case_companies: dict[Any, str],
    facts_by_name: dict[str, dict[str, Any]],
    session: Session,
    stats: dict[str, int],
) -> None:
    """Emit company -> case ``dated_in`` edges from the ``heideltime_event`` fact.

    One aggregated edge per company, because the edge unique key collapses every
    event of a decision onto a single edge.  Actors are matched only against
    ``case_companies`` - the nodes this decision already has - so no company node
    is ever invented from an LLM-written actor string, and
    ``repo.resolve_company`` is never called here.

    A decision with no ``heideltime_event`` fact is counted, not skipped
    silently: the temporal layer cascades away with the fact, so a rebuild that
    runs before the fact is regenerated must be visible in the run summary.
    """
    fact = facts_by_name.get(TEMPORAL_EDGE_FAMILY)
    if fact is None:
        stats["heideltime_fact_missing"] += 1
        return

    events = parse_heideltime_events(fact["value_json"])
    if not events:
        stats["heideltime_fact_empty"] += 1
        return

    candidates = [
        CompanyCandidate(entity_id=entity_id, canonical_name=name)
        for entity_id, name in case_companies.items()
    ]
    projection = project_case_events(
        events,
        candidates,
        min_stem_len=settings.HEIDELTIME_ACTOR_MIN_STEM_LEN,
        alias_resolver=lambda name: repo.canonicalize_alias(name, session),
    )

    for counter, value in projection.counters.items():
        stats[counter] += value
    for actor, colliding in projection.ambiguous_actors:
        logger.warning(
            "%s: actor %r matches %d companies (%s); refused rather than guessed",
            celex_id, actor, len(colliding), ", ".join(colliding),
        )

    for company_id, payload in projection.payloads.items():
        repo.upsert_edge(
            session,
            src_entity_id=company_id,
            dst_entity_id=case_entity_id,
            relation=EdgeRelation.DATED_IN,
            celex_id=celex_id,
            source_fact_id=fact["id"],
            value_json=payload.to_json(),
        )
        stats[f"edge_{EdgeRelation.DATED_IN.value}"] += 1

    if not projection.payloads:
        stats["heideltime_no_actor_resolved"] += 1
        logger.warning(
            "%s: %d heideltime events, no actor resolved to a company node",
            celex_id, len(events),
        )


def _materialize_celex(
    repo: EntityGraphRepository,
    provision_map: ProvisionMap,
    celex_id: str,
    session: Session,
    stats: dict[str, int],
) -> None:
    """Rebuild all edges for one CELEX document within the given transaction."""
    repo.delete_edges_for_celex(celex_id, session)
    case_entity_id = repo.upsert_entity(
        session,
        kind=EntityKind.CASE,
        natural_key=celex_id,
        canonical_name=repo.case_name(celex_id, session),
        celex_id=celex_id,
    )

    facts = repo.fetch_facts_for_celex(celex_id, session)
    facts_by_name = {fact["name"]: fact for fact in facts}

    # Company nodes touched by this decision, in insertion order; the temporal
    # pass matches actors against exactly this set, so it must run afterwards.
    case_companies: dict[Any, str] = {}

    for fact in facts:
        spec = FAMILY_EDGE_MAP.get(fact["name"])
        if spec is None or spec.relation == EdgeRelation.VIOLATED:
            continue  # violated handled once, with source preference, below
        emitted = _materialize_company_edges(
            repo, fact, case_entity_id, celex_id, session, stats, case_companies
        )
        if fact["name"] == COMPANY_ROSTER_FAMILY and emitted == 0:
            stats["roster_empty"] += 1
            logger.warning("%s: %s parsed to no company", celex_id, COMPANY_ROSTER_FAMILY)

    if COMPANY_ROSTER_FAMILY not in facts_by_name:
        stats["roster_missing"] += 1
        logger.warning("%s: no %s fact, no company layer", celex_id, COMPANY_ROSTER_FAMILY)

    _materialize_violated_edges(
        repo, provision_map, celex_id, case_entity_id, facts_by_name, session, stats
    )
    _materialize_temporal_edges(
        repo, celex_id, case_entity_id, case_companies, facts_by_name, session, stats
    )


def run(dry_run: bool = False, celex_filter: str | None = None) -> dict[str, int]:
    """Materialize the entity graph for all documents, or one via ``celex_filter``.

    Parameters
    ----------
    dry_run:
        When ``True`` every document is processed inside a transaction that is
        rolled back — real counts, no writes.
    celex_filter:
        Restrict materialization to a single CELEX id.

    Returns
    -------
    dict[str, int]
        Counters (edges per relation, unresolved companies, processed, errors).
    """
    engine = make_engine(DATABASE_URL)
    repo = EntityGraphRepository(engine=engine)
    provision_map = load_provision_map()

    celex_ids = [celex_filter] if celex_filter else repo.distinct_celex_ids()
    total = len(celex_ids)
    logger.info("Materializing entity graph for %d document(s)%s", total, " [DRY-RUN]" if dry_run else "")

    stats: dict[str, int] = defaultdict(int)
    for index, celex_id in enumerate(celex_ids, start=1):
        session = repo.Session()
        try:
            _materialize_celex(repo, provision_map, celex_id, session, stats)
            if dry_run:
                session.rollback()
            else:
                session.commit()
            stats["celex_processed"] += 1
        except Exception:
            session.rollback()
            stats["errors"] += 1
            logger.exception("Failed to materialize %s", celex_id)
        finally:
            session.close()
        if index % _LOG_INTERVAL == 0:
            logger.info("Processed %d/%d documents", index, total)

    _log_summary(stats, dry_run)
    return dict(stats)


def _log_summary(stats: dict[str, int], dry_run: bool) -> None:
    """Log the final counters as a single sorted summary line."""
    suffix = " [DRY-RUN]" if dry_run else ""
    rendered = ", ".join(f"{key}={value}" for key, value in sorted(stats.items()))
    logger.info("Entity-graph materialization complete%s: %s", suffix, rendered or "no-op")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Materialize the typed entity graph from celex_facts.facts"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Process every document but roll back all writes (report counts only)",
    )
    parser.add_argument(
        "--celex-id",
        metavar="CELEX_ID",
        default=None,
        help="Restrict materialization to a single CELEX document",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run, celex_filter=args.celex_id)


if __name__ == "__main__":
    main()
