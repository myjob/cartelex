"""Detect which ingested documents are in the wrong language.

A document loaded from its original-language PDF instead of the English
translation is invisible to the English fact extractors and is mis-stemmed on
the lexical retrieval leg (``document_tsv`` is hardcoded to the ``'english'``
FTS configuration). This module scores chunks and aggregates a per-document
verdict so the operator can re-load the correct source.

Strategy
--------
1. Per chunk, ``langdetect`` yields a language and a probability. Chunks that
   are too short, too low-confidence, or featureless (markdown tables, bare
   citations) are recorded as unscorable and excluded from the evidence base
   entirely -- counting them as English dilutes the share, counting them as
   foreign inflates it.
2. Per document, the non-expected-language share is computed **weighted by
   characters, not by chunk count**. Detector misfires cluster in short chunks,
   so char-weighting is what separates a wrong-language load from an English
   decision quoting foreign material: the FRUBO decision is 51.6% of chunks but
   only 35.5% of characters.
3. A two-threshold ladder turns the share into a verdict, and the per-document
   descriptor languages provide an independent cross-check.

Why no LLM
----------
The observed per-document distribution is sharply bimodal -- one document at
0.992, the worst false positive at 0.355, then a tail under 0.17 -- so a
threshold separates the classes cleanly. The residual grey band is one or two
documents corpus-wide, and its failure mode (a genuinely English decision
quoting foreign-language exhibits verbatim) is exactly what char-weighting plus
the descriptor cross-check already encode; an LLM would have to be told the same
rule, at the cost of non-determinism. Revisit only if a future corpus pushes the
review band past roughly 20 documents per run.

Thresholds live in ``settings`` (``LANGUAGE_AUDIT_*``). Detection is pinned to
``DetectorFactory.seed = 0``: langdetect is non-deterministic without it, and a
re-run of an audit must reproduce its verdicts.
"""

import logging
from collections import defaultdict
from enum import StrEnum

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException
from pydantic import BaseModel, Field

from config.settings import settings

logger = logging.getLogger(__name__)

# Pinned once, at import: langdetect's inference is stochastic otherwise, and
# audit verdicts must be reproducible across runs.
DetectorFactory.seed = 0

# How many of the largest foreign-language chunks to name per document, so the
# operator can jump straight to the offending text.
WORST_POSITION_COUNT = 3

# How many languages the per-document tally reports.
TOP_LANGUAGE_COUNT = 3


class ObservationStatus(StrEnum):
    """Whether a chunk yielded usable language evidence, and if not, why not."""

    DETECTED = "detected"
    TOO_SHORT = "too_short"
    LOW_CONFIDENCE = "low_confidence"
    NO_FEATURES = "no_features"


class LanguageVerdict(StrEnum):
    """Per-document audit outcome."""

    ENGLISH = "english"
    REVIEW = "review"
    NON_ENGLISH = "non_english"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class DescriptorAgreement(StrEnum):
    """How the detected verdict relates to the document descriptor's metadata."""

    AGREES = "agrees"
    UNEXPECTED_NON_ENGLISH = "unexpected_non_english"
    EXPECTED_NON_ENGLISH = "expected_non_english"
    NO_METADATA = "no_metadata"


class ChunkLanguageObservation(BaseModel):
    """The language evidence a single chunk contributes."""

    doc_pos: int = Field(description="Structural position of the chunk in the document.")
    char_count: int = Field(description="Length of the chunk text in characters.")
    language: str | None = Field(
        default=None,
        description="ISO 639-1 code of the detected language, or None when unscorable.",
    )
    probability: float = Field(
        default=0.0, description="Detector confidence in the detected language."
    )
    status: ObservationStatus = Field(
        description="Whether this chunk yielded usable evidence, and if not, why not."
    )


class LanguageTally(BaseModel):
    """Chunk and character totals for one detected language in one document."""

    language: str = Field(description="ISO 639-1 code of the detected language.")
    chunks: int = Field(description="Number of scored chunks in this language.")
    chars: int = Field(description="Total characters of scored text in this language.")


