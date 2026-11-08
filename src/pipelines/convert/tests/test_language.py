"""Tests for Markdown language detection and the language decision."""

import unittest
from unittest.mock import patch

from pipelines.convert.language import decide_language, detect_markdown_language
from pipelines.convert.models import LanguageAction
from pipelines.legal import language_audit

GERMAN = (
    "Die Kommission stellt fest, dass die Unternehmen an einer Zuwiderhandlung "
    "gegen Artikel 101 des Vertrags beteiligt waren. Die Unternehmen haben "
    "Preise abgesprochen und Marktanteile untereinander aufgeteilt, was den "
    "Wettbewerb im Binnenmarkt erheblich beeintraechtigt hat. "
) * 4
ENGLISH = (
    "The Commission finds that the undertakings participated in an "
    "infringement of Article 101 of the Treaty. The undertakings fixed prices "
    "and shared markets, which restricted competition in the internal market. "
) * 4


class DetectMarkdownLanguageTest(unittest.TestCase):
    """detect_markdown_language behaviour."""

    def test_blocks_skip_images_tables_and_headings_only(self) -> None:
        md = (
            f"# Heading\n\n![](images/a.jpg)\n\n<table><tr><td>x</td></tr></table>\n\n"
            f"| a | b |\n| - | - |\n\n{ENGLISH}\n\n## Another"
        )
        seen: list[str] = []
        real = language_audit.detect_chunk_language

        def spy(document: str, doc_pos: int):
            seen.append(document)
            return real(document, doc_pos)

        with patch("pipelines.convert.language.detect_chunk_language", spy):
            detect_markdown_language(md, "d1")
        self.assertEqual(seen, [ENGLISH.strip()])

    def test_detects_german_prose(self) -> None:
        lang, conf = detect_markdown_language(f"{GERMAN}\n\n{GERMAN}\n\n{GERMAN}", "d2")
        self.assertEqual(lang, "de")
        self.assertEqual(conf, 1.0)

    def test_insufficient_prose_is_undetermined(self) -> None:
        self.assertEqual(detect_markdown_language("# T\n\nshort text", "d3"), (None, None))


class DecideLanguageTest(unittest.TestCase):
    """decide_language rules."""

    def test_set_when_stored_missing_or_na(self) -> None:
        for stored in (None, "NA"):
            d = decide_language("de", 0.9, stored)
            self.assertEqual(d.action, LanguageAction.SET)
            self.assertEqual((d.registry_value, d.master_value), ("DE", "de"))

    def test_kept_equal(self) -> None:
        d = decide_language("de", 0.9, "DE")
        self.assertEqual(d.action, LanguageAction.KEPT_EQUAL)
        self.assertIsNone(d.registry_value)

    def test_kept_mismatch(self) -> None:
        with self.assertLogs("pipelines.convert.language", level="WARNING"):
            d = decide_language("de", 0.9, "EN")
        self.assertEqual(d.action, LanguageAction.KEPT_MISMATCH)
        self.assertIsNone(d.registry_value)

    def test_undetermined(self) -> None:
        d = decide_language(None, None, "EN")
        self.assertEqual(d.action, LanguageAction.UNDETERMINED)
        self.assertIsNone(d.master_value)


if __name__ == "__main__":
    unittest.main()
