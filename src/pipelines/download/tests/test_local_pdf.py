"""Tests for the on-disk PDF layout helpers."""

import unittest
from pathlib import Path

from pipelines.download.local_pdf import conventional_markdown_path, markdown_path_for


class TestMarkdownPathFor(unittest.TestCase):
    """markdown_path_for puts the Markdown in a mineru/ folder beside the PDF."""

    def test_markdown_path_for_selected(self) -> None:
        """A selected PDF maps to <folder>/mineru/<stem>.md."""
        self.assertEqual(
            markdown_path_for(Path("/d/X/X.pdf")), Path("/d/X/mineru/X.md")
        )

    def test_markdown_path_for_candidate_uses_pdf_stem(self) -> None:
        """A candidate keeps its own stem inside candidates/mineru/."""
        self.assertEqual(
            markdown_path_for(Path("/d/X/candidates/AT_40013_5708398_1084_5.pdf")),
            Path("/d/X/candidates/mineru/AT_40013_5708398_1084_5.md"),
        )


class TestConventionalMarkdownPath(unittest.TestCase):
    """conventional_markdown_path mirrors the legacy MinerU output layout."""

    def test_conventional_markdown_path(self) -> None:
        """A decision id maps to <docs>/<id>/mineru/auto/<id>.md."""
        self.assertEqual(
            conventional_markdown_path(Path("/docs"), "39525_1916_7"),
            Path("/docs/39525_1916_7/mineru/auto/39525_1916_7.md"),
        )


if __name__ == "__main__":
    unittest.main()
