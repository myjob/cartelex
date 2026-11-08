"""Unit tests for the DSPy Markdown chunk translator (LM boundary faked).

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest pipelines.translate.tests.test_translator
"""

import unittest
from unittest.mock import patch

import dspy
import litellm

from pipelines.translate.models import (
    ChunkRejected,
    TranslatedChunk,
    TranslationConnectionError,
)
from pipelines.translate.translator import (
    MarkdownTranslator,
    TranslateMarkdownChunk,
    check_chunk,
)

SOURCE = "# Entscheidung\n\nDie Kommission stellt fest, dass die Vereinbarung gilt."
ENGLISH = "# Decision\n\nThe Commission finds that the agreement applies."


class _Predictor:
    """Scripted predictor: returns or raises the queued steps in order."""

    def __init__(self, *steps: object) -> None:
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.raw = False

    def __call__(self, **kwargs: object) -> dspy.Prediction:
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        if self.raw:
            return dspy.Prediction(translation=str(step))
        return dspy.Prediction(translation=TranslatedChunk(markdown=str(step)))


def _translator(predictor: _Predictor) -> MarkdownTranslator:
    """Build a translator around a fake predictor.

    Args:
        predictor: Fake predictor.

    Returns:
        The translator.
    """
    return MarkdownTranslator(
        dspy.LM("ollama_chat/fake"), predictor, retries=1, ratio_min=0.5, ratio_max=2.0,
        language_min_chars=20)


class SignatureTests(unittest.TestCase):
    """Declarative signature rules."""

    def test_signature_fields_have_desc(self) -> None:
        """Every field documents itself."""
        fields = {**TranslateMarkdownChunk.input_fields, **TranslateMarkdownChunk.output_fields}
        self.assertEqual(set(fields), {"source_language", "markdown_chunk", "translation"})
        for name, field in fields.items():
            self.assertTrue(field.json_schema_extra.get("desc"), name)

    def test_output_field_is_plain_text(self) -> None:
        """A pydantic-typed output field makes DSPy JSON-repair the Markdown and keep only the
        first bracketed fragment ('[currently Article 101 ...]'); the text arrives as str and is
        validated into TranslatedChunk by the module."""
        self.assertIs(TranslateMarkdownChunk.output_fields["translation"].annotation, str)

    def test_bracketed_text_survives_forward(self) -> None:
        """A full chunk with square brackets is returned whole."""
        text = ("# COMMISSION DECISION\n\nof 31.3.2011 amending Decision C(2007) 4257 under Article 81 "
                "[currently Article 101 of the Treaty] and [...] confidential.")
        predictor = _Predictor(text)
        predictor.raw = True
        self.assertEqual(_translator(predictor)(markdown_chunk=SOURCE + " [x] " * 10, source_language="French"),
                         text)


class CheckChunkTests(unittest.TestCase):
    """Structural checks of one translated chunk."""

    def test_accepts_faithful_translation(self) -> None:
        """Same headings and similar length pass."""
        check_chunk(SOURCE, ENGLISH, 0.5, 2.0)

    def test_rejects_empty(self) -> None:
        """Whitespace only is rejected."""
        with self.assertRaises(ChunkRejected):
            check_chunk(SOURCE, "  \n", 0.5, 2.0)

    def test_rejects_heading_count_change(self) -> None:
        """A dropped heading is rejected."""
        with self.assertRaises(ChunkRejected):
            check_chunk("# A\n\ntext body here", "text body here", 0.1, 10.0)

    def test_rejects_short_translation(self) -> None:
        """A truncated answer is rejected."""
        with self.assertRaises(ChunkRejected):
            check_chunk(SOURCE, "# Decision\n\nThe.", 0.5, 2.0)


