"""Chapter layout of the case summary and its Markdown rendering (pure, DB-free).

Every chapter names the facts it is built from, so a reader can see where each
part of a summary comes from and how well it is verified (ISSUES #355, review
A2). Three kinds of chapter:

- ``verbatim``: the stored ``value_md`` of its source facts, copied. Chapters the
  old signature told the LLM to "take verbatim" (cartel period, fines table,
  commissioner, ...) are copied by code instead.
- ``generated``: text the LLM writes from its named source facts.
- ``synthesis``: text the LLM writes from all the facts, with no source fact of
  its own (``overview``, ``outcome``); labelled as such.

Each chapter ends with a source line listing every source fact with its
``verification_status`` (``not stored`` when absent) and, for several
sources, the weakest of them. A ``References`` chapter lists the chunk that
best supports each source fact.

Exports:
    ChapterKind, ChapterSpec, CHAPTERS -- the layout.
    StoredFact, ChunkReference -- the inputs the renderer reads.
    chapter_status -- the weakest status of a chapter's stored sources.
    source_facts -- every source fact name, in chapter order.
    render_summary -- the Markdown summary.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from pipelines.agent_dspy.evidence_verifier_module import VerificationStatus, weakest_status

NA = "NA"
_NOT_STORED = "not stored"
_SYNTHESIS_NOTE = "*Generated from the facts of the other chapters; no source fact of its own.*"


class ChapterKind(StrEnum):
    """How the text of a chapter is produced."""

    VERBATIM = "verbatim"
    GENERATED = "generated"
    SYNTHESIS = "synthesis"


class ChapterSpec(BaseModel):
    """One chapter of the summary and the facts it is built from."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="Chapter key; for LLM chapters the CaseSummaryChapters field name.")
    kind: ChapterKind = Field(description="How the chapter text is produced.")
    sources: tuple[str, ...] = Field(default=(), description="Source fact names, in display order.")

    @property
    def title(self) -> str:
        """Return the heading, e.g. ``key_timeline`` -> ``Key Timeline``."""
        return " ".join(word.capitalize() for word in self.key.split("_"))


def _verbatim(key: str, *sources: str) -> ChapterSpec:
    return ChapterSpec(key=key, kind=ChapterKind.VERBATIM, sources=sources or (key,))


def _generated(key: str, *sources: str) -> ChapterSpec:
    return ChapterSpec(key=key, kind=ChapterKind.GENERATED, sources=sources)


# Display order is the order of the summary before #355. The source names must
# stay in step with the context_facts of case_summary in FACT_EXTRACTOR.yaml
# (asserted by the tests).
CHAPTERS: tuple[ChapterSpec, ...] = (
    _verbatim("celex_case_id"),
    _generated("case_title", "case_title"),
    ChapterSpec(key="overview", kind=ChapterKind.SYNTHESIS),
    _generated("industry", "companies_industry", "products"),
    _generated("oecd_nace", "OECD_sector", "NACE_group_code", "NACE_class_code"),
    _generated("companies_undertakings", "companies_investigated", "companies_country"),
    _generated("market", "market_geographical", "market_outside_eu", "market_oligopoly", "market_turbulence"),
    _verbatim("cartel_period"),
    _generated("key_timeline", "heideltime_event_agg", "eu_commission_decision_date"),
    _generated("legal_framework", "law_article_operative", "law_article_all_relevant", "infringement_concept"),
    _generated(
        "infringement_category",
        "infringement_category_procedural", "infringement_category_member_states",
        "infringement_category_abuse_of_dominance", "infringement_category_hor_vert_lic_joint",
    ),
    _generated(
        "infringement_sub_category",
        "infringement_subcategory_horizontal_restraints", "infringement_subcategory_vertical_restraints",
    ),
    _verbatim("infringement_gravity"),
    _generated("exemption", "exemption_case", "exemption_applicants"),
    _generated("leniency", "leniency_case", "leniency_applicants", "leniency_granted", "leniency_rejected"),
    _verbatim("fines"),
    _generated("remedies", "decision_remedies"),
    _verbatim("company_recidivism"),
    _verbatim("case_commission_info_request"),
    _verbatim("case_commission_no_action"),
    _verbatim("case_commission_infringement"),
    _verbatim("case_commission_decision"),
    _verbatim("commissioner", "case_commissioner"),
    _generated("courts", "is_court_involved", "court_list"),
    _generated(
        "book_reference",
        "case_is_in_book", "case_is_landmark_in_book", "case_infringement_type_book", "case_infringement_book",
    ),
    ChapterSpec(key="outcome", kind=ChapterKind.SYNTHESIS),
)


