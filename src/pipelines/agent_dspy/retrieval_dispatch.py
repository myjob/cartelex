"""Tool dispatcher, read-only SQL gate, and KB schema description.

Encapsulates the seven retrieval tools (``kb_sql``, ``case_facts``,
``rag_search``, ``rag_search_in_case``, ``doc_search``, ``knowledge_search``,
``graph_search``) behind :class:`_ToolDispatcher`, each
returning a typed :class:`EvidenceResult` (Rule 3).  The ``kb_sql`` tool is gated
to read-only ``SELECT`` / ``WITH`` statements by :func:`_validate_read_only_sql`,
and :data:`KB_SCHEMA_DESCRIPTION` is the schema string injected into the planner.

DB clients and searchers are injected via the Provisioner pattern; this module
imports only the shared text helpers from :mod:`retrieval_text` plus the
retrieval protocols and :class:`RetrievalStep`.
"""

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from config.settings import settings
from pipelines.agent_dspy.dspy_protocols import (
    CrossCaseAggregator,
    DocChunkSearcher,
    GraphSearcher,
    HybridSearcher,
    KBFactSearcher,
    KnowledgeSearcher,
    SQLFactStore,
)
from pipelines.graph.entity_graph_models import EdgeRelation, GraphNeighbourhood
from pipelines.agent_dspy.dspy_signature import RetrievalStep
from pipelines.agent_dspy.retrieval_text import (
    _UNSTRUCTURED_TOPICS,
    _clean_case_title,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TOOLS = frozenset(
    {
        "kb_sql",
        "case_facts",
        "rag_search",
        "rag_search_in_case",
        "doc_search",
        "knowledge_search",
        "graph_search",
    }
)

_MAX_EVIDENCE_CHARS = 12_000  # cap per step to stay within prompt budget

# Fact names that label structure rather than an extracted fact; never used as
# a provenance root.  Applied when the dispatcher collects fact names from
# typed retrieval results.
_NON_FACT_NAMES = frozenset({"case_title"})


_FACTS_SCHEMA = """\
Table: celex_facts.facts  (Entity-Attribute-Value design)

CRITICAL: This is an EAV table. Each row stores ONE fact about ONE case.
The `value_md` column is POLYMORPHIC — its data type depends on the `name` column.
You CANNOT mix facts from different `name` values in the same row.

Columns:
  - celex_id   TEXT   — case identifier (e.g. '38344_5856_3', 'CELEX-31975D0358')
  - name       TEXT   — fact name (determines the type/meaning of value_md)
  - value_md   TEXT   — the fact value (type depends on `name`, see below)
  - value_json JSONB  — structured JSON value (may be NULL)

Fact names and their value_md TYPES:
  Boolean facts (value_md is 'True' or 'False'):
    leniency_case, exemption_case, market_outside_eu, market_oligopoly,
    is_court_involved, case_commission_decision, case_commission_no_action,
    case_commission_info_request, leniency_case_match,
    infringement_category_abuse_of_dominance,
    infringement_category_procedural, infringement_category_member_states

  Date facts (value_md is 'YYYY-MM-DD'):
    eu_commisssion_decision_date

  Date-range facts (value_md is 'YYYY-MM-DD : YYYY-MM-DD'):
    heideltime_cartel_period, legal_assessment_period

  Category / classification facts (value_md is a short label):
    infringement_category_hor_vert_lic_joint,
    infringement_subcategory_horizontal_restraints,
    infringement_subcategory_vertical_restraints,
    infringement_gravity, OECD_sector, NACE_group_code, NACE_class_code

  Free-text / markdown facts:
    leniency_applicants, leniency_granted, leniency_rejected,
    fines, companies_investigated, companies_country,
    companies_address, company_recidivism, companies_industry,
    authorities_canoncial,
    case_commissioner, case_summary, case_title,
    market_geographical, market_turbulence,
    products, products_name,
    decision_remedies, cartel_period,
    exemption_applicants, law_article_all_relevant,
    human_index_nace,
    court_list_raw, court_list,
    heideltime_event, heideltime_event_agg, heideltime_raw

IMPORTANT — FACT NAME MATCHING:
  When the user mentions a fact name (e.g. 'court_list_raw'), use that
  EXACT string as the `name` filter.  To discover which fact names exist,
  query:
    SELECT DISTINCT name FROM celex_facts.facts ORDER BY name;

ANTI-PATTERNS (NEVER do these):
  X DATE(value_md) on a boolean fact — crashes with "invalid input syntax for type date"
  X value_md::date without a length / regex guard — date facts may contain 'NA'
    or empty strings; ALWAYS gate the cast with
        AND LENGTH(value_md) = 10
    or
        AND value_md ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
  X Mixing different fact names in one WHERE without self-join
  X Assuming value_md is always a date or always a number

CORRECT PATTERNS:

  -- Count cases with leniency
  SELECT COUNT(DISTINCT celex_id) FROM celex_facts.facts
  WHERE name = 'leniency_case' AND value_md = 'True';

  -- Leniency cases by decision year (requires SELF-JOIN across two fact names)
  SELECT EXTRACT(YEAR FROM d.value_md::date) AS yr, COUNT(*) AS cnt
  FROM celex_facts.facts l
  JOIN celex_facts.facts d ON l.celex_id = d.celex_id
  WHERE l.name = 'leniency_case' AND l.value_md = 'True'
    AND d.name = 'eu_commisssion_decision_date' AND LENGTH(d.value_md) = 10
  GROUP BY yr ORDER BY yr;

  -- Find leniency applicants for a specific case
  SELECT value_md FROM celex_facts.facts
  WHERE celex_id = '38344_5856_3' AND name = 'leniency_applicants';

  -- Cases in a specific OECD sector
  SELECT DISTINCT celex_id FROM celex_facts.facts
  WHERE name = 'OECD_sector' AND value_md ILIKE '%chemical%';

  -- Cases decided in a given year (date column may contain 'NA' — GUARD the cast)
  SELECT DISTINCT celex_id FROM celex_facts.facts
  WHERE name = 'eu_commisssion_decision_date'
    AND LENGTH(value_md) = 10
    AND EXTRACT(YEAR FROM value_md::date) = 1991;

  -- Counts of decisions per year (same guard required)
  SELECT EXTRACT(YEAR FROM value_md::date) AS yr, COUNT(DISTINCT celex_id) AS cnt
  FROM celex_facts.facts
  WHERE name = 'eu_commisssion_decision_date'
    AND LENGTH(value_md) = 10
  GROUP BY yr ORDER BY yr;

  -- Commissioner with most cases
  SELECT value_md AS commissioner, COUNT(DISTINCT celex_id) AS cnt
  FROM celex_facts.facts
  WHERE name = 'case_commissioner' AND LENGTH(value_md) > 2
  GROUP BY value_md ORDER BY cnt DESC LIMIT 10;

  -- NACE group code for a specific case (use case_facts tool or direct SQL)
  SELECT value_md FROM celex_facts.facts
  WHERE celex_id = 'CELEX-31991D0562' AND name = 'NACE_group_code';

  -- List court_list_raw data (markdown table with court, country, jurisdiction)
  SELECT celex_id, value_md FROM celex_facts.facts
  WHERE name = 'court_list_raw';

  -- Cases with court involvement
  SELECT celex_id FROM celex_facts.facts
  WHERE name = 'is_court_involved' AND value_md = 'True';

  -- Infringement gravity distribution
  SELECT value_md AS gravity, COUNT(DISTINCT celex_id) AS cnt
  FROM celex_facts.facts
  WHERE name = 'infringement_gravity' AND LENGTH(value_md) > 1
  GROUP BY value_md ORDER BY cnt DESC;

  -- MULTI-HOP: facts for a discovered case set (e.g. cartel periods of
  -- brewery cases). TWO chained steps — the finder projects DISTINCT
  -- celex_id, the dependent step (input_from_step=1) uses the {celex_ids}
  -- placeholder that the executor fills in. Note the SHORT STEM '%brew%'
  -- (stored values say 'brewing', never 'brewery').
  --   Step 1 (finder):
  SELECT DISTINCT celex_id FROM celex_facts.facts
  WHERE name IN ('companies_industry','OECD_sector','case_title','products_name')
    AND value_md ILIKE '%brew%';
  --   Step 2 (input_from_step=1):
  SELECT celex_id, name, value_md FROM celex_facts.facts
  WHERE name IN ('cartel_period','heideltime_cartel_period')
    AND celex_id IN ({celex_ids});

  -- One-shot alternative when a single step is preferred: self-join
  SELECT p.celex_id, p.value_md AS cartel_period
  FROM celex_facts.facts p
  JOIN celex_facts.facts i ON p.celex_id = i.celex_id
  WHERE p.name = 'cartel_period'
    AND i.name IN ('companies_industry','OECD_sector','case_title','products_name')
    AND i.value_md ILIKE '%brew%';
"""


_GRAPH_SCHEMA = """\
---

Views: celex_facts.company_case_relations_vw
       celex_facts.case_provision_relations_vw
       celex_facts.case_timeline_vw
  (flat projections of the typed entity graph — query with kb_sql)

The first two views hold ONE ROW PER RELATIONSHIP, already resolved. Company
aliases were collapsed to one identity at build time, and legal provisions
were collapsed across the treaty renumbering. The facts table cannot do
either: it stores a stringified JSON list per case, verbatim as extracted.

WHEN TO USE THE VIEWS (not celex_facts.facts):
  - Which cases a NAMED COMPANY was involved in, fined in, or applied for
    leniency in: "list all cases related to NSK Deutschland GmbH".
  - A question that joins a COMPANY IDENTITY or ATTRIBUTE with a CASE OUTCOME
    across cases: "which German companies were fined", "which companies appear
    in more than one cartel", "who applied for leniency most often".
  - A question about which cases VIOLATED a legal provision, especially one
    spanning treaty eras ("Article 101 or its predecessors").

WHEN NOT TO USE THEM:
  X Detail about ONE case ("what fines in CELEX-31991D0562?") — use the
    case_facts tool. The views are a lossy projection; facts is the source.
  X Any fact family that is not a company or provision relationship
    (sectors, dates, gravity, remedies, market) — use celex_facts.facts.

company_case_relations_vw columns:
  - company_name       TEXT — resolved company label
  - lei_id             TEXT — GLEIF LEI, NULL for ~70% of companies
  - company_country    TEXT — ISO 3166-1 alpha-2 ('DE', 'FR'), nullable
  - legal_jurisdiction TEXT — GLEIF jurisdiction ('DE', 'US-DE')
  - relation           TEXT — one of: participated_in, fined,
                              leniency_applied, leniency_granted,
                              leniency_rejected
  - source_fact_name   TEXT — the facts.name that produced the edge
  - celex_id           TEXT — the decision
  - case_name          TEXT — human-readable case label
  - amount             TEXT — fine / reduction, free text as extracted
  - source_fact_id     UUID — lineage anchor into celex_facts.facts.id

case_provision_relations_vw columns:
  - celex_id           TEXT
  - case_name          TEXT
  - canonical_concept  TEXT — provision(s) collapsed across treaty eras, as a
                              COMMA-SEPARATED list when a case violated more
                              than one. Only three concepts exist:
                              anticompetitive_agreement, abuse_of_dominance,
                              public_undertaking.
  - article_number     INT  — NULL on every current row (all edges come from
                              infringement_concept, which carries no article).
                              Do not filter on it.
  - source_fact_name   TEXT — infringement_concept or law_article_all_relevant
  - source_fact_id     UUID

ALWAYS project source_fact_id — it is what lets the answer cite its evidence.

ANTI-PATTERNS (NEVER do these):
  X relation = 'leniency_applied' alone when the question means LENIENCY.
    That relation is emitted by BOTH leniency_applicants AND
    exemption_applicants, which are legally distinct, and the exemption family
    DOMINATES it — 1,813 of its 2,254 rows are exemption applicants, only 441
    are genuine leniency applicants. Always add
        AND source_fact_name = 'leniency_applicants'
    (or 'exemption_applicants') when the question means one of them. Without
    the filter the answer is wrong roughly four times out of five.
  X canonical_concept = 'anticompetitive_agreement' — the column holds a
    comma-separated list, so equality silently misses every multi-concept
    case. ALWAYS use  canonical_concept ILIKE '%anticompetitive_agreement%'.
  X Casting or summing `amount` — it is free text ('100%', 'ECU 5 000',
    'EUR 1.5 million'), and only 1,811 of 9,954 rows have one at all. Report
    it verbatim.
  X GROUP BY lei_id or company_country without an IS NOT NULL guard — only
    3,680 of 9,954 rows resolved to GLEIF, so the rest would collapse into one
    meaningless NULL bucket. A country breakdown covers ~37% of rows: say so.
  X Presenting a count from these views as a corpus-wide total. Coverage is
    partial: the `fines` fact is empty for 431 of 744 decisions, so
    relation = 'fined' counts are a FLOOR, not a census. Say so, or
    cross-check the denominator against celex_facts.facts.
  X Joining celex_facts.legal_provisions to case_provision_relations_vw — it
    has one row per treaty era and would duplicate every case.

COUNTRY MATCHING:
  Translate the nationality adjective to an ISO code: "German" -> 'DE',
  "French" -> 'FR', "Italian" -> 'IT', "Dutch" -> 'NL', "Spanish" -> 'ES',
  "Belgian" -> 'BE', "Swedish" -> 'SE', "British" -> 'GB'.

COMPANY MATCHING:
  Alias variants still split across company_name (only 1,972 of 6,531
  companies are LEI-keyed), so match ONE distinctive WHOLE WORD of the name,
  never the full legal name, with a case-insensitive word-boundary regex:
      company_name ~* '[[:<:]]bayer[[:>:]]'   NOT  company_name = 'Bayer AG'
  A bare substring over-matches: ILIKE '%bayer%' adds six unrelated
  'Bayerische ...' entities, ILIKE '%nsk%' adds 'Dansk', 'Svenska', 'Slovenske'.

CORRECT PATTERNS:

  -- All cases related to one named company (roster, fines, leniency)
  SELECT celex_id, case_name, company_name, relation, source_fact_id
  FROM celex_facts.company_case_relations_vw
  WHERE company_name ~* '[[:<:]]nsk[[:>:]]'
  ORDER BY celex_id, relation;

  -- Fines imposed on German companies
  SELECT company_name, celex_id, case_name, amount, source_fact_id
  FROM celex_facts.company_case_relations_vw
  WHERE relation = 'fined' AND company_country = 'DE'
  ORDER BY company_name;

  -- Companies appearing in more than one cartel (recidivism across cases)
  SELECT company_name, COUNT(DISTINCT celex_id) AS cartel_count
  FROM celex_facts.company_case_relations_vw
  WHERE relation = 'participated_in'
  GROUP BY company_name
  HAVING COUNT(DISTINCT celex_id) > 1
  ORDER BY cartel_count DESC, company_name;

  -- Genuine leniency applicants only (exclude the exemption alias)
  SELECT company_name, celex_id, source_fact_id
  FROM celex_facts.company_case_relations_vw
  WHERE relation = 'leniency_applied'
    AND source_fact_name = 'leniency_applicants';

  -- Cases violating Article 101 and its pre-Lisbon predecessors (85 EEC / 81 EC).
  -- ILIKE, not '=', because a case can carry several concepts in one string.
  SELECT celex_id, case_name, canonical_concept, source_fact_id
  FROM celex_facts.case_provision_relations_vw
  WHERE canonical_concept ILIKE '%anticompetitive_agreement%'
  ORDER BY celex_id;

  -- Cartel participation by country (covers only the GLEIF-resolved ~37%)
  SELECT company_country, COUNT(DISTINCT celex_id) AS cases
  FROM celex_facts.company_case_relations_vw
  WHERE relation = 'participated_in' AND company_country IS NOT NULL
  GROUP BY company_country ORDER BY cases DESC;

---

case_timeline_vw — the WHEN of a case, one row per dated event (57,758 rows).
Built from the HeidelTime event stream, so it answers chronology questions the
other two views cannot: what happened, in what order, and who acted.

WHEN TO USE IT:
  - "What happened between the inspection and the decision in case X?"
  - "When was company Y active in this cartel?"
  - Cross-checking a heideltime_cartel_period fact against the event evidence
    that should support it.

WHEN NOT TO USE IT:
  X NEVER as a company census or as proof of participation. Only about half of
    company-like actors resolve to a node, so a company absent from this view
    may still be a cartel member — use company_case_relations_vw for that.
  X NEVER filter or sort on event_date without IS NOT NULL: year-only and
    month-only precisions are deliberately NULL there and live in
    event_date_raw.
  X Do not report actor_class = 'placeholder' rows as parties. 'redacted' alone
    is 31% of all events; it means the text named nobody.
  X dated_in edges are NOT in company_case_relations_vw, by design. This view
    is evidence of activity, never of participation.

case_timeline_vw columns:
  - celex_id           TEXT
  - case_name          TEXT
  - event_seq          INT  — 1-based position; the extractor writes events
                              chronologically, so this orders a timeline even
                              where event_date is NULL
  - event_date         DATE — NULL unless a full, valid ISO date
  - event_date_raw     TEXT — the value as extracted ('1994', '1994-03', 'NA')
  - actor              TEXT — verbatim actor string
  - actor_class        TEXT — company | institution | placeholder | unresolved
  - company_name       TEXT — NULL unless actor_class = 'company'
  - company_entity_id  UUID — join key into the graph, NULL otherwise
  - lei_id             TEXT
  - predicate_object   TEXT — what happened, free text
  - celex_event_count  INT  — events in this case, so you can see truncation
  - source_fact_name   TEXT — always 'heideltime_event'
  - source_fact_id     UUID — lineage anchor into celex_facts.facts.id

  -- Ordered timeline for one case (NULL dates last, sequence keeps them placed)
  SELECT event_seq, event_date, event_date_raw, actor, actor_class,
         predicate_object, source_fact_id
  FROM celex_facts.case_timeline_vw
  WHERE celex_id = 'CELEX-31994D0815'
  ORDER BY event_date NULLS LAST, event_seq;

  -- When was each company active, per case, from dated evidence only
  SELECT celex_id, company_name,
         MIN(event_date) AS first_seen, MAX(event_date) AS last_seen,
         COUNT(*) AS dated_events
  FROM celex_facts.case_timeline_vw
  WHERE actor_class = 'company' AND event_date IS NOT NULL
  GROUP BY celex_id, company_name
  ORDER BY celex_id, first_seen;

  -- Does the stored cartel period agree with the event evidence?
  SELECT t.celex_id, MIN(t.event_date) AS first_event,
         MAX(t.event_date) AS last_event, f.value_md AS stored_period
  FROM celex_facts.case_timeline_vw t
  JOIN celex_facts.facts f
    ON f.celex_id = t.celex_id AND f.name = 'heideltime_cartel_period'
  WHERE t.event_date IS NOT NULL
  GROUP BY t.celex_id, f.value_md;
"""


_KNOWLEDGE_SCHEMA = """\
---

Table: celex_facts.knowledge_items  (distilled knowledge nuggets)

Contains cross-case knowledge facts extracted from previous Q&A interactions.
Each row is a natural-language nugget about EU competition-law cases — exemptions,
joint ventures, company identities, procedural outcomes, etc.

Columns (relevant to search):
  - knowledge_text TEXT  — the distilled knowledge nugget
  - knowledge_type TEXT  — classification label (e.g. 'legal_principle', 'procedure')
  - scope           TEXT — 'global', 'jurisdiction', or 'topic'

Use the **knowledge_search** tool (NOT SQL) to query this table.
knowledge_search uses full-text search against knowledge_text.

WHEN TO USE knowledge_search:
  - As a SUPPLEMENTARY step after kb_sql / case_facts / rag_search have run.
  - For questions about procedural context, legal principles, or analytical
    synthesis that are NOT directly answerable from the structured facts table.
  - Examples: court rulings interpretation, exemption criteria reasoning,
    procedural history commentary.

WHEN NOT TO USE knowledge_search as the primary (or only) step:
  - Any question answerable by SQL over celex_facts.facts — use kb_sql instead.
  - "Which companies were granted exemptions?" → kb_sql on exemption_applicants / exemption_case.
  - "Who received leniency?" → kb_sql on leniency_case / leniency_applicants.
  - Any cross-case aggregation (counts, lists, distributions) → always kb_sql first.
"""


_GRAPH_TRAVERSAL_SCHEMA = """\
---

Tool: graph_search  (TRAVERSAL over celex_facts.entity_edges)

The three views above are FLAT PROJECTIONS - one row per relationship. They
hold no edge between two COMPANIES, so no SQL over them can answer "which
companies were in cartels together". graph_search walks the graph instead:
it seeds company nodes by name stem and expands company -> case -> company.

Parameters (typed sub-task fields, NOT the query string):
  graph_seed      SHORT company stem, e.g. 'bayer'   (never a full legal name)
  graph_relation  edge type defining co-membership; default participated_in
  graph_hops      1 = direct co-participants (default), 2 = peers of peers

WHEN TO USE graph_search:
  - "which companies were in cartels together with Bayer"
  - "who is connected to Hoechst through shared cases"
  - "which companies co-participated with X"
  Anything about a relationship BETWEEN TWO COMPANIES.

WHEN NOT TO USE IT:
  X Which cases a company was fined in, company countries, LEI breakdowns,
    counts, groupings, "appears in the most cartels" -> kb_sql over
    company_case_relations_vw. That is one flat SELECT and it is complete.
  X Detail about one case -> the case_facts tool.
  X Which provisions a case violated -> case_provision_relations_vw.
  X Commissioners, towns, products, sectors, any free-text entity -> doc_search.
    Those are NOT nodes in the graph; graph_search returns no match and the
    step is wasted.

ANTI-PATTERNS (NEVER do these):
  X A FULL LEGAL NAME as graph_seed. Company aliases are split across separate
    nodes ('Bayer FRG', 'Bayer Italia', 'Farbenfabriken Bayer AG' are all
    distinct rows, only some LEI-keyed). A short stem seeds all of them and
    expands from the whole set; a full name seeds one and under-reports.
  X graph_hops above 2. Depth 3 reaches over a tenth of every company in the
    corpus and the peer list stops carrying information.
  X Presenting the peer list as a census. An edge exists only where a company
    roster fact was extracted, and some nodes are noise (an entity literally
    named 'EC' is typed as a company). Peer counts are a FLOOR. Say so.
  X Using graph_search to COUNT anything. It enumerates a neighbourhood; the
    views aggregate. Counting questions go to kb_sql.

EXAMPLE:
  question: "Which companies were in cartels together with Bayer?"
  sub-task: tool=graph_search, graph_seed='bayer',
            graph_relation='participated_in', graph_hops=1
"""


def build_kb_schema(graph_enabled: bool, traversal_enabled: bool = False) -> str:
    """Assemble the schema prose injected into the planner prompts.

    Both the primary ``QAPlannerModule`` and the fallback ``MasterPlannerModule``
    read this one string, so a section added here reaches both.

    Args:
        graph_enabled: When True, advertise the flat entity-graph views
            alongside the EAV facts table. When False the output is
            byte-identical to the pre-graph prose, which is what makes the
            ``settings.GRAPH_QA_ENABLED`` switch a clean A/B.
        traversal_enabled: When True, additionally advertise the
            ``graph_search`` traversal tool. Defaults to False and carries its
            own setting, so the multi-hop surface stays independently A/B-able
            from the flat views.

    Returns:
        The rendered schema description.
    """
    sections = [_FACTS_SCHEMA]
    if graph_enabled:
        sections.append(_GRAPH_SCHEMA)
    if traversal_enabled:
        sections.append(_GRAPH_TRAVERSAL_SCHEMA)
    sections.append(_KNOWLEDGE_SCHEMA)
    return "\n".join(sections)


KB_SCHEMA_DESCRIPTION = build_kb_schema(
    settings.GRAPH_QA_ENABLED, settings.GRAPH_SEARCH_ENABLED
)


# ---------------------------------------------------------------------------
# SQL Safety Gate
# ---------------------------------------------------------------------------


def _validate_read_only_sql(sql: str) -> str:
    """Validate that *sql* is a single, read-only SELECT statement.

    Uses basic regex validation to block dangerous operations.
    Returns the cleaned SQL string.

    Raises
    ------
    ValueError
        If the SQL contains write operations or multiple statements.
    """
    cleaned = sql.strip()

    # Strip markdown code fences emitted by some LLMs (```sql ... ``` or ``` ... ```)
    fence_match = re.match(
        r"^```(?:sql)?\s*\n?(.*?)```\s*$", cleaned, re.DOTALL | re.IGNORECASE
    )
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # Strip leading SQL line comments (-- ...) that precede the SELECT keyword
    cleaned = re.sub(r"^(--[^\n]*\n)+", "", cleaned).strip()

    cleaned = cleaned.rstrip(";")

    # Block multiple statements
    if ";" in cleaned:
        raise ValueError("Multi-statement SQL is not allowed.")

    upper = cleaned.upper()

    # Must start with SELECT or WITH (CTE)
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("Only SELECT / WITH queries are allowed.")

    # Block write keywords
    _BLOCKED = {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
    }
    # Tokenize on word boundaries to avoid false positives
    tokens = set(re.findall(r"\b[A-Z]+\b", upper))
    blocked_found = tokens & _BLOCKED
    if blocked_found:
        raise ValueError(f"Blocked SQL keywords detected: {blocked_found}")

    # Enforce row limit if not already present
    if "LIMIT" not in upper:
        cleaned += " LIMIT 500"

    return cleaned


# ---------------------------------------------------------------------------
# Tool Dispatcher (used by the Executor)
# ---------------------------------------------------------------------------


class EvidenceResult(BaseModel):
    """Typed result of a single retrieval step (Rule 3).

    Carries the rendered evidence ``text`` alongside the identity of the
    retrieved data, so provenance and the knowledge-extraction queue read real
    ids/names instead of regex-parsing them back out of the rendered prose.
    """

    text: str = Field(
        description="Rendered evidence block for the synthesizer prompt."
    )
    fact_ids: list[str] = Field(
        default_factory=list,
        description="Fact UUIDs (as strings) grounding this evidence.",
    )
    chunk_ids: list[str] = Field(
        default_factory=list,
        description="Chunk PKs (as strings) grounding this evidence.",
    )
    fact_names: list[str] = Field(
        default_factory=list,
        description="Fact names retrieved, in first-seen order.",
    )
    celex_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Distinct celex_ids surfaced by this step, in first-seen order. "
            "Consumed by the executor to resolve chained steps "
            "(RetrievalStep.input_from_step / {celex_ids} placeholder)."
        ),
    )


