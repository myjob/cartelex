"""Value objects and enums for the typed entity graph.

These mirror the ``entity_kind_enum`` / ``edge_relation_enum`` Postgres types
and the JSON payload carried on an edge.  Per the project's Pydantic rules,
structured edge payloads are ``pydantic.BaseModel`` value objects rather than
raw dicts; the enums are ``StrEnum`` so their values equal the DB enum labels.
"""
from __future__ import annotations

import json
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EntityKind(StrEnum):
    """Node kinds in the entity graph (mirrors ``celex_facts.entity_kind_enum``)."""

    COMPANY = "company"
    CASE = "case"
    MARKET = "market"
    PROVISION = "provision"


class EdgeRelation(StrEnum):
    """Edge relations (mirrors ``celex_facts.edge_relation_enum``).

    ``PARENT_OF`` and ``ACTIVE_IN`` are reserved but not materialized in
    Phase 5.1 (no corporate-hierarchy source family; thin market coverage).

    ``DATED_IN`` is the temporal layer: a company the HeidelTime event stream
    places inside a case, carrying the aggregated date bounds as payload.  It is
    evidence of activity, *not* of participation - it is deliberately excluded
    from ``company_case_relations_vw``, which serves the five roster-grounded
    relations only.
    """

    PARTICIPATED_IN = "participated_in"
    FINED = "fined"
    LENIENCY_APPLIED = "leniency_applied"
    LENIENCY_GRANTED = "leniency_granted"
    LENIENCY_REJECTED = "leniency_rejected"
    PARENT_OF = "parent_of"
    ACTIVE_IN = "active_in"
    VIOLATED = "violated"
    DATED_IN = "dated_in"


class EdgeValue(BaseModel):
    """Immutable JSON payload carried on an edge.

    Only the fields relevant to the materialized relations are modelled.
    ``amount`` holds the fine value (``fined``) or leniency reduction
    (``leniency_granted``) exactly as extracted (e.g. ``"ECU 5 000"``, ``"50%"``).

    The remaining fields carry the ``dated_in`` temporal payload.  They are
    aggregated rather than per-event because the edge unique key
    ``(src, dst, relation, source_fact_id)`` collapses every event of a case onto
    one edge per company - there is exactly one ``heideltime_event`` fact per
    decision.  ``actors`` keeps the *verbatim* actor strings that resolved to
    this company, because ``case_timeline_vw`` joins back on them to recover the
    per-event granularity the edge cannot hold.  ``first_date`` / ``last_date``
    are full ISO dates only; a year-only or month-only event still counts toward
    ``event_count`` but never invents a day for the bounds.

    Because ``to_json`` excludes unset fields, the five roster-grounded relations
    keep emitting byte-identical payloads after this extension.
    """

    model_config = ConfigDict(frozen=True)

    amount: str | None = None
    actors: tuple[str, ...] | None = None
    first_date: str | None = None
    last_date: str | None = None
    event_count: int | None = None

    def to_json(self) -> str | None:
        """Serialize to a compact JSON string, or ``None`` when empty.

        Returns
        -------
        str | None
            ``json.dumps`` of the non-null fields, or ``None`` when the payload
            carries no data (so the edge's ``value_json`` column stays NULL).
        """
        data = self.model_dump(exclude_none=True)
        return json.dumps(data, ensure_ascii=False) if data else None


class ValidatedRelation(BaseModel):
    """One materialized ``entity_edges`` row, flattened for a trusting consumer.

    The knowledge-ingestion pipeline treats the entity graph as validated: an
    edge only exists because a fact family produced it, and ``source_fact_id``
    is ``NOT NULL`` in the schema, so every relation here is already anchored
    to real evidence.  This value object carries the shape a consumer needs to
    reason about a relation without re-joining ``entities`` -- both endpoint
    names, the relation, the decision it holds in, and the lineage anchor.

    ``amount`` is lifted out of the edge's ``value_json`` payload because it is
    the only field a knowledge nugget can contradict on value rather than on
    existence (a fine or a leniency reduction).
    """

    model_config = ConfigDict(frozen=True)

    src_name: str = Field(description="Canonical name of the source node (company).")
    relation: EdgeRelation = Field(description="The materialized edge relation.")
    dst_name: str = Field(
        description="Canonical name of the destination node (case, provision, market)."
    )
    celex_id: str = Field(description="Decision the edge was materialized from.")
    source_fact_id: str = Field(
        description="Lineage anchor into facts.id that produced this edge."
    )
    amount: str | None = Field(
        default=None,
        description="Fine amount or leniency reduction from value_json, when present.",
    )


class GraphSeed(BaseModel):
    """One company node a ``graph_search`` name stem matched.

    Returned alongside the peers so an answer can show *which* nodes the stem
    resolved to.  This matters because company aliases still split across
    several nodes (only a minority are LEI-keyed), so a stem such as ``bayer``
    legitimately matches many rows and the reader cannot infer them.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: UUID = Field(description="Primary key of the matched company node.")
    canonical_name: str = Field(description="Human-readable company label.")
    lei_id: str | None = Field(
        default=None, description="GLEIF LEI when the node resolved to one."
    )
    edge_count: int = Field(
        default=0,
        description="Outgoing edges of the traversed relation on this node.",
    )


class GraphPeer(BaseModel):
    """A company reachable from the seed set through shared cases.

    ``shared_celex_ids`` names the decisions that connect this peer to the
    cohort, and ``source_fact_ids`` are the lineage anchors of the edges that
    carried the connection - the same anchors ``fact_provenance()`` resolves, so
    a graph-derived answer keeps citing real evidence.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: UUID = Field(description="Primary key of the peer company node.")
    canonical_name: str = Field(description="Human-readable company label.")
    lei_id: str | None = Field(
        default=None, description="GLEIF LEI when the node resolved to one."
    )
    hops: int = Field(
        description="Minimum traversal distance from the seed set (1 = direct)."
    )
    shared_celex_ids: tuple[str, ...] = Field(
        default=(),
        description="Decisions linking this peer to the cohort, first-seen order.",
    )
    source_fact_ids: tuple[str, ...] = Field(
        default=(),
        description="Lineage anchors (facts.id) of the edges that carried the link.",
    )


class GraphNeighbourhood(BaseModel):
    """Result of one ``graph_search`` co-participation traversal.

    An unmatched stem yields empty ``seeds`` and ``peers`` rather than an error,
    so a caller renders "no match" instead of handling an exception.
    """

    model_config = ConfigDict(frozen=True)

    seed_stem: str = Field(description="The name stem that seeded the traversal.")
    relation: EdgeRelation = Field(
        description="Edge relation traversed to define co-membership."
    )
    max_hops: int = Field(description="Traversal depth bound actually applied.")
    seeds: tuple[GraphSeed, ...] = Field(
        default=(), description="Company nodes the stem matched."
    )
    peers: tuple[GraphPeer, ...] = Field(
        default=(), description="Reachable companies, nearest hop first."
    )
    truncated: bool = Field(
        default=False,
        description="True when the peer list hit its cap and is incomplete.",
    )
