"""Unit tests for :mod:`tools.md_name_cleanser`.

Runs in isolation on temporary folders; no database, LLM or network boundary
is touched.
"""

import tempfile
import unittest
from pathlib import Path

from tools.md_name_cleanser import (
    Rename,
    add_title_heading,
    is_windows_safe,
    plan,
    rewrite_links,
    run,
    sanitize_name,
)


class TestSanitizeName(unittest.TestCase):
    """Per-segment Windows rules."""

    def test_safe_names_are_unchanged(self) -> None:
        """A legal name, including a non-ASCII one, is returned as-is."""
        for name in ("Design BgeRanker.md", "FlashRanker (obsolete).md", "Runbook — guide.md"):
            self.assertTrue(is_windows_safe(name))
            self.assertEqual(sanitize_name(name, is_file=True), name)

    def test_question_mark_is_dropped(self) -> None:
        """``?`` disappears and the space before it is trimmed."""
        self.assertEqual(sanitize_name("Whats my IP ?.md", is_file=True), "Whats my IP.md")

    def test_quotes_and_trailing_stem_dot(self) -> None:
        """Quotes go and a dot left before the extension is stripped."""
        name = 'Ranker "answer a question" vs. "enumerate all instances of X.".md'
        self.assertEqual(
            sanitize_name(name, is_file=True),
            "Ranker answer a question vs. enumerate all instances of X.md",
        )

    def test_colon_and_pipe(self) -> None:
        """``:`` reads as a separator, ``|`` becomes a hyphen."""
        self.assertEqual(sanitize_name("Design: A|B.md", is_file=True), "Design - A-B.md")

    def test_reserved_device_name(self) -> None:
        """``CON.md`` and ``nul`` are reserved and get a suffix."""
        self.assertEqual(sanitize_name("CON.md", is_file=True), "CON_.md")
        self.assertEqual(sanitize_name("nul", is_file=False), "nul_")

    def test_trailing_dot_and_space_on_folder(self) -> None:
        """A folder may not end with a dot or space."""
        self.assertEqual(sanitize_name("notes. ", is_file=False), "notes")

    def test_name_of_only_forbidden_chars(self) -> None:
        """A name that sanitizes to nothing gets a placeholder."""
        self.assertEqual(sanitize_name("???", is_file=False), "_")


class TestAddTitleHeading(unittest.TestCase):
    """Old file name as the note's first chapter."""

    def test_heading_on_top(self) -> None:
        """The old name becomes the level-1 heading above the body."""
        self.assertEqual(add_title_heading("body\n", "Whats my IP ?"), "# Whats my IP ?\n\nbody\n")

    def test_after_front_matter(self) -> None:
        """YAML front matter stays first; the heading follows it."""
        text = "---\ntags: [a]\n---\nbody"
        self.assertEqual(add_title_heading(text, "T?"), "---\ntags: [a]\n---\n# T?\n\nbody")

    def test_idempotent(self) -> None:
        """An existing identical heading is not duplicated."""
        text = '\n# Ranker "a" vs. "b."\nbody'
        self.assertEqual(add_title_heading(text, 'Ranker "a" vs. "b."'), text)


class TestRewriteLinks(unittest.TestCase):
    """Link forms used in the Obsidian vault."""

    def setUp(self) -> None:
        """Build one rename of ``Whats my IP ?.md``."""
        self.renames = [
            Rename(old=Path("/d/infra/Whats my IP ?.md"), new=Path("/d/infra/Whats my IP.md"), is_file=True)
        ]

    def test_wikilink_forms(self) -> None:
        """Plain, aliased, heading and folder-prefixed wikilinks are retargeted."""
        text = "[[Whats my IP ?]] [[Whats my IP ?|ip]] [[Whats my IP ?#Top]] [[infra/Whats my IP ?]]"
        self.assertEqual(
            rewrite_links(text, self.renames),
            "[[Whats my IP]] [[Whats my IP|ip]] [[Whats my IP#Top]] [[infra/Whats my IP]]",
        )

    def test_markdown_and_raw_forms(self) -> None:
        """Encoded Markdown links and raw backtick paths are retargeted."""
        text = "[x](infra/Whats%20my%20IP%20%3F.md) [y](<infra/Whats my IP ?.md>) `docs/infra/Whats my IP ?.md:13`"
        self.assertEqual(
            rewrite_links(text, self.renames),
            "[x](infra/Whats%20my%20IP.md) [y](<infra/Whats my IP.md>) `docs/infra/Whats my IP.md:13`",
        )

    def test_unrelated_text_untouched(self) -> None:
        """A wikilink with a longer name sharing the prefix stays as it is."""
        text = "[[Whats my IP ? notes]]"
        self.assertEqual(rewrite_links(text, self.renames), text)