def render_knowledge_items(
    items: list,
    get_sources: Callable[[Any], list[dict]] | None,
    header: str,
) -> EvidenceResult:
    """Render knowledge items as an evidence block with their source refs.

    Shared by the ``knowledge_search`` tool and the pre-plan knowledge probe,
    so knowledge reads the same to the synthesizer whichever path found it.

    Args:
        items: ``KnowledgeItem``-like objects (``id``, ``knowledge_text``,
            ``knowledge_type``, ``scope``, ``confidence``).
        get_sources: ``KnowledgeRepository.get_knowledge_sources`` or
            ``None``; a failing lookup drops that item's refs, not the item.
        header: First line of the block, naming the path that found it.

    Returns:
        :class:`EvidenceResult` with one ``kb_item`` line per item, its chunk
        and fact refs, and the chunk_ids / celex_ids those refs name.
    """
    lines = [header]
    chunk_ids: list[str] = []
    celex_ids: list[str] = []
    for i, item in enumerate(items, start=1):
        confidence = getattr(item, "confidence", None)
        score_part = f" score={confidence:.2f}" if confidence is not None else ""
        lines.append(
            f"  [kb_item:{i}{score_part}]"
            f" [{getattr(item, 'knowledge_type', '')} / "
            f"{getattr(item, 'scope', '')}] "
            f"{getattr(item, 'knowledge_text', '')}"
        )
        if get_sources is None:
            continue
        try:
            sources = get_sources(item.id)
        except Exception as exc:
            logger.debug("get_knowledge_sources failed for item %s: %s", item.id, exc)
            sources = []
        for src in sources:
            if src["celex_id"] and src["celex_id"] not in celex_ids:
                celex_ids.append(src["celex_id"])
            if src["source_kind"] == "chunk" and src["chunk_id"]:
                pos_part = f" pos={src['doc_pos']}" if src["doc_pos"] is not None else ""
                celex_part = f" {src['celex_id']}" if src["celex_id"] else ""
                lines.append(f"    ref:[doc{pos_part}{celex_part}]")
                chunk_ids.append(src["chunk_id"])
            elif src["source_kind"] == "fact" and src["fact_name"]:
                celex_part = f" {src['celex_id']}" if src["celex_id"] else ""
                lines.append(f"    ref:[fact {src['fact_name']}{celex_part}]")
    return EvidenceResult(
        text="\n".join(lines), chunk_ids=chunk_ids, celex_ids=celex_ids
    )


