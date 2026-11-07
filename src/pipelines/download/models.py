"""Pydantic models for the HTTP document downloader.

Three levels: competition case ids -> decision (``DecisionRequest``) ->
candidate document (``CandidateDocument``, one per PDF URL).
"""

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class Outcome(StrEnum):
    """Result of processing one candidate document (audit table outcome)."""

    DOWNLOADED = "downloaded"
    REGISTERED_EXISTING = "registered_existing"
    SKIPPED_EXISTS = "skipped_exists"
    NOT_SELECTED = "not_selected"
    NO_PDF_SOURCE = "no_pdf_source"
    FAILED = "failed"


class SelectionStatus(StrEnum):
    """Candidate selection state of a decision."""

    SELECTED = "selected"
    UNDECIDED = "undecided"
    NO_PDF_SOURCE = "no_pdf_source"


class CandidateDocument(BaseModel):
    """One downloadable document of a decision (one PDF URL)."""

    document_id: str = Field(description="File stem derived from the URL.")
    source_url: str = Field(description="Absolute URL of the document.")
    file_format: str = Field(default="pdf", description="Document format.")
    language: str | None = Field(
        default=None,
        description="Upper-case language from the URL marker, None if unknown.",
    )
    origin: str = Field(
        default="other",
        description="Source site: eur-lex, ec.europa.eu, db-comp or other.",
    )
    manifest_row_id: str | None = Field(
        default=None, description="Upstream per-file manifest row id, if known."
    )

    @field_validator("language")
    @classmethod
    def _upper_language(cls, value: str | None) -> str | None:
        """Normalise the language code to upper case.

        Args:
            value: Raw language code or None.

        Returns:
            Upper-case code, or None when blank.
        """
        if value is None or not value.strip():
            return None
        return value.strip().upper()


class DecisionRequest(BaseModel):
    """A decision as produced by a loader mapper."""

    decision_id: str = Field(
        description="Master JSON key and folder name of the decision."
    )
    celex_number: str | None = Field(
        default=None,
        description=(
            "CELEX number (bare or CELEX- prefixed) for real CELEX decisions "
            "only, else None; stored prefixed via identifiers.celex_key."
        ),
    )
    case_ids: list[str] = Field(
        default_factory=list,
        description="Normalised competition case ids (IV/N, AT.N, COMP/...).",
    )
    decision_date: dt.date | None = Field(default=None, description="Decision date.")
    title: str = Field(default="", description="Decision title.")
    parties: str = Field(default="", description="Parties / firms.")
    document_class: str = Field(default="", description="Document class.")
    meta: dict[str, str] = Field(
        default_factory=dict,
        description="Every other loader column, verbatim.",
    )
    review_reasons: list[str] = Field(
        default_factory=list, description="Reasons the decision needs review."
    )
    candidates: list[CandidateDocument] = Field(
        default_factory=list, description="Candidate documents (PDF URLs)."
    )
    loader_file: str = Field(description="Loader file the row came from.")
    loader_row: int = Field(description="Row number within the loader file.")

    @field_validator("decision_id")
    @classmethod
    def _require_decision_id(cls, value: str) -> str:
        """Reject blank decision ids.

        Args:
            value: Raw decision id.

        Returns:
            The stripped id.

        Raises:
            ValueError: If the id is blank.
        """
        value = value.strip()
        if not value:
            raise ValueError("decision_id must not be blank")
        return value


class RowFailure(BaseModel):
    """A loader row that could not be mapped (audited, never raised)."""

    loader_file: str = Field(description="Loader file name.")
    loader_row: int = Field(description="Row number within the loader file.")
    reason: str = Field(description="Why the row failed.")


class FetchResult(BaseModel):
    """Result of fetching or checking one candidate document."""

    document_id: str = Field(description="Document id.")
    source_url: str = Field(description="Source URL.")
    outcome: Outcome = Field(description="Outcome of the attempt.")
    local_path: str | None = Field(default=None, description="Stored PDF path.")
    sha256: str | None = Field(default=None, description="SHA-256 of the file.")
    size_bytes: int | None = Field(default=None, description="File size in bytes.")
    content_type: str | None = Field(
        default=None, description="Content-Type header (recorded, not trusted)."
    )
    http_status: int | None = Field(default=None, description="HTTP status code.")
    failure_reason: str | None = Field(
        default=None, description="Failure reason; required when outcome is failed."
    )
    duration_ms: int = Field(description="Elapsed time in milliseconds.")

    @model_validator(mode="after")
    def _failed_needs_reason(self) -> "FetchResult":
        """Require a failure reason for failed outcomes.

        Returns:
            The validated model.

        Raises:
            ValueError: If outcome is failed and failure_reason is empty.
        """
        if self.outcome is Outcome.FAILED and not self.failure_reason:
            raise ValueError("failed outcome requires failure_reason")
        return self


class MapperResult(BaseModel):
    """Output of a loader mapper."""

    decisions: list[DecisionRequest] = Field(
        default_factory=list, description="Mapped decisions."
    )
    failures: list[RowFailure] = Field(
        default_factory=list, description="Rows that could not be mapped."
    )
