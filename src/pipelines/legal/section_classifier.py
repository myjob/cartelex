"""Rule-first classification of decision chunks into ``SectionType``.

Every EU Commission competition decision follows one rhetorical skeleton:
parties -> recitals/facts -> legal assessment -> fines -> operative articles
-> annex. This module assigns that structure to markdown chunks from
``pg_embedding_qwen`` so retrieval can filter and rank by section instead of
raw document position (knowledge-representation review, Weakness 4).

Classification strategy (executed by ``src/batch/Main_classify_sections.py``):

1. **Rules** — keyword-anchored regexes over the markdown headings embedded in
   the chunk text (the splitter keeps heading lines in ``page_content``), plus
   the operative-part marker ("HAS ADOPTED THIS DECISION"). Heading structure
   varies across decades and OCR-ingested documents are unreliable, so rules
   never guess: an unrecognized or garbled heading is a no-match, and bare
   numbering is never used as a signal.
2. **Plausibility gate** — rule labels for a document are only trusted when
   the labeled sequence respects the canonical section order and covers a
   minimum share of chunks; otherwise the whole document falls through to the
   LLM (the designated path for the untrustworthy-OCR cohort).
3. **Forward-fill** — sections are contiguous in ``doc_pos`` order, so
   unlabeled chunks inherit the preceding label.
4. **LLM fallback** — ``SectionClassifierModule`` classifies remaining chunks
   from content, not headings.

The rule layer is pure (no DB, no LLM) — same charter as
``infringement_concept.py`` — so it can be reused at ingest time and tested
without fixtures.
"""

import logging
import re
from typing import Any

import dspy
from pydantic import BaseModel, Field

from extract.rag.models.chunk_vo import SectionType

logger = logging.getLogger(__name__)

# Markdown heading lines the splitter left inside the chunk text.
_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)

# The hinge between the reasoning and the numbered final Articles. Tolerant of
# OCR whitespace; both historical spellings.
_OPERATIVE_MARKER_RE = re.compile(
    r"HAS\s+ADOPTED\s+(?:THIS|THE\s+FOLLOWING)\s+DECISION", re.IGNORECASE
)

# A bare operative article heading ("Article 1", "ARTICLE 4"). Anything after
# the number (e.g. "ARTICLE 85 (3)") is a treaty-article reference, not an
# operative article, and falls through to the legal-assessment rules.
_OPERATIVE_ARTICLE_RE = re.compile(r"^ARTICLE\s+\d+\s*$")

# Ordered (pattern, SectionType) rules, matched against the upper-cased,
# whitespace-collapsed heading text; first match wins. Keyword-anchored only —
# numbering prefixes ("II.", "3.4.4.", "B.") carry no signal on their own.
_HEADING_RULES: tuple[tuple[re.Pattern[str], SectionType], ...] = (
    (_OPERATIVE_ARTICLE_RE, SectionType.OPERATIVE),
    (re.compile(r"^(ANNEX|APPENDIX)\b"), SectionType.ANNEX),
    (re.compile(r"\bFINES?\b"), SectionType.FINES),
    (re.compile(r"\bPERIODIC\s+PENALT"), SectionType.FINES),
    (re.compile(r"\bREMEDIES\b"), SectionType.FINES),
    (re.compile(r"\bBASIC\s+AMOUNT\b"), SectionType.FINES),
    (re.compile(r"\b(AGGRAVATING|MITIGATING)\b"), SectionType.FINES),
    (re.compile(r"\bDETERRENCE\b"), SectionType.FINES),
    (re.compile(r"\bLEGAL\s+(ASSESSMENT|APPRAISAL)\b"), SectionType.LEGAL_ASSESSMENT),
    (re.compile(r"\bAPPLICA(BILITY|TION)\s+OF\s+ARTICLE\b"), SectionType.LEGAL_ASSESSMENT),
    (re.compile(r"\bARTICLE\s+(85|86|90|81|82|101|102|106)\b"), SectionType.LEGAL_ASSESSMENT),
    (re.compile(r"\bRESTRICTION\s+OF\s+COMPETITION\b"), SectionType.LEGAL_ASSESSMENT),
    (re.compile(r"\bEFFECT\s+ON\s+TRADE\b"), SectionType.LEGAL_ASSESSMENT),
    (re.compile(r"\bDURATION\s+OF\s+THE\s+INFRINGEMENT\b"), SectionType.LEGAL_ASSESSMENT),
    (
        re.compile(r"\b(COMMENTS|ARGUMENTS|OBSERVATIONS)\s+OF\s+THE\s+PART"),
        SectionType.LEGAL_ASSESSMENT,
    ),
    (re.compile(r"\bLIABILITY\b"), SectionType.LEGAL_ASSESSMENT),
    (re.compile(r"\bADDRESSEES?\b"), SectionType.LEGAL_ASSESSMENT),
    # Whole-heading match ("A. THE PARTIES", "II. THE PARTIES CONCERNED"):
    # sentence-like headings merely mentioning the parties must not match.
    (
        re.compile(r"(?:^|[.\d]\s+)THE\s+PARTIES(?:\s+CONCERNED)?\s*:?$"),
        SectionType.PARTIES,
    ),
    (re.compile(r"\bPARTIES\s+CONCERNED\b"), SectionType.PARTIES),
    (re.compile(r"\bUNDERTAKINGS?\s+CONCERNED\b"), SectionType.PARTIES),
    (re.compile(r"^THE\s+UNDERTAKINGS?\b"), SectionType.PARTIES),
    (re.compile(r"\bTHE\s+FACTS\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"^FACTS\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"\bINTRODUCTION\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"\bPROCEDURE\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"^WHEREAS\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"\bTHE\s+(INDUSTRY|PRODUCTS?|MARKETS?)\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"\bTHE\s+(NOTIFIED\s+)?AGREEMENTS?\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"\bDESCRIPTION\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"\bBACKGROUND\b"), SectionType.RECITALS_FACTS),
    (re.compile(r"\bTABLE\s+OF\s+CONTENTS\b"), SectionType.OTHER),
    (re.compile(r"^COMMISSION\s+DECISION\b"), SectionType.OTHER),
    (re.compile(r"\bRESTREINT\b"), SectionType.OTHER),
)

