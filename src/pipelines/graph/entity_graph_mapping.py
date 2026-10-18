"""Pure (DB-free) projection of fact families into typed graph edges.

``FAMILY_EDGE_MAP`` declares, for each materialized ``facts.name`` family, which
edge ``relation`` it produces, the endpoint kinds, and how to pull the endpoint
token and payload out of the fact's ``value_json``.  ``parse_rows`` performs the
extraction; both are deliberately free of any database or ontology dependency so
they are unit-testable in isolation.  Entity resolution (company -> LEI, article
-> concept) happens downstream in the repository / batch, which own the DB and
the provision ontology.

Ground-truth ``value_json`` shapes (confirmed by live sampling) are lists of
dicts, one dict per row, with the fine / leniency amount paired to its company
inside each element::

    fines / leniency_granted  -> [{"company": "Roto", "amount": "100%"}, ...]
    leniency_applicants etc.  -> [{"company": "RWE AG"}, ...]
    companies_investigated    -> [{"companies": "RWE AG", "location": "DE"}, ...]
                              -> [{"company": "RWE AG"}, ...]   (variant, see below)
    infringement_concept      -> [{"infringement_concept": "anticompetitive_agreement"}]
    law_article_all_relevant  -> [{"law_article": "Article 82"}, ...]

so the parser emits one row per list element, preserving the pairing.  When
``value_json`` is ``"NA"`` / ``"[]"`` / empty / unparseable the fact is skipped
(there is no ``value_md`` fallback: ``value_md`` carries the identical ``"NA"``).

Row-key variance
----------------
``companies_investigated`` declares ``output_schema: companies, location`` but
was not written consistently: measured on the live store, 514 CELEX documents
(4,839 rows) key the company name as ``"companies"`` and 229 documents (1,627
rows) key it as ``"company"``; no document mixes the two.  ``endpoint_fields``
is therefore an *ordered tuple* of candidate keys and the first present key
wins — the same COALESCE the ad-hoc SQL already does.  The variance must not be
collapsed blindly: ``fines`` and the ``leniency_*`` families legitimately own
the ``"company"`` vocabulary and stay single-keyed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from pipelines.graph.entity_graph_models import EdgeRelation, EdgeValue, EntityKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EdgeSpec:
    """Declarative rule mapping one fact family to an edge relation.

    Attributes
    ----------
    relation:
        The edge relation this family produces.
    src_kind, dst_kind:
        Endpoint node kinds.
    endpoint_fields:
        Ordered candidate ``value_json`` dict keys holding the variable endpoint
        token (the company name, the concept, or the ``Article N`` string); the
        first key present on an element wins.  Most families declare exactly one
        key; ``companies_investigated`` declares two because both were written
        (see the module docstring).  The other endpoint is the case (fixed per
        fact via ``celex_id``).
    endpoint_is_src:
        ``True`` when the parsed token is the ``src`` node (company families);
        ``False`` when it is the ``dst`` node (violated: case -> provision).
    amount_field:
        Optional ``value_json`` key whose value becomes the edge ``amount``
        payload (fines, leniency_granted); ``None`` for payload-free relations.
    """

    relation: EdgeRelation
    src_kind: EntityKind
    dst_kind: EntityKind
    endpoint_fields: tuple[str, ...]
    endpoint_is_src: bool = True
    amount_field: str | None = None


@dataclass(frozen=True)
class ParsedRow:
    """One parsed endpoint + payload from a fact's ``value_json`` list.

    ``source_field`` names the ``value_json`` key the token was read from, so
    callers can count how often a non-primary candidate key was needed and keep
    the stored row-key variance visible instead of silently absorbing it.
    """

    endpoint_token: str
    payload: EdgeValue = field(default_factory=EdgeValue)
    source_field: str | None = None


# Sentinel value_json contents that mean "no data" -> skip the fact entirely.
_EMPTY_JSON_MARKERS: frozenset[str] = frozenset({"", "na", "[]", "null", "none"})


# Directly-parseable families. ``companies_investigated`` is the authoritative
# company roster and the sole source of ``participated_in``.
# ``law_article_all_relevant`` is handled here for
# token extraction, but its "Article N" tokens require ontology + treaty-era
# resolution downstream (the repository owns that); ``infringement_concept``
# already yields the canonical concept and is the preferred ``violated`` source.
FAMILY_EDGE_MAP: dict[str, EdgeSpec] = {
    "companies_investigated": EdgeSpec(
        relation=EdgeRelation.PARTICIPATED_IN,
        src_kind=EntityKind.COMPANY,
        dst_kind=EntityKind.CASE,
        # The roster fact declares ``output_schema: companies, location`` so its
        # rows should be {"companies": "<name>"}, but 229 documents were written
        # with the "company" key instead; both are accepted, schema key first.
        endpoint_fields=("companies", "company"),
    ),
    "fines": EdgeSpec(
        relation=EdgeRelation.FINED,
        src_kind=EntityKind.COMPANY,
        dst_kind=EntityKind.CASE,
        endpoint_fields=("company",),
        amount_field="amount",
    ),
    "leniency_applicants": EdgeSpec(
        relation=EdgeRelation.LENIENCY_APPLIED,
        src_kind=EntityKind.COMPANY,
        dst_kind=EntityKind.CASE,
        endpoint_fields=("company",),
    ),
    "leniency_granted": EdgeSpec(
        relation=EdgeRelation.LENIENCY_GRANTED,
        src_kind=EntityKind.COMPANY,
        dst_kind=EntityKind.CASE,
        endpoint_fields=("company",),
        amount_field="amount",
    ),
    "leniency_rejected": EdgeSpec(
        relation=EdgeRelation.LENIENCY_REJECTED,
        src_kind=EntityKind.COMPANY,
        dst_kind=EntityKind.CASE,
        endpoint_fields=("company",),
    ),
    "exemption_applicants": EdgeSpec(
        # Exemption applicants map onto the leniency_applied relation (documented
        # alias): both are "company sought lenient treatment in this case".
        relation=EdgeRelation.LENIENCY_APPLIED,
        src_kind=EntityKind.COMPANY,
        dst_kind=EntityKind.CASE,
        endpoint_fields=("company",),
    ),
    "infringement_concept": EdgeSpec(
        relation=EdgeRelation.VIOLATED,
        src_kind=EntityKind.CASE,
        dst_kind=EntityKind.PROVISION,
        endpoint_fields=("infringement_concept",),
        endpoint_is_src=False,
    ),
    "law_article_all_relevant": EdgeSpec(
        relation=EdgeRelation.VIOLATED,
        src_kind=EntityKind.CASE,
        dst_kind=EntityKind.PROVISION,
        endpoint_fields=("law_article",),
        endpoint_is_src=False,
    ),
}

# The company roster family. Named once here so the batch can report roster
# coverage without hardcoding the string, and so the retired predecessors of
# this fact (``companies``, ``companies_all``, ``companies_canoncial``,
# ``company_country`` - all dropped from FACT_EXTRACTOR.yaml and, for the first
# three, from the fact store) have exactly one place to be replaced.
COMPANY_ROSTER_FAMILY: str = "companies_investigated"

# The temporal family.  Deliberately absent from FAMILY_EDGE_MAP: its
# ``value_json`` elements carry three load-bearing fields (event_date, actor,
# predicate_object) and must be aggregated *across* elements, neither of which
# ``parse_rows`` can express.  ``heideltime_actor_resolution`` owns its parsing
# and ``Main_materialize_entity_graph._materialize_temporal_edges`` emits its
# ``dated_in`` edges after the company layer is complete.
TEMPORAL_EDGE_FAMILY: str = "heideltime_event"

# Fact families intentionally excluded from materialization (documented so the
# coverage test can assert they stay out).  Two groups:
#
#   * per-company *attribute* facts derived from the companies_investigated
#     roster (country, address, industry, recidivism): they qualify an existing
#     company node rather than creating a new endpoint pair;
#   * the non-event heideltime families.  ``heideltime_raw`` is a proxy fact
#     whose payload is a row-count summary, and ``heideltime_event_agg`` /
#     ``heideltime_cartel_period`` are per-*case* derivations with no company
#     endpoint at all.  Naming them here is what keeps the heideltime layer from
#     being invisible by omission: after this, every heideltime family is either
#     projected (``TEMPORAL_EDGE_FAMILY``) or excluded on the record.
EXCLUDED_FAMILIES: frozenset[str] = frozenset(
    {
        "companies_country",
        "companies_address",
        "companies_industry",
        "company_recidivism",
        "heideltime_raw",
        "heideltime_event_agg",
        "heideltime_cartel_period",
    }
)


def parse_rows(value_json: str | None, spec: EdgeSpec) -> list[ParsedRow]:
    """Extract endpoint tokens and payloads from a fact's ``value_json``.

    Parameters
    ----------
    value_json:
        The raw ``facts.value_json`` string (a JSON list of dicts, or a
        placeholder such as ``"NA"``).
    spec:
        The :class:`EdgeSpec` for this fact family, naming the endpoint and
        amount fields.

    Returns
    -------
    list[ParsedRow]
        One row per list element that carries a non-empty endpoint token.
        Empty / ``"NA"`` / malformed input yields ``[]`` (the fact is skipped).
    """
    if value_json is None:
        return []
    stripped = value_json.strip()
    if stripped.lower() in _EMPTY_JSON_MARKERS:
        return []
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        logger.debug("Unparseable value_json skipped: %.80r", stripped)
        return []

    rows: list[ParsedRow] = []
    for element in _iter_elements(payload):
        token, source_field = _endpoint_token(element, spec.endpoint_fields)
        if not token:
            continue
        amount = _amount_value(element, spec.amount_field)
        rows.append(
            ParsedRow(
                endpoint_token=token,
                payload=EdgeValue(amount=amount),
                source_field=source_field,
            )
        )
    return rows


def _iter_elements(payload: object) -> list[object]:
    """Normalize a decoded ``value_json`` payload to a list of elements."""
    if isinstance(payload, list):
        return payload
    # Defensive: a bare dict or string is treated as a single-element list.
    if isinstance(payload, (dict, str)):
        return [payload]
    return []


def _endpoint_token(
    element: object, endpoint_fields: tuple[str, ...]
) -> tuple[str | None, str | None]:
    """Pull the endpoint token from a list element, trying each candidate key.

    Handles the canonical ``{field: value}`` dict shape and, defensively, a bare
    string element (treated as the token itself).

    Parameters
    ----------
    element:
        One decoded ``value_json`` list element.
    endpoint_fields:
        Ordered candidate dict keys; the first one carrying a non-blank string
        wins, so a family's declared schema key takes precedence over a variant.

    Returns
    -------
    tuple[str | None, str | None]
        The stripped token and the key it came from; ``(None, None)`` when the
        element carries none of the candidate keys.
    """
    if isinstance(element, dict):
        for endpoint_field in endpoint_fields:
            value = element.get(endpoint_field)
            if isinstance(value, str) and value.strip():
                return value.strip(), endpoint_field
        return None, None
    if isinstance(element, str):
        return (element.strip() or None), None
    return None, None


def _amount_value(element: object, amount_field: str | None) -> str | None:
    """Pull the optional amount payload from a list element."""
    if amount_field is None or not isinstance(element, dict):
        return None
    value = element.get(amount_field)
    return value.strip() if isinstance(value, str) and value.strip() else None
