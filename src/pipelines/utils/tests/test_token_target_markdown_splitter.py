"""Unit tests for the separators option of TokenTargetMarkdownSplitter.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest pipelines.utils.tests.test_token_target_markdown_splitter
"""

import unittest

from config.settings import settings
from pipelines.utils.TokenSizeTargetMarkdownSplitter import TokenTargetMarkdownSplitter

HEADERS = [("#", "Header 1"), ("##", "Header 2")]
LANGCHAIN_DEFAULT = ["\n\n", "\n", " ", ""]
SENTENCES = [
    f"Die Kommission stellt in Randnummer {n} fest, dass die Vereinbarung den Wettbewerb beschraenkt."
    for n in range(40)
]


class SeparatorTests(unittest.TestCase):
    """Fallback splitting of a single oversized block."""

    def test_oversized_paragraph_splits_at_sentence_end(self) -> None:
        """No sentence is cut when a paragraph exceeds the chunk size."""
        text = "# Entscheidung\n\n" + " ".join(SENTENCES)
        splitter = TokenTargetMarkdownSplitter(
            HEADERS, chunk_size=60, separators=settings.TRANSLATE_SPLIT_SEPARATORS)
        chunks = [d.page_content for d in splitter.split_text(text)]
        self.assertGreater(len(chunks), 3)
        for chunk in (c for c in chunks if not c.startswith("#")):
            self.assertTrue(chunk.rstrip().endswith("."), chunk[-60:])
        joined = "\n".join(chunks)
        for sentence in SENTENCES:
            self.assertIn(sentence, joined)

    def test_default_separators_unchanged(self) -> None:
        """Without separators the ingestion behaviour stays LangChain's default."""
        splitter = TokenTargetMarkdownSplitter(HEADERS, chunk_size=60)
        self.assertEqual(splitter.recursive_splitter._separators, LANGCHAIN_DEFAULT)


if __name__ == "__main__":
    unittest.main()
