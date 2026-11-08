"""Highest recital number of a decision, from its line-start numbering.

Pure and DB-free: takes decision text, returns a :class:`RecitalCount`.

A recital is a paragraph that starts on its own line with ``(n)`` (modern
decisions) or, as a fallback for older decisions that have no bracketed
numbering, ``n.``. Numbers anywhere else -- cross-references such as "recital
(7) above", ``Article 7(1)``, footnote markers, years -- are never candidates.

The candidates, in document order, still contain noise: sub-lists that restart
at (1) inside a recital, OCR outliers, chunk-overlap repeats. The recital max is
the last number of the *longest chain* of candidates whose every step is
``+1 .. +max_gap`` and which starts at or below ``max_start``. Noise cannot
extend that chain, so it drops out without special rules. Coverage (chain
length / max) grades how much of the numbering was actually seen; numbers
left above the max (``beyond_max``) reveal a chain cut short by a wide gap.
"""

import logging
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from pipelines.legal.section_classifier import OPERATIVE_MARKER_RE

logger = logging.getLogger(__name__)

REASON_NO_NUMBERING = "no line-start numbering"
REASON_CHAIN_TOO_SHORT = "chain too short"
REASON_LOW_COVERAGE = "low coverage"
REASON_CHUNK_GAP = "chunk gap after last recital"
REASON_NUMBERS_BEYOND_MAX = "numbers beyond max"
REASON_NUMBERING_RESTARTS = "numbering restarts"
REASON_OPERATIVE_NOT_SEEN = "operative part not seen"

_COVERAGE_DECIMALS = 3

# Horizontal whitespace is "[^\S\n]" so MinerU's no-break and narrow no-break
# spaces count. Optional list / quote marker, then "(n)" with tolerated inner
# spaces, then
# whitespace, a letter, or end of line (a bare "(452)" line followed by its
# text counts).
# A letter may follow directly: MinerU drops the space after 4-digit numbers
# ("(1000)After the creation ..."); a digit may not ("(12)3" is no marker).
_AFTER_BRACKET = r"(?=[^\S\n]|$|[^\W\d_])"
_BRACKET_RE = re.compile(
    r"^[^\S\n]*(?:[>\-*][^\S\n]*)?\([^\S\n]*(\d{1,4})[^\S\n]*\)" + _AFTER_BRACKET, re.M
)
# "n." followed by text; header ("#") and table ("|") lines never match
# because the line must start with the digits.
_DOT_RE = re.compile(r"^[^\S\n]*(\d{1,4})\.(?=[^\S\n]+\S)", re.M)


class RecitalNumbering(StrEnum):
    """Which numbering style produced the recital chain."""

    BRACKET = "bracket"
    DOT = "dot"
    NONE = "none"


class RecitalStatus(StrEnum):
    """How far an analyst can trust ``recitals_max``."""

    CONFIDENT = "confident"
    DOUBTFUL = "doubtful"
    UNDETERMINED = "undetermined"


class RecitalThresholds(BaseModel):
    """Tuning knobs for chain building and status grading."""

    model_config = ConfigDict(frozen=True)

    max_gap: int = Field(ge=1, description="Largest allowed step between consecutive chain numbers")
    max_start: int = Field(ge=1, description="A chain must start at or below this number")
    min_chain: int = Field(ge=1, description="Chains shorter than this are undetermined")
    min_coverage: float = Field(ge=0, le=1, description="Coverage below this is undetermined")
    min_coverage_confident: float = Field(
        ge=0, le=1, description="Coverage at or above this is confident; between the bars doubtful"
    )
    restart_ratio: float = Field(
        default=settings.RECITAL_RESTART_RATIO,
        gt=0,
        le=1,
        description="A second chain at least this share of the main one means the numbering restarts",
    )

    @classmethod
    def from_settings(cls) -> "RecitalThresholds":
        """Build the thresholds from ``settings.RECITAL_*``.

        Returns:
            The configured thresholds.
        """
        return cls(
            max_gap=settings.RECITAL_MAX_GAP,
            max_start=settings.RECITAL_MAX_START,
            min_chain=settings.RECITAL_MIN_CHAIN,
            min_coverage=settings.RECITAL_MIN_COVERAGE,
            min_coverage_confident=settings.RECITAL_MIN_COVERAGE_CONFIDENT,
            restart_ratio=settings.RECITAL_RESTART_RATIO,
        )


class RecitalCandidate(BaseModel):
    """A line-start number found in the text."""

    model_config = ConfigDict(frozen=True)

    number: int = Field(description="The recital number as printed")
    line_no: int = Field(description="0-based line index of the number in the text")


