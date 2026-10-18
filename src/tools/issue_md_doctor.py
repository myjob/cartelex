"""Maintenance pass over ``ISSUES.md``.

The project tracks work as GitHub-flavoured task list items under two chapter
headings, ``## Open Issues`` and ``## Closed Issues``. Items are solved in
place by flipping ``- [ ]`` to ``- [x]``, which means resolved work keeps
accumulating in the open chapter and distorts any count taken from it.

This module performs three jobs:

1. Number every unnumbered open item, so an issue can be cited by a short
   handle instead of its opening words. Numbers are assigned in reverse
   document order -- the bottom-most, oldest unnumbered item takes the lowest
   new number -- and are never reassigned: once written, a number belongs to
   that issue for good, and an item pasted on top of the chapter simply takes
   the next free one. Only open items are numbered; an item that later closes
   keeps the number it had.
2. Relocate every ``- [x]`` item found in the open chapter to the top of the
   closed chapter, newest-resolved-first, preserving each item's body verbatim.
3. Report how many issues remain open, broken down by the ``#severity`` tag
   that follows the number where one is present, and how many issues the
   closed chapter holds once the relocation is done.

An "item" is the line starting with ``- [ ]`` / ``- [x]`` plus every following
line up to the next such line -- the file's own convention, under which log
excerpts, tables and code blocks indented or not are continuations of the item
above them.

A numbered item reads ``- [ ] 42 #high 20260919 the summary``: the number sits
between the checkbox and the severity tag, exactly where the project's
hand-written ``- [ ] #high <datetag>`` convention leaves room for it. Only a
run of at most four digits counts as a number, so an eight-digit datetag on an
untagged item is never mistaken for one.

Usage (from project root):

    .venv/bin/python src/tools/issue_md_doctor.py
    .venv/bin/python src/tools/issue_md_doctor.py --dry-run
    .venv/bin/python src/tools/issue_md_doctor.py --path docs/OTHER_ISSUES.md
"""

import argparse
import logging
import re
from pathlib import Path

from pydantic import BaseModel, Field

from config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_ISSUES_PATH: Path = settings.PRJ_ROOT_DIR / "ISSUES.md"
OPEN_CHAPTER_TITLE: str = "Open Issues"
CLOSED_CHAPTER_TITLE: str = "Closed Issues"
UNTAGGED_LABEL: str = "(untagged)"

#: Severity tags in report order. Tags outside this list are reported after
#: the known ones so vocabulary drift stays visible instead of being folded away.
SEVERITY_ORDER: tuple[str, ...] = ("#highest", "#high", "#med", "#low")

_ITEM_RE = re.compile(r"^- \[(?P<state>[ xX])\]\s?(?P<body>.*)$")
#: An issue number directly after the checkbox. Bounded to four digits and
#: guarded against a following digit so an eight-digit datetag cannot match.
_NUMBER_RE = re.compile(r"^(?P<number>\d{1,4})(?!\d)(?:\s+|$)")
_SEVERITY_RE = re.compile(r"^(?P<severity>#[A-Za-z][A-Za-z0-9_-]*)\b")
_HEADING_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")


class IssueItem(BaseModel):
    """A single task list item together with its continuation lines.

    Attributes:
        closed: True when the checkbox is ``[x]``.
        number: The issue number after the checkbox, or None when unnumbered.
        severity: The ``#tag`` after the number, or None when absent.
        lines: The item's raw lines, first one being the ``- [ ]`` line.
        start: Zero-based index of the first line within the source file.
    """

    closed: bool = Field(description="True when the checkbox reads [x].")
    number: int | None = Field(
        default=None, description="Issue number after the checkbox, None when unnumbered."
    )
    severity: str | None = Field(
        default=None, description="Severity tag such as #high, or None when untagged."
    )
    lines: list[str] = Field(description="Raw item lines, continuations included.")
    start: int = Field(description="Zero-based line index of the item in the file.")

    @property
    def title(self) -> str:
        """Return the item's first line, trimmed for log and report use."""
        return self.lines[0].strip()