class StoredFact(BaseModel):
    """A stored input fact as the renderer needs it."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Fact name.")
    value_md: str = Field(description="Stored Markdown value.")
    verification_status: str | None = Field(default=None, description="facts.verification_status; None if never verified.")


class ChunkReference(BaseModel):
    """The chunk that best supports one source fact."""

    model_config = ConfigDict(frozen=True)

    fact_name: str = Field(description="The source fact.")
    chunk_id: str = Field(description="pg_embedding_qwen chunk id.")
    doc_pos: int | None = Field(default=None, description="Chunk position in the decision, when known.")


def source_facts() -> list[str]:
    """Return every source fact name of the layout, in chapter order, once each.

    Returns:
        The fact names the chapters are built from.
    """
    names: list[str] = []
    for spec in CHAPTERS:
        names.extend(name for name in spec.sources if name not in names)
    return names


def chapter_status(spec: ChapterSpec, stored: Mapping[str, StoredFact]) -> VerificationStatus | None:
    """Return the weakest verification status of a chapter's stored sources.

    Args:
        spec: The chapter.
        stored: The stored, non-empty input facts by name.

    Returns:
        The weakest status (see ``weakest_status``); None when the chapter has
        no source fact or none of them is stored.
    """
    present = [stored[name] for name in spec.sources if name in stored]
    if not present:
        return None
    # One source: its own verdict is more telling than the combined one
    # (an unverified fact stays "unverified" instead of "weak_support").
    if len(present) == 1 and present[0].verification_status in VerificationStatus:
        return VerificationStatus(present[0].verification_status)
    return weakest_status(fact.verification_status for fact in present)


def _source_line(spec: ChapterSpec, stored: Mapping[str, StoredFact]) -> str:
    """Return the italic source line closing a chapter."""
    if spec.kind is ChapterKind.SYNTHESIS:
        return _SYNTHESIS_NOTE
    parts = []
    for name in spec.sources:
        fact = stored.get(name)
        label = _NOT_STORED if fact is None else (fact.verification_status or VerificationStatus.UNVERIFIED.value)
        parts.append(f"{name} ({label})")
    line = f"*Sources: {', '.join(parts)}"
    status = chapter_status(spec, stored)
    if len(spec.sources) > 1 and status is not None:
        line += f". Weakest: {status.value}"
    return line + "*"


def _body(spec: ChapterSpec, generated: Mapping[str, str], stored: Mapping[str, StoredFact]) -> str:
    """Return the chapter text: copied facts or LLM text, ``NA`` when empty."""
    if spec.kind is ChapterKind.VERBATIM:
        values = [stored[name].value_md.strip() for name in spec.sources if name in stored]
        return "\n\n".join(values) or NA
    return (generated.get(spec.key) or "").strip() or NA


def _references(references: Sequence[ChunkReference]) -> str:
    """Return the References chapter body."""
    if not references:
        return NA
    lines = []
    for ref in references:
        where = f"chunk {ref.doc_pos}" if ref.doc_pos is not None else "chunk"
        lines.append(f"- {ref.fact_name}: {where} (`{ref.chunk_id}`)")
    return "\n".join(lines)


def render_summary(
    generated: Mapping[str, str],
    stored: Mapping[str, StoredFact],
    references: Sequence[ChunkReference] = (),
) -> str:
    """Render the case summary as Markdown, one ``###`` chapter per spec.

    Args:
        generated: LLM text by chapter key (generated and synthesis chapters).
        stored: The stored, non-empty input facts by name.
        references: The supporting chunk of each source fact, in display order.

    Returns:
        The summary; no timestamp, so an unchanged input renders identically.
    """
    chapters = [
        f"### {spec.title}\n{_body(spec, generated, stored)}\n\n{_source_line(spec, stored)}\n"
        for spec in CHAPTERS
    ]
    chapters.append(f"### References\n{_references(references)}\n")
    return "\n".join(chapters)
