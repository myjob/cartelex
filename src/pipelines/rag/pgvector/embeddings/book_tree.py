"""
Build a hierarchical tree of the Russo-Schinkel book's infringement chapters.

All markdown headings are rendered as H1 (``#``) by MinerU, so logical depth
is inferred from the heading-text numbering pattern:

    Level 1 — ``2 Horizontal restrictions``   (chapter,  ``\\d+ ``)
    Level 2 — ``2.1 Price fixing``            (section,  ``\\d+\\.\\d+ ``)
    Level 3 — ``A Introduction``              (subsect., ``[A-Z] ``)
    Level 4 — ``F.1 Vitamins …``             (case,     ``[A-Z]\\.\\d+ ``)
    Level 5 — ``a. Relevant facts``           (sub-case, ``[a-z]\\. ``)
    Level 6 — everything else                (leaf)

Usage::

    builder = InfringementTreeBuilder()
    root = builder.build(
        "/path/to/Russo_Schinkel_Günster_Carree_2010_Book-mineru.md"
    )
    for chapter in root.children:
        print(chapter.title, "→", len(chapter.children), "sections")
"""

from __future__ import annotations

import logging
import re

import mistune
import tiktoken
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_TOKENIZER = tiktoken.get_encoding("cl100k_base")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# Matched against the RAW (unstripped) heading text so that TOC entries like
# "2 Horizontal restrictions 26" (with trailing page number) are ignored and
# only the real content headings (no trailing digit) trigger the window.
_START_PATTERN = re.compile(r"^2\s+Horizontal\s+restrictions\s*$", re.IGNORECASE)
_END_PATTERN = re.compile(r"^8\s+Mergers\s+and\s+acquisitions\s*$", re.IGNORECASE)

_TRAILING_PAGE_NUMBER = re.compile(r"\s+\d+\s*$")

# Ordered list of (logical_level, compiled_regex).  First match wins.
_LEVEL_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (1, re.compile(r"^\d+\s")),          # "2 Horizontal..."
    (2, re.compile(r"^\d+\.\d+\s")),     # "2.1 Price fixing..."
    (3, re.compile(r"^[A-Z]\s")),        # "A Introduction"
    (4, re.compile(r"^[A-Z]\.\d+\s")),   # "F.1 Vitamins..."
    (5, re.compile(r"^[a-z]\.\s")),      # "a. Relevant facts"
]


class BookNode(BaseModel):
    """A single node in the book's section hierarchy.

    Attributes:
        title: The heading text (stripped of trailing page-number artefacts).
        level: Logical depth — 0 = virtual root, 1 = chapter, …, 5 = sub-case.
        path: Ancestor chain including this node, joined by " -> ".
            Example: ``"Infringements -> 8 Mergers and acquisitions -> A Introduction"``.
        text_line: 1-based line number of the heading in the original markdown file.
            0 for the virtual root node.
        raw_text: Concatenated plain text of all non-heading content that
            belongs directly to this node (i.e. before any child heading).
        children: Ordered list of child ``BookNode`` objects.
    """

    title: str = Field(description="Heading text, page numbers stripped.")
    level: int = Field(description="Logical depth (0=root, 1=chapter, …).")
    path: str = Field(
        default="",
        description=(
            "Full ancestor chain to this node (inclusive), titles joined by ' -> '. "
            "Example: 'Infringements -> 8 Mergers and acquisitions -> A Introduction'."
        ),
    )
    text_line: int = Field(
        default=0,
        description=(
            "1-based line number of this heading in the original markdown file. "
            "0 for the virtual root node."
        ),
    )
    raw_text: str = Field(
        default="",
        description=(
            "Plain text of this node: the heading title on the first line, "
            "followed by all non-heading content that belongs directly to this node."
        ),
    )
    token_count: int = Field(
        default=0,
        description=(
            "Total tiktoken (cl100k_base) token count of this node's raw_text "
            "plus the token counts of all descendant nodes."
        ),
    )
    children: list["BookNode"] = Field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"BookNode(level={self.level}, title={self.title!r}, "
            f"children={len(self.children)}, chars={len(self.raw_text)}, "
            f"tokens={self.token_count})"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _heading_text(token: dict) -> str:
    """Extract plain text from a mistune heading token.

    Args:
        token: A mistune AST token of type ``heading``.

    Returns:
        The concatenated text of all inline children.
    """
    return _inline_text(token.get("children") or []).strip()


