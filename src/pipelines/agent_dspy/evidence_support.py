"""
Shared evidence-support primitives reused across verification modules.

Both :class:`pipelines.agent_dspy.evidence_verifier_module.EvidenceVerifierModule`
(document chunks) and
:class:`pipelines.agent_dspy.book_cross_check_module.BookCrossCheckModule`
(book chapters) judge whether a piece of text supports an extracted fact. The
declarative DSPy signature, its Pydantic verdict, and the small pure helpers
(claim builder, quote-window snippet, judge-call wrapper) live here so neither
module imports the other's privates (see ISSUES.md reuse-boundary item).

Design rules honoured: declarative DSPy signature with ``desc`` on every field
(Rule 1), Pydantic structured judge output (Rule 3).
"""

import logging
import re
import unicodedata
from typing import Any

import dspy
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Quote containment folds the surface differences a PDF-to-markdown conversion
# introduces (see ``quote_in_text``): a hyphen followed by a line break splits a
# word, soft hyphens are invisible, typographic quotes/dashes are normalised,
# and whitespace runs collapse. None of these lets a paraphrase through.
_SOFT_HYPHEN = "\u00ad"
_HYPHEN_LINE_BREAK = re.compile(r"-\s*\n\s*")
_WHITESPACE_RUN = re.compile(r"\s+")
_TYPOGRAPHIC_MAP = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00a0": " ",
    }
)


class EvidenceSupportResult(BaseModel):
    """Structured verdict on whether one chunk supports one fact."""

    label: str = Field(
        description=(
            "One of: supports, contradicts, insufficient, partially_supports, "
            "ambiguous. Use 'supports' ONLY when the chunk directly states or "
            "entails the fact with every required element present."
        )
    )
    support_score: float = Field(
        description="0.0-1.0 confidence that the chunk supports the fact."
    )
    evidence_quote: str | None = Field(
        default=None,
        description=(
            "An EXACT substring of the chunk that proves the fact. Must be null "
            "when the label is not 'supports'. Never paraphrase."
        ),
    )
    supported_elements: list[str] = Field(
        default_factory=list,
        description="Required elements the chunk explicitly supports.",
    )
    missing_elements: list[str] = Field(
        default_factory=list,
        description="Required elements the chunk fails to support.",
    )
    reason: str = Field(
        default="", description="One-sentence justification for the verdict."
    )


class EvidenceSupportSignature(dspy.Signature):
    """Decide whether a document chunk directly supports an extracted fact.

    Rules:
      - Mark ``supports`` only when the chunk states or entails the fact with no
        missing required element, and return an EXACT supporting quote.
      - Mark ``contradicts`` when the chunk asserts something incompatible.
      - Mark ``insufficient`` when related but not proving the fact.
      - Do not use outside knowledge.
      - If the fact names a company/entity, the relation must apply to THAT
        entity (not merely another party mentioned nearby).
      - If you cannot give an exact quote, do not mark ``supports``.
      - When ``extraction_question`` is given, treat it as the rubric that
        defines the fact: it is the question whose answer is the fact value, so
        the chunk supports the fact only when it answers that question
        consistently with the value.
      - When ``case_reference`` is given, the fact belongs to that case: text
        about a different case is ``insufficient``, never ``contradicts``, even
        when it states a conflicting date, fine or party.
    """

    fact_statement: str = dspy.InputField(
        desc="The extracted fact to verify, stated as a claim."
    )
    required_elements: str = dspy.InputField(
        desc="The entities and concepts the chunk must cover to support the fact."
    )
    extraction_question: str = dspy.InputField(
        desc=(
            "The original question that defined this fact - the criteria the "
            "chunk must satisfy. May be empty."
        )
    )
    case_reference: str = dspy.InputField(
        desc=(
            "The case (e.g. Commission case number) the fact belongs to. Text "
            "about another case cannot support or contradict it. May be empty."
        )
    )
    chunk_text: str = dspy.InputField(
        desc="The single candidate chunk of document text."
    )
    output: EvidenceSupportResult = dspy.OutputField(
        desc="Structured support verdict with an exact quote when supported."
    )


