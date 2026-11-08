"""Pydantic models for the PDF-to-Markdown conversion batch.

Wire models mirror the MinerU 4.1 Tasks API snapshots; they ignore unknown
fields because the server may add some. Task and file statuses stay open
strings (the server may add states).
"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversionOutcome(StrEnum):
    """Result of converting one document (audit table outcome)."""

    CONVERTED = "converted"
    SKIPPED_EXISTS = "skipped_exists"
    NO_TARGET_PATH = "no_target_path"
    PDF_MISSING = "pdf_missing"
    FAILED = "failed"
    TIMEOUT = "timeout"


class LanguageAction(StrEnum):
    """What the conversion did with the registry language of a document."""

    SET = "set"
    KEPT_EQUAL = "kept_equal"
    KEPT_MISMATCH = "kept_mismatch"
    UNDETERMINED = "undetermined"


class MineruError(BaseModel):
    """Public error object of the Tasks API."""

    model_config = ConfigDict(extra="ignore")

    code: str = Field(description="Machine-readable error code.")
    message: str = Field(description="Human-readable error message.")
    param: str | None = Field(default=None, description="Offending request parameter, if any.")


class MineruParse(BaseModel):
    """Parse statistics of a successfully parsed file."""

    model_config = ConfigDict(extra="ignore")

    tier: str = Field(description="Tier that actually parsed the file.")
    duration_ms: int | None = Field(default=None, description="Parse duration in milliseconds.")
    parser_version: str | None = Field(default=None, description="Executing worker version.")


class MineruUpload(BaseModel):
    """Upload link for a file that still needs its bytes."""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(description="URL to PUT the raw bytes to.")
    headers: dict[str, str] = Field(
        default_factory=dict, description="Headers to send with the PUT.")
    expires_at: int = Field(description="Link expiry as Unix seconds.")


class MineruFile(BaseModel):
    """One file entry of a task snapshot."""

    model_config = ConfigDict(extra="ignore")

    file_id: str = Field(description="Server-allocated public file id.")
    file_name: str = Field(description="Logical file name as submitted.")
    status: str = Field(description="File state, e.g. waiting-file, pending, running, completed.")
    upload: MineruUpload | None = Field(
        default=None, description="Upload link; present only while bytes are needed.")
    parse: MineruParse | None = Field(
        default=None, description="Parse statistics; successful files only.")
    result_url: str | None = Field(
        default=None, description="Result download URL; completed files only.")
    error: MineruError | None = Field(default=None, description="Failure detail, if any.")


class MineruTask(BaseModel):
    """Task snapshot (creation receipt or GET detail)."""

    model_config = ConfigDict(extra="ignore")

    task_id: str = Field(description="Server-allocated task id.")
    status: str = Field(description="Aggregated task state.")
    files: list[MineruFile] = Field(
        default_factory=list, description="Files in submission order.")
    error: MineruError | None = Field(default=None, description="Task-level error, if any.")


class ConversionItem(BaseModel):
    """One registry row in scope of the conversion queue."""

    document_id: str = Field(description="Registry document id.")
    decision_id: str = Field(description="Decision the document belongs to.")
    is_source: bool = Field(description="True for the decision's source document.")
    local_file_pdf: str = Field(description="Path of the PDF to convert.")
    local_file_md: str | None = Field(
        default=None, description="Spec'd Markdown target path; None when unknown.")
    language: str | None = Field(
        default=None, description="Registry language (upper-case) or None/NA when unset.")
    source_pdf: str | None = Field(
        default=None, description="PDF path of the decision's source row; None when unknown.")
    original_file_md: str | None = Field(
        default=None, description="Foreign-language Markdown of a translated source row.")
    translated: bool = Field(
        default=False,
        description="Source row whose local_file_md is an English translation (decision language_translated = 'en').")


class ConversionAuditRecord(BaseModel):
    """One row of ``document_conversion_audit`` (id and created_at are DB defaults)."""

    run_id: UUID = Field(description="Id of the batch run.")
    outcome: ConversionOutcome = Field(description="Result of the conversion attempt.")
    decision_id: str | None = Field(default=None, description="Registry decision id.")
    document_id: str | None = Field(default=None, description="Registry document id.")
    local_file_pdf: str | None = Field(default=None, description="PDF path used.")
    local_file_md: str | None = Field(default=None, description="Markdown path used.")
    failure_reason: str | None = Field(
        default=None, description="MinerU error code or own failure reason.")
    error_message: str | None = Field(default=None, description="Error message detail.")
    mineru_task_id: str | None = Field(default=None, description="MinerU task id.")
    mineru_file_id: str | None = Field(default=None, description="MinerU file id.")
    mineru_api_url: str | None = Field(default=None, description="MinerU server used.")
    tier: str | None = Field(default=None, description="Tier that parsed the file.")
    parser_version: str | None = Field(default=None, description="Worker version.")
    server_duration_ms: int | None = Field(default=None, description="Server parse time in ms.")
    wall_duration_ms: int | None = Field(default=None, description="Wall time submit to placed.")
    pdf_size_bytes: int | None = Field(default=None, description="PDF size in bytes.")
    pdf_sha256: str | None = Field(default=None, description="PDF SHA-256 hex digest.")
    pdf_pages: int | None = Field(default=None, description="PDF page count.")
    md_chars: int | None = Field(default=None, description="Characters in the Markdown.")
    image_count: int | None = Field(default=None, description="Images in the result.")
    table_count: int | None = Field(default=None, description="Tables in the result.")
    formula_count: int | None = Field(default=None, description="Formulas in the result.")
    language_detected: str | None = Field(default=None, description="Detected language code.")
    language_confidence: float | None = Field(
        default=None, description="Confidence of the detected language.")
    language_stored: str | None = Field(
        default=None, description="Registry language before the run.")
    language_action: LanguageAction | None = Field(
        default=None, description="What happened to the registry language.")