# Canonical position per section for the plausibility gate; OTHER floats and
# is excluded from order checking.
_CANONICAL_ORDER: dict[SectionType, int] = {
    SectionType.PARTIES: 0,
    SectionType.RECITALS_FACTS: 1,
    SectionType.LEGAL_ASSESSMENT: 2,
    SectionType.FINES: 3,
    SectionType.OPERATIVE: 4,
    SectionType.ANNEX: 5,
}

# Plausibility-gate tuning. Coverage is deliberately low: rules only label
# heading-bearing chunks and forward-fill closes the gaps; the gate exists to
# catch OCR documents whose headings match randomly, which shows up as order
# regressions, not as sparseness.
GATE_MIN_RULE_HITS: int = 2
GATE_MIN_COVERAGE: float = 0.05
GATE_MAX_REGRESSION_RATIO: float = 0.25


def iter_headings(chunk_text: str) -> list[str]:
    """Extract the markdown heading texts embedded in *chunk_text*.

    Parameters
    ----------
    chunk_text:
        Raw chunk text as stored in the ``document`` column; the splitter
        keeps ``#``/``##`` heading lines inside it.

    Returns
    -------
    list[str]
        Heading texts (without the ``#`` markers), in document order.
    """
    return [m.group(1) for m in _HEADING_RE.finditer(chunk_text)]


def classify_heading(heading: str) -> SectionType | None:
    """Classify a single heading text, or return ``None`` when unrecognized.

    An OCR-garbled or unknown heading is a deliberate no-match — never a
    guess — so it falls to forward-fill or the LLM instead of mislabeling.

    Parameters
    ----------
    heading:
        Heading text without the markdown ``#`` markers.

    Returns
    -------
    SectionType | None
        The matched section, or ``None`` when no rule applies.
    """
    normalized = " ".join(heading.upper().split())
    for pattern, section in _HEADING_RULES:
        if pattern.search(normalized):
            return section
    return None


def classify_chunk_by_rules(chunk_text: str, doc_pos: int) -> SectionType | None:
    """Classify one chunk from its embedded headings and structural markers.

    Precedence: the operative-part marker anywhere in the text wins, then the
    first classifiable heading, then the cover-page default (``doc_pos`` 0
    names the case and its addressees -> ``parties``).

    Parameters
    ----------
    chunk_text:
        Raw chunk text as stored in the ``document`` column.
    doc_pos:
        The chunk's structural index within its document.

    Returns
    -------
    SectionType | None
        The rule-assigned section, or ``None`` when the rules abstain.
    """
    if _OPERATIVE_MARKER_RE.search(chunk_text):
        return SectionType.OPERATIVE
    for heading in iter_headings(chunk_text):
        section = classify_heading(heading)
        if section is not None:
            return section
    if doc_pos == 0:
        return SectionType.PARTIES
    return None