class DoctorReport(BaseModel):
    """Outcome of one doctor pass over an issues file.

    Attributes:
        path: The file that was inspected.
        open_count: Number of ``- [ ]`` items left in the open chapter.
        closed_count: Number of items the closed chapter holds after the move.
        severity_counts: Open item count per severity label, in report order.
        numbered: Open items that received a number in this pass, lowest first.
        moved: Items relocated from the open to the closed chapter.
        changed: True when the file content would differ after the pass.
        written: True when the new content was actually persisted.
    """

    path: Path = Field(description="Issues file inspected by this pass.")
    open_count: int = Field(description="Open items remaining after the move.")
    closed_count: int = Field(
        description="Items in the closed chapter after the move, relocations included."
    )
    severity_counts: dict[str, int] = Field(
        description="Open item counts keyed by severity label, in report order."
    )
    numbered: list[IssueItem] = Field(
        default_factory=list,
        description="Open items numbered by this pass, in assignment order.",
    )
    moved: list[IssueItem] = Field(
        default_factory=list, description="Items moved into the closed chapter."
    )
    changed: bool = Field(description="True when the pass produced different content.")
    written: bool = Field(default=False, description="True when the file was rewritten.")


class IssuesFileError(RuntimeError):
    """Raised when the issues file lacks a chapter the doctor needs."""


def _find_chapter(lines: list[str], title: str) -> int:
    """Return the index of the ``## <title>`` heading line.

    Matching is case-insensitive and restricted to level-two headings, so the
    many deeper headings pasted into issue bodies cannot be mistaken for a
    chapter boundary.

    Args:
        lines: The file's lines, without trailing newlines.
        title: Chapter title to locate, for example ``Open Issues``.

    Returns:
        Zero-based index of the heading line.

    Raises:
        IssuesFileError: If no such heading exists.
    """
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and match.group("title").casefold() == title.casefold():
            return index
    raise IssuesFileError(f"No '## {title}' chapter found")


def parse_items(lines: list[str], offset: int = 0) -> tuple[list[str], list[IssueItem]]:
    """Split a chapter body into its leading prose and its task list items.

    Args:
        lines: Chapter body lines, excluding the heading itself.
        offset: Line index of ``lines[0]`` within the whole file, used to make
            :attr:`IssueItem.start` absolute.

    Returns:
        A ``(preamble, items)`` pair. ``preamble`` holds the lines before the
        first item; each item owns every line up to the next item start.
    """
    preamble: list[str] = []
    items: list[IssueItem] = []

    for index, line in enumerate(lines):
        match = _ITEM_RE.match(line)
        if match:
            body = match.group("body").strip()
            number_match = _NUMBER_RE.match(body)
            if number_match:
                body = body[number_match.end() :]
            severity_match = _SEVERITY_RE.match(body)
            items.append(
                IssueItem(
                    closed=match.group("state").lower() == "x",
                    number=int(number_match.group("number")) if number_match else None,
                    severity=severity_match.group("severity") if severity_match else None,
                    lines=[line],
                    start=offset + index,
                )
            )
        elif items:
            items[-1].lines.append(line)
        else:
            preamble.append(line)

    return preamble, items


def _strip_trailing_blanks(lines: list[str]) -> list[str]:
    """Return *lines* without its trailing blank lines."""
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _with_number(line: str, number: int) -> str:
    """Return *line* with *number* written between the checkbox and the body.

    Args:
        line: An item's first line, expected to match :data:`_ITEM_RE`.
        number: The issue number to write.

    Returns:
        The rewritten line, or *line* unchanged when it is not an item line.
    """
    match = _ITEM_RE.match(line)
    if not match:
        return line
    prefix = f"- [{match.group('state')}] {number}"
    body = match.group("body")
    return f"{prefix} {body}" if body else prefix


def next_free_number(items: list[IssueItem]) -> int:
    """Return the first number no item in *items* has taken.

    The sequence only ever continues upwards, so a number freed by a closure
    is not handed out again and a reference to "issue 42" keeps pointing at the
    same issue for the life of the file.

    Args:
        items: Every item whose number is already spoken for, open and closed.

    Returns:
        One above the largest number found, or 1 when nothing is numbered yet.
    """
    return max((item.number for item in items if item.number is not None), default=0) + 1


