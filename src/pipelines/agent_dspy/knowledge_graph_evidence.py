"""
Entity-graph evidence for the knowledge-ingestion pipeline.

The typed entity graph (``entities`` / ``entity_edges``) is a *validated*
projection of the fact store: an edge exists only because a fact family
produced it, and ``entity_edges.source_fact_id`` is ``NOT NULL``, so every
relation is anchored to real evidence.  The knowledge pipeline, however, sees
those same facts flattened into a numbered ``name: value`` catalog, which
discards the relational structure entirely.

This module recovers that structure for two consumers:

* **Rendering** -- :func:`render_validated_relations` turns the relations into
  a compact block the extractor and the LLM judge receive as trustworthy
  context, so the judge's ``correctness`` criterion has a factual backstop
  instead of prose-only intuition.
* **Contradiction** -- :func:`find_graph_contradiction` is a deterministic,
  high-precision check that runs *before* the judge.  Because the graph is
  authoritative, a nugget that denies a relation the graph holds is wrong, and
  no LLM call needs to be spent to establish that.

The detector is deliberately **narrow**: it fires only when one sentence
mentions the relation's source entity, carries a negation, and that negation
sits close in front of a surface form of the relation.  It is a precision
instrument, not a recall instrument -- everything it does not catch falls
through to the judge, which now sees the same relations as evidence.

Exports:
    GraphContradiction        -- structured description of a detected conflict.
    render_validated_relations -- prompt block for extractor and judge.
    find_graph_contradiction  -- the deterministic pre-judge gate.
    relations_for_fact_ids    -- narrow a relation set to a nugget's cited facts.
"""

import logging
import re
from collections.abc import Iterable, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from pipelines.agent_dspy.knowledge_probe import extract_anchors
from pipelines.graph.entity_graph_models import EdgeRelation, ValidatedRelation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------

# Surface forms that assert a relation in prose.  Only the relations a nugget
# can plausibly state are listed: ``parent_of`` and ``active_in`` are reserved
# and unmaterialized, and ``dated_in`` expresses aggregated event bounds rather
# than a claim a sentence can deny.  Those three therefore render as evidence
# but never trigger a contradiction -- silence is the honest outcome when no
# reliable surface form exists.
_RELATION_SURFACE_RE: Final[dict[EdgeRelation, re.Pattern[str]]] = {
    EdgeRelation.PARTICIPATED_IN: re.compile(
        r"\b(participat\w*|took part|taking part|party to|member of|involved in)\b",
        re.IGNORECASE,
    ),
    EdgeRelation.FINED: re.compile(
        r"\b(fine[sd]?|fining|penalt(?:y|ies)|sanction(?:ed|s)?)\b", re.IGNORECASE
    ),
    EdgeRelation.LENIENCY_APPLIED: re.compile(
        r"\b(applied for leniency|leniency application|sought leniency"
        r"|requested leniency)\b",
        re.IGNORECASE,
    ),
    EdgeRelation.LENIENCY_GRANTED: re.compile(
        r"\b(granted leniency|leniency was granted|granted immunity"
        r"|leniency reduction|reduction (?:of|in) (?:the )?fine)\b",
        re.IGNORECASE,
    ),
    EdgeRelation.LENIENCY_REJECTED: re.compile(
        r"\b(rejected leniency|refused leniency"
        r"|leniency (?:application )?(?:was )?(?:rejected|refused|denied))\b",
        re.IGNORECASE,
    ),
    EdgeRelation.VIOLATED: re.compile(
        r"\b(violat\w*|infring\w*|breach\w*)\b", re.IGNORECASE
    ),
}

# Relations that cannot both hold for the same company and case.  A nugget
# affirming one while the graph holds the other is a contradiction even without
# a negation word.
_MUTUALLY_EXCLUSIVE: Final[dict[EdgeRelation, EdgeRelation]] = {
    EdgeRelation.LENIENCY_GRANTED: EdgeRelation.LENIENCY_REJECTED,
    EdgeRelation.LENIENCY_REJECTED: EdgeRelation.LENIENCY_GRANTED,
}

_NEGATION_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(not|never|no|neither|nor|without|nothing)\b|n't", re.IGNORECASE
)

