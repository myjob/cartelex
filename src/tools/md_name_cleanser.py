"""Make every file and folder name under ``docs/`` safe to clone on Windows.

The Obsidian vault in ``docs/`` is authored on Linux, where a file name may
hold any byte except ``/``. Git on Windows refuses to check out a path that
NTFS / Win32 rejects, and the clone then fails or leaves files missing. This
module finds such names, renames them to a Windows-acceptable spelling and
rewrites the Markdown links that point at them, so the vault stays navigable.

A name segment is rejected on Windows when it

- contains one of ``< > : " / \\ | ? *`` or a control character (0x00-0x1F),
- ends with a space or a dot,
- is a reserved device name (``CON``, ``PRN``, ``AUX``, ``NUL``,
  ``COM0``-``COM9``, ``LPT0``-``LPT9``), with or without an extension.

Two names that differ only in case also collide on Windows; they are reported
as conflicts and never renamed automatically, because which one should win is
an owner decision. Non-ASCII names (em dashes, non-breaking hyphens) are legal
on NTFS and only reported as warnings.

A renamed note keeps its original title: the old file name (without
``.md``) is inserted as its ``#`` top heading, after any YAML front matter,
unless the note already opens with exactly that heading.

Links rewritten in every ``.md`` file under the project root (``.git``,
``.venv``, ``.obsidian`` and similar folders skipped), per renamed segment:

- wikilinks ``[[old stem]]``, ``[[old stem|alias]]``, ``[[old stem#heading]]``
  and ``[[folder/old stem]]``,
- the full old segment, raw (angle-bracket links, backtick paths) or
  URL-encoded (``%20`` style Markdown links).

Usage (from project root):

    .venv/bin/python src/tools/md_name_cleanser.py --dry-run
    .venv/bin/python src/tools/md_name_cleanser.py
    .venv/bin/python src/tools/md_name_cleanser.py --check   # release gate
"""

import argparse
import logging
import re
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, Field

from config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_DOCS_ROOT: Path = settings.PRJ_ROOT_DIR / "docs"
DEFAULT_LINK_ROOT: Path = settings.PRJ_ROOT_DIR

#: Folders never renamed nor scanned for links.
SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".venv", ".obsidian", ".trash", "node_modules", "__pycache__"}
)

#: Replacement per Windows-forbidden character. ``:`` reads as a separator in
#: titles ("Design: X"), so it becomes `` -``; quotes and wildcards add no
#: meaning to a title and are dropped; the rest become a hyphen.
CHAR_REPLACEMENTS: dict[str, str] = {
    '"': "",
    "?": "",
    "*": "",
    ":": " -",
    "<": "-",
    ">": "-",
    "|": "-",
    "\\": "-",
}

RESERVED_NAME_RE: re.Pattern[str] = re.compile(
    r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE
)
CONTROL_CHAR_RE: re.Pattern[str] = re.compile(r"[\x00-\x1f]")
MULTI_SPACE_RE: re.Pattern[str] = re.compile(r" {2,}")
FRONT_MATTER_RE: re.Pattern[str] = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
EXTENSION_RE: re.Pattern[str] = re.compile(r"\.[A-Za-z0-9]{1,10}$")

#: Windows' classic MAX_PATH is 260 characters for the absolute path. The repo
#: path is unknown, so warn once the repo-relative path leaves less than this
#: much headroom for ``C:\Users\<name>\...\celex-facts\``.
MAX_RELATIVE_PATH_LEN: int = 200

MARKDOWN_SUFFIX: str = ".md"

#: Placeholder for a segment that sanitizes to nothing.
EMPTY_NAME_PLACEHOLDER: str = "_"


class Rename(BaseModel):
    """One planned rename of a single path segment.

    Attributes:
        old: Absolute path as it exists today.
        new: Absolute path after the rename (same parent folder).
        is_file: Whether the segment is a file rather than a folder.
    """

    old: Path = Field(description="Absolute path as it exists today.")
    new: Path = Field(description="Absolute path after the rename, same parent folder.")
    is_file: bool = Field(description="Whether the segment is a file rather than a folder.")


