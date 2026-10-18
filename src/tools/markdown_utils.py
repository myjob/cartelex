"""Render extracted fact values as Markdown.

Formerly built its table with ``pandas.DataFrame.to_markdown()``. That pulled
in both pandas and its optional ``tabulate`` dependency for what is a dozen
lines of string joining, so the table is now assembled directly -- polars has
no ``to_markdown`` equivalent and a DataFrame buys nothing here.
"""

from typing import Any, Dict, List, Union

_MISSING = ""


def _markdown_table(rows: List[Dict[str, Any]]) -> str:
    """Render a list of dictionaries as a GitHub-flavoured Markdown table.

    Columns are the union of all keys, ordered by first appearance -- the same
    column order ``pandas.DataFrame`` produced. Rows missing a key render as an
    empty cell.

    Args:
        rows: Dictionaries to render, one per table row.

    Returns:
        The Markdown table, without a trailing newline.
    """
    columns: List[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    def cell(value: Any) -> str:
        """Stringify *value* and escape pipes so they cannot break the table."""
        return str(value).replace("|", "\\|")

    lines = [
        "| " + " | ".join(cell(c) for c in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend(
        "| " + " | ".join(cell(row.get(c, _MISSING)) for c in columns) + " |"
        for row in rows
    )
    return "\n".join(lines)


def format_fact_to_markdown(data: Union[str, List[Any], Dict[str, Any]]) -> str:
    """
    Format various data structures into a clean Markdown string.

    Rules:
    - Single strings return as-is.
    - An empty list returns an empty string.
    - A list with exactly 1 dictionary containing exactly 1 key returns just the value.
    - A list of dictionaries (where len > 1 or keys > 1) forms a Markdown table.
    - A list of strings forms a Markdown bulleted list.

    Args:
        data: The fact value to render.

    Returns:
        A Markdown representation of *data*.
    """
    if isinstance(data, str):
        return data

    if isinstance(data, list):
        if not data:
            return ""

        # Case: List of Dictionaries
        if all(isinstance(item, dict) for item in data):
            # Check edge case: exactly 1 dict, exactly 1 key -> return raw value string
            if len(data) == 1 and len(data[0]) == 1:
                return str(list(data[0].values())[0])

            return _markdown_table(data)

        # Case: Simple Bulleted List
        return "\n".join(f"- {item}" for item in data)

    # Fallback to pure string representation
    return str(data)
