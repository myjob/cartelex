"""DSPy translation of one Markdown chunk into English, with structural checks."""

import logging
import re
from collections.abc import Callable

import dspy
import httpx
import litellm

from config.settings import settings
from pipelines.convert.language import detect_markdown_language
from pipelines.translate.models import (
    ChunkRejected,
    TranslatedChunk,
    TranslationConnectionError,
)

logger = logging.getLogger(__name__)

_HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)
_CHUNK_ID = "chunk"
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.ServiceUnavailableError,
    httpx.ConnectError,
)


class TranslateMarkdownChunk(dspy.Signature):
    """Translate a chunk of an EU competition law decision into English.

    Give a faithful legal translation, not a summary. Keep the Markdown
    structure exactly: the same headings at the same levels, paragraph and
    recital numbering, list markers, footnote markers, tables and line breaks.
    Keep party and company names, case numbers, article references, dates in
    their numeric form and amounts unchanged. Output only the translation.
    """

    source_language: str = dspy.InputField(desc="Name of the language of the chunk, e.g. German.")
    markdown_chunk: str = dspy.InputField(desc="Markdown chunk of the decision to translate.")
    # Plain text on purpose: a pydantic-typed field makes DSPy JSON-repair the
    # Markdown and keep only its first bracketed fragment ("[currently Article
    # 101 ...]"); the module validates the text into TranslatedChunk.
    translation: str = dspy.OutputField(
        desc="English translation of the chunk with the Markdown structure preserved.")


def check_chunk(source: str, translated: str, ratio_min: float, ratio_max: float) -> None:
    """Reject a translation that is empty, lost headings or has an odd length.

    Args:
        source: Source chunk.
        translated: Translated chunk.
        ratio_min: Lowest accepted ``len(translated) / len(source)``.
        ratio_max: Highest accepted ``len(translated) / len(source)``.

    Raises:
        ChunkRejected: If any check fails; the message names the check.
    """
    if not translated.strip():
        raise ChunkRejected("empty translation")
    src_headings, out_headings = len(_HEADING.findall(source)), len(_HEADING.findall(translated))
    if src_headings != out_headings:
        raise ChunkRejected(f"heading count {out_headings} != {src_headings}")
    ratio = len(translated) / max(len(source), 1)
    if not ratio_min <= ratio <= ratio_max:
        raise ChunkRejected(f"length ratio {ratio:.2f} outside [{ratio_min}, {ratio_max}]")


class MarkdownTranslator(dspy.Module):
    """Translate one Markdown chunk with retry on rejected answers."""

    lm: dspy.LM
    translate: Callable[..., dspy.Prediction]

    def __init__(
        self,
        lm: dspy.LM,
        predictor: Callable[..., dspy.Prediction] | None = None,
        *,
        retries: int,
        ratio_min: float,
        ratio_max: float,
        language_min_chars: int,
    ) -> None:
        """Provision the translator.

        Args:
            lm: Language model the chunks are translated with.
            predictor: Callable with the signature's inputs; defaults to
                ``dspy.Predict(TranslateMarkdownChunk)`` (tests inject a fake).
            retries: Extra attempts after a rejected answer.
            ratio_min: Lowest accepted length ratio.
            ratio_max: Highest accepted length ratio.
            language_min_chars: Translated chunks at least this long must be
                detected as English (catches an echoed source chunk).
        """
        super().__init__()
        self.lm = lm
        self.translate = predictor or dspy.Predict(TranslateMarkdownChunk)
        self._retries = retries
        self._ratio_min = ratio_min
        self._ratio_max = ratio_max
        self._language_min_chars = language_min_chars

    def _check_not_echoed(self, translated: str, source_language: str) -> None:
        """Reject a long enough chunk that is still in the source language (an echo).

        A third-language guess (tables of names and places detect as e.g.
        Catalan) is no echo; the whole-document check still requires English.

        Raises:
            ChunkRejected: If the chunk is detected as the source language.
        """
        if len(translated) < self._language_min_chars:
            return
        detected, _ = detect_markdown_language(translated, _CHUNK_ID)
        if detected is None:
            return
        if settings.TRANSLATE_LANGUAGE_NAMES.get(detected.upper()) == source_language:
            raise ChunkRejected(f"chunk still in {source_language} ({detected}), not translated")

    def forward(self, markdown_chunk: str, source_language: str) -> str:
        """Translate one chunk.

        Args:
            markdown_chunk: Source Markdown chunk.
            source_language: Language name passed to the LLM.

        Returns:
            The checked English Markdown.

        Raises:
            ChunkRejected: If every attempt failed the checks.
            TranslationConnectionError: If the LLM endpoint is unreachable.
        """
        last: ChunkRejected | None = None
        for attempt in range(self._retries + 1):
            try:
                with dspy.context(lm=self.lm):
                    prediction = self.translate(
                        source_language=source_language, markdown_chunk=markdown_chunk)
            except _CONNECTION_ERRORS as exc:
                raise TranslationConnectionError(str(exc)) from exc
            translated = TranslatedChunk.model_validate(prediction.translation).markdown
            try:
                check_chunk(markdown_chunk, translated, self._ratio_min, self._ratio_max)
                self._check_not_echoed(translated, source_language)
                return translated
            except ChunkRejected as exc:
                last = exc
                logger.warning("chunk rejected (attempt %d): %s", attempt + 1, exc)
        raise last or ChunkRejected("no attempt made")