def assign_numbers(targets: list[IssueItem], known: list[IssueItem]) -> list[IssueItem]:
    """Write an issue number onto every target that lacks one.

    Numbering runs in reverse document order, so the oldest unnumbered item at
    the bottom of the chapter takes the lowest new number and an item pasted on
    top takes the highest. Items that already carry a number are left verbatim.

    Args:
        targets: Items eligible for a number, in document order. Mutated in
            place: the first line of each newly numbered item is rewritten and
            :attr:`IssueItem.number` is set.
        known: Every item whose number is already taken, the targets included,
            used to decide where the sequence continues.

    Returns:
        The targets that received a number, in the order they were assigned
        -- lowest number first.
    """
    number = next_free_number(known)
    assigned: list[IssueItem] = []
    for item in reversed(targets):
        if item.number is not None:
            continue
        item.number = number
        item.lines[0] = _with_number(item.lines[0], number)
        assigned.append(item)
        number += 1
    return assigned


def _warn_duplicate_numbers(items: list[IssueItem]) -> None:
    """Log a warning for every issue number carried by more than one item.

    A duplicate can only come from a hand edit; the doctor never creates one.
    It is reported rather than repaired, since only the author knows which of
    the two items the number was meant for.

    Args:
        items: Items to check, open and closed together.
    """
    tallied: dict[int, int] = {}
    for item in items:
        if item.number is not None:
            tallied[item.number] = tallied.get(item.number, 0) + 1
    for number, count in sorted(tallied.items()):
        if count > 1:
            logger.warning("Issue number %d is carried by %d items", number, count)


def count_by_severity(items: list[IssueItem]) -> dict[str, int]:
    """Tally items per severity label, ordered for reporting.

    Known tags come first in :data:`SEVERITY_ORDER`, then any unknown tag in
    alphabetical order, then untagged items. Labels with a zero count are
    omitted, so a vanished severity does not clutter the report.

    Args:
        items: Items to tally.

    Returns:
        An insertion-ordered mapping of severity label to count.
    """
    tallied: dict[str, int] = {}
    for item in items:
        label = item.severity or UNTAGGED_LABEL
        tallied[label] = tallied.get(label, 0) + 1

    unknown = sorted(
        label
        for label in tallied
        if label not in SEVERITY_ORDER and label != UNTAGGED_LABEL
    )
    ordered_labels = [*SEVERITY_ORDER, *unknown, UNTAGGED_LABEL]
    return {label: tallied[label] for label in ordered_labels if label in tallied}