class DocumentLanguageAudit(BaseModel):
    """Aggregated language verdict for one CELEX document."""

    celex_id: str = Field(description="The audited document identifier.")
    verdict: LanguageVerdict = Field(description="The audit outcome for the document.")
    non_english_char_share: float = Field(
        description="Share of scored characters not in the expected language; drives the verdict."
    )
    non_english_chunk_share: float = Field(
        description="Share of scored chunks not in the expected language; reported only."
    )
    chunks_total: int = Field(description="Chunks the document has in the chunk table.")
    chunks_scored: int = Field(description="Chunks that yielded usable evidence.")
    chunks_too_short: int = Field(
        description="Chunks below the minimum length for reliable detection."
    )
    chunks_low_confidence: int = Field(
        description="Chunks whose best detector verdict fell below the confidence floor."
    )
    chunks_no_features: int = Field(
        description="Chunks the detector could not read at all (tables, pure numerals)."
    )
    chars_scored: int = Field(description="Total characters of scored text.")
    dominant_non_english: str | None = Field(
        default=None,
        description="Non-expected language with the most scored characters; the reload target's language.",
    )
    top_languages: list[LanguageTally] = Field(
        default_factory=list,
        description="Highest-volume detected languages, by scored characters descending.",
    )
    language_init: str | None = Field(
        default=None, description="Original language recorded in the document descriptor."
    )
    language_translated: str | None = Field(
        default=None,
        description="Translation language recorded in the document descriptor.",
    )
    descriptor_agreement: DescriptorAgreement = Field(
        description="How the detected verdict relates to the descriptor's metadata."
    )
    worst_doc_positions: list[int] = Field(
        default_factory=list,
        description="Positions of the largest non-expected-language chunks, for triage.",
    )


def detect_chunk_language(document: str, doc_pos: int) -> ChunkLanguageObservation:
    """Detect the language of one chunk of text.

    The only caller of ``langdetect`` in the codebase. Chunks shorter than
    ``settings.LANGUAGE_AUDIT_MIN_CHARS`` short-circuit without invoking the
    detector at all -- below that length its verdicts are noise, and skipping
    the call is also the bulk of the scan's speed.

    Parameters
    ----------
    document:
        The chunk text to classify.
    doc_pos:
        Structural position of the chunk, carried through for triage.

    Returns
    -------
    ChunkLanguageObservation
        The evidence this chunk contributes. A non-``DETECTED`` status carries
        ``language=None`` and is excluded from the document's evidence base.
    """
    char_count = len(document)
    if char_count < settings.LANGUAGE_AUDIT_MIN_CHARS:
        return ChunkLanguageObservation(
            doc_pos=doc_pos, char_count=char_count, status=ObservationStatus.TOO_SHORT
        )

    try:
        best = detect_langs(document)[0]
    except (LangDetectException, IndexError):
        return ChunkLanguageObservation(
            doc_pos=doc_pos, char_count=char_count, status=ObservationStatus.NO_FEATURES
        )

    if best.prob < settings.LANGUAGE_AUDIT_MIN_CONFIDENCE:
        return ChunkLanguageObservation(
            doc_pos=doc_pos,
            char_count=char_count,
            language=best.lang,
            probability=best.prob,
            status=ObservationStatus.LOW_CONFIDENCE,
        )

    return ChunkLanguageObservation(
        doc_pos=doc_pos,
        char_count=char_count,
        language=best.lang,
        probability=best.prob,
        status=ObservationStatus.DETECTED,
    )


def _classify_descriptor_agreement(
    verdict: LanguageVerdict,
    language_init: str | None,
    language_translated: str | None,
) -> DescriptorAgreement:
    """Relate a detected verdict to the descriptor's recorded languages.

    Parameters
    ----------
    verdict:
        The document's detected verdict.
    language_init:
        Original language from the descriptor, normalized (``None`` if absent).
    language_translated:
        Translation language from the descriptor, normalized.

    Returns
    -------
    DescriptorAgreement
        ``EXPECTED_NON_ENGLISH`` when the descriptor already explains the
        finding, ``UNEXPECTED_NON_ENGLISH`` when it contradicts it (the
        actionable anomaly), ``NO_METADATA`` when the descriptor is silent.
    """
    expected = settings.LANGUAGE_AUDIT_EXPECTED_LANGUAGE
    if language_init is None and language_translated is None:
        return DescriptorAgreement.NO_METADATA

    flagged = verdict in (LanguageVerdict.NON_ENGLISH, LanguageVerdict.REVIEW)
    if not flagged:
        return DescriptorAgreement.AGREES

    # A non-English original with no English translation on record explains the
    # finding; anything else means the descriptor and the stored text disagree.
    if language_init not in (None, expected) and language_translated != expected:
        return DescriptorAgreement.EXPECTED_NON_ENGLISH
    return DescriptorAgreement.UNEXPECTED_NON_ENGLISH