def _inline_text(nodes: list[dict]) -> str:
    """Recursively flatten inline AST nodes to a plain string.

    Args:
        nodes: List of mistune inline tokens.

    Returns:
        A plain-text representation, preserving soft line-breaks as spaces.
    """
    parts: list[str] = []
    for node in nodes:
        node_type = node.get("type", "")
        if node_type == "text":
            parts.append(node.get("raw", ""))
        elif node_type in ("softlinebreak", "linebreak"):
            parts.append(" ")
        elif node_type == "codespan":
            parts.append(node.get("raw", ""))
        elif "children" in node:
            parts.append(_inline_text(node["children"]))
        elif "raw" in node:
            parts.append(node["raw"])
    return "".join(parts)


def _block_text(token: dict) -> str:
    """Convert a block-level mistune token to plain text.

    Args:
        token: Any mistune AST block token.

    Returns:
        A plain-text representation of the block, terminated with ``\\n``.
    """
    token_type = token.get("type", "")
    if token_type in ("blank_line",):
        return "\n"
    if token_type == "paragraph":
        return _inline_text(token.get("children") or []) + "\n"
    if token_type in ("block_code", "code"):
        return token.get("raw", "") + "\n"
    if token_type == "list":
        lines: list[str] = []
        for item in token.get("children") or []:
            item_text = "".join(
                _block_text(child) for child in (item.get("children") or [])
            )
            lines.append("- " + item_text.strip())
        return "\n".join(lines) + "\n"
    if "children" in token:
        return "".join(_block_text(child) for child in token["children"])
    if "raw" in token:
        return token["raw"] + "\n"
    return ""


def _detect_level(title: str) -> int:
    """Infer logical heading level from the heading text.

    Args:
        title: The stripped heading text.

    Returns:
        An integer in [1, 6] where 1 is a top-level chapter and 6 is a leaf.
    """
    for level, pattern in _LEVEL_PATTERNS:
        if pattern.match(title):
            return level
    return 6


def _scan_heading_lines(markdown_text: str) -> list[tuple[str, int]]:
    """Return an ordered list of (heading_text, 1-based line number) pairs.

    Scans the raw markdown source line by line.  Because MinerU renders every
    heading as H1 (``#``), any line that starts with ``#`` is treated as a
    heading.  The leading ``#`` characters and surrounding whitespace are
    stripped to produce text comparable to the output of :func:`_heading_text`.

    Args:
        markdown_text: Full contents of the markdown file.

    Returns:
        List of ``(stripped_heading_text, lineno)`` tuples in document order.
    """
    result: list[tuple[str, int]] = []
    for lineno, line in enumerate(markdown_text.splitlines(), start=1):
        if line.startswith("#"):
            text = line.lstrip("#").strip()
            if text:
                result.append((text, lineno))
    return result


def _strip_page_number(title: str) -> str:
    """Remove trailing page-number artefacts injected by MinerU.

    MinerU sometimes appends the page number to heading text, e.g.
    ``"2 Horizontal restrictions 26"``.

    Args:
        title: Raw heading text.

    Returns:
        Heading text with any trailing integer stripped.
    """
    return re.sub(r"\s+\d+$", "", title).strip()


def _compute_token_counts(node: BookNode) -> int:
    """Recursively compute and store token counts for *node* and all descendants.

    The count for a node is the number of cl100k_base tokens in its own
    ``raw_text`` plus the sum of its children's counts.  The result is stored
    in ``node.token_count`` and also returned so the caller can accumulate it.

    Args:
        node: The node whose subtree should be annotated.

    Returns:
        Total token count for the subtree rooted at *node*.
    """
    own_tokens = len(_TOKENIZER.encode(node.raw_text))
    children_tokens = sum(_compute_token_counts(child) for child in node.children)
    node.token_count = own_tokens + children_tokens
    return node.token_count


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


