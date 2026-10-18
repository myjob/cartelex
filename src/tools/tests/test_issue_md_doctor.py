"""Unit tests for :mod:`tools.issue_md_doctor`.

Runs in isolation: every fixture is an in-memory Markdown string or a
temporary file, so no database, LLM or network boundary is touched.
"""

import tempfile
import unittest
from pathlib import Path

from tools.issue_md_doctor import (
    DoctorReport,
    IssueItem,
    IssuesFileError,
    assign_numbers,
    count_by_severity,
    format_report,
    next_free_number,
    parse_items,
    process,
    run,
)

#: Already numbered, so the relocation tests exercise the move alone.
SIMPLE_DOC = """## Open Issues
- [ ] 12 #high 20260101 still open
- [x] 11 #med 20260102 done already
- [ ] 10 #low 20260103 also open

## Closed Issues

- [x] 9 #high 20251201 an older closure
"""

#: The shape a file has before its first numbering pass.
UNNUMBERED_DOC = """## Open Issues
- [ ] #high 20260103 newest
- [ ] #med 20260102 middle
- [ ] #low 20260101 oldest

## Closed Issues

- [x] #high 20251201 an older closure
"""


class ParseItemsTest(unittest.TestCase):
    """Item boundaries, severity extraction and preamble handling."""

    def test_continuation_lines_belong_to_preceding_item(self) -> None:
        """Indented and unindented lines attach to the item above them."""
        body = [
            "- [ ] #high first",
            "      a code excerpt",
            "",
            "not indented but still a continuation",
            "- [ ] #low second",
        ]
        preamble, items = parse_items(body)

        self.assertEqual([], preamble)
        self.assertEqual(2, len(items))
        self.assertEqual(4, len(items[0].lines))
        self.assertEqual(["- [ ] #low second"], items[1].lines)

    def test_lines_before_first_item_become_preamble(self) -> None:
        """Chapter prose ahead of the first checkbox is kept separate."""
        preamble, items = parse_items(["", "some prose", "- [ ] #high item"])

        self.assertEqual(["", "some prose"], preamble)
        self.assertEqual(1, len(items))

    def test_severity_and_state_are_extracted(self) -> None:
        """The tag right after the checkbox is the severity; [x] means closed."""
        _, items = parse_items(["- [x] #med 20260102 done", "- [ ] no tag here"])

        self.assertTrue(items[0].closed)
        self.assertEqual("#med", items[0].severity)
        self.assertFalse(items[1].closed)
        self.assertIsNone(items[1].severity)

    def test_deeper_list_markers_are_not_items(self) -> None:
        """Nested checkboxes stay part of their parent item."""
        _, items = parse_items(["- [ ] #high parent", "  - [ ] nested child"])

        self.assertEqual(1, len(items))
        self.assertEqual(2, len(items[0].lines))

    def test_start_index_is_absolute(self) -> None:
        """The offset makes item positions refer to the whole file."""
        _, items = parse_items(["- [ ] #high item"], offset=7)

        self.assertEqual(7, items[0].start)

    def test_number_is_read_from_between_checkbox_and_severity(self) -> None:
        """The severity tag is still found once the number is stripped."""
        _, items = parse_items(["- [ ] 42 #high 20260919 numbered"])

        self.assertEqual(42, items[0].number)
        self.assertEqual("#high", items[0].severity)

    def test_unnumbered_item_has_no_number(self) -> None:
        """A hand-written item carries None until a pass numbers it."""
        _, items = parse_items(["- [ ] #high 20260919 fresh"])

        self.assertIsNone(items[0].number)

    def test_eight_digit_datetag_is_not_read_as_a_number(self) -> None:
        """An untagged item opening with a datetag stays unnumbered."""
        _, items = parse_items(["- [ ] 20260705 FAILED (failures=8)"])

        self.assertIsNone(items[0].number)
        self.assertIsNone(items[0].severity)

    def test_number_is_read_on_a_closed_item_too(self) -> None:
        """A number survives the checkbox being flipped."""
        _, items = parse_items(["- [x] 7 #low 20260919 done"])

        self.assertEqual(7, items[0].number)


