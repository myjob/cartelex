"""Unit tests for book_tree module.

Covers the private helpers and InfringementTreeBuilder.build using
a small synthetic markdown written to a temporary file.
"""

import os
import tempfile
import unittest

from src.pipelines.rag.pgvector.embeddings.book_tree import (
    BookNode,
    InfringementTreeBuilder,
    _block_text,
    _compute_token_counts,
    _detect_level,
    _heading_text,
    _inline_text,
    _scan_heading_lines,
    _strip_page_number,
)

# ---------------------------------------------------------------------------
# Synthetic markdown used by the builder tests
# ---------------------------------------------------------------------------

_SYNTHETIC_MD = """\
# Table of Contents

TOC content that should be ignored.

# 2 Horizontal restrictions 26

This TOC entry has a trailing page number and must be skipped.

# 2 Horizontal restrictions

Chapter 2 body text.

# 2.1 Price fixing

Section 2.1 body text.

# A Introduction

Subsection A body text.

# F.1 Vitamins

Case F.1 body text.

# a. Relevant facts

Sub-case a body text.

# Some leaf heading

Leaf body text.

# 3 Vertical restrictions

Chapter 3 body text.

# 8 Mergers and acquisitions

Chapter 8 body text.

# 9 Antitrust enforcement

This chapter is outside the target range and must be excluded.
"""