class RecitalCount(BaseModel):
    """The ``recitals_max`` fact value with its evidence and provenance."""

    recitals_max: int | None = Field(
        default=None, description="Highest recital number; null when undetermined"
    )
    recitals_found: int = Field(default=0, description="Length of the recital chain")
    first_recital: int | None = Field(
        default=None, description="First number of the chain (1 unless MinerU dropped it)"
    )
    coverage: float = Field(default=0.0, description="recitals_found / last chain number")
    gaps: int = Field(default=0, description="Chain steps larger than one")
    beyond_max: int = Field(
        default=0,
        description="Line-start numbers of the same style above recitals_max (truncated-chain evidence)",
    )
    numbering: RecitalNumbering = Field(
        default=RecitalNumbering.NONE, description="Numbering style of the chain"
    )
    status: RecitalStatus = Field(
        default=RecitalStatus.UNDETERMINED, description="Trust grade of recitals_max"
    )
    reason: str = Field(default="", description="Why the status is not confident; empty otherwise")
    text_source: str = Field(default="", description="'markdown' or 'chunks'; empty when no text")
    source_path: str = Field(default="", description="Markdown file read, empty for chunks")
    chunk_gaps: int = Field(default=0, description="Missing doc_pos positions when read from chunks")


def find_candidates(text: str, numbering: RecitalNumbering) -> list[RecitalCandidate]:
    """List the line-start numbers of one numbering style in document order.

    Args:
        text: Decision text (markdown).
        numbering: ``BRACKET`` for ``(n)`` or ``DOT`` for ``n.``.

    Returns:
        The candidates in document order; empty for ``NONE``.
    """
    match numbering:
        case RecitalNumbering.BRACKET:
            pattern = _BRACKET_RE
        case RecitalNumbering.DOT:
            pattern = _DOT_RE
        case _:
            return []
    return [
        RecitalCandidate(number=int(m.group(1)), line_no=text.count("\n", 0, m.start()))
        for m in pattern.finditer(text)
    ]


def longest_chain(
    candidates: list[RecitalCandidate], max_gap: int, max_start: int
) -> list[RecitalCandidate]:
    """Find the longest admissible recital chain.

    A chain is a document-order subsequence whose steps are each ``+1 ..
    +max_gap`` and whose first number is at most ``max_start``. On equal length
    the chain ending on the higher number wins.

    Args:
        candidates: Candidates in document order.
        max_gap: Largest allowed step.
        max_start: Highest admissible first number.

    Returns:
        The chain in document order; empty when no candidate can start one.
    """
    # best[v] = (length, index) of the longest chain seen so far ending at value v
    best: dict[int, tuple[int, int]] = {}
    pred: list[int | None] = [None] * len(candidates)
    length: list[int] = [0] * len(candidates)
    for i, cand in enumerate(candidates):
        v = cand.number
        prev = max(
            (best[v - d] for d in range(1, max_gap + 1) if v - d in best),
            default=None,
        )
        if prev is not None:
            length[i], pred[i] = prev[0] + 1, prev[1]
        elif v <= max_start:
            length[i] = 1
        else:
            continue
        if v not in best or length[i] > best[v][0]:
            best[v] = (length[i], i)

    if not best:
        return []
    _, end = max(
        ((length[i], candidates[i].number), i) for i in range(len(candidates)) if length[i]
    )
    chain: list[RecitalCandidate] = []
    cursor: int | None = end
    while cursor is not None:
        chain.append(candidates[cursor])
        cursor = pred[cursor]
    return chain[::-1]


def _grade(
    candidates: list[RecitalCandidate],
    numbering: RecitalNumbering,
    thresholds: RecitalThresholds,
) -> RecitalCount:
    """Build and grade the chain of one numbering style.

    Args:
        candidates: Candidates of that style in document order.
        numbering: The style, recorded on the result.
        thresholds: Chain and grading thresholds.

    Returns:
        The graded result; ``recitals_max`` is None when undetermined.
    """
    chain = longest_chain(candidates, thresholds.max_gap, thresholds.max_start)
    if not chain:
        return RecitalCount(numbering=numbering, reason=REASON_CHAIN_TOO_SHORT)
    last = chain[-1].number
    found = len(chain)
    coverage = round(found / last, _COVERAGE_DECIMALS)
    gaps = sum(1 for a, b in zip(chain, chain[1:]) if b.number - a.number > 1)
    beyond = sum(1 for c in candidates if c.number > last)
    result = RecitalCount(
        recitals_found=found,
        first_recital=chain[0].number,
        coverage=coverage,
        gaps=gaps,
        beyond_max=beyond,
        numbering=numbering,
    )
    if found < thresholds.min_chain:
        return result.model_copy(update={"reason": REASON_CHAIN_TOO_SHORT})
    if coverage < thresholds.min_coverage:
        return result.model_copy(update={"reason": REASON_LOW_COVERAGE})
    status = (
        RecitalStatus.CONFIDENT
        if coverage >= thresholds.min_coverage_confident
        else RecitalStatus.DOUBTFUL
    )
    reason = "" if status is RecitalStatus.CONFIDENT else REASON_LOW_COVERAGE
    # A run's worth of numbers above the max means the chain was cut short
    # (a gap wider than max_gap), so the max is a lower bound, not the max.
    if beyond >= thresholds.min_chain:
        status, reason = RecitalStatus.DOUBTFUL, REASON_NUMBERS_BEYOND_MAX
    # A second chain of comparable length means the numbering restarts (older
    # decisions number "1." afresh under every heading), so no single max
    # exists. Sub-lists inside recitals are far shorter than the main chain.
    elif _restarts(candidates, chain, thresholds):
        status, reason = RecitalStatus.DOUBTFUL, REASON_NUMBERING_RESTARTS
    return result.model_copy(update={"recitals_max": last, "status": status, "reason": reason})