class CountBySeverityTest(unittest.TestCase):
    """Severity tallying and report ordering."""

    def test_known_tags_are_ordered_by_severity(self) -> None:
        """Counts come back highest-first, not in encounter order."""
        _, items = parse_items(
            [
                "- [ ] #low a",
                "- [ ] #high b",
                "- [ ] #highest c",
                "- [ ] #high d",
                "- [ ] #med e",
            ]
        )

        self.assertEqual(
            ["#highest", "#high", "#med", "#low"],
            list(count_by_severity(items)),
        )
        self.assertEqual(2, count_by_severity(items)["#high"])

    def test_unknown_tags_sort_after_known_ones_and_before_untagged(self) -> None:
        """Vocabulary drift stays visible instead of being folded into untagged."""
        _, items = parse_items(
            ["- [ ] #high a", "- [ ] #urgent b", "- [ ] #blocked c", "- [ ] plain d"]
        )

        self.assertEqual(
            ["#high", "#blocked", "#urgent", "(untagged)"],
            list(count_by_severity(items)),
        )

    def test_absent_labels_are_omitted(self) -> None:
        """A severity with no items does not appear with a zero count."""
        _, items = parse_items(["- [ ] #high a"])

        self.assertEqual({"#high": 1}, count_by_severity(items))


def _item(line: str) -> IssueItem:
    """Build a single-line item from *line* the way the parser would."""
    _, items = parse_items([line])
    return items[0]


class NextFreeNumberTest(unittest.TestCase):
    """Where the number sequence continues."""

    def test_nothing_numbered_yet_starts_at_one(self) -> None:
        """A file's first pass begins the sequence at 1."""
        self.assertEqual(1, next_free_number([_item("- [ ] #high a")]))

    def test_sequence_continues_above_the_largest_number(self) -> None:
        """Gaps below the maximum are not filled, so numbers are not reused."""
        items = [_item("- [ ] 9 #high a"), _item("- [ ] 3 #low b")]

        self.assertEqual(10, next_free_number(items))

    def test_closed_items_hold_their_numbers_against_reuse(self) -> None:
        """A closure must not free its number for a different issue."""
        items = [_item("- [ ] 2 #high open"), _item("- [x] 40 #low closed")]

        self.assertEqual(41, next_free_number(items))


class AssignNumbersTest(unittest.TestCase):
    """Reverse-order assignment and the write-once guarantee."""

    def test_lowest_number_goes_to_the_bottom_item(self) -> None:
        """Reverse order means the oldest item at the bottom gets 1."""
        targets = [_item("- [ ] #high newest"), _item("- [ ] #low oldest")]
        assigned = assign_numbers(targets, targets)

        self.assertEqual([1, 2], [item.number for item in assigned])
        self.assertEqual("- [ ] 2 #high newest", targets[0].lines[0])
        self.assertEqual("- [ ] 1 #low oldest", targets[1].lines[0])

    def test_existing_numbers_are_left_verbatim(self) -> None:
        """Only the unnumbered item is touched, and the rest keep their line."""
        keep = _item("- [ ] 5 #high already numbered")
        fresh = _item("- [ ] #low new arrival")
        targets = [fresh, keep]
        assigned = assign_numbers(targets, targets)

        self.assertEqual([fresh], assigned)
        self.assertEqual("- [ ] 5 #high already numbered", keep.lines[0])
        self.assertEqual(6, fresh.number)

    def test_continuation_lines_are_untouched(self) -> None:
        """Only the checkbox line is rewritten, never the item's body."""
        _, items = parse_items(["- [ ] #high head", "      detail line"])
        assign_numbers(items, items)

        self.assertEqual("      detail line", items[0].lines[1])

    def test_number_lands_ahead_of_a_leading_datetag(self) -> None:
        """An untagged item keeps its datetag, with the number in front."""
        item = _item("- [ ] 20260705 FAILED (failures=8)")
        assign_numbers([item], [item])

        self.assertEqual("- [ ] 1 20260705 FAILED (failures=8)", item.lines[0])

    def test_nothing_to_do_returns_an_empty_list(self) -> None:
        """A fully numbered chapter reports no assignment."""
        targets = [_item("- [ ] 1 #high a")]

        self.assertEqual([], assign_numbers(targets, targets))


