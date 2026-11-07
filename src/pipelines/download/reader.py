"""Loader file reader of the HTTP document downloader.

Reads csv, xlsx and txt loader files into an all-string polars frame so the
mappers see the same shape regardless of the input format.

Fully blank data rows are dropped (csv, xlsx; txt blank and comment lines are
skipped), so the mappers' ``loader_row`` is the 1-based position among the
rows as returned here, not a spreadsheet row number.
"""

import io
import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

_DELIMITERS = (";", ",", "\t", "|")
_URL_COLUMN = "url"
_COMMENT_PREFIX = "#"


def _sniff_delimiter(header_line: str) -> str:
    """Pick the csv delimiter that occurs most often in the header line.

    Args:
        header_line: First line of the csv text.

    Returns:
        The most frequent candidate delimiter (comma when none occurs).
    """
    counts = {d: header_line.count(d) for d in _DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _drop_blank_rows(df: pl.DataFrame) -> pl.DataFrame:
    """Drop rows whose cells are all null or blank.

    Args:
        df: String frame.

    Returns:
        Frame without fully blank rows.
    """
    if not df.columns:
        return df
    return df.filter(
        pl.any_horizontal(
            pl.col(c).str.strip_chars().fill_null("").ne("") for c in df.columns
        )
    )


def _read_csv(path: Path) -> pl.DataFrame:
    """Read a csv file as strings (BOM-safe, CRLF-safe, delimiter sniffed).

    Args:
        path: Csv file path.

    Returns:
        Frame with trimmed header names and all columns as strings.
    """
    text = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    delimiter = _sniff_delimiter(text.split("\n", 1)[0])
    df = pl.read_csv(
        io.StringIO(text), separator=delimiter, infer_schema=False
    )
    return _drop_blank_rows(df.rename({c: c.strip() for c in df.columns}))


def _read_xlsx(path: Path) -> pl.DataFrame:
    """Read the first sheet of an xlsx file as strings.

    Args:
        path: Xlsx file path.

    Returns:
        Frame with trimmed header names and all columns as strings.
    """
    df = pl.read_excel(path, engine="openpyxl", infer_schema_length=0)
    return _drop_blank_rows(df.rename({c: c.strip() for c in df.columns}))


def _read_txt(path: Path) -> pl.DataFrame:
    """Read a one-URL-per-line text file.

    Args:
        path: Text file path.

    Returns:
        Frame with a single ``url`` column; blanks and ``#`` comments skipped.
    """
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    urls = [
        s
        for s in (line.strip() for line in lines)
        if s and not s.startswith(_COMMENT_PREFIX)
    ]
    return pl.DataFrame({_URL_COLUMN: urls}, schema={_URL_COLUMN: pl.String})


def read_loader(path: Path) -> pl.DataFrame:
    """Read a loader file into a frame whose columns are all strings.

    Args:
        path: Loader file (``.csv``, ``.xlsx`` or ``.txt``).

    Returns:
        String frame; ``.txt`` yields a single ``url`` column.

    Raises:
        ValueError: If the extension is not supported.
        FileNotFoundError: If the file does not exist.
    """
    suffix = path.suffix.lower()
    match suffix:
        case ".csv" | ".tsv":
            return _read_csv(path)
        case ".xlsx":
            return _read_xlsx(path)
        case ".txt":
            return _read_txt(path)
        case _:
            raise ValueError(f"unsupported loader file type: {path.name}")