# Words that turn an apparent denial into a statement about our *knowledge* of
# the relation ("it is not clear whether X was fined").  A hedge between the
# negation and the relation surface disqualifies the match.
_HEDGE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(clear|unclear|certain|uncertain|known|unknown|whether|if|apparent"
    r"|evident|stated|specified|mentioned|recorded|possible|likely|obvious)\b",
    re.IGNORECASE,
)

# Corporate-form and geographic tokens that identify no particular company.
# Matching a relation's source entity on one of these would let "GmbH" in an
# unrelated sentence stand in for the company itself.
_GENERIC_NAME_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "gmbh", "ag", "ltd", "limited", "plc", "inc", "corp", "corporation",
        "company", "companies", "kg", "kgaa", "mbh", "sa", "sas", "sarl",
        "srl", "spa", "bv", "nv", "ab", "oy", "oyj", "aps", "asa",
        "holding", "holdings", "group", "groupe", "gruppe", "international",
        "europe", "european", "deutschland", "france", "italia", "espana",
        "nederland", "belgium", "iberica", "uk", "usa", "und", "and", "the",
        "co", "cie", "societe", "société", "industries", "industrie",
    }
)

# Maximum characters between the end of a negation and the start of the
# relation surface for the two to count as one denial.  Wide enough for
# "was not, according to the decision, fined"; narrow enough that an unrelated
# negation earlier in a long sentence does not reach the relation.
_MAX_NEGATION_GAP: Final[int] = 40

# Sentence boundaries, subject to the abbreviation guard below.  Naive
# splitting on ".\s" is unusable here: canonical company names in this corpus
# are full of "Inc.", "S.A." and "Co.", and splitting inside one detaches the
# company from the claim made about it, silently disabling the entity check.
_BOUNDARY_RE: Final[re.Pattern[str]] = re.compile(r"[.!?;]\s+")