class NumberingProcessTest(unittest.TestCase):
    """Numbering as it behaves over a whole file."""

    def test_first_pass_numbers_open_items_bottom_up(self) -> None:
        """The oldest open item takes 1 and the newest the highest number."""
        new_content, report = process(UNNUMBERED_DOC)
        open_chapter = new_content.split("## Closed Issues")[0]

        self.assertEqual(3, len(report.numbered))
        self.assertIn("- [ ] 3 #high 20260103 newest", open_chapter)
        self.assertIn("- [ ] 2 #med 20260102 middle", open_chapter)
        self.assertIn("- [ ] 1 #low 20260101 oldest", open_chapter)

    def test_numbering_alone_marks_the_file_as_changed(self) -> None:
        """A file with nothing to move is still rewritten to carry numbers."""
        doc = "## Open Issues\n- [ ] #high open\n\n## Closed Issues\n"
        new_content, report = process(doc)

        self.assertTrue(report.changed)
        self.assertEqual([], report.moved)
        self.assertIn("- [ ] 1 #high open", new_content)

    def test_numbering_is_idempotent(self) -> None:
        """A second pass finds every open item numbered and changes nothing."""
        once, _ = process(UNNUMBERED_DOC)
        twice, report = process(once)

        self.assertFalse(report.changed)
        self.assertEqual(once, twice)
        self.assertEqual([], report.numbered)

    def test_item_added_on_top_takes_the_next_free_number(self) -> None:
        """Prepending an issue leaves every existing number untouched."""
        once, _ = process(UNNUMBERED_DOC)
        with_new = once.replace(
            "## Open Issues\n", "## Open Issues\n- [ ] #high 20260104 brand new\n", 1
        )
        twice, report = process(with_new)

        self.assertEqual(1, len(report.numbered))
        self.assertIn("- [ ] 4 #high 20260104 brand new", twice)
        self.assertIn("- [ ] 3 #high 20260103 newest", twice)
        self.assertIn("- [ ] 1 #low 20260101 oldest", twice)

    def test_closed_items_in_the_open_chapter_are_not_numbered(self) -> None:
        """A resolved item on its way out gains no number."""
        doc = (
            "## Open Issues\n"
            "- [x] #high resolved\n"
            "- [ ] #low stays\n"
            "\n"
            "## Closed Issues\n"
        )
        new_content, report = process(doc)

        self.assertEqual(1, len(report.numbered))
        self.assertIn("- [x] #high resolved", new_content.split("## Closed Issues")[1])

    def test_a_closed_item_keeps_the_number_it_had(self) -> None:
        """Numbers travel with the issue, so a citation still resolves."""
        new_content, _ = process(SIMPLE_DOC)

        self.assertIn("- [x] 11 #med 20260102 done already", new_content)

    def test_closed_chapter_numbers_are_not_handed_out_again(self) -> None:
        """The sequence clears the closed chapter's maximum."""
        doc = (
            "## Open Issues\n"
            "- [ ] #high fresh\n"
            "\n"
            "## Closed Issues\n"
            "- [x] 88 #low long done\n"
        )
        new_content, _ = process(doc)

        self.assertIn("- [ ] 89 #high fresh", new_content)

    def test_existing_closed_chapter_is_not_renumbered(self) -> None:
        """Only open items are numbered; history is left as written."""
        new_content, report = process(UNNUMBERED_DOC)
        closed_chapter = new_content.split("## Closed Issues")[1]

        self.assertIn("- [x] #high 20251201 an older closure", closed_chapter)
        self.assertEqual([], report.moved)

    def test_duplicate_number_is_reported_not_repaired(self) -> None:
        """A hand-made collision is left alone but warned about."""
        doc = (
            "## Open Issues\n"
            "- [ ] 5 #high one\n"
            "- [ ] 5 #low two\n"
            "\n"
            "## Closed Issues\n"
        )
        with self.assertLogs("tools.issue_md_doctor", level="WARNING") as logged:
            new_content, report = process(doc)

        self.assertFalse(report.changed)
        self.assertEqual(doc, new_content)
        self.assertIn("Issue number 5", "\n".join(logged.output))