def process(content: str) -> tuple[str, DoctorReport]:
    """Number the open items, move the closed ones, and tally what stays open.

    Args:
        content: Full text of the issues file.

    Returns:
        A ``(new_content, report)`` pair. ``new_content`` equals ``content``
        when every open item already carries a number and the open chapter
        holds no closed item.

    Raises:
        IssuesFileError: If either chapter heading is missing, or the closed
            chapter does not follow the open one.
    """
    trailing_newline = content.endswith("\n")
    lines = content.split("\n")
    if trailing_newline:
        lines = lines[:-1]

    open_at = _find_chapter(lines, OPEN_CHAPTER_TITLE)
    closed_at = _find_chapter(lines, CLOSED_CHAPTER_TITLE)
    if closed_at < open_at:
        raise IssuesFileError(
            f"'## {CLOSED_CHAPTER_TITLE}' precedes '## {OPEN_CHAPTER_TITLE}'"
        )

    open_preamble, open_items = parse_items(lines[open_at + 1 : closed_at], open_at + 1)
    closed_body = lines[closed_at + 1 :]
    _, closed_items = parse_items(closed_body, closed_at + 1)

    moved = [item for item in open_items if item.closed]
    staying = [item for item in open_items if not item.closed]

    _warn_duplicate_numbers([*open_items, *closed_items])
    numbered = assign_numbers(staying, [*open_items, *closed_items])

    report = DoctorReport(
        path=Path(),
        open_count=len(staying),
        closed_count=len(closed_items) + len(moved),
        severity_counts=count_by_severity(staying),
        numbered=numbered,
        moved=moved,
        changed=bool(moved or numbered),
    )
    if not report.changed:
        return content, report

    for item in numbered:
        logger.info("Numbered open item %d: %s", item.number, item.title)
    for item in moved:
        logger.info("Moving closed item to '%s': %s", CLOSED_CHAPTER_TITLE, item.title)

    relocated: list[str] = []
    for item in moved:
        relocated.extend(_strip_trailing_blanks(item.lines))
        relocated.append("")

    new_lines = [
        lines[open_at],
        *open_preamble,
    ]
    for item in staying:
        new_lines.extend(item.lines)
    new_lines.append(lines[closed_at])
    if relocated:
        new_lines.append("")
        new_lines.extend(relocated)
    new_lines.extend(closed_body)

    # The blank line appended after each relocated block must not grow the
    # end of the file when the closed chapter was empty.
    if lines and lines[-1].strip():
        new_lines = _strip_trailing_blanks(new_lines)

    new_content = "\n".join(new_lines)
    if trailing_newline:
        new_content += "\n"
    return new_content, report


def format_report(report: DoctorReport) -> str:
    """Render a doctor report as plain text for the console.

    Args:
        report: The report to render.

    Returns:
        A multi-line string without a trailing newline.
    """
    if report.moved:
        action = "moved" if report.written else "movable (dry run)"
        moved_line = f"{len(report.moved)} closed item(s) {action} to '{CLOSED_CHAPTER_TITLE}'"
    else:
        moved_line = f"no closed items left in '{OPEN_CHAPTER_TITLE}'"

    lines = [f"{report.path}: {moved_line}", ""]
    for item in report.moved:
        lines.append(f"  moved ({len(item.lines)} lines) {item.title[:100]}")
    if report.moved:
        lines.append("")

    if report.numbered:
        action = "numbered" if report.written else "numberable (dry run)"
        first = report.numbered[0].number
        last = report.numbered[-1].number
        span = f"{first}" if first == last else f"{first}-{last}"
        lines.append(f"{len(report.numbered)} open item(s) {action}: {span}")
        lines.append("")

    lines.append(f"Open issues: {report.open_count}")
    width = max((len(label) for label in report.severity_counts), default=0)
    for label, count in report.severity_counts.items():
        lines.append(f"  {label.ljust(width)}  {count:>4}")
    lines.append(f"Closed issues: {report.closed_count}")
    return "\n".join(lines)


def run(path: Path = DEFAULT_ISSUES_PATH, dry_run: bool = False) -> DoctorReport:
    """Run one doctor pass over *path*.

    Args:
        path: Issues file to process.
        dry_run: When True, report what would change without writing. The
            numbers in the report are the ones a real pass would assign.

    Returns:
        The report for this pass.

    Raises:
        FileNotFoundError: If *path* does not exist.
        IssuesFileError: If the file lacks the open or closed chapter.
    """
    content = path.read_text(encoding="utf-8")
    new_content, report = process(content)
    report.path = path

    if report.changed and not dry_run:
        path.write_text(new_content, encoding="utf-8")
        report.written = True
        logger.info("Rewrote %s", path)

    return report


def main() -> None:
    """Parse arguments, run the doctor pass, and print the report."""
    parser = argparse.ArgumentParser(
        prog="issue-md-doctor",
        description=(
            "Number the open items, move closed items to the top of the closed "
            "chapter in an issues file, and report the remaining open issues "
            "per severity."
        ),
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_ISSUES_PATH,
        help=f"Issues file to process (default: {DEFAULT_ISSUES_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be numbered and moved without rewriting the file.",
    )
    args = parser.parse_args()

    report = run(path=args.path, dry_run=args.dry_run)
    print(format_report(report))


if __name__ == "__main__":
    main()