class TestRun(unittest.TestCase):
    """End-to-end passes on a temporary vault."""

    def setUp(self) -> None:
        """Create a project root with ``docs/`` and a top-level issues file."""
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        self.docs = self.project / "docs"
        (self.docs / "infra").mkdir(parents=True)
        (self.docs / "infra" / "Whats my IP ?.md").write_text("ip", encoding="utf-8")
        (self.docs / "index.md").write_text("see [[Whats my IP ?]]", encoding="utf-8")
        (self.project / "ISSUES.md").write_text("`docs/infra/Whats my IP ?.md:13`", encoding="utf-8")

    def tearDown(self) -> None:
        """Remove the temporary vault."""
        self._tmp.cleanup()

    def test_dry_run_writes_nothing(self) -> None:
        """A dry run plans the rename and link updates but keeps the files."""
        report = run(root=self.docs, link_root=self.project, dry_run=True)
        self.assertFalse(report.applied)
        self.assertEqual(len(report.renames), 1)
        self.assertEqual(len(report.rewritten_files), 2)
        self.assertTrue((self.docs / "infra" / "Whats my IP ?.md").exists())

    def test_apply_renames_and_rewrites(self) -> None:
        """A real pass renames the file and fixes both link sites."""
        report = run(root=self.docs, link_root=self.project)
        self.assertTrue(report.applied)
        self.assertEqual(
            (self.docs / "infra" / "Whats my IP.md").read_text(encoding="utf-8"),
            "# Whats my IP ?\n\nip",
        )
        self.assertEqual((self.docs / "index.md").read_text(encoding="utf-8"), "see [[Whats my IP]]")
        self.assertIn("Whats my IP.md:13", (self.project / "ISSUES.md").read_text(encoding="utf-8"))
        self.assertTrue(plan(self.docs).clean)

    def test_nested_folder_and_file(self) -> None:
        """A bad file inside a bad folder is renamed child first."""
        bad_dir = self.docs / "a: b"
        bad_dir.mkdir()
        (bad_dir / "c?.md").write_text("c", encoding="utf-8")
        run(root=self.docs, link_root=self.project)
        self.assertTrue((self.docs / "a - b" / "c.md").exists())

    def test_collision_blocks_everything(self) -> None:
        """A sanitized name that already exists is a conflict and nothing is written."""
        (self.docs / "infra" / "Whats my IP.md").write_text("other", encoding="utf-8")
        report = run(root=self.docs, link_root=self.project)
        self.assertFalse(report.applied)
        self.assertEqual(len(report.conflicts), 1)
        self.assertTrue((self.docs / "infra" / "Whats my IP ?.md").exists())
        self.assertIn("[[Whats my IP ?]]", (self.docs / "index.md").read_text(encoding="utf-8"))

    def test_case_only_difference_is_conflict(self) -> None:
        """Two names differing only in case are reported."""
        (self.docs / "Readme.md").write_text("a", encoding="utf-8")
        (self.docs / "README.md").write_text("b", encoding="utf-8")
        self.assertTrue(any("differ only in case" in c for c in plan(self.docs).conflicts))

    def test_obsidian_folder_skipped(self) -> None:
        """Names inside ``.obsidian`` are never checked."""
        (self.docs / ".obsidian").mkdir()
        (self.docs / ".obsidian" / "x?.json").write_text("{}", encoding="utf-8")
        names = [r.old.name for r in plan(self.docs).renames]
        self.assertNotIn("x?.json", names)


if __name__ == "__main__":
    unittest.main()