class CleanserReport(BaseModel):
    """Outcome of one cleanser pass.

    Attributes:
        root: Folder whose names were checked.
        renames: Renames planned (and applied unless dry-run), deepest first.
        conflicts: Problems that block an automatic rename.
        warnings: Legal but risky names (non-ASCII, long paths).
        rewritten_files: Markdown files whose links were updated.
        applied: Whether the renames and link rewrites were written to disk.
    """

    root: Path = Field(description="Folder whose names were checked.")
    renames: list[Rename] = Field(default_factory=list, description="Planned renames, deepest first.")
    conflicts: list[str] = Field(default_factory=list, description="Problems that block an automatic rename.")
    warnings: list[str] = Field(default_factory=list, description="Legal but risky names.")
    rewritten_files: list[Path] = Field(default_factory=list, description="Markdown files whose links changed.")
    applied: bool = Field(default=False, description="Whether changes were written to disk.")

    @property
    def clean(self) -> bool:
        """Return True when nothing needs renaming and nothing conflicts."""
        return not self.renames and not self.conflicts


def is_windows_safe(name: str) -> bool:
    """Tell whether a single path segment is acceptable to Windows.

    Args:
        name: One file or folder name, without any separator.

    Returns:
        True when Win32 / NTFS accepts the name as-is.
    """
    if not name or name.endswith((" ", ".")):
        return False
    if CONTROL_CHAR_RE.search(name) or any(ch in name for ch in CHAR_REPLACEMENTS):
        return False
    return RESERVED_NAME_RE.match(name) is None


def sanitize_name(name: str, is_file: bool) -> str:
    """Return a Windows-acceptable spelling of one path segment.

    The extension of a file is kept; trailing dots and spaces are stripped
    from the stem as well, so ``X.".md`` becomes ``X.md`` rather than ``X..md``.

    Args:
        name: One file or folder name, without any separator.
        is_file: Whether the segment is a file (its suffix is preserved).

    Returns:
        The sanitized name; ``name`` itself when it is already safe.
    """
    if is_windows_safe(name):
        return name

    stem, suffix = name, ""
    extension = EXTENSION_RE.search(name) if is_file else None
    if extension:
        stem, suffix = name[: extension.start()], extension.group()

    for bad, good in CHAR_REPLACEMENTS.items():
        stem = stem.replace(bad, good)
    stem = CONTROL_CHAR_RE.sub("", stem)
    stem = MULTI_SPACE_RE.sub(" ", stem).strip().rstrip(". ") or EMPTY_NAME_PLACEHOLDER

    result = stem + suffix
    if RESERVED_NAME_RE.match(result):
        result = stem + EMPTY_NAME_PLACEHOLDER + suffix
    return result


def _walk(root: Path) -> list[Path]:
    """List every path below ``root``, skipping :data:`SKIP_DIRS`.

    Args:
        root: Folder to walk.

    Returns:
        All files and folders below ``root``, deepest first.
    """
    found: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        found.append(path)
    found.sort(key=lambda p: len(p.parts), reverse=True)
    return found


