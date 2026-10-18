"""
Retrieve case summaries from celex_facts.celex_index_vw and write them to disk.

Outputs:
  - reports/case_summary/<celex_id>_summary.md   — one Markdown file per case
  - reports/case_summary/<celex_id>_summary.pdf  — PDF rendered from the markdown
  - reports/case_summary/summary_index.html       — HTML index with collapsible summaries
"""

import logging
import html
from pathlib import Path

import polars as pl
from markdown_it import MarkdownIt
from weasyprint import HTML as WeasyHTML

from config.settings import settings

log = logging.getLogger(__name__)

_QUERY = """
SELECT celex_id, case_title, industy, cartel_period, case_summary
FROM celex_facts.celex_index_vw
ORDER BY celex_id
"""

_OUTPUT_SUBDIR = "case_summary"

_md_renderer = MarkdownIt()

_PDF_CSS = """
body {
  font-family: Georgia, serif;
  font-size: 11pt;
  line-height: 1.6;
  margin: 2.5cm 2cm;
  color: #1a1a1a;
}
h1 { font-size: 16pt; margin-bottom: 0.3em; }
h2 { font-size: 13pt; margin-top: 1.4em; border-bottom: 1px solid #bbb; padding-bottom: 0.15em; }
h3 { font-size: 11.5pt; margin-top: 1em; }
table { border-collapse: collapse; width: 100%; margin: 0.8em 0; font-size: 10pt; }
th, td { border: 1px solid #bbb; padding: 4px 8px; text-align: left; }
th { background: #f0f0f0; }
ul, ol { padding-left: 1.5em; }
em { font-style: italic; }
strong { font-weight: bold; }
"""


def _clean_db_url(url: str) -> str:
    """Strip SQLAlchemy dialect suffixes that connectorx does not accept."""
    return url.replace("+psycopg2", "").replace("+psycopg", "")


def _strip_outer_brackets(text: str | None) -> str:
    """
    Remove a single leading and trailing bracket character from *text*.

    Handles '[' / ']' and '(' / ')' pairs independently so that a leading
    '(' is removed regardless of what the trailing character is.

    Args:
        text: Raw string, possibly None.

    Returns:
        Stripped string, never None.
    """
    if text is None:
        return ""
    stripped = text.strip()
    if stripped.startswith(("[", "(")):
        stripped = stripped[1:]
    if stripped.endswith(("]", ")")):
        stripped = stripped[:-1]
    return stripped.strip()


def _render_markdown_html(markdown_text: str) -> str:
    """
    Render *markdown_text* to an HTML fragment string.

    Args:
        markdown_text: CommonMark-compatible markdown source.

    Returns:
        HTML string suitable for embedding inside a <body> element.
    """
    return _md_renderer.render(markdown_text)


def fetch_index(db_url: str) -> pl.DataFrame:
    """
    Query celex_index_vw and return a Polars DataFrame.

    Args:
        db_url: SQLAlchemy-style database connection string.

    Returns:
        DataFrame with columns: celex_id, case_title, industy,
        cartel_period, case_summary.

    Raises:
        Exception: propagated from the database driver on connection or
            query failure.
    """
    clean_url = _clean_db_url(db_url)
    df = pl.read_database_uri(query=_QUERY, uri=clean_url)
    log.info("Fetched %d rows from celex_index_vw.", df.height)
    return df


def write_case_markdown(row: dict, output_dir: Path) -> Path:
    """
    Write a single case summary to a Markdown file.

    The file is written to *output_dir*/<celex_id>_summary.md.

    Args:
        row: Dictionary with keys celex_id, case_title, industy,
            cartel_period, case_summary.
        output_dir: Directory that receives the Markdown files.

    Returns:
        Path of the written file.
    """
    celex_id: str = row["celex_id"] or "unknown"
    case_title: str = _strip_outer_brackets(row.get("case_title"))
    industry: str = row.get("industy") or ""
    cartel_period: str = row.get("cartel_period") or ""
    case_summary: str = row.get("case_summary") or ""

    md_path = output_dir / f"{celex_id}_summary.md"
    content = (
        f"# {case_title}\n\n"
        f"**CELEX ID:** {celex_id}  \n"
        f"**Industry:** {industry}  \n"
        f"**Cartel Period:** {cartel_period}  \n\n"
        f"## Summary\n\n"
        f"{case_summary}\n"
    )
    md_path.write_text(content, encoding="utf-8")
    return md_path


def write_case_pdf(row: dict, output_dir: Path) -> Path:
    """
    Convert a case summary to a PDF file via WeasyPrint.

    Renders the markdown summary to HTML, wraps it in a styled document,
    and writes the PDF to *output_dir*/<celex_id>_summary.pdf.

    Args:
        row: Dictionary with keys celex_id, case_title, industy,
            cartel_period, case_summary.
        output_dir: Directory that receives the PDF files.

    Returns:
        Path of the written PDF file.
    """
    celex_id: str = row["celex_id"] or "unknown"
    case_title: str = _strip_outer_brackets(row.get("case_title"))
    industry: str = row.get("industy") or ""
    cartel_period: str = row.get("cartel_period") or ""
    case_summary: str = row.get("case_summary") or ""

    summary_html = _render_markdown_html(case_summary)
    full_html = (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "  <meta charset=\"UTF-8\">\n"
        f"  <title>{html.escape(case_title)}</title>\n"
        f"  <style>{_PDF_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"<h1>{html.escape(case_title)}</h1>\n"
        f"<p><strong>CELEX ID:</strong> {html.escape(celex_id)}</p>\n"
        f"<p><strong>Industry:</strong> {html.escape(industry)}</p>\n"
        f"<p><strong>Cartel Period:</strong> {html.escape(cartel_period)}</p>\n"
        f"{summary_html}\n"
        "</body>\n"
        "</html>\n"
    )

    pdf_path = output_dir / f"{celex_id}_summary.pdf"
    WeasyHTML(string=full_html).write_pdf(str(pdf_path))
    return pdf_path


