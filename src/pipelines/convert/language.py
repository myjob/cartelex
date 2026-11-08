"""Language detection from converted Markdown and the registry write decision.

Reuses the seeded detector in ``pipelines.legal.language_audit``; no second
detector is introduced. Markdown is split into paragraph blocks, non-prose
blocks (images, tables, bare headings) are dropped, and the rest is scored as
if each block were a chunk.
"""

import logging
import re

from pydantic import BaseModel, Field

from pipelines.convert.models import LanguageAction
from pipelines.legal.language_audit import (
    LanguageVerdict,
    aggregate_document_language,
    detect_chunk_language,
)

logger = logging.getLogger(__name__)

_BLOCK_SPLIT = re.compile(r"\n\s*\n")
_IMAGE_ONLY = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
_HEADING_ONLY = re.compile(r"^#{1,6}\s")
_NA_VALUE = "NA"


class LanguageDecision(BaseModel):
    """Outcome of comparing the detected language with the stored one."""

    detected: str | None = Field(description="Detected ISO 639-1 code, or None if undetermined.")
    confidence: float | None = Field(
        description="Share of scored characters in the detected language, or None."
    )
    action: LanguageAction = Field(description="What the conversion does with the language.")
    registry_value: str | None = Field(
        description="Upper-case code to write to the registry; None unless action is set."
    )
    master_value: str | None = Field(
        description="Lower-case code to write to the master JSON; None unless action is set."
    )


def _is_prose_block(block: str) -> bool:
    """Return True when a Markdown block is prose worth language scoring.

    Args:
        block: A stripped paragraph block.

    Returns:
        False for empty blocks, image links, tables and single heading lines.
    """
    if not block:
        return False
    if _IMAGE_ONLY.match(block) or block.startswith(("|", "<table")):
        return False
    return not (_HEADING_ONLY.match(block) and "\n" not in block)


def detect_markdown_language(markdown: str, document_id: str) -> tuple[str | None, float | None]:
    """Detect the dominant language of a Markdown document.

    Args:
        markdown: Converted Markdown text.
        document_id: Identifier used for the audit aggregate (logging only).

    Returns:
        ``(language, confidence)`` where confidence is the top language's
        character share of scored characters; ``(None, None)`` when the
        evidence is insufficient.
    """
    blocks = [b.strip() for b in _BLOCK_SPLIT.split(markdown)]
    observations = [
        detect_chunk_language(block, index)
        for index, block in enumerate(b for b in blocks if _is_prose_block(b))
    ]
    audit = aggregate_document_language(document_id, observations)
    if audit.verdict is LanguageVerdict.INSUFFICIENT_EVIDENCE or not audit.top_languages:
        logger.info("Language undetermined for %s", document_id)
        return None, None
    top = audit.top_languages[0]
    return top.language, top.chars / audit.chars_scored


def decide_language(
    detected: str | None, confidence: float | None, stored: str | None
) -> LanguageDecision:
    """Decide whether the detected language is written to registry and master.

    Args:
        detected: Detected code, or None when undetermined.
        confidence: Detection confidence, passed through.
        stored: Registry language (any case), ``"NA"`` or None.

    Returns:
        A decision; values are set only for ``LanguageAction.SET``.
    """
    if detected is None:
        return LanguageDecision(
            detected=None,
            confidence=confidence,
            action=LanguageAction.UNDETERMINED,
            registry_value=None,
            master_value=None,
        )
    stored_norm = (stored or "").strip()
    if not stored_norm or stored_norm.upper() == _NA_VALUE:
        return LanguageDecision(
            detected=detected,
            confidence=confidence,
            action=LanguageAction.SET,
            registry_value=detected.upper(),
            master_value=detected.lower(),
        )
    if stored_norm.lower() == detected.lower():
        action = LanguageAction.KEPT_EQUAL
    else:
        logger.warning("Language mismatch: stored %s, detected %s", stored_norm, detected)
        action = LanguageAction.KEPT_MISMATCH
    return LanguageDecision(
        detected=detected,
        confidence=confidence,
        action=action,
        registry_value=None,
        master_value=None,
    )