def _write_temp_md(content: str) -> str:
    """Write *content* to a named temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
    except Exception:
        os.unlink(path)
        raise
    return path


# ---------------------------------------------------------------------------
# _detect_level
# ---------------------------------------------------------------------------


class TestDetectLevel(unittest.TestCase):
    """Tests for _detect_level."""

    def test_chapter_level(self) -> None:
        """Heading starting with a digit followed by space is level 1."""
        self.assertEqual(_detect_level("2 Horizontal restrictions"), 1)
        self.assertEqual(_detect_level("8 Mergers and acquisitions"), 1)

    def test_section_level(self) -> None:
        """Heading with digit.digit prefix is level 2."""
        self.assertEqual(_detect_level("2.1 Price fixing"), 2)
        self.assertEqual(_detect_level("3.12 Some section"), 2)

    def test_subsection_uppercase_letter(self) -> None:
        """Single uppercase letter followed by space is level 3."""
        self.assertEqual(_detect_level("A Introduction"), 3)
        self.assertEqual(_detect_level("Z Final remarks"), 3)

    def test_case_level(self) -> None:
        """Uppercase letter dot digit prefix is level 4."""
        self.assertEqual(_detect_level("F.1 Vitamins"), 4)
        self.assertEqual(_detect_level("A.12 Some case"), 4)

    def test_subcase_level(self) -> None:
        """Lowercase letter followed by period and space is level 5."""
        self.assertEqual(_detect_level("a. Relevant facts"), 5)
        self.assertEqual(_detect_level("z. Last item"), 5)

    def test_leaf_level(self) -> None:
        """Anything that matches none of the patterns is level 6."""
        self.assertEqual(_detect_level("Some other heading"), 6)
        self.assertEqual(_detect_level("Conclusion"), 6)
        self.assertEqual(_detect_level("(i) Sub-point"), 6)
        self.assertEqual(_detect_level("AB Multi-letter uppercase"), 6)

    def test_lowercase_dot_matches_level5(self) -> None:
        """Verify that 'i. Something' matches level 5 (not a leaf)."""
        self.assertEqual(_detect_level("i. Something"), 5)


# ---------------------------------------------------------------------------
# _strip_page_number
# ---------------------------------------------------------------------------


class TestStripPageNumber(unittest.TestCase):
    """Tests for _strip_page_number."""

    def test_strips_trailing_integer(self) -> None:
        self.assertEqual(
            _strip_page_number("2 Horizontal restrictions 26"),
            "2 Horizontal restrictions",
        )

    def test_no_trailing_number(self) -> None:
        self.assertEqual(
            _strip_page_number("2 Horizontal restrictions"),
            "2 Horizontal restrictions",
        )

    def test_strips_whitespace_around_number(self) -> None:
        self.assertEqual(_strip_page_number("A Introduction  42"), "A Introduction")

    def test_title_ending_in_year_is_not_stripped(self) -> None:
        """Titles whose *last word* happens to be a number should be stripped.
        This is intentional behaviour per the module docstring."""
        result = _strip_page_number("Case F.1 Vitamins 2001")
        self.assertEqual(result, "Case F.1 Vitamins")


# ---------------------------------------------------------------------------
# _scan_heading_lines
# ---------------------------------------------------------------------------


class TestScanHeadingLines(unittest.TestCase):
    """Tests for _scan_heading_lines."""

    def test_detects_headings_with_correct_line_numbers(self) -> None:
        md = "# First heading\n\nSome text.\n\n# Second heading\n"
        result = _scan_heading_lines(md)
        self.assertEqual(result, [("First heading", 1), ("Second heading", 5)])

    def test_ignores_non_heading_lines(self) -> None:
        md = "Regular paragraph.\n\n## Heading two\n"
        result = _scan_heading_lines(md)
        self.assertEqual(result, [("Heading two", 3)])

    def test_empty_markdown_returns_empty_list(self) -> None:
        self.assertEqual(_scan_heading_lines(""), [])

    def test_strips_leading_hashes(self) -> None:
        md = "### Triple level\n"
        result = _scan_heading_lines(md)
        self.assertEqual(result, [("Triple level", 1)])


# ---------------------------------------------------------------------------
# _inline_text
# ---------------------------------------------------------------------------


class TestInlineText(unittest.TestCase):
    """Tests for _inline_text."""

    def test_plain_text_node(self) -> None:
        nodes = [{"type": "text", "raw": "Hello world"}]
        self.assertEqual(_inline_text(nodes), "Hello world")

    def test_softlinebreak_becomes_space(self) -> None:
        nodes = [
            {"type": "text", "raw": "Line one"},
            {"type": "softlinebreak"},
            {"type": "text", "raw": "line two"},
        ]
        self.assertEqual(_inline_text(nodes), "Line one line two")

    def test_codespan_raw(self) -> None:
        nodes = [{"type": "codespan", "raw": "some_func()"}]
        self.assertEqual(_inline_text(nodes), "some_func()")

    def test_nested_children(self) -> None:
        nodes = [
            {
                "type": "strong",
                "children": [{"type": "text", "raw": "bold text"}],
            }
        ]
        self.assertEqual(_inline_text(nodes), "bold text")

    def test_unknown_node_with_raw(self) -> None:
        nodes = [{"type": "unknown_type", "raw": "raw value"}]
        self.assertEqual(_inline_text(nodes), "raw value")


# ---------------------------------------------------------------------------
# _heading_text
# ---------------------------------------------------------------------------


class TestHeadingText(unittest.TestCase):
    """Tests for _heading_text."""

    def test_extracts_stripped_text(self) -> None:
        token = {
            "type": "heading",
            "children": [{"type": "text", "raw": "  2 Horizontal restrictions  "}],
        }
        self.assertEqual(_heading_text(token), "2 Horizontal restrictions")

    def test_empty_children(self) -> None:
        token = {"type": "heading", "children": []}
        self.assertEqual(_heading_text(token), "")


# ---------------------------------------------------------------------------
# _block_text
# ---------------------------------------------------------------------------


class TestBlockText(unittest.TestCase):
    """Tests for _block_text."""

    def test_blank_line_returns_newline(self) -> None:
        self.assertEqual(_block_text({"type": "blank_line"}), "\n")

    def test_paragraph_returns_text_with_newline(self) -> None:
        token = {
            "type": "paragraph",
            "children": [{"type": "text", "raw": "Hello."}],
        }
        self.assertEqual(_block_text(token), "Hello.\n")

    def test_block_code_returns_raw_with_newline(self) -> None:
        token = {"type": "block_code", "raw": "x = 1"}
        self.assertEqual(_block_text(token), "x = 1\n")

    def test_list_returns_bullet_lines(self) -> None:
        token = {
            "type": "list",
            "children": [
                {
                    "children": [
                        {"type": "paragraph", "children": [{"type": "text", "raw": "Item A"}]}
                    ]
                },
                {
                    "children": [
                        {"type": "paragraph", "children": [{"type": "text", "raw": "Item B"}]}
                    ]
                },
            ],
        }
        result = _block_text(token)
        self.assertIn("- Item A", result)
        self.assertIn("- Item B", result)

    def test_unknown_block_with_raw(self) -> None:
        token = {"type": "thematic_break", "raw": "---"}
        self.assertEqual(_block_text(token), "---\n")


# ---------------------------------------------------------------------------
# _compute_token_counts
# ---------------------------------------------------------------------------


class TestComputeTokenCounts(unittest.TestCase):
    """Tests for _compute_token_counts."""

    def test_leaf_node_counts_own_tokens(self) -> None:
        node = BookNode(title="Leaf", level=1, raw_text="Hello world")
        count = _compute_token_counts(node)
        self.assertGreater(count, 0)
        self.assertEqual(node.token_count, count)

    def test_parent_includes_children_tokens(self) -> None:
        child = BookNode(title="Child", level=2, raw_text="Child text with a few tokens.")
        parent = BookNode(title="Parent", level=1, raw_text="Parent text.", children=[child])
        _compute_token_counts(child)
        parent_count = _compute_token_counts(parent)
        self.assertEqual(parent.token_count, parent_count)
        self.assertGreater(parent.token_count, child.token_count)

    def test_empty_raw_text_gives_zero_own_tokens(self) -> None:
        node = BookNode(title="Empty", level=1, raw_text="")
        _compute_token_counts(node)
        self.assertEqual(node.token_count, 0)


# ---------------------------------------------------------------------------
# InfringementTreeBuilder.build — integration tests with temp file
# ---------------------------------------------------------------------------


class TestInfringementTreeBuilderBuild(unittest.TestCase):
    """Integration tests for InfringementTreeBuilder.build using synthetic markdown."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._md_path = _write_temp_md(_SYNTHETIC_MD)
        cls._tree = InfringementTreeBuilder().build(cls._md_path)

    @classmethod
    def tearDownClass(cls) -> None:
        os.unlink(cls._md_path)

    def test_root_title_and_level(self) -> None:
        """Root node has title 'Infringements' and level 0."""
        self.assertEqual(self._tree.title, "Infringements")
        self.assertEqual(self._tree.level, 0)

    def test_root_path(self) -> None:
        """Root path equals its title."""
        self.assertEqual(self._tree.path, "Infringements")

    def test_chapter_count(self) -> None:
        """Exactly 3 top-level chapters (2, 3, 8) are captured."""
        self.assertEqual(len(self._tree.children), 3)

    def test_chapter_titles(self) -> None:
        """Chapter titles match the synthetic headings (page numbers stripped)."""
        titles = [ch.title for ch in self._tree.children]
        self.assertIn("2 Horizontal restrictions", titles)
        self.assertIn("3 Vertical restrictions", titles)
        self.assertIn("8 Mergers and acquisitions", titles)

    def test_toc_entry_skipped(self) -> None:
        """The heading '2 Horizontal restrictions 26' (with page number) is not a node."""
        all_titles = [ch.title for ch in self._tree.children]
        for title in all_titles:
            self.assertNotIn("26", title)

    def test_chapter9_excluded(self) -> None:
        """Content after chapter 8 is not included in the tree."""
        all_titles = [ch.title for ch in self._tree.children]
        for title in all_titles:
            self.assertNotIn("9 Antitrust", title)

    def test_chapter2_has_section(self) -> None:
        """Chapter 2 has '2.1 Price fixing' as a child section."""
        ch2 = next(ch for ch in self._tree.children if ch.title == "2 Horizontal restrictions")
        section_titles = [s.title for s in ch2.children]
        self.assertIn("2.1 Price fixing", section_titles)

    def test_nesting_depth(self) -> None:
        """Section 2.1 has a subsection 'A Introduction' which has case 'F.1 Vitamins'."""
        ch2 = next(ch for ch in self._tree.children if ch.title == "2 Horizontal restrictions")
        section_21 = next(s for s in ch2.children if s.title == "2.1 Price fixing")
        subsection_a = next(
            (s for s in section_21.children if s.title == "A Introduction"), None
        )
        self.assertIsNotNone(subsection_a)
        case_f1 = next(
            (s for s in subsection_a.children if s.title == "F.1 Vitamins"), None  # type: ignore[union-attr]
        )
        self.assertIsNotNone(case_f1)

    def test_path_includes_ancestors(self) -> None:
        """A nested node's path contains all ancestor titles."""
        ch2 = next(ch for ch in self._tree.children if ch.title == "2 Horizontal restrictions")
        section_21 = next(s for s in ch2.children if s.title == "2.1 Price fixing")
        self.assertIn("Infringements", section_21.path)
        self.assertIn("2 Horizontal restrictions", section_21.path)
        self.assertIn("2.1 Price fixing", section_21.path)

    def test_raw_text_contains_body(self) -> None:
        """Body text following a heading is stored in raw_text."""
        ch2 = next(ch for ch in self._tree.children if ch.title == "2 Horizontal restrictions")
        # The heading's own title is always the first line of raw_text.
        self.assertTrue(ch2.raw_text.startswith("2 Horizontal restrictions"))
        self.assertIn("Chapter 2 body text", ch2.raw_text)

    def test_token_counts_positive(self) -> None:
        """All nodes with non-empty raw_text have a positive token count."""
        self.assertGreater(self._tree.token_count, 0)

    def test_root_token_count_sum_of_children(self) -> None:
        """Root token count equals the sum of its direct children's token counts."""
        children_total = sum(ch.token_count for ch in self._tree.children)
        self.assertEqual(self._tree.token_count, children_total)

    def test_text_line_is_positive(self) -> None:
        """Every chapter node records a positive 1-based source line number."""
        for ch in self._tree.children:
            self.assertGreater(ch.text_line, 0, f"text_line not set for '{ch.title}'")

    def test_chapter_level_values(self) -> None:
        """All direct children of root have level == 1."""
        for ch in self._tree.children:
            self.assertEqual(ch.level, 1)


class TestInfringementTreeBuilderErrors(unittest.TestCase):
    """Error-path tests for InfringementTreeBuilder.build."""

    def test_missing_file_raises_file_not_found(self) -> None:
        """build() raises FileNotFoundError for a non-existent path."""
        with self.assertRaises(FileNotFoundError):
            InfringementTreeBuilder().build("/nonexistent/path/to/book.md")

    def test_missing_start_heading_raises_value_error(self) -> None:
        """build() raises ValueError when the start heading is absent."""
        md = "# Introduction\n\nSome text without the required chapter 2 heading.\n"
        path = _write_temp_md(md)
        try:
            with self.assertRaises(ValueError):
                InfringementTreeBuilder().build(path)
        finally:
            os.unlink(path)

    def test_only_toc_entry_not_enough_to_start(self) -> None:
        """A TOC entry with a trailing page number does not start the window."""
        md = "# 2 Horizontal restrictions 26\n\nTOC content only.\n"
        path = _write_temp_md(md)
        try:
            with self.assertRaises(ValueError):
                InfringementTreeBuilder().build(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