def forward_fill(labels: list[SectionType | None]) -> list[SectionType | None]:
    """Propagate section labels onto following unlabeled chunks.

    Sections are contiguous in ``doc_pos`` order, so a chunk without its own
    label belongs to the section opened before it. Leading ``None`` entries
    stay ``None`` (LLM-fallback territory); an ``operative`` label naturally
    persists until an ``annex`` label appears.

    Parameters
    ----------
    labels:
        Per-chunk labels in ``doc_pos`` order; ``None`` marks rule abstention.

    Returns
    -------
    list[SectionType | None]
        A new list with gaps after the first label filled.
    """
    filled: list[SectionType | None] = []
    previous: SectionType | None = None
    for label in labels:
        effective = label if label is not None else previous
        filled.append(effective)
        if effective is not None:
            previous = effective
    return filled


def rule_labels_plausible(labels: list[SectionType | None]) -> bool:
    """Gate deciding whether a document's rule labels can be trusted.

    Heading structure differs across eras/formats and OCR-ingested documents
    carry garbled or spurious headings; random matches show up as violations
    of the canonical section order. Labels pass the gate only when there are
    enough of them (``GATE_MIN_RULE_HITS`` and ``GATE_MIN_COVERAGE``) and the
    orderable subsequence regresses in at most
    ``GATE_MAX_REGRESSION_RATIO`` of its transitions (real decisions do
    legitimately regress occasionally, e.g. an ``ADDRESSEES`` heading after
    the fines calculation).

    Parameters
    ----------
    labels:
        Rule-assigned per-chunk labels (pre forward-fill) in ``doc_pos``
        order.

    Returns
    -------
    bool
        ``True`` when the labels may be persisted and forward-filled;
        ``False`` when the whole document should go to the LLM fallback.
    """
    if not labels:
        return False
    assigned = [label for label in labels if label is not None]
    if len(assigned) < GATE_MIN_RULE_HITS:
        return False
    if len(assigned) / len(labels) < GATE_MIN_COVERAGE:
        return False

    ordered = [_CANONICAL_ORDER[label] for label in assigned if label in _CANONICAL_ORDER]
    transitions = list(zip(ordered, ordered[1:]))
    if not transitions:
        return True
    regressions = sum(1 for earlier, later in transitions if later < earlier)
    return regressions / len(transitions) <= GATE_MAX_REGRESSION_RATIO


class SectionClassification(BaseModel):
    """Structured section verdict for one chunk."""

    section_type: SectionType = Field(
        description="The decision section this chunk belongs to."
    )
    reason: str = Field(
        default="", description="One-sentence justification for the classification."
    )


class SectionTypeSignature(dspy.Signature):
    """Classify which structural section of an EU Commission competition
    decision a text chunk belongs to.

    Decisions follow a canonical order: parties (who the decision addresses),
    recitals_facts (facts, procedure, market and product description),
    legal_assessment (application of the competition articles to the facts),
    fines (fine calculation, remedies), operative (the numbered final
    Articles after "HAS ADOPTED THIS DECISION"), annex (appendices and
    tables). Cover pages, tables of contents and boilerplate are "other".
    Judge from the content itself; headings may be missing or OCR-garbled.
    """

    chunk_text: str = dspy.InputField(
        desc="The chunk text, possibly containing markdown headings."
    )
    neighbor_context: str = dspy.InputField(
        desc="Section labels or headings of adjacent chunks in document order; may be empty."
    )
    output: SectionClassification = dspy.OutputField(
        desc="Structured section classification with a short justification."
    )


class SectionClassifierModule(dspy.Module):
    """LLM fallback classifying chunks the rule layer abstained on.

    The predictor is injectable (Provisioner pattern) so tests never touch a
    live model; the batch entrypoint configures the DSPy LM globally.

    Parameters
    ----------
    predictor:
        Optional DSPy-compatible callable; defaults to
        ``dspy.Predict(SectionTypeSignature)``.
    """

    def __init__(self, predictor: Any | None = None) -> None:
        super().__init__()
        self.classify = predictor or dspy.Predict(SectionTypeSignature)

    def forward(self, chunk_text: str, neighbor_context: str = "") -> dspy.Prediction:
        """Classify one chunk's decision section with the configured LM.

        Parameters
        ----------
        chunk_text:
            The chunk text to classify.
        neighbor_context:
            Optional labels/headings of adjacent chunks for disambiguation.

        Returns
        -------
        dspy.Prediction
            Prediction whose ``output`` is a validated
            ``SectionClassification``.

        Raises
        ------
        pydantic.ValidationError
            If the LM output cannot be validated into
            ``SectionClassification``.
        """
        prediction = self.classify(
            chunk_text=chunk_text, neighbor_context=neighbor_context
        )
        output = getattr(prediction, "output", prediction)
        validated = SectionClassification.model_validate(output)
        return dspy.Prediction(output=validated)