def aggregate_document_language(
    celex_id: str,
    observations: list[ChunkLanguageObservation],
    language_init: str | None = None,
    language_translated: str | None = None,
) -> DocumentLanguageAudit:
    """Aggregate per-chunk observations into one document verdict.

    Unscorable chunks are excluded from both the numerator and the denominator
    of the shares and reported as separate counters, so evidence quality stays
    visible rather than being silently folded into either class.

    Parameters
    ----------
    celex_id:
        The document being audited.
    observations:
        Per-chunk evidence, in any order.
    language_init:
        Original language from the document descriptor, if known.
    language_translated:
        Translation language from the document descriptor, if known.

    Returns
    -------
    DocumentLanguageAudit
        The document's verdict, shares, evidence counters and triage hints.
    """
    expected = settings.LANGUAGE_AUDIT_EXPECTED_LANGUAGE

    scored = [o for o in observations if o.status is ObservationStatus.DETECTED]
    foreign = [o for o in scored if o.language != expected]

    chars_scored = sum(o.char_count for o in scored)
    foreign_chars = sum(o.char_count for o in foreign)

    char_share = foreign_chars / chars_scored if chars_scored else 0.0
    chunk_share = len(foreign) / len(scored) if scored else 0.0

    chunk_tally: dict[str, int] = defaultdict(int)
    char_tally: dict[str, int] = defaultdict(int)
    for observation in scored:
        language = observation.language or ""
        chunk_tally[language] += 1
        char_tally[language] += observation.char_count

    # Deterministic ordering: characters descending, then language code, so a
    # re-run of the audit produces a byte-identical report.
    ranked = sorted(char_tally.items(), key=lambda item: (-item[1], item[0]))
    top_languages = [
        LanguageTally(language=language, chunks=chunk_tally[language], chars=chars)
        for language, chars in ranked[:TOP_LANGUAGE_COUNT]
    ]
    dominant_non_english = next(
        (language for language, _ in ranked if language != expected), None
    )
    worst_doc_positions = [
        o.doc_pos
        for o in sorted(foreign, key=lambda o: (-o.char_count, o.doc_pos))[
            :WORST_POSITION_COUNT
        ]
    ]

    if chars_scored < settings.LANGUAGE_AUDIT_MIN_SCORED_CHARS:
        verdict = LanguageVerdict.INSUFFICIENT_EVIDENCE
    elif char_share >= settings.LANGUAGE_AUDIT_RELOAD_SHARE:
        verdict = LanguageVerdict.NON_ENGLISH
    elif char_share >= settings.LANGUAGE_AUDIT_REVIEW_SHARE:
        verdict = LanguageVerdict.REVIEW
    else:
        verdict = LanguageVerdict.ENGLISH

    return DocumentLanguageAudit(
        celex_id=celex_id,
        verdict=verdict,
        non_english_char_share=char_share,
        non_english_chunk_share=chunk_share,
        chunks_total=len(observations),
        chunks_scored=len(scored),
        chunks_too_short=sum(
            1 for o in observations if o.status is ObservationStatus.TOO_SHORT
        ),
        chunks_low_confidence=sum(
            1 for o in observations if o.status is ObservationStatus.LOW_CONFIDENCE
        ),
        chunks_no_features=sum(
            1 for o in observations if o.status is ObservationStatus.NO_FEATURES
        ),
        chars_scored=chars_scored,
        dominant_non_english=dominant_non_english,
        top_languages=top_languages,
        language_init=language_init,
        language_translated=language_translated,
        descriptor_agreement=_classify_descriptor_agreement(
            verdict, language_init, language_translated
        ),
        worst_doc_positions=worst_doc_positions,
    )


def expected_languages_from_descriptors(
    payload: dict,
) -> dict[str, tuple[str | None, str | None]]:
    """Extract the per-document expected languages from a descriptor payload.

    Takes an already-parsed dict rather than a path so this module stays free of
    file I/O and testable without fixtures on disk. The descriptors encode
    "no translation" as the literal string ``"NA"``, which is normalized to
    ``None`` along with empty strings and absent keys.

    Parameters
    ----------
    payload:
        The parsed descriptor JSON, either the top-level object with a
        ``documents`` key or the document map itself.

    Returns
    -------
    dict[str, tuple[str | None, str | None]]
        Maps celex_id to ``(language_init, language_translated)``, lowercased.
    """
    documents = payload.get("documents", payload)
    expected: dict[str, tuple[str | None, str | None]] = {}
    for celex_id, descriptor in documents.items():
        if not isinstance(descriptor, dict):
            continue
        expected[celex_id] = (
            _normalize_language(descriptor.get("language_init")),
            _normalize_language(descriptor.get("language_translated")),
        )
    return expected


def _normalize_language(value: object) -> str | None:
    """Normalize a descriptor language field to a lowercase code or ``None``.

    Parameters
    ----------
    value:
        The raw descriptor value; ``"NA"``, empty and non-string values all mean
        "not recorded".

    Returns
    -------
    str | None
        The lowercased language code, or ``None`` when nothing is recorded.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized or normalized == "na":
        return None
    return normalized
