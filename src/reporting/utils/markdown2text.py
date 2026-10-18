"""Convert Markdown text to plain text using the markdown-it-py parser."""

# https://github.com/executablebooks/markdown-it-py

import re

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Collapse runs of whitespace to a single space and strip leading/trailing whitespace."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _strip_html(text: str) -> str:
    """Remove HTML tags from *text*, leaving only text content."""
    return _HTML_TAG_RE.sub("", text)


def _render_table(node: SyntaxTreeNode) -> str:
    """Render a markdown table node as an ASCII-formatted plain-text table.

    Parameters
    ----------
    node:
        A ``SyntaxTreeNode`` whose type is ``"table"``.

    Returns
    -------
    str
        Plain-text table with columns separated by `` | `` and a header
        underline row, or an empty string when the table contains no rows.
    """
    rows: list[list[str]] = []
    for section in node.children:
        if section.type in {"thead", "tbody"}:
            for tr in section.children:
                if tr.type == "tr":
                    row = [
                        _render_node(cell)
                        for cell in tr.children
                        if cell.type in {"th", "td"}
                    ]
                    if row:
                        rows.append(row)

    if not rows:
        return ""

    col_count = max(len(row) for row in rows)
    widths = [
        max(len(row[i]) if i < len(row) else 0 for row in rows)
        for i in range(col_count)
    ]

    def _fmt_row(row: list[str]) -> str:
        return " | ".join(
            (row[i] if i < len(row) else "").ljust(widths[i])
            for i in range(len(widths))
        ).rstrip()

    lines = [_fmt_row(rows[0])]
    if len(rows) > 1:
        lines.append("-+-".join("-" * w for w in widths))
        lines.extend(_fmt_row(row) for row in rows[1:])
    return "\n".join(lines)


def _render_list_item(node: SyntaxTreeNode, list_sep: str) -> str:
    """Render a single list item, including any nested lists or block content.

    Parameters
    ----------
    node:
        A ``SyntaxTreeNode`` whose type is ``"list_item"``.
    list_sep:
        Separator inserted between sibling list items (propagated to nested lists).

    Returns
    -------
    str
        Plain-text representation of the list item.
    """
    head_parts: list[str] = []
    nested_parts: list[str] = []

    for child in node.children:
        if child.type in {"bullet_list", "ordered_list", "table", "blockquote"}:
            nested = _render_node(child, list_sep=list_sep)
            if nested:
                nested_parts.append(nested)
        else:
            text = _render_node(child, list_sep=list_sep)
            if text:
                head_parts.append(text)

    head = _clean(" ".join(head_parts))

    if nested_parts:
        suffix = " ".join(
            f"[{part}]" if "\n" in part else f"({part})"
            for part in nested_parts
        )
        return _clean(f"{head} {suffix}") if head else suffix

    return head


def _render_node(node: SyntaxTreeNode, list_sep: str = "; ") -> str:
    """Recursively convert a ``SyntaxTreeNode`` to plain text.

    Parameters
    ----------
    node:
        The syntax tree node to render.
    list_sep:
        Separator inserted between sibling list items at every nesting level.

    Returns
    -------
    str
        Plain-text content of *node* and all its descendants.
    """
    node_type = node.type

    if node_type in {"text", "code_inline"}:
        return node.content

    if node_type == "html_inline":
        return _strip_html(node.content)

    if node_type == "html_block":
        # html_block nodes carry their content directly; strip tags for plain text.
        return _strip_html(node.content).strip()

    if node_type in {"softbreak", "hardbreak"}:
        return " "

    if node_type == "inline":
        return _clean("".join(_render_node(c, list_sep=list_sep) for c in node.children))

    if node_type in {"paragraph", "heading", "blockquote"}:
        return "\n".join(
            part
            for part in (_render_node(c, list_sep=list_sep) for c in node.children)
            if part
        )

    if node_type in {"em", "strong", "s", "link", "image", "th", "td"}:
        return _clean(" ".join(
            part
            for part in (_render_node(c, list_sep=list_sep) for c in node.children)
            if part
        ))

    if node_type in {"bullet_list", "ordered_list"}:
        items: list[str] = []
        start = int(node.attrGet("start") or 1) if node_type == "ordered_list" else 1

        for i, child in enumerate(
            (c for c in node.children if c.type == "list_item"), start=start
        ):
            item = _render_list_item(child, list_sep)
            if item and node_type == "ordered_list":
                item = f"{i}. {item}"
            if item:
                items.append(item)

        return list_sep.join(items)

    if node_type == "list_item":
        return _render_list_item(node, list_sep)

    if node_type == "table":
        return _render_table(node)

    if node_type in {"fence", "code_block"}:
        return node.content.rstrip()

    if node_type == "root":
        parts = [_render_node(c, list_sep=list_sep) for c in node.children]
        return "\n\n".join(part for part in parts if part)

    # Fallback: render children and join non-empty results.
    return "\n".join(
        part for part in (_render_node(c, list_sep=list_sep) for c in node.children) if part
    )


def markdown_to_text(md_text: str, list_sep: str = "; ") -> str:
    """Convert a Markdown string to plain text.

    All formatting is stripped; tables are rendered as ASCII grids; lists
    are flattened using *list_sep* as the item separator.

    Parameters
    ----------
    md_text:
        Source Markdown string.
    list_sep:
        Separator inserted between list items (default ``"; "``).

    Returns
    -------
    str
        Plain-text representation of *md_text*.
    """
    md = MarkdownIt("commonmark").enable("table")
    root = SyntaxTreeNode(md.parse(md_text))
    return _render_node(root, list_sep=list_sep)