class InfringementTreeBuilder:
    """Parses the Russo-Schinkel book markdown and returns a section tree.

    The virtual root node is labelled ``"Infringements"`` (level 0) and
    contains exactly the seven chapter nodes 2–8 as direct children.

    Usage::

        tree = InfringementTreeBuilder().build("/path/to/book.md")
    """

    def build(self, md_file: str) -> BookNode:
        """Parse *md_file* and return the infringement section tree.

        The tree spans from the heading ``"2 Horizontal restrictions"`` up to
        and including ``"8 Mergers and acquisitions"`` and all of their
        descendants.

        Args:
            md_file: Absolute path to the MinerU-converted markdown file.

        Returns:
            A ``BookNode`` with ``title="Infringements"`` and ``level=0``
            whose children are the seven chapter nodes.

        Raises:
            FileNotFoundError: If *md_file* does not exist.
            ValueError: If the start or end heading is not found in the file.
        """
        with open(md_file, encoding="utf-8") as fh:
            markdown_text = fh.read()

        md_parser = mistune.create_markdown(renderer="ast")
        tokens: list[dict] = md_parser(markdown_text)

        # Pre-scan line numbers before entering the token loop so that each
        # heading node can record its exact 1-based source line.  Headings are
        # processed in document order, so a forward-only pointer is sufficient.
        heading_lines: list[tuple[str, int]] = _scan_heading_lines(markdown_text)
        heading_ptr: int = 0

        root = BookNode(title="Infringements", level=0, path="Infringements")
        # stack[i] = the current open node at each logical level; stack[0] = root
        stack: list[BookNode] = [root]

        inside = False
        ch8_seen = False

        for token in tokens:
            if token.get("type") != "heading":
                if inside:
                    stack[-1].raw_text += _block_text(token)
                continue

            raw_title = _heading_text(token)

            # ----------------------------------------------------------------
            # Window entry: match the raw title (with trailing page number
            # still present) so that TOC entries like
            # "2 Horizontal restrictions 26" are skipped, while the actual
            # content heading "2 Horizontal restrictions" (no trailing digit)
            # triggers the start.
            # ----------------------------------------------------------------
            if not inside:
                if _START_PATTERN.match(raw_title):
                    inside = True
                else:
                    continue

            title = _strip_page_number(raw_title)
            level = _detect_level(title)

            # ----------------------------------------------------------------
            # Window exit: a level-1 heading with a trailing page number means
            # we've re-entered a repeated TOC / appendix section — stop.
            # ----------------------------------------------------------------
            if inside and level == 1 and _TRAILING_PAGE_NUMBER.search(raw_title):
                break

            # ----------------------------------------------------------------
            # Window exit: a second level-1 heading after chapter 8 that is
            # NOT chapter 8 itself means we've passed the target range.
            # (Handles the case where the file has a chapter 9.)
            # ----------------------------------------------------------------
            if inside and level == 1 and ch8_seen and not _END_PATTERN.match(raw_title):
                break

            # Pop the stack until the top is a valid parent (level < node.level).
            # The level-0 root is never popped.
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()

            # Advance the heading-line pointer to the next entry whose text
            # matches raw_title (the pre-stripped text from the source line).
            text_line: int = 0
            while heading_ptr < len(heading_lines):
                candidate_text, candidate_lineno = heading_lines[heading_ptr]
                heading_ptr += 1
                if candidate_text == raw_title:
                    text_line = candidate_lineno
                    break

            path = " -> ".join(n.title for n in stack) + " -> " + title
            node = BookNode(
                title=title,
                level=level,
                path=path,
                text_line=text_line,
                raw_text=title + "\n",
            )

            stack[-1].children.append(node)
            stack.append(node)

            if level == 1 and _END_PATTERN.match(raw_title):
                ch8_seen = True

        if not root.children:
            raise ValueError(
                f"Start heading '2 Horizontal restrictions' not found in {md_file}"
            )

        _compute_token_counts(root)

        logger.info(
            "Built infringement tree: %d top-level chapters, %d total tokens.",
            len(root.children),
            root.token_count,
        )
        return root


def _count_nodes(node: BookNode) -> int:
    """Return total number of nodes in the subtree rooted at *node*.

    Args:
        node: Root of the subtree.

    Returns:
        Count of all nodes including *node* itself.
    """
    return 1 + sum(_count_nodes(child) for child in node.children)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    from config.settings import settings

    md_path = sys.argv[1] if len(sys.argv) > 1 else settings.BOOK_MD_PATH

    tree = InfringementTreeBuilder().build(md_path)
    print(f"Root: {tree.title}  ({len(tree.children)} chapters, {tree.token_count} tokens)")
    for chapter in tree.children:
        print(
            f"  [{chapter.level}] {chapter.title}"
            f"  ({len(chapter.children)} sections, {chapter.token_count} tokens)"
        )
        for section in chapter.children:
            print(
                f"    [{section.level}] {section.title}"
                f"  ({len(section.children)} sub-sections,"
                f" {len(section.raw_text)} chars, {section.token_count} tokens)"
            )
            for subsection in section.children:
                print(
                    f"      [{subsection.level}] {subsection.title}"
                    f"  ({len(subsection.children)} sub-sub-sections,"
                    f" {len(subsection.raw_text)} chars, {subsection.token_count} tokens)"
                )
    print(f"\nTotal nodes: {_count_nodes(tree)}")
