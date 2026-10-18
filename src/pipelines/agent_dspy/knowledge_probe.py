"""
Pre-plan knowledge probe: has this question been answered before?

Every knowledge item is ingested with 3-4 embedded question variants
(``knowledge_question_embeddings``).  The probe embeds the incoming question,
takes the items whose closest variant lies within
``settings.KNOWLEDGE_PROBE_MAX_DISTANCE``, and hands them to the executor as a
``Step 0 (knowledge_match)`` evidence block -- independent of whether the
planner chose ``knowledge_search``.

Embeddings barely register which entity or case a question names: measured on
the live store, "list cartel peers of NSK" lies 0.17 from the NTN item, and
"list leniency cases" lies 0.07 from a variant scoped to one case.  Distance
is therefore paired with a deterministic anchor guard:

- identifier tokens (anything containing a digit: celex ids, case numbers,
  years) must be the same set in the question and the matched variant;
- capitalised name tokens of the question must occur in the matched variant
  or in the knowledge text.

Exports:
    KnowledgeProbe       -- the probe, provisioned with a matcher and embedder.
    QueryAnchors         -- identifier / name tokens of a text.
    extract_anchors      -- tokenizer behind the guard.
    anchors_compatible   -- the guard predicate.
"""

import logging
import re
from collections.abc import Callable
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from pipelines.agent_dspy.dspy_protocols import KnowledgeQuestionMatcher
from pipelines.agent_dspy.retrieval_dispatch import (
    EvidenceResult,
    render_knowledge_items,
)

logger = logging.getLogger(__name__)

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\w(?:[\w./\-]*\w)?")

# Capitalised words that name no particular entity: question openers and the
# institutional vocabulary nearly every question and variant shares.
_NON_NAME_WORDS: Final[frozenset[str]] = frozenset(
    {
        "which", "what", "who", "whom", "whose", "when", "where", "why", "how",
        "list", "show", "give", "name", "find", "tell", "please", "can",
        "could", "did", "does", "do", "is", "are", "was", "were", "has",
        "have", "the", "a", "an", "in", "of", "for", "and", "or",
        "eu", "ec", "eec", "eea", "tfeu", "european", "commission", "article",
        "treaty", "court", "general", "justice", "regulation", "council",
        "case", "cases", "celex", "celex_id", "context",
    }
)

_CELEX_PREFIX: Final[str] = "celex-"


class QueryAnchors(BaseModel):
    """Tokens that pin a question to specific cases and entities."""

    model_config = ConfigDict(frozen=True)

    ids: frozenset[str] = Field(
        description="Lowercased digit-bearing tokens (celex ids, case numbers, years)."
    )
    names: frozenset[str] = Field(
        description="Lowercased capitalised tokens naming an entity (company, place)."
    )


class _ProbeMatch(BaseModel):
    """One repository hit, validated at the boundary."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    item: Any = Field(description="The matched KnowledgeItem.")
    question_variant: str = Field(description="The stored phrasing that matched.")
    distance: float = Field(description="Cosine distance to the question.")


def extract_anchors(text: str) -> QueryAnchors:
    """Split *text* into identifier and name anchors.

    Args:
        text: A question or a stored question variant.

    Returns:
        The identifier tokens (any token containing a digit, with a leading
        ``CELEX-`` removed) and the name tokens (containing an uppercase
        letter, not a generic or question word).
    """
    ids: set[str] = set()
    names: set[str] = set()
    for token in _TOKEN_RE.findall(text or ""):
        lowered = token.lower()
        if any(ch.isdigit() for ch in token):
            ids.add(lowered.removeprefix(_CELEX_PREFIX))
        elif any(ch.isupper() for ch in token) and lowered not in _NON_NAME_WORDS:
            names.add(lowered)
    return QueryAnchors(ids=frozenset(ids), names=frozenset(names))


def anchors_compatible(question: str, variant: str, knowledge_text: str) -> bool:
    """Return True when a matched variant is about the same cases and entities.

    Args:
        question: The incoming question.
        variant: The stored question variant the embedding matched.
        knowledge_text: The knowledge item's text.

    Returns:
        True when the identifier sets of question and variant are equal and
        every name anchor of the question occurs as a token of the variant or
        the knowledge text.
    """
    asked = extract_anchors(question)
    if asked.ids != extract_anchors(variant).ids:
        return False
    vocabulary = {
        token.lower() for token in _TOKEN_RE.findall(f"{variant} {knowledge_text}")
    }
    return asked.names <= vocabulary


class KnowledgeProbe:
    """Surfaces knowledge items distilled from previously answered questions.

    Provisioner pattern: the matcher (``KnowledgeRepository``) and the query
    embedder are injected; the embedder must be the model the variants were
    stored with (qwen3, see ``KnowledgeIngestor``).

    Args:
        matcher: Repository satisfying :class:`KnowledgeQuestionMatcher`.
        embed_query: ``(str) -> list[float]`` query embedder.
        max_distance: Cosine-distance ceiling for a variant match.
        limit: Maximum items per probe.
    """

    def __init__(
        self,
        matcher: KnowledgeQuestionMatcher,
        embed_query: Callable[[str], list[float]],
        max_distance: float = settings.KNOWLEDGE_PROBE_MAX_DISTANCE,
        limit: int = settings.KNOWLEDGE_PROBE_LIMIT,
    ) -> None:
        self.matcher = matcher
        self.embed_query = embed_query
        self.max_distance = max_distance
        self.limit = limit

    def probe(self, question: str) -> EvidenceResult | None:
        """Return knowledge matching *question*, or ``None`` when nothing does.

        Never raises: an embedding or database failure is logged and treated
        as no match, so the probe cannot break answering.

        Args:
            question: The question as the planner saw it (router context
                suffix included, so a case-scoped question only matches
                variants scoped to the same case).

        Returns:
            A ``[knowledge_match]`` :class:`EvidenceResult` naming the matched
            question(s), or ``None``.
        """
        try:
            rows = self.matcher.search_knowledge_by_question(
                self.embed_query(question),
                max_distance=self.max_distance,
                limit=self.limit,
            )
        except Exception:
            logger.warning("Knowledge probe failed; continuing without it.", exc_info=True)
            return None

        matches = [_ProbeMatch.model_validate(row) for row in rows]
        kept = [
            m
            for m in matches
            if anchors_compatible(
                question, m.question_variant, getattr(m.item, "knowledge_text", "")
            )
        ]
        logger.info(
            "Knowledge probe: %d variant match(es) within %.2f, %d past the anchor guard.",
            len(matches),
            self.max_distance,
            len(kept),
        )
        if not kept:
            return None

        asked = ", ".join(
            f"'{variant}'" for variant in dict.fromkeys(m.question_variant for m in kept)
        )
        return render_knowledge_items(
            [m.item for m in kept],
            getattr(self.matcher, "get_knowledge_sources", None),
            header=(
                "[knowledge_match] Knowledge distilled from previously answered "
                f"question(s) {asked}:"
            ),
        )