def plan(root: Path) -> CleanserReport:
    """Work out the renames, conflicts and warnings for ``root``.

    Renames are computed per segment on today's names and ordered deepest
    first, so renaming a child never invalidates the planned path of its
    parent.

    Args:
        root: Folder to check.

    Returns:
        A report with ``applied`` False.

    Raises:
        FileNotFoundError: If ``root`` does not exist.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"docs root not found: {root}")

    report = CleanserReport(root=root)
    paths = _walk(root)

    by_folder: dict[Path, dict[str, list[str]]] = {}
    for path in paths:
        by_folder.setdefault(path.parent, {}).setdefault(path.name.lower(), []).append(path.name)

    for path in paths:
        rel = path.relative_to(root)
        is_file = path.is_file()
        new_name = sanitize_name(path.name, is_file=is_file)
        siblings = by_folder[path.parent]

        if new_name != path.name:
            taken = [n for n in siblings.get(new_name.lower(), []) if n != path.name]
            if taken:
                report.conflicts.append(
                    f"{rel}: sanitized name '{new_name}' collides with existing '{taken[0]}'"
                )
                continue
            siblings.setdefault(new_name.lower(), []).append(new_name)
            report.renames.append(Rename(old=path, new=path.with_name(new_name), is_file=is_file))
        elif any(ord(ch) > 127 for ch in path.name):
            report.warnings.append(f"{rel}: non-ASCII name (legal on Windows)")

        if len(str(rel)) > MAX_RELATIVE_PATH_LEN:
            report.warnings.append(f"{rel}: {len(str(rel))} characters, close to Windows MAX_PATH")

    for folder, names in by_folder.items():
        for variants in names.values():
            unique = sorted(set(variants))
            if len(unique) > 1 and all(is_windows_safe(n) for n in unique):
                report.conflicts.append(
                    f"{folder.relative_to(root) or '.'}: names differ only in case: {', '.join(unique)}"
                )
    return report


def _link_patterns(old: str, new: str, is_file: bool) -> list[tuple[re.Pattern[str], str]]:
    """Build the (pattern, replacement) pairs that retarget links to one segment.

    Args:
        old: Old segment name.
        new: New segment name.
        is_file: Whether the segment is a file (adds stem-only wikilinks).

    Returns:
        Regex pairs, most specific first.
    """
    pairs: list[tuple[re.Pattern[str], str]] = []
    if is_file and old.endswith(MARKDOWN_SUFFIX):
        old_stem, new_stem = old[: -len(MARKDOWN_SUFFIX)], new[: -len(MARKDOWN_SUFFIX)]
        pairs.append(
            (
                re.compile(r"(\[\[(?:[^\]|#]*/)?)" + re.escape(old_stem) + r"(?:\.md)?(?=[\]|#])"),
                r"\g<1>" + new_stem.replace("\\", r"\\"),
            )
        )
    for old_form, new_form in {
        (old, new),
        (old.replace(" ", "%20"), new.replace(" ", "%20")),
        (quote(old), quote(new)),
    }:
        pairs.append((re.compile(re.escape(old_form)), new_form.replace("\\", r"\\")))
    return pairs


def rewrite_links(text: str, renames: list[Rename]) -> str:
    """Retarget links in one Markdown text to the renamed segments.

    Args:
        text: Markdown content.
        renames: Renames to apply.

    Returns:
        The content with every matching link updated.
    """
    for rename in renames:
        for pattern, replacement in _link_patterns(rename.old.name, rename.new.name, rename.is_file):
            text = pattern.sub(replacement, text)
    return text


def _markdown_files(link_root: Path) -> list[Path]:
    """List the Markdown files whose links may need rewriting.

    Args:
        link_root: Folder to search (usually the project root).

    Returns:
        Every ``.md`` file below ``link_root`` outside :data:`SKIP_DIRS`.
    """
    return [
        p
        for p in link_root.rglob("*.md")
        if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(link_root).parts)
    ]


def add_title_heading(text: str, title: str) -> str:
    """Put the old file name on top of a note as its level-1 heading.

    The heading goes after a YAML front matter block when one opens the
    file. Nothing is added when the first non-blank line already is that
    heading, so repeated passes stay idempotent.

    Args:
        text: Markdown content of the renamed note.
        title: Old file name without the ``.md`` extension.

    Returns:
        The content with ``# <title>`` as its first chapter.
    """
    heading = f"# {title}"
    front_matter = ""
    body = text
    match = FRONT_MATTER_RE.match(text)
    if match:
        front_matter, body = text[: match.end()], text[match.end() :]
    if body.lstrip("\n").split("\n", 1)[0].strip() == heading:
        return text
    return f"{front_matter}{heading}\n\n{body}"


def run(
    root: Path = DEFAULT_DOCS_ROOT,
    link_root: Path = DEFAULT_LINK_ROOT,
    dry_run: bool = False,
) -> CleanserReport:
    """Plan and, unless ``dry_run``, apply the renames and link rewrites.

    Links are rewritten first, while the old names still resolve, then the
    segments are renamed deepest first. A renamed Markdown note gets its old
    file name as a top heading, so the original title survives the rename. Nothing is renamed when the plan has
    conflicts, so a half-applied pass cannot leave links dangling.

    Args:
        root: Folder whose names are cleansed.
        link_root: Folder whose Markdown files get their links rewritten.
        dry_run: Report only; write nothing.

    Returns:
        The report; ``applied`` tells whether anything was written.

    Raises:
        FileNotFoundError: If ``root`` does not exist.
    """
    report = plan(root)
    if not report.renames:
        return report

    for md_file in _markdown_files(link_root):
        content = md_file.read_text(encoding="utf-8", errors="surrogateescape")
        updated = rewrite_links(content, report.renames)
        if updated != content:
            report.rewritten_files.append(md_file)
            if not dry_run and not report.conflicts:
                md_file.write_text(updated, encoding="utf-8", errors="surrogateescape")

    if dry_run or report.conflicts:
        return report

    for rename in report.renames:
        rename.old.rename(rename.new)
        logger.info("Renamed %s -> %s", rename.old, rename.new.name)
        if rename.is_file and rename.new.suffix == MARKDOWN_SUFFIX:
            content = rename.new.read_text(encoding="utf-8", errors="surrogateescape")
            titled = add_title_heading(content, rename.old.name[: -len(MARKDOWN_SUFFIX)])
            if titled != content:
                rename.new.write_text(titled, encoding="utf-8", errors="surrogateescape")
    report.applied = True
    return report


def format_report(report: CleanserReport, link_root: Path = DEFAULT_LINK_ROOT) -> str:
    """Render the report as plain text for the terminal.

    Args:
        report: Outcome of :func:`run`.
        link_root: Folder that relative Markdown paths are shown against.

    Returns:
        A multi-line summary.
    """
    verb = "Renamed" if report.applied else "Would rename"
    lines = [f"Checked names under {report.root}"]
    if report.clean:
        lines.append("All names are Windows-safe.")
    for rename in report.renames:
        lines.append(f"  {verb}: {rename.old.relative_to(report.root)} -> {rename.new.name}")
    for path in report.rewritten_files:
        lines.append(f"  Links {'updated' if report.applied else 'to update'} in: {path.relative_to(link_root)}")
    for conflict in report.conflicts:
        lines.append(f"  CONFLICT: {conflict}")
    if report.conflicts and report.renames:
        lines.append("  Nothing written: resolve the conflicts first.")
    for warning in report.warnings:
        lines.append(f"  warning: {warning}")
    return "\n".join(lines)


def main() -> int:
    """Parse arguments, run the cleanser and print the report.

    Returns:
        Process exit code: 1 under ``--check`` when the tree is not clean, or
        whenever conflicts block the pass; 0 otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="md-name-cleanser",
        description="Rename files under docs/ to Windows-acceptable names and fix the links to them.",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_DOCS_ROOT, help=f"Folder to cleanse (default: {DEFAULT_DOCS_ROOT})")
    parser.add_argument("--link-root", type=Path, default=DEFAULT_LINK_ROOT, help=f"Folder whose .md links are rewritten (default: {DEFAULT_LINK_ROOT})")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    parser.add_argument("--check", action="store_true", help="Release gate: report only and exit 1 unless every name is safe.")
    args = parser.parse_args()

    report = run(root=args.root, link_root=args.link_root, dry_run=args.dry_run or args.check)
    print(format_report(report, link_root=args.link_root))
    if report.conflicts or (args.check and not report.clean):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
