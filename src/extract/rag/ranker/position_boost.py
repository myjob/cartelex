"""Structural ranking boosts shared by every reranker.

Both boosts encode the same domain fact — a Commission decision climaxes at its
operative articles and fine calculations — as an additive prior on top of the
cross-encoder score, dispatched by :func:`structural_boost` on
``settings.RANKER_BOOST_MODE``:

* ``section_type`` (default) — weight chunks by their classified decision
  section (:func:`section_type_boost`). The real structural prior: it survives
  documents with long annexes, where raw depth mis-fires.
* ``legal_climax`` — the pre-Phase-3 positional prior
  (:func:`legal_climax_boost`), biasing toward chunks deep in their own
  document. Kept only for A/B comparison against the section weights; delete
  this branch once the section-type mode is validated.
* ``none`` — cross-encoder score only.

Kept in one module so :class:`FlashRanker` and :class:`BgeReranker` stay in
lock-step and the tuning constants live in exactly one place.
"""

from config.settings import settings
from extract.rag.models.chunk_vo import ChunkVo, SectionType

# Depth is capped just below 1.0 so the very last chunks (signatures, notary
# stamps, service-of-process addresses) do not out-rank the actual final orders
# (Article 1, Article 2) that sit immediately above them.
LEGAL_CLIMAX_DEPTH_CAP: float = 0.98
# A soft power curve: chunks at 50-80% depth get almost nothing; only the final
# stretch spikes. Paired with a moderate weight so the boost breaks ties rather
# than overriding a perfect mid-document semantic match.
LEGAL_CLIMAX_POWER: int = 4
LEGAL_CLIMAX_WEIGHT: float = 2.0


def legal_climax_boost(doc_pos: int, local_max: int) -> float:
    """Return the additive Legal Climax boost for a chunk at *doc_pos*.

    Args:
        doc_pos: The chunk's structural position within its document.
        local_max: The largest ``doc_pos`` among the candidates being ranked,
            used as the depth denominator (relative depth within the retrieved
            slice of the document).

    Returns:
        A non-negative float to add to the chunk's base reranker score. ``0.0``
        at the start of the document, rising to
        ``LEGAL_CLIMAX_DEPTH_CAP ** LEGAL_CLIMAX_POWER * LEGAL_CLIMAX_WEIGHT`` at
        the (capped) end.
    """
    depth = doc_pos / max(1, local_max)
    capped_depth = min(depth, LEGAL_CLIMAX_DEPTH_CAP)
    return (capped_depth**LEGAL_CLIMAX_POWER) * LEGAL_CLIMAX_WEIGHT


# Additive weight per decision section. The cross-encoder score sits in (0, 1)
# and the climax boost maxed at ~1.84; these are deliberately smaller — they
# break ties in favour of the sections where holdings and amounts live without
# overriding a strong semantic match elsewhere. Unlisted sections (parties,
# recitals_facts, annex, other) and unclassified chunks get 0.0.
SECTION_TYPE_BOOSTS: dict[SectionType, float] = {
    SectionType.OPERATIVE: 0.30,
    SectionType.FINES: 0.30,
    SectionType.LEGAL_ASSESSMENT: 0.10,
}


def section_type_boost(section_type: SectionType | str | None) -> float:
    """Return the additive section-structure boost for a chunk.

    Args:
        section_type: The chunk's classified decision section; ``None`` (not
            yet classified) yields no boost.

    Returns:
        A non-negative float to add to the chunk's base reranker score.
    """
    if section_type is None:
        return 0.0
    try:
        section = SectionType(section_type)
    except ValueError:
        return 0.0
    return SECTION_TYPE_BOOSTS.get(section, 0.0)


def structural_boost(
    chunk: ChunkVo, local_max: int, mode: str | None = None
) -> float:
    """Dispatch the structural prior for *chunk* on the configured boost mode.

    Args:
        chunk: The candidate chunk (supplies ``section_type`` and ``doc_pos``).
        local_max: The largest ``doc_pos`` among the candidates being ranked
            (the ``legal_climax`` depth denominator).
        mode: Boost mode override; defaults to ``settings.RANKER_BOOST_MODE``.

    Returns:
        A non-negative float to add to the chunk's base reranker score.

    Raises:
        ValueError: If *mode* is not one of ``section_type``, ``legal_climax``
            or ``none``.
    """
    effective_mode = mode if mode is not None else settings.RANKER_BOOST_MODE
    match effective_mode:
        case "section_type":
            return section_type_boost(chunk.section_type)
        case "legal_climax":
            return legal_climax_boost(chunk.doc_pos, local_max)
        case "none":
            return 0.0
        case _:
            raise ValueError(f"Unknown RANKER_BOOST_MODE: {effective_mode!r}")