# Tokens that end an abbreviation rather than a sentence.  A single letter is
# treated the same way, covering initials and "S.A." style forms.
_ABBREVIATION_TOKENS: Final[frozenset[str]] = frozenset(
    {
        "inc", "ltd", "co", "corp", "plc", "gmbh", "ag", "sa", "nv", "bv",
        "spa", "srl", "sarl", "sas", "kg", "kgaa", "mbh", "oy", "ab", "aps",
        "no", "nos", "art", "arts", "para", "paras", "cf", "eg", "ie", "etc",
        "vs", "al", "jr", "sr", "st", "mr", "mrs", "ms", "dr", "prof",
    }
)
_NAME_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
_MIN_DISTINCTIVE_TOKEN_LEN: Final[int] = 3
_CELEX_PREFIX: Final[str] = "celex-"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class GraphContradiction(BaseModel):
    """A knowledge nugget conflicting with a validated entity-graph relation.

    Attributes
    ----------
    relation:
        The graph relation the nugget conflicts with.
    trigger:
        Which rule fired -- ``negated_relation`` (the nugget denies a relation
        the graph holds) or ``mutually_exclusive`` (the nugget asserts a
        relation that cannot coexist with the one the graph holds).
    sentence:
        The nugget sentence that carried the conflict, for the audit trail.
    """

    model_config = ConfigDict(frozen=True)

    relation: ValidatedRelation = Field(
        description="The validated graph relation that was contradicted."
    )
    trigger: str = Field(
        description="Rule that fired: negated_relation or mutually_exclusive."
    )
    sentence: str = Field(
        description="The nugget sentence containing the conflicting claim."
    )

    def describe(self) -> str:
        """Return a one-line human-readable summary for logs and audit rows.

        Returns
        -------
        str
            The trigger, the graph relation it conflicts with, and the
            offending sentence.
        """
        return (
            f"{self.trigger}: graph holds "
            f"{self.relation.src_name} --{self.relation.relation}--> "
            f"{self.relation.dst_name} [{self.relation.celex_id}], "
            f"nugget says {self.sentence!r}"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _distinctive_tokens(canonical_name: str) -> frozenset[str]:
    """Return the tokens of a company name that actually identify it.

    Corporate-form and geographic words are dropped, as are tokens shorter
    than :data:`_MIN_DISTINCTIVE_TOKEN_LEN`, so "NSK Deutschland GmbH" reduces
    to ``{"nsk"}``.  A name that reduces to nothing yields an empty set, and
    callers must then skip the relation rather than match on a generic word.

    Parameters
    ----------
    canonical_name:
        The entity's ``canonical_name`` as stored in the graph.

    Returns
    -------
    frozenset[str]
        Lowercased identifying tokens, possibly empty.
    """
    return frozenset(
        token
        for token in _NAME_TOKEN_RE.findall(canonical_name.lower())
        if len(token) >= _MIN_DISTINCTIVE_TOKEN_LEN
        and token not in _GENERIC_NAME_TOKENS
        and not token.isdigit()
    )


def _split_sentences(knowledge_text: str) -> list[str]:
    """Split a nugget into sentences without breaking inside abbreviations.

    A boundary candidate is rejected when the word before the period is a
    known abbreviation or a single letter, so "Hitachi-LG Data Storage, Inc.
    and ... Korea, Inc. was not fined." stays one sentence and the entity check
    can still see the company alongside the claim.

    Parameters
    ----------
    knowledge_text:
        The candidate nugget.

    Returns
    -------
    list[str]
        Non-empty sentences, in order.
    """
    sentences: list[str] = []
    start = 0
    for boundary in _BOUNDARY_RE.finditer(knowledge_text):
        head = knowledge_text[start : boundary.start()]
        words = _NAME_TOKEN_RE.findall(head.lower())
        if words and (words[-1] in _ABBREVIATION_TOKENS or len(words[-1]) == 1):
            continue
        chunk = knowledge_text[start : boundary.end()].strip()
        if chunk:
            sentences.append(chunk)
        start = boundary.end()
    tail = knowledge_text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _mentions_entity(sentence: str, tokens: Iterable[str]) -> bool:
    """Return True when *sentence* names the entity behind *tokens*.

    Matching is word-boundary, matching the graph's own ``\\m``/``\\M`` seed
    discipline: a substring test on "bayer" would also fire on "Bayerische
    Hypo- und Vereinsbank AG", an unrelated company sharing a Bavarian prefix.

    Parameters
    ----------
    sentence:
        One sentence of the knowledge nugget.
    tokens:
        Distinctive tokens of the entity's canonical name.

    Returns
    -------
    bool
        True when any distinctive token occurs as a whole word.
    """
    lowered = sentence.lower()
    return any(
        re.search(rf"\b{re.escape(token)}\b", lowered) for token in tokens
    )


def _negated_before(sentence: str, surface: re.Match[str]) -> bool:
    """Return True when a negation denies the relation matched by *surface*.

    The negation must **precede** the surface form -- "X participated but was
    not fined" denies ``fined``, not ``participated_in`` -- sit within
    :data:`_MAX_NEGATION_GAP` characters of it, and carry no hedge word in
    between, which would make the sentence a statement about uncertainty
    rather than a denial.

    Parameters
    ----------
    sentence:
        The sentence containing the surface match.
    surface:
        The relation surface-form match within that sentence.

    Returns
    -------
    bool
        True when the relation is denied.
    """
    for negation in _NEGATION_RE.finditer(sentence[: surface.start()]):
        gap = sentence[negation.end() : surface.start()]
        if len(gap) <= _MAX_NEGATION_GAP and not _HEDGE_RE.search(gap):
            return True
    return False


def _case_out_of_scope(nugget_ids: frozenset[str], celex_id: str) -> bool:
    """Return True when the nugget is explicitly scoped to a different case.

    A nugget naming no identifier is treated as in scope, since a general
    claim about a company can still contradict a per-case relation.  A nugget
    that *does* name identifiers must name this relation's decision among
    them, otherwise it is talking about some other case.

    Parameters
    ----------
    nugget_ids:
        Identifier anchors extracted from the nugget text.
    celex_id:
        The relation's decision id.

    Returns
    -------
    bool
        True when the relation should be skipped for this nugget.
    """
    if not nugget_ids:
        return False
    return celex_id.lower().removeprefix(_CELEX_PREFIX) not in nugget_ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def relations_for_fact_ids(
    relations: Sequence[ValidatedRelation], fact_ids: Iterable[object]
) -> tuple[ValidatedRelation, ...]:
    """Narrow a relation set to those anchored on *fact_ids*.

    The module resolves relations once for every fact the agent cited, then
    hands each nugget only the relations behind the facts that nugget itself
    cited -- one query instead of one per nugget, without widening any
    nugget's evidence beyond what it claimed.

    Parameters
    ----------
    relations:
        Relations resolved for the full supporting-fact set.
    fact_ids:
        The fact ids one nugget cited (``UUID`` or ``str``).

    Returns
    -------
    tuple[ValidatedRelation, ...]
        The subset anchored on those facts, in the input order.
    """
    wanted = {str(fact_id) for fact_id in fact_ids}
    return tuple(rel for rel in relations if rel.source_fact_id in wanted)


def render_validated_relations(
    relations: Sequence[ValidatedRelation], max_lines: int | None = None
) -> str:
    """Render relations as a prompt block for the extractor and the judge.

    The block is capped because edges per fact are heavily skewed: a
    participation fact family emits one edge per party, so a single wide trace
    can carry hundreds of rows that would crowd out the actual evidence.  When
    the cap bites, the block says so explicitly -- a silently truncated list
    invites the model to read absence as a denial.

    Parameters
    ----------
    relations:
        The validated relations behind the facts under consideration.
    max_lines:
        Maximum relations to render; defaults to
        ``settings.KNOWLEDGE_GRAPH_EVIDENCE_RENDER_LIMIT``.

    Returns
    -------
    str
        One line per relation, or the empty string when there are none, so a
        signature field simply carries nothing rather than an empty header.
    """
    if not relations:
        return ""
    cap = (
        max_lines
        if max_lines is not None
        else settings.KNOWLEDGE_GRAPH_EVIDENCE_RENDER_LIMIT
    )
    lines = ["VALIDATED RELATIONS (entity graph, trustworthy):"]
    for rel in relations[:cap]:
        amount = f" (amount: {rel.amount})" if rel.amount else ""
        lines.append(
            f"- {rel.src_name} --{rel.relation}--> {rel.dst_name} "
            f"[{rel.celex_id}]{amount}"
        )
    remaining = len(relations) - cap
    if remaining > 0:
        lines.append(
            f"... and {remaining} further relation(s) not shown; this list is "
            f"partial, so absence from it proves nothing."
        )
    return "\n".join(lines)


def find_graph_contradiction(
    knowledge_text: str, relations: Sequence[ValidatedRelation]
) -> GraphContradiction | None:
    """Detect a nugget claim that conflicts with a validated graph relation.

    Two rules fire, both scoped to a single sentence that names the relation's
    source entity:

    1. **negated_relation** -- the sentence denies a relation the graph holds
       ("NSK was not fined in the bearings cartel").
    2. **mutually_exclusive** -- the sentence affirms a relation that cannot
       coexist with the held one (asserting ``leniency_rejected`` where the
       graph holds ``leniency_granted``).

    Relations are skipped when the entity's name reduces to no distinctive
    token, when the nugget is explicitly scoped to other decisions, or when the
    relation has no reliable prose surface form.  Skipping is the safe default:
    a missed contradiction still reaches the LLM judge, whereas a false one
    would discard a sound nugget without an LLM ever seeing it.

    Parameters
    ----------
    knowledge_text:
        The candidate nugget.
    relations:
        Validated relations behind the facts the nugget cited.

    Returns
    -------
    GraphContradiction | None
        The first conflict found in relation order, or ``None``.
    """
    if not knowledge_text or not relations:
        return None

    nugget_ids = extract_anchors(knowledge_text).ids
    sentences = _split_sentences(knowledge_text)

    for rel in relations:
        if _case_out_of_scope(nugget_ids, rel.celex_id):
            continue
        tokens = _distinctive_tokens(rel.src_name)
        if not tokens:
            continue

        held_surface = _RELATION_SURFACE_RE.get(rel.relation)
        excluded = _MUTUALLY_EXCLUSIVE.get(rel.relation)
        excluded_surface = (
            _RELATION_SURFACE_RE.get(excluded) if excluded is not None else None
        )
        if held_surface is None and excluded_surface is None:
            continue

        for sentence in sentences:
            if not _mentions_entity(sentence, tokens):
                continue

            if held_surface is not None:
                match = held_surface.search(sentence)
                if match is not None and _negated_before(sentence, match):
                    return GraphContradiction(
                        relation=rel,
                        trigger="negated_relation",
                        sentence=sentence.strip(),
                    )

            if excluded_surface is not None:
                match = excluded_surface.search(sentence)
                if match is not None and not _negated_before(sentence, match):
                    return GraphContradiction(
                        relation=rel,
                        trigger="mutually_exclusive",
                        sentence=sentence.strip(),
                    )

    return None