def write_index_html(df: pl.DataFrame, output_dir: Path) -> Path:
    """
    Write an HTML index page linking to each case PDF.

    Each row in the table has a collapsible sub-row that renders the
    case_summary markdown as HTML, presented as a nested sub-table.
    All PDF links open in a new browser tab.

    Args:
        df: DataFrame with columns celex_id, case_title, industy,
            cartel_period, case_summary produced by :func:`fetch_index`.
        output_dir: Directory that receives summary_index.html.

    Returns:
        Path of the written HTML file.
    """
    rows_html: list[str] = []
    for row in df.iter_rows(named=True):
        celex_id: str = row["celex_id"] or ""
        case_title: str = _strip_outer_brackets(row.get("case_title"))
        industry: str = row.get("industy") or ""
        cartel_period: str = row.get("cartel_period") or ""
        case_summary: str = row.get("case_summary") or ""

        pdf_filename = f"{html.escape(celex_id)}_summary.pdf"
        summary_body_html = _render_markdown_html(case_summary) if case_summary else "<em>No summary available.</em>"

        rows_html.append(
            f'    <tr class="case-row">\n'
            f'      <td><a href="{pdf_filename}" target="_blank">{html.escape(celex_id)}</a></td>\n'
            f"      <td>{html.escape(case_title)}</td>\n"
            f"      <td>{html.escape(industry)}</td>\n"
            f"      <td>{html.escape(cartel_period)}</td>\n"
            f"    </tr>\n"
            f'    <tr class="summary-row">\n'
            f'      <td colspan="4">\n'
            f"        <details>\n"
            f"          <summary>Case Summary</summary>\n"
            f'          <div class="summary-body">{summary_body_html}</div>\n"'
            f"        </details>\n"
            f"      </td>\n"
            f"    </tr>"
        )

    table_rows = "\n".join(rows_html)
    page = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="UTF-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        "  <title>CELEX Case Summary Index</title>\n"
        "  <style>\n"
        "    body { font-family: sans-serif; margin: 2rem; color: #1a1a1a; }\n"
        "    h1 { border-bottom: 2px solid #ccc; padding-bottom: 0.4em; }\n"
        "    table { border-collapse: collapse; width: 100%; }\n"
        "    th, td { border: 1px solid #ccc; padding: 0.5rem 0.75rem; text-align: left; vertical-align: top; }\n"
        "    th { background: #f0f0f0; position: sticky; top: 0; }\n"
        "    tr.case-row:nth-of-type(4n+1) { background: #fff; }\n"
        "    tr.case-row:nth-of-type(4n+3) { background: #f9f9f9; }\n"
        "    tr.summary-row td { border-top: none; padding-top: 0; background: inherit; }\n"
        "    details summary { cursor: pointer; color: #0057a8; font-weight: 600; }\n"
        "    .summary-body { margin-top: 0.75rem; padding: 0.75rem; background: #fff;"
        "                    border: 1px solid #ddd; border-radius: 4px; }\n"
        "    .summary-body table { width: auto; font-size: 0.9em; }\n"
        "    .summary-body th, .summary-body td { border: 1px solid #bbb; padding: 3px 8px; }\n"
        "    .summary-body th { background: #eee; }\n"
        "    a { color: #0057a8; }\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <h1>CELEX Case Summary Index</h1>\n"
        "  <table>\n"
        "    <thead>\n"
        "      <tr>\n"
        "        <th>CELEX ID</th>\n"
        "        <th>Case Title</th>\n"
        "        <th>Industry</th>\n"
        "        <th>Cartel Period</th>\n"
        "      </tr>\n"
        "    </thead>\n"
        "    <tbody>\n"
        f"{table_rows}\n"
        "    </tbody>\n"
        "  </table>\n"
        "</body>\n"
        "</html>\n"
    )

    html_path = output_dir / "summary_index.html"
    html_path.write_text(page, encoding="utf-8")
    return html_path


def main() -> None:
    """Fetch case summaries from the database, write Markdown, PDF, and HTML index files."""
    db_url = settings.DATABASE_URL
    if not db_url:
        log.error("DATABASE_URL is not set in settings.")
        return

    output_dir = Path(settings.REPORT_PATH) / _OUTPUT_SUBDIR
    output_dir.mkdir(parents=True, exist_ok=True)
    log.info("Output directory: %s", output_dir)

    df = fetch_index(db_url)
    if df.is_empty():
        log.warning("No rows returned from celex_index_vw. Nothing to write.")
        return

    md_count = 0
    pdf_count = 0
    for row in df.iter_rows(named=True):
        md_path = write_case_markdown(row, output_dir)
        log.debug("Wrote markdown: %s", md_path)
        md_count += 1

        try:
            pdf_path = write_case_pdf(row, output_dir)
            log.debug("Wrote PDF: %s", pdf_path)
            pdf_count += 1
        except Exception:
            log.exception("Failed to write PDF for %s", row.get("celex_id"))

    log.info("Wrote %d Markdown files, %d PDF files.", md_count, pdf_count)

    html_path = write_index_html(df, output_dir)
    log.info("Index written to: %s", html_path)


if __name__ == "__main__":
    main()


# cd ~/a/celex-facts && .venv/bin/python -m reporting.retrieve_case_summary_md2disk