class TranslatorTests(unittest.TestCase):
    """Forward dispatch with retry and error mapping."""

    def test_returns_translated_markdown(self) -> None:
        """The predictor's markdown is returned and the language is passed through."""
        predictor = _Predictor(ENGLISH)
        self.assertEqual(_translator(predictor)(markdown_chunk=SOURCE, source_language="German"), ENGLISH)
        self.assertEqual(predictor.calls[0], {"source_language": "German", "markdown_chunk": SOURCE})

    def test_bare_markdown_answer_accepted(self) -> None:
        """The adapter may hand back the model's plain Markdown instead of the object."""
        predictor = _Predictor(ENGLISH)
        predictor.raw = True
        self.assertEqual(_translator(predictor)(markdown_chunk=SOURCE, source_language="German"), ENGLISH)

    def test_json_string_answer_accepted(self) -> None:
        """A JSON object as string is parsed into the chunk model."""
        predictor = _Predictor('{"markdown": "# Decision\\n\\nThe Commission finds that the agreement applies."}')
        predictor.raw = True
        self.assertEqual(_translator(predictor)(markdown_chunk=SOURCE, source_language="German"), ENGLISH)

    def test_echoed_chunk_is_rejected_and_retried(self) -> None:
        """A chunk returned untranslated is caught per chunk, not only per document."""
        predictor = _Predictor(SOURCE, ENGLISH)
        with patch("pipelines.translate.translator.detect_markdown_language",
                   side_effect=lambda text, _id: ("de", 0.9) if "Kommission" in text else ("en", 0.9)):
            result = _translator(predictor)(markdown_chunk=SOURCE, source_language="German")
        self.assertEqual(result, ENGLISH)
        self.assertEqual(len(predictor.calls), 2)

    def test_third_language_guess_on_table_chunk_is_accepted(self) -> None:
        """36212 chunk 196: addresses and meeting tables detect as 'ca'; only the source language means an echo."""
        predictor = _Predictor(ENGLISH)
        with patch("pipelines.translate.translator.detect_markdown_language", return_value=("ca", 0.6)):
            result = _translator(predictor)(markdown_chunk=SOURCE, source_language="French")
        self.assertEqual(result, ENGLISH)
        self.assertEqual(len(predictor.calls), 1)

    def test_short_chunk_skips_language_check(self) -> None:
        """Chunks below the minimum length are not language-checked."""
        predictor = _Predictor("# A")
        translator = MarkdownTranslator(
            dspy.LM("ollama_chat/fake"), predictor, retries=0, ratio_min=0.5,
            ratio_max=2.0, language_min_chars=100)
        with patch("pipelines.translate.translator.detect_markdown_language") as detect:
            self.assertEqual(translator(markdown_chunk="# B", source_language="German"), "# A")
        detect.assert_not_called()

    def test_bracketed_answer_parsed_as_list_is_restored(self) -> None:
        """'[currently Article 101 ...]' arrives as a JSON list from the adapter."""
        self.assertEqual(
            TranslatedChunk.model_validate(["currently Article 101 of the Treaty"]).markdown,
            "[currently Article 101 of the Treaty]")
        self.assertEqual(TranslatedChunk.model_validate(["a", "b"]).markdown, "[a, b]")

    def test_retries_once_then_raises(self) -> None:
        """Two rejected answers fail the chunk."""
        predictor = _Predictor("", "")
        with self.assertRaises(ChunkRejected):
            _translator(predictor)(markdown_chunk=SOURCE, source_language="German")
        self.assertEqual(len(predictor.calls), 2)

    def test_retry_recovers(self) -> None:
        """A rejected first answer is retried."""
        predictor = _Predictor("", ENGLISH)
        self.assertEqual(_translator(predictor)(markdown_chunk=SOURCE, source_language="German"), ENGLISH)

    def test_connection_error_mapped(self) -> None:
        """Transport errors become TranslationConnectionError without retry."""
        error = litellm.APIConnectionError(message="refused", llm_provider="ollama", model="m")
        predictor = _Predictor(error, ENGLISH)
        with self.assertRaises(TranslationConnectionError):
            _translator(predictor)(markdown_chunk=SOURCE, source_language="German")
        self.assertEqual(len(predictor.calls), 1)


if __name__ == "__main__":
    unittest.main()