class ProcessTest(unittest.TestCase):
    """Relocation of closed items and the resulting report."""

    def test_closed_item_moves_to_top_of_closed_chapter(self) -> None:
        """The moved item precedes items the closed chapter already held."""
        new_content, report = process(SIMPLE_DOC)

        self.assertTrue(report.changed)
        self.assertEqual(1, len(report.moved))
        self.assertNotIn(
            "- [x] 11 #med 20260102 done already",
            new_content.split("## Closed Issues")[0],
        )

        closed_chapter = new_content.split("## Closed Issues")[1]
        moved_at = closed_chapter.index("20260102 done already")
        existing_at = closed_chapter.index("20251201 an older closure")
        self.assertLess(moved_at, existing_at)

    def test_open_items_keep_their_order_and_content(self) -> None:
        """Removing an item in the middle does not disturb its neighbours."""
        new_content, _ = process(SIMPLE_DOC)
        open_chapter = new_content.split("## Closed Issues")[0]

        self.assertIn("- [ ] 12 #high 20260101 still open", open_chapter)
        self.assertIn("- [ ] 10 #low 20260103 also open", open_chapter)
        self.assertLess(
            open_chapter.index("20260101"), open_chapter.index("20260103")
        )

    def test_report_counts_only_items_that_stay_open(self) -> None:
        """The closed item is excluded from the open tally."""
        _, report = process(SIMPLE_DOC)

        self.assertEqual(2, report.open_count)
        self.assertEqual({"#high": 1, "#low": 1}, report.severity_counts)

    def test_closed_count_includes_relocated_and_existing_items(self) -> None:
        """The closed tally reflects the chapter as it stands after the move."""
        _, report = process(SIMPLE_DOC)

        self.assertEqual(2, report.closed_count)

    def test_closed_count_is_stable_across_a_second_pass(self) -> None:
        """Re-running adds nothing, so the closed tally must not grow."""
        once, first = process(SIMPLE_DOC)
        _, second = process(once)

        self.assertEqual(first.closed_count, second.closed_count)

    def test_multi_line_item_moves_whole_block(self) -> None:
        """Continuation lines travel with the item they belong to."""
        doc = (
            "## Open Issues\n"
            "- [x] #high closed with detail\n"
            "      supporting line one\n"
            "      supporting line two\n"
            "- [ ] #low stays\n"
            "\n"
            "## Closed Issues\n"
        )
        new_content, report = process(doc)
        closed_chapter = new_content.split("## Closed Issues")[1]

        self.assertEqual(3, len(report.moved[0].lines))
        self.assertIn("supporting line one", closed_chapter)
        self.assertIn("supporting line two", closed_chapter)
        self.assertNotIn("supporting line", new_content.split("## Closed Issues")[0])

    def test_no_closed_items_leaves_content_byte_identical(self) -> None:
        """A clean file is never rewritten, so diffs stay meaningful."""
        doc = "## Open Issues\n- [ ] 3 #high open\n\n## Closed Issues\n- [x] 2 #low old\n"
        new_content, report = process(doc)

        self.assertFalse(report.changed)
        self.assertEqual(doc, new_content)

    def test_pass_is_idempotent(self) -> None:
        """Running the doctor twice changes nothing the second time."""
        once, _ = process(SIMPLE_DOC)
        twice, report = process(once)

        self.assertFalse(report.changed)
        self.assertEqual(once, twice)

    def test_chapter_preamble_survives_the_move(self) -> None:
        """Prose between the heading and the first item stays in place."""
        doc = (
            "## Open Issues\n"
            "\n"
            "sorted newest first\n"
            "- [x] #high done\n"
            "\n"
            "## Closed Issues\n"
        )
        new_content, _ = process(doc)
        open_chapter = new_content.split("## Closed Issues")[0]

        self.assertIn("sorted newest first", open_chapter)

    def test_deeper_headings_inside_bodies_are_not_chapters(self) -> None:
        """A '### Closed Issues' inside an item body cannot split the file."""
        doc = (
            "## Open Issues\n"
            "- [ ] #high mentions a heading\n"
            "### Closed Issues\n"
            "- [x] #med genuinely closed\n"
            "\n"
            "## Closed Issues\n"
        )
        _, report = process(doc)

        self.assertEqual(1, report.open_count)
        self.assertEqual(1, len(report.moved))

    def test_missing_open_chapter_raises(self) -> None:
        """A file without the open chapter is rejected rather than guessed at."""
        with self.assertRaises(IssuesFileError):
            process("## Closed Issues\n- [x] #high old\n")

    def test_missing_closed_chapter_raises(self) -> None:
        """A file without the closed chapter is rejected."""
        with self.assertRaises(IssuesFileError):
            process("## Open Issues\n- [x] #high done\n")

    def test_reversed_chapter_order_raises(self) -> None:
        """Moving items upward is not supported and fails loudly."""
        with self.assertRaises(IssuesFileError):
            process("## Closed Issues\n- [x] a\n\n## Open Issues\n- [x] b\n")

    def test_trailing_newline_is_preserved(self) -> None:
        """The file keeps -- or keeps lacking -- its final newline."""
        without = "## Open Issues\n- [x] #high done\n\n## Closed Issues"
        new_content, _ = process(without)

        self.assertFalse(new_content.endswith("\n"))