def _restarts(
    candidates: list[RecitalCandidate],
    chain: list[RecitalCandidate],
    thresholds: RecitalThresholds,
) -> bool:
    """Tell whether the leftover candidates form a second comparable chain.

    Args:
        candidates: All candidates of the style in document order.
        chain: The main chain built from them.
        thresholds: Supplies ``min_chain`` and ``restart_ratio``.

    Returns:
        True when the longest chain among the leftovers is at least
        ``min_chain`` long and at least ``restart_ratio`` of the main chain.
    """
    used = {c.line_no for c in chain}
    leftover = [c for c in candidates if c.line_no not in used]
    second = longest_chain(leftover, thresholds.max_gap, thresholds.max_start)
    return len(second) >= max(thresholds.min_chain, thresholds.restart_ratio * len(chain))


def recital_part(text: str) -> str:
    """Cut the text at the operative part, where recitals end by definition.

    After "HAS ADOPTED THIS DECISION" (or an older "HAS ISSUED / MADE THE
    FOLLOWING DECISION") come the articles, annexes and -- in
    MinerU output -- footnotes rendered as line-start ``(n)``, which would
    otherwise extend the chain past the last recital (33708_6_7: recitals end
    at 218, footnotes reach 432). The *last* marker is used so a recital
    quoting an earlier decision's marker does not cut the text short.

    Args:
        text: Decision text (markdown).

    Returns:
        The text before the last operative marker, or the whole text when the
        decision has none.
    """
    starts = [m.start() for m in OPERATIVE_MARKER_RE.finditer(text)]
    return text[: max(starts)] if starts else text


def count_recitals(text: str, thresholds: RecitalThresholds) -> RecitalCount:
    """Determine the highest recital number of a decision text.

    Only the text before the operative part counts (:func:`recital_part`).
    Both styles are graded; of those not undetermined the longer chain wins
    (a short "(1)".."(10)" list of parties must not beat the dot-numbered
    recitals of an older decision), bracket on a tie. Provenance fields (``text_source``,
    ``source_path``, ``chunk_gaps``) are left empty for the caller to fill.

    Args:
        text: Decision text (markdown).
        thresholds: Chain and grading thresholds.

    Returns:
        The graded recital count.
    """
    text = recital_part(text)
    bracket_cands = find_candidates(text, RecitalNumbering.BRACKET)
    bracket = _grade(bracket_cands, RecitalNumbering.BRACKET, thresholds)
    dot_cands = find_candidates(text, RecitalNumbering.DOT)
    dot = _grade(dot_cands, RecitalNumbering.DOT, thresholds)
    graded = [r for r in (bracket, dot) if r.status is not RecitalStatus.UNDETERMINED]
    if graded:
        return max(graded, key=lambda r: r.recitals_found)
    if bracket_cands:
        return bracket
    if dot_cands:
        return dot
    return RecitalCount(reason=REASON_NO_NUMBERING)


def line_start_marker(number: int, numbering: RecitalNumbering) -> re.Pattern[str]:
    """Pattern matching recital ``number`` at a line start in one style.

    Args:
        number: The recital number to locate.
        numbering: ``BRACKET`` or ``DOT``.

    Returns:
        A multiline pattern; for ``NONE`` one that never matches.
    """
    match numbering:
        case RecitalNumbering.BRACKET:
            return re.compile(
                rf"^[^\S\n]*(?:[>\-*][^\S\n]*)?\([^\S\n]*{number}[^\S\n]*\)" + _AFTER_BRACKET, re.M
            )
        case RecitalNumbering.DOT:
            return re.compile(rf"^[^\S\n]*{number}\.(?=[^\S\n]+\S)", re.M)
        case _:
            return re.compile(r"(?!x)x")