def fact_statement(fact: Any) -> str:
    """Build the claim string passed to the judge.

    Parameters
    ----------
    fact:
        A fact row exposing ``name``, ``value_md`` and ``value_json`` attributes.

    Returns
    -------
    str
        ``"<name>: <value>"`` using the markdown value, falling back to the JSON
        value, with surrounding whitespace stripped.
    """
    value = (fact.value_md or fact.value_json or "").strip()
    return f"{fact.name}: {value}"


def snippet(text: str, surfaces: set[str], window: int = 60) -> str | None:
    """Return a short text window around the first matched surface.

    Parameters
    ----------
    text:
        The full text to search.
    surfaces:
        Candidate surface strings; the first one found (case-insensitive) anchors
        the window.
    window:
        Number of characters of context to keep on each side of the match.

    Returns
    -------
    str or None
        The trimmed window, or ``None`` when no surface is found.
    """
    lowered = text.lower()
    for surface in surfaces:
        idx = lowered.find(surface.lower())
        if idx != -1:
            start = max(0, idx - window)
            end = min(len(text), idx + len(surface) + window)
            return text[start:end].strip()
    return None


def normalise_surface(text: str) -> str:
    """Fold conversion artefacts so two renderings of one passage compare equal.

    Applies NFKC normalisation (ligatures such as "fi" become two letters),
    maps typographic quotes and dashes to their ASCII forms, drops soft hyphens,
    joins words split by a hyphen at a line break, and collapses whitespace
    runs to one space.

    Parameters
    ----------
    text:
        Any text (a judge quote or a chunk).

    Returns
    -------
    str
        The folded text, stripped.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_TYPOGRAPHIC_MAP)
    folded = folded.replace(_SOFT_HYPHEN, "")
    folded = _HYPHEN_LINE_BREAK.sub("", folded)
    return _WHITESPACE_RUN.sub(" ", folded).strip()


def quote_in_text(
    quote: str | None, text: str, ignore_case: bool = False
) -> bool:
    """Return True when *quote* occurs verbatim in *text* up to surface folding.

    Both sides go through :func:`normalise_surface`, so whitespace, hyphenation
    at line breaks, soft hyphens, ligatures and typographic punctuation do not
    break containment. Word choice is not folded: a paraphrase still fails.

    Parameters
    ----------
    quote:
        The judge's evidence quote; ``None`` or blank never matches.
    text:
        The judged chunk text.
    ignore_case:
        Fold letter case as well (for identifiers matched with ``ILIKE``).

    Returns
    -------
    bool
        Whether the folded quote is a substring of the folded text.
    """
    if not quote:
        return False
    needle = normalise_surface(quote)
    haystack = normalise_surface(text)
    if ignore_case:
        needle, haystack = needle.casefold(), haystack.casefold()
    return bool(needle) and needle in haystack


def judge_support(
    judge: Any,
    statement: str,
    required_elements: str,
    chunk_text: str,
    question: str | None = None,
    case_reference: str | None = None,
) -> EvidenceSupportResult | None:
    """Call a support judge, returning ``None`` instead of crashing the run.

    Mirrors ``KnowledgeIngestor._judge_or_reject`` discipline: a crashing or
    malformed judge is logged and treated as ``None`` (skip this candidate), so
    a single bad LLM call never aborts an offline batch.

    Parameters
    ----------
    judge:
        A predictor implementing :class:`EvidenceSupportSignature` (e.g.
        ``dspy.Predict(EvidenceSupportSignature)`` or a test fake).
    statement:
        The fact stated as a claim (see :func:`fact_statement`).
    required_elements:
        Comma-separated entities/concepts the text must cover.
    chunk_text:
        The candidate text judged against the claim.
    question:
        Optional extraction question used as the judging rubric.
    case_reference:
        Optional case identifier the fact belongs to; text about another case
        is then insufficient rather than contradicting.

    Returns
    -------
    EvidenceSupportResult or None
        The structured verdict, or ``None`` when the judge raised or returned a
        non-structured output.
    """
    try:
        pred = judge(
            fact_statement=statement,
            required_elements=required_elements,
            extraction_question=question or "",
            case_reference=case_reference or "",
            chunk_text=chunk_text,
        )
    except Exception:
        logger.warning("Support judge raised - skipping candidate", exc_info=True)
        return None
    verdict = getattr(pred, "output", pred)
    if not isinstance(verdict, EvidenceSupportResult):
        logger.debug("Judge returned non-structured output; skipping")
        return None
    return verdict
