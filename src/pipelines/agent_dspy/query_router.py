"""Intent-based router over Q&A and enumeration fact-extraction strategies.

The router is the thin layer that sits *above* both strategies. It owns
exactly one responsibility — classifying each question as Q&A vs.
enumeration — and dispatches to the matching downstream module. It
deliberately does **not** know how either strategy works internally;
each downstream module stays cohesive and independently testable.

The classifier is heuristic for v1. Real query phrasings are small and
fixed (driven by YAML fact targets), so regex pattern matching is
cheap, transparent, and easy to evolve. The decided intent is attached
to the returned prediction as ``routing_decision`` so misroutes show
up in logs and audit trails rather than being silent.

If queries later prove too varied for heuristics, swap
``classify_intent`` for an LLM-backed classifier — the rest of the
module stays the same.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

import dspy

from pipelines.agent_dspy.enumeration_fact_module import EnumerationFactModule
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.universal_modules import UniversalFactModule

logger = logging.getLogger(__name__)


Intent = Literal["enumeration", "qa"]


# Patterns ordered from most-specific to most-generic. A single match is
# enough to flip the question into the enumeration path. The patterns
# intentionally over-trigger on the side of recall: a wrongly-routed
# enumeration query still produces a valid answer (just slower and more
# thorough), while a wrongly-routed Q&A query silently drops rare hits.
_ENUMERATION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\blist\s+(all|every|each)\b",
        r"\benumerate\b",
        r"\ball\s+(courts?|parties|companies|fines|applicants?|remedies)\b",
        r"\bevery\s+(court|party|company|fine|applicant|remedy)\b",
        r"\bwhich\s+(courts?|parties|companies|applicants?|remedies)\b",
        r"\bidentify\s+(all|every)\b",
    )
)


def classify_intent(question: str) -> Intent:
    """Classify a fact-extraction question as ``"enumeration"`` or ``"qa"``.

    Pure function — no I/O, no LLM call. Lower-cases the input and tests
    against a fixed pattern table. Used both at runtime by
    ``QueryRouterModule`` and directly by unit tests.

    Args:
        question: The natural-language question from the YAML target.

    Returns:
        ``"enumeration"`` if any enumeration pattern matches, else
        ``"qa"``.
    """
    if not question:
        return "qa"
    for pattern in _ENUMERATION_PATTERNS:
        if pattern.search(question):
            return "enumeration"
    return "qa"


class QueryRouterModule(dspy.Module):
    """Dispatch each fact-extraction call to Q&A or enumeration strategy.

    Parameters
    ----------
    qa:
        ``UniversalFactModule`` — the existing precision-tuned strategy,
        used for "answer this question" style targets.
    enumeration:
        ``EnumerationFactModule`` — the recall-tuned strategy, used for
        "list all X" style targets.
    """

    def __init__(
        self,
        qa: UniversalFactModule,
        enumeration: EnumerationFactModule,
    ):
        super().__init__()
        self.qa = qa
        self.enumeration = enumeration

    def forward(self, params: FactExtractorParamVO) -> dspy.Prediction:
        """Classify the question, dispatch, and tag the prediction.

        The downstream prediction is returned unmodified except for an
        added ``routing_decision`` attribute — useful for log triage
        when a query is suspected to have been misrouted.
        """
        intent = classify_intent(params.question)
        logger.info(
            "QueryRouter routed target '%s' to %s strategy",
            params.target_fact_name,
            intent,
        )
        target = self.enumeration if intent == "enumeration" else self.qa
        prediction = target(params=params)
        prediction.routing_decision = intent
        return prediction
