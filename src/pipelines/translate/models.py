"""Data models of the Markdown translation batch."""

import json
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class TranslatedChunk(BaseModel):
    """One translated Markdown chunk returned by the LLM."""

    markdown: str = Field(description="English translation of the chunk, Markdown preserved.")

    @model_validator(mode="before")
    @classmethod
    def _accept_plain_answer(cls, value: Any) -> Any:
        """Accept the LLM's bare Markdown, a JSON string or a bracketed line parsed as a list.

        DSPy's adapter hands back the raw answer when the model replies with
        plain Markdown instead of ``{"markdown": ...}``.

        Args:
            value: Raw input.

        Returns:
            A mapping for the model fields, or the value unchanged.
        """
        if isinstance(value, list):
            # A Markdown answer that is one bracketed line ("[currently
            # Article 101 ...]") is parsed as a JSON array by the adapter.
            return {"markdown": "[" + ", ".join(str(v) for v in value) + "]"}
        if not isinstance(value, str):
            return value
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"markdown": value}
        return parsed if isinstance(parsed, dict) else {"markdown": value}


class TranslationOutcome(StrEnum):
    """Result of one queue item; mirrors the audit CHECK constraint."""

    TRANSLATED = "translated"
    SKIPPED = "skipped"
    ALREADY_ENGLISH = "already_english"
    FAILED = "failed"


class TranslationItem(BaseModel):
    """One source registry row in scope of the translation queue."""

    document_id: str = Field(description="Registry id of the source row.")
    decision_id: str = Field(description="Decision id (also the master key).")
    language: str | None = Field(description="Upper-case registry language; None/NA when unset.")
    local_file_md: str = Field(description="Working Markdown of the row.")
    original_file_md: str | None = Field(
        default=None, description="Foreign-language Markdown already translated from; None when untranslated.")
    language_translated: str | None = Field(
        default=None, description="decision_registry.language_translated ('en' once translated).")


class TranslationAuditRecord(BaseModel):
    """One row of ``document_translation_audit`` (id and created_at are DB defaults)."""

    run_id: UUID = Field(description="Id of the batch run.")
    decision_id: str | None = Field(default=None, description="Registry decision id.")
    document_id: str | None = Field(default=None, description="Registry source document id.")
    source_file_md: str | None = Field(default=None, description="Markdown that was translated.")
    target_file_md: str | None = Field(default=None, description="English Markdown written.")
    source_language: str | None = Field(default=None, description="Upper-case source language.")
    model_id: str | None = Field(default=None, description="LLM model id.")
    chunk_count: int | None = Field(default=None, description="Number of chunks.")
    input_tokens: int | None = Field(default=None, description="Prompt tokens, when reported.")
    output_tokens: int | None = Field(default=None, description="Completion tokens, when reported.")
    duration_ms: int | None = Field(default=None, description="Wall clock of the item in ms.")
    outcome: TranslationOutcome = Field(description="Result of the item.")
    reason: str | None = Field(default=None, description="Failure or skip reason.")


class ChunkRejected(Exception):
    """A translated chunk failed the structural checks."""


class TranslationConnectionError(Exception):
    """The LLM endpoint could not be reached."""