class _ToolDispatcher:
    """Encapsulates the retrieval tools and dispatches by name.

    Provisioned once and shared across plan executions.
    """

    def __init__(
        self,
        sql_store: SQLFactStore,
        kb_searcher: KBFactSearcher,
        aggregator: CrossCaseAggregator,
        hybrid_searcher: HybridSearcher,
        doc_searcher: DocChunkSearcher,
        knowledge_searcher: KnowledgeSearcher | None = None,
        graph_searcher: GraphSearcher | None = None,
    ) -> None:
        self.sql_store = sql_store
        self.kb_searcher = kb_searcher
        self.aggregator = aggregator
        self.hybrid_searcher = hybrid_searcher
        self.doc_searcher = doc_searcher
        self.knowledge_searcher = knowledge_searcher
        self.graph_searcher = graph_searcher

    def dispatch(self, step: RetrievalStep) -> EvidenceResult:
        """Execute a single retrieval step and return typed evidence.

        The rendered ``text`` is truncated to the prompt budget, but the
        ``fact_ids`` / ``chunk_ids`` / ``fact_names`` are collected before
        rendering and therefore survive truncation.
        """
        tool = step.tool
        if tool not in _VALID_TOOLS:
            return EvidenceResult(text=f"[error] Unknown tool '{tool}'.")

        t0 = time.perf_counter()
        try:
            if tool == "kb_sql":
                result = self._run_kb_sql(step.query)
            elif tool == "case_facts":
                result = self._run_case_facts(step.case_id)
            elif tool == "rag_search":
                result = self._run_rag_search(step.query)
            elif tool == "rag_search_in_case":
                result = self._run_rag_search_in_case(step.case_id, step.query)
            elif tool == "doc_search":
                result = self._run_doc_search(step.query)
            elif tool == "knowledge_search":
                result = self._run_knowledge_search(step.query)
            elif tool == "graph_search":
                result = self._run_graph_search(step)
            else:
                result = EvidenceResult(text="[error] Unhandled tool.")
        except Exception as exc:
            logger.warning("Tool '%s' failed: %s", tool, exc)
            result = EvidenceResult(text=f"[error] {exc}")

        elapsed = time.perf_counter() - t0
        logger.debug("Tool '%s' completed in %.3fs", tool, elapsed)

        # Truncate the rendered text to avoid blowing the prompt budget; ids
        # and names are already captured and are unaffected.
        if len(result.text) > _MAX_EVIDENCE_CHARS:
            result.text = result.text[:_MAX_EVIDENCE_CHARS] + "\n... [truncated]"

        return result

    # -- individual tool implementations ----------------------------------

    def _run_kb_sql(self, query: str) -> EvidenceResult:
        safe_sql = _validate_read_only_sql(query)
        rows = self.aggregator.aggregate_facts(safe_sql)
        if not rows:
            return EvidenceResult(text="[kb_sql] No rows returned.")
        rows = self._enrich_with_case_titles(rows)
        # When the query projects the fact ``id`` column (e.g. the fact-name
        # fallback), capture it as a real edge; arbitrary planner SQL that omits
        # ``id`` simply yields no fact_ids.  The graph Q&A views project the
        # same anchor as ``source_fact_id`` instead, so a view-backed answer
        # keeps its lineage trail.  Deduplicated because one fact produces many
        # edges, so a view row set repeats the same anchor.
        fact_ids = list(
            dict.fromkeys(
                str(r.get("id") or r.get("source_fact_id"))
                for r in rows
                if isinstance(r, dict) and (r.get("id") or r.get("source_fact_id"))
            )
        )
        celex_ids = list(
            dict.fromkeys(
                str(r["celex_id"])
                for r in rows
                if isinstance(r, dict) and r.get("celex_id")
            )
        )
        lines = [str(r) for r in rows]
        return EvidenceResult(
            text="[kb_sql] " + "\n".join(lines),
            fact_ids=fact_ids,
            celex_ids=celex_ids,
        )

    def _fetch_case_titles(self, celex_ids: list[str]) -> dict[str, str]:
        """Resolve ``case_title`` for each unique celex_id via the facts table.

        Looks up ``name='case_title'`` through ``sql_store.select_fact4celex``
        exactly once per distinct id, so a result set with repeated ids
        triggers only one DB call each. Ids that cannot be resolved (missing
        fact, or a failing DB call) map to the empty string.

        Args:
            celex_ids: celex_ids to resolve; blanks and duplicates are skipped.

        Returns:
            Mapping ``celex_id -> case_title`` (empty string when unresolved).
        """
        titles: dict[str, str] = {}
        for cid in celex_ids:
            if not cid or cid in titles:
                continue
            try:
                facts = self.sql_store.select_fact4celex(cid, "case_title")
                if facts and hasattr(facts[0], "value_md") and facts[0].value_md:
                    titles[cid] = _clean_case_title(facts[0].value_md)
                else:
                    titles[cid] = ""
            except Exception as exc:
                logger.warning(
                    "Failed to fetch case_title for '%s': %s",
                    cid,
                    exc,
                )
                titles[cid] = ""
        return titles

    def _enrich_with_case_titles(
        self,
        rows: list[dict],
    ) -> list[dict]:
        """Add ``case_title`` to each row that contains a ``celex_id`` key.

        Returns the *same* list of dicts (mutated in-place for efficiency).
        Rows without a ``celex_id`` key or where the title cannot be
        resolved are left untouched.
        """
        # Fast path — nothing to enrich if no row carries celex_id.
        if not rows or "celex_id" not in rows[0]:
            return rows

        titles = self._fetch_case_titles([row.get("celex_id", "") for row in rows])
        for row in rows:
            title = titles.get(row.get("celex_id", ""), "")
            if title:
                row["case_title"] = title
        return rows

    def _run_case_facts(self, case_id: str) -> EvidenceResult:
        if not case_id:
            return EvidenceResult(text="[case_facts] No case_id provided.")
        rows = self.sql_store.select_facts4celex_with_ids(case_id)
        if not rows:
            return EvidenceResult(
                text=f"[case_facts] No facts found for celex_id='{case_id}'."
            )
        lines: list[str] = []
        fact_ids: list[str] = []
        fact_names: list[str] = []
        for fact_id, name, value in rows:
            lines.append(f"  {name}: {value[:500]}")
            # Structural labels (e.g. case_title) never anchor an edge.
            if name in _NON_FACT_NAMES:
                continue
            fact_ids.append(fact_id)
            if name not in fact_names:
                fact_names.append(name)
        text = f"[case_facts] celex_id={case_id}\n" + "\n".join(lines)
        return EvidenceResult(
            text=text,
            fact_ids=fact_ids,
            fact_names=fact_names,
            celex_ids=[case_id],
        )

    def _run_rag_search(self, query: str) -> EvidenceResult:
        results = self.kb_searcher.semantic_search_facts(query=query, hits=10)
        if not results:
            return EvidenceResult(text="[rag_search] No results.")

        # Enrich each celex_id with its case_title from the facts table so a
        # general query's answer reads as a titled list of cases.
        titles = self._fetch_case_titles([r.celex_id for r in results])
        lines = []
        fact_ids: list[str] = []
        fact_names: list[str] = []
        for r in results:
            title = titles.get(r.celex_id, "")
            title_part = f' case_title="{title}"' if title else ""
            lines.append(
                f"  celex_id={r.celex_id}{title_part} fact={r.fact_name} "
                f"sim={r.similarity:.3f}: {r.value_txt[:300]}"
            )
            fact_ids.append(r.id)
            if r.fact_name not in _NON_FACT_NAMES and r.fact_name not in fact_names:
                fact_names.append(r.fact_name)
        return EvidenceResult(
            text="[rag_search]\n" + "\n".join(lines),
            fact_ids=fact_ids,
            fact_names=fact_names,
            celex_ids=list(dict.fromkeys(r.celex_id for r in results if r.celex_id)),
        )

    def _run_rag_search_in_case(self, case_id: str, query: str) -> EvidenceResult:
        if not case_id:
            return EvidenceResult(text="[rag_search_in_case] No case_id provided.")
        # Extract a keyword from the query for the ILIKE pass.  The hybrid
        # searcher skips keyword search when the query is a single
        # unsplittable word — passing the full question as ``query`` avoids
        # that gate, while the first matched unstructured-topic keyword
        # feeds the ILIKE path reliably.
        q_lower = query.lower()
        keyword_for_ilike = next(
            (t for t in _UNSTRUCTURED_TOPICS if t in q_lower), query
        )
        chunks = self.hybrid_searcher.search(
            celex_id=case_id, query=query, keyword=keyword_for_ilike, top_k=15
        )
        if not chunks:
            return EvidenceResult(
                text=f"[rag_search_in_case] No chunks for celex_id='{case_id}'."
            )
        lines = [f"  [doc_pos:{c.doc_pos}] {c.chunk[:800]}" for c in chunks]
        chunk_ids = [c.id for c in chunks if c.id]
        text = f"[rag_search_in_case] celex_id={case_id}\n" + "\n".join(lines)
        return EvidenceResult(text=text, chunk_ids=chunk_ids, celex_ids=[case_id])

    def _run_doc_search(self, query: str) -> EvidenceResult:
        """Cross-case document search: keyword-first, then semantic supplement.

        Pure vector search underperforms for proper-noun / entity lookups —
        a single-word query like "Koninklijke" embeds poorly and is out-ranked
        by tiny header chunks. So an ILIKE keyword pass runs first (one chunk
        per matching case), and the semantic pass only contributes cases the
        keyword pass did not already surface. Results are deduplicated by
        celex_id so an "all cases" query lists each case exactly once.

        Additionally, a keyword **count** query is issued to capture how many
        chunks per case contain the keyword. This aggregate data is placed in
        the evidence header so the synthesizer can answer counting / "how
        many" questions — the deduplication-by-celex sampling alone cannot
        support those queries.
        """
        keyword_chunks = self.doc_searcher.keyword_search_chunks_all_cases(query)
        semantic_chunks = self.doc_searcher.semantic_search_chunks(query=query, hits=5)

        # -- Multi-word full-text leg ---------------------------------------
        # A single ILIKE '%multi word query%' matches nothing, so multi-word
        # doc_search queries ("list all cartel periods") previously fell back
        # to semantic-only. Postgres FTS AND's the stemmed lexemes instead.
        # Guarded so lightweight test stubs / older toolkits degrade cleanly.
        fts_chunks: list = []
        try:
            _raw_fts = self.doc_searcher.fulltext_search_chunks_all_cases(query)
            if isinstance(_raw_fts, list):
                fts_chunks = _raw_fts
        except (AttributeError, TypeError, NotImplementedError):
            logger.debug(
                "fulltext_search_chunks_all_cases not available; skipping FTS leg."
            )

        # -- Keyword occurrence counts (aggregate, non-deduplicated) -------
        # Gracefully degrade when the searcher does not implement the count
        # method (e.g. older toolkits or lightweight test stubs).
        keyword_counts: list[dict] = []
        try:
            _raw_counts = self.doc_searcher.keyword_count_chunks_all_cases(query)
            if isinstance(_raw_counts, list):
                keyword_counts = _raw_counts
        except (AttributeError, TypeError, NotImplementedError):
            logger.debug("keyword_count_chunks_all_cases not available; skipping counts.")

        seen: set[str] = set()
        merged = []
        for chunk in [*keyword_chunks, *fts_chunks, *semantic_chunks]:
            if chunk.celex_id not in seen:
                merged.append(chunk)
                seen.add(chunk.celex_id)

        if not merged and not keyword_counts:
            return EvidenceResult(text="[doc_search] No chunks found.")

        # -- Build the count header ----------------------------------------
        header_lines: list[str] = []
        if keyword_counts:
            total_hits = sum(entry.get("count", 0) for entry in keyword_counts)
            num_cases = len(keyword_counts)
            header_lines.append(
                f"[doc_search] Keyword '{query}' found in {total_hits} chunks "
                f"across {num_cases} cases."
            )
            header_lines.append("Per-case chunk counts (descending):")
            for entry in keyword_counts:
                header_lines.append(
                    f"  celex_id={entry['celex_id']}  chunks={entry['count']}"
                )
            header_lines.append("")  # blank line before sample evidence

        # -- Build sample chunk evidence -----------------------------------
        # Enrich each surfaced case with its case_title from the facts table —
        # a general (no-celex) query answers with a list of celex_ids, and the
        # title makes that list human-readable for the synthesizer.
        if merged:
            titles = self._fetch_case_titles([c.celex_id for c in merged])
            if not header_lines:
                header_lines.append("[doc_search]")
            header_lines.append("Sample chunks per case:")
            for c in merged:
                title = titles.get(c.celex_id, "")
                title_part = f' case_title="{title}"' if title else ""
                header_lines.append(
                    f"  celex_id={c.celex_id}{title_part} "
                    f"[doc_pos:{c.doc_pos}] ({c.origin}) {c.chunk[:400]}"
                )

        chunk_ids = [c.id for c in merged if c.id]
        celex_ids = list(
            dict.fromkeys(
                [c.celex_id for c in merged if c.celex_id]
                + [
                    str(entry["celex_id"])
                    for entry in keyword_counts
                    if entry.get("celex_id")
                ]
            )
        )
        return EvidenceResult(
            text="\n".join(header_lines),
            chunk_ids=chunk_ids,
            celex_ids=celex_ids,
        )

    def _run_knowledge_search(self, query: str) -> EvidenceResult:
        """Full-text search over the distilled knowledge_items store.

        Falls back gracefully when no ``knowledge_searcher`` was injected (e.g.
        lightweight test stubs) by returning an empty evidence result rather
        than raising.

        Args:
            query: The user question or keyword string used for FTS.

        Returns:
            :class:`EvidenceResult` whose ``text`` lists matching knowledge
            nuggets, one per line, prefixed with their type and scope.
        """
        if self.knowledge_searcher is None:
            logger.debug("knowledge_search skipped: no knowledge_searcher injected.")
            return EvidenceResult(text="[knowledge_search] Not available.")

        # AND-based FTS first (higher precision); fall back to OR-based search
        # when the AND query has no hits because question words (e.g. "corporations")
        # are absent from the stored nugget text.
        items = self.knowledge_searcher.search_knowledge_fulltext(query, limit=20)
        if not items:
            items = self.knowledge_searcher.search_knowledge_any_terms(query, limit=20)
        if not items:
            return EvidenceResult(text="[knowledge_search] No matching knowledge items.")

        return render_knowledge_items(
            items,
            getattr(self.knowledge_searcher, "get_knowledge_sources", None),
            header="[knowledge_search]",
        )

    def _run_graph_search(self, step: RetrievalStep) -> EvidenceResult:
        """Expand the company cohort reachable from a seed stem via shared cases.

        The one retrieval surface that traverses ``entity_edges`` rather than
        projecting it. Answers "which companies were in cartels together with
        X" - a company-to-company relationship that the flat Q&A views hold no
        edge for, so no amount of ``kb_sql`` over them can reach it.

        Degrades to an "unavailable" result rather than raising when no
        ``graph_searcher`` was injected (lightweight test stubs) or when the
        feature is switched off, mirroring ``_run_knowledge_search``.

        Args:
            step: The retrieval step; the traversal parameters are read from
                its typed ``graph_seed`` / ``graph_relation`` / ``graph_hops``
                fields rather than from ``query``.

        Returns:
            :class:`EvidenceResult` listing the matched seed nodes and the
            reachable peers. ``fact_ids`` carries the edge lineage anchors, so
            a graph-derived answer stays provenance-resolvable; ``celex_ids``
            carries the connecting decisions.
        """
        if not settings.GRAPH_SEARCH_ENABLED:
            return EvidenceResult(text="[graph_search] Disabled.")
        if self.graph_searcher is None:
            logger.debug("graph_search skipped: no graph_searcher injected.")
            return EvidenceResult(text="[graph_search] Not available.")

        seed_stem = (step.graph_seed or "").strip()
        if not seed_stem:
            return EvidenceResult(text="[graph_search] No graph_seed provided.")

        try:
            relation = EdgeRelation(step.graph_relation or "participated_in")
        except ValueError:
            logger.warning(
                "graph_search got unknown relation '%s'; using participated_in.",
                step.graph_relation,
            )
            relation = EdgeRelation.PARTICIPATED_IN

        # Clamp here rather than trusting the planner: depth 3 returns a tenth
        # of every company node and means nothing.
        hops = max(1, min(int(step.graph_hops or 1), settings.GRAPH_SEARCH_MAX_HOPS))

        neighbourhood = self.graph_searcher.co_participants_by_name(
            name_stem=seed_stem, relation=relation, max_hops=hops
        )
        return self._render_graph_evidence(neighbourhood)

    @staticmethod
    def _render_graph_evidence(neighbourhood: GraphNeighbourhood) -> EvidenceResult:
        """Render a traversal result, keeping celex_ids machine-readable.

        Every connecting decision is emitted as its raw stored id, because the
        Master Planner recovers case ids by regexing this text back out
        (``master_planner_module._extract_all_celex_ids``); an id that is not in
        the text never reaches the join stage.

        Args:
            neighbourhood: The traversal result to render.

        Returns:
            :class:`EvidenceResult` with the rendered text plus the edge
            lineage anchors and connecting decisions as typed ids.
        """
        if not neighbourhood.seeds:
            return EvidenceResult(
                text=(
                    f"[graph_search] No company node matches the stem "
                    f"'{neighbourhood.seed_stem}'."
                )
            )

        lines = [
            f"[graph_search] relation={neighbourhood.relation.value} "
            f"hops<={neighbourhood.max_hops}",
            f"Seed nodes matching '{neighbourhood.seed_stem}' "
            f"({len(neighbourhood.seeds)}): "
            + "; ".join(
                f"{seed.canonical_name} ({seed.edge_count} edge(s))"
                for seed in neighbourhood.seeds
            ),
        ]
        if not neighbourhood.peers:
            lines.append("No co-participating companies found.")
            return EvidenceResult(text="\n".join(lines))

        suffix = " (capped, list is incomplete)" if neighbourhood.truncated else ""
        lines.append(f"Co-participating companies ({len(neighbourhood.peers)}){suffix}:")
        celex_ids: list[str] = []
        fact_ids: list[str] = []
        for peer in neighbourhood.peers:
            lei_part = f" lei={peer.lei_id}" if peer.lei_id else ""
            shared = ", ".join(peer.shared_celex_ids)
            lines.append(
                f"  {peer.canonical_name}{lei_part} hops={peer.hops} "
                f"shared_cases: {shared}"
            )
            for cid in peer.shared_celex_ids:
                if cid not in celex_ids:
                    celex_ids.append(cid)
            for fid in peer.source_fact_ids:
                if fid not in fact_ids:
                    fact_ids.append(fid)
        # Coverage is a floor: edges exist only where a roster fact was
        # extracted, so absence from this list is not absence from the cartel.
        lines.append(
            "NOTE: an edge exists only where a company roster fact was "
            "extracted, so this list is a floor, not a census."
        )
        return EvidenceResult(
            text="\n".join(lines), fact_ids=fact_ids, celex_ids=celex_ids
        )
