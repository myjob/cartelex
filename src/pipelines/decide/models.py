"""Data models of the source-document decision step."""

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DocumentKind(StrEnum):
    """What a candidate document is, judged from its title page and ending."""

    DECISION = "decision"
    SUMMARY = "summary"
    LETTER = "letter"
    COMBINED = "combined"
    UNKNOWN = "unknown"


class TitleKey(BaseModel):
    """Identity of a Commission decision: case number(s), subject and date."""

    model_config = ConfigDict(frozen=True)

    case_numbers: tuple[str, ...] = Field(description="Digits of the case numbers in the title (AT.39924 -> 39924).")
    subject: str = Field(description="Lower-case, punctuation-free subject line after the case number.")
    doc_date: dt.date = Field(description="Date the decision states in its title.")


class CandidateEvidence(BaseModel):
    """Facts about one candidate document used by the rule ladder."""

    document_id: str = Field(description="Registry document id.")
    decision_id: str = Field(description="Manifest decision the candidate belongs to.")
    origin: str = Field(description="Source site (ec.europa.eu, eur-lex, db-comp, other).")
    sha256: str | None = Field(description="SHA-256 of the PDF.")
    local_file_pdf: str = Field(description="Stored PDF path.")
    local_file_md: str = Field(description="MinerU Markdown path.")
    language: str | None = Field(description="Upper-case detected language, None when undetermined.")
    kind: DocumentKind = Field(description="Document kind.")
    title_key: TitleKey | None = Field(description="Decision identity; None when no decision title was found.")
    operative: bool = Field(description="True when the ending holds the operative part.")
    md_chars: int = Field(description="Length of the Markdown in characters.")
    text_chars: int = Field(default=0, description="Length of the text with images, HTML and footnotes stripped.")
    vocabulary: frozenset[str] = Field(
        default=frozenset(), repr=False,
        description="Distinct lower-case words of six or more letters; the text identity used beside the title.")


class UndecidedDecision(BaseModel):
    """A manifest decision without a source document."""

    decision_id: str = Field(description="Registry decision id.")
    decision_date: dt.date | None = Field(description="Registry decision date.")
    case_ids: list[str] = Field(description="Case ids linked in competition_case_decision.")
    title: str = Field(default="", description="Registry title.")


class ExistingSource(BaseModel):
    """A source document already in the corpus."""

    decision_id: str = Field(description="Decision the source belongs to.")
    document_id: str = Field(description="Source document id.")
    sha256: str | None = Field(description="SHA-256 of the source PDF.")
    title_key: TitleKey | None = Field(description="Decision identity read from the source Markdown.")
    case_ids: list[str] = Field(
        default_factory=list, description="Case ids of the owning decision (competition_case_decision).")
    decision_date: dt.date | None = Field(default=None, description="Registry date of the owning decision.")
    language: str | None = Field(default=None, description="Upper-case registry language of the source row.")
    text_chars: int | None = Field(default=None, description="Stripped text length of the source's original Markdown.")
    vocabulary: frozenset[str] = Field(
        default=frozenset(), repr=False, description="Distinct words of six or more letters of the source Markdown.")


class Verdict(StrEnum):
    """What happens to a manifest decision."""

    PROMOTE = "promote"
    SPLIT = "split"
    MERGE = "merge"
    REVIEW = "review"


class Repoint(BaseModel):
    """A candidate that belongs to another, existing decision."""

    document_id: str = Field(description="Candidate document id.")
    owner_decision_id: str = Field(description="Existing decision that receives it.")
    make_source: bool = Field(
        default=False, description="True when the candidate is a better copy and becomes the owner's source.")


class Promotion(BaseModel):
    """One Commission decision made the source of a (possibly new) decision."""

    decision_id: str = Field(description="Manifest decision id, or a new id for a split.")
    is_new: bool = Field(description="True when the decision row has to be created.")
    winner: CandidateEvidence = Field(description="The document promoted to source.")
    members: list[str] = Field(description="Document ids of the group (versions and mirrors), winner included.")
    decision_date: dt.date = Field(description="Date the winner states in its title.")
    language_init: str = Field(description="Lower-case language of the winner, 'NA' when undetermined.")


class DecisionPlan(BaseModel):
    """The ladder's verdict for one manifest decision."""

    decision_id: str = Field(description="Manifest decision id.")
    verdict: Verdict = Field(description="Overall verdict.")
    repoints: list[Repoint] = Field(default_factory=list, description="Candidates moved to existing decisions.")
    promotions: list[Promotion] = Field(default_factory=list, description="Sources to promote.")
    merge_into: str | None = Field(default=None, description="Owner receiving the case links when D is deleted.")
    review_reason: str | None = Field(default=None, description="Why the decision needs a human.")
    candidate_outcomes: dict[str, str] = Field(
        default_factory=dict, description="Document id -> rule outcome, for the report.")