class RunTest(unittest.TestCase):
    """File-level behaviour, including the dry-run guard."""

    def setUp(self) -> None:
        """Create a temporary issues file for each test."""
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "ISSUES.md"
        self.path.write_text(SIMPLE_DOC, encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def test_run_rewrites_the_file(self) -> None:
        """A real pass persists the relocation."""
        report = run(path=self.path)

        self.assertTrue(report.written)
        self.assertIn("20260102", self.path.read_text(encoding="utf-8").split("## Closed Issues")[1])

    def test_dry_run_reports_without_writing(self) -> None:
        """--dry-run leaves the file untouched but still names what would move."""
        report = run(path=self.path, dry_run=True)

        self.assertTrue(report.changed)
        self.assertFalse(report.written)
        self.assertEqual(1, len(report.moved))
        self.assertEqual(SIMPLE_DOC, self.path.read_text(encoding="utf-8"))

    def test_report_path_is_the_processed_file(self) -> None:
        """The report names the file it came from, for multi-file use."""
        report = run(path=self.path, dry_run=True)

        self.assertEqual(self.path, report.path)

    def test_missing_file_raises(self) -> None:
        """A wrong --path fails with the standard filesystem error."""
        with self.assertRaises(FileNotFoundError):
            run(path=self.path.parent / "nope.md")


class FormatReportTest(unittest.TestCase):
    """Console rendering of the report."""

    def test_clean_file_reports_nothing_to_move(self) -> None:
        """The wording distinguishes 'nothing to do' from 'moved zero'."""
        report = DoctorReport(
            path=Path("ISSUES.md"),
            open_count=2,
            closed_count=7,
            severity_counts={"#high": 1, "(untagged)": 1},
            changed=False,
        )
        rendered = format_report(report)

        self.assertIn("no closed items left in 'Open Issues'", rendered)
        self.assertIn("Open issues: 2", rendered)
        self.assertIn("Closed issues: 7", rendered)
        self.assertIn("(untagged)", rendered)

    def test_numbered_span_is_reported(self) -> None:
        """The reader sees how many numbers were handed out, and which."""
        _, report = process(UNNUMBERED_DOC)
        report.path = Path("ISSUES.md")
        report.written = True

        self.assertIn("3 open item(s) numbered: 1-3", format_report(report))

    def test_single_number_is_reported_without_a_span(self) -> None:
        """One new issue reads as one number, not as a degenerate range."""
        _, report = process("## Open Issues\n- [ ] #high a\n\n## Closed Issues\n")
        report.path = Path("ISSUES.md")

        self.assertIn("1 open item(s) numberable (dry run): 1", format_report(report))

    def test_dry_run_wording_differs_from_a_real_move(self) -> None:
        """The reader can tell whether the file was actually rewritten."""
        _, report = process(SIMPLE_DOC)
        report.path = Path("ISSUES.md")

        self.assertIn("movable (dry run)", format_report(report))
        report.written = True
        self.assertIn("1 closed item(s) moved", format_report(report))


if __name__ == "__main__":
    unittest.main()
