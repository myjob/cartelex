"""Shared row rules of the loader mappers.

Cell cleaning, id and date parsing, review-reason rules and the decision
assembly used by ``mappers``. No behaviour of its own beyond those rules.

Conventions:

* ``loader_row`` is the 1-based position of the row among the data rows as
  read (``reader.read_loader`` drops blank rows); it is not a spreadsheet
  row number.
* ``DecisionRequest.decision_id`` and every candidate ``document_id`` are
  PROVISIONAL: the decision id is built with ``taken=set()``. The
  orchestrator assigns the final decision id (collision suffix) and then
  re-derives every candidate id from (source_url, final decision_id,
  file_format) via ``identifiers.document_id_for``.
* A row whose identity cannot be built becomes a ``RowFailure``; nothing is
  raised. A candidate URL with no usable document id is dropped into the
  failures while the decision is kept.
"""

import datetime as dt
import logging
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from config.settings import settings
from pipelines.download.identifiers import (
    UnsafeIdentifier,
    celex_key,
    decision_id_for,
    detect_language,
    document_id_for,
    normalize_case_id,
)
from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    MapperResult,
    RowFailure,
)
from pipelines.download.selector import PDF_FORMAT

logger = logging.getLogger(__name__)

PIPE = "|"
CASE_SEP = ";"
HTML_FORMAT = "html"
CELEX_IN_URL = re.compile(r"CELEX:([0-9A-Za-z()]+)", re.IGNORECASE)
REASON_DUP_CELEX = "duplicate CELEX within file"
REASON_NON_CELEX_KEY = "non-CELEX master key"
EC_ORIGIN = "ec.europa.eu"
_ORIGINS = {
    "eur-lex.europa.eu": "eur-lex",
    EC_ORIGIN: EC_ORIGIN,
    "db-comp.eu": "db-comp",
}


def clean(value: object) -> str:
    """Return a stripped string, blank for None.

    Args:
        value: Cell value.

    Returns:
        The stripped string; ``""`` for None.
    """
    return "" if value is None else str(value).strip()


def cell(row: dict[str, object], name: str) -> str:
    """Read a stripped cell by header name.

    Args:
        row: Row dict.
        name: Column name.

    Returns:
        The stripped value, ``""`` when missing or null.
    """
    return clean(row.get(name))


def split_cell(value: str, sep: str) -> list[str]:
    """Split on a separator, trim and drop blanks.

    Args:
        value: Cell text.
        sep: Separator.

    Returns:
        Non-blank trimmed parts.
    """
    return [p.strip() for p in value.split(sep) if p.strip()]


def case_ids(value: str) -> list[str]:
    """Split a ``;`` separated case cell into normalised case ids.

    Args:
        value: Cell text.

    Returns:
        Normalised case ids, in order, without duplicates.
    """
    out: list[str] = []
    for part in split_cell(value, CASE_SEP):
        case = normalize_case_id(part)
        if case not in out:
            out.append(case)
    return out


def parse_date(value: str) -> dt.date | None:
    """Parse an ISO date (first 10 characters).

    Args:
        value: Cell text.

    Returns:
        The date, or None when blank or unparseable.
    """
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def origin_of(url: str) -> str:
    """Classify the source site of a URL.

    Args:
        url: Document URL.

    Returns:
        eur-lex, ec.europa.eu, db-comp or other.
    """
    return _ORIGINS.get(urlparse(url).hostname or "", "other")


def infer_format(url: str, default: str = PDF_FORMAT) -> str:
    """Infer the document format from a URL.

    Args:
        url: Document URL.
        default: Format returned when the URL shows neither HTML nor PDF.

    Returns:
        ``html`` for HTML URLs, ``pdf`` for PDF URLs (``.pdf``, eur-lex
        ``/TXT/PDF``, db-comp ``.download``), otherwise ``default``.
    """
    low = url.lower()
    path = low.split("?")[0]
    if "/txt/html" in low or path.endswith((".html", ".htm")):
        return HTML_FORMAT
    if "/txt/pdf" in low or path.endswith((".pdf", ".download")):
        return PDF_FORMAT
    return default


def duplicate_celex(values: list[str]) -> set[str]:
    """Find CELEX keys occurring more than once.

    Args:
        values: Raw CELEX cells (blank ones ignored).

    Returns:
        Keys (``celex_key`` spelling) that repeat. Unusable values are
        ignored here; they fail or fall back later in the row.
    """
    keys: list[str] = []
    for value in values:
        if value:
            try:
                keys.append(celex_key(value))
            except UnsafeIdentifier:
                continue
    return {k for k, n in Counter(keys).items() if n > 1}


def is_duplicate(celex: str, dups: set[str]) -> bool:
    """Check whether a CELEX value is among the duplicated keys.

    Args:
        celex: Raw CELEX cell.
        dups: Duplicated keys from :func:`duplicate_celex`.

    Returns:
        True when the value's key repeats in the file.
    """
    try:
        return celex_key(celex) in dups
    except UnsafeIdentifier:
        return False


def collect_meta(row: dict[str, object], used: set[str]) -> dict[str, str]:
    """Collect every unused loader column verbatim.

    Args:
        row: Row dict.
        used: Column names mapped to typed fields.

    Returns:
        Column name to stripped string (blank for null).
    """
    return {k: clean(v) for k, v in row.items() if k not in used}


def _fail(result: MapperResult, loader_file: Path, row_no: int, reason: str) -> None:
    """Append a row failure.

    Args:
        result: Result to append to.
        loader_file: Loader path.
        row_no: 1-based data-row number.
        reason: Failure reason.
    """
    result.failures.append(
        RowFailure(loader_file=loader_file.name, loader_row=row_no, reason=reason)
    )


def _candidates(
    urls: list[tuple[str, str, str | None]],
    decision_id: str,
    loader_file: Path,
    row_no: int,
    result: MapperResult,
) -> list[CandidateDocument]:
    """Build candidate documents, dropping unusable URLs into the failures.

    Args:
        urls: Triples (url, format or blank to infer, manifest_row_id).
        decision_id: Provisional decision id.
        loader_file: Loader path.
        row_no: 1-based data-row number.
        result: Result receiving candidate failures.

    Returns:
        Candidates with distinct URLs.
    """
    out: list[CandidateDocument] = []
    seen: set[str] = set()
    for url, fmt, mrid in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        fmt = fmt.strip().lower() or infer_format(url)
        try:
            doc_id = document_id_for(url, decision_id, fmt)
        except UnsafeIdentifier as exc:
            _fail(result, loader_file, row_no, f"candidate dropped: {exc}")
            continue
        out.append(CandidateDocument(
            document_id=doc_id, source_url=url, file_format=fmt,
            language=detect_language(url), origin=origin_of(url),
            manifest_row_id=mrid))
    return out


def build_decision(
    *,
    loader_file: Path,
    row_no: int,
    celex: str,
    cases: list[str],
    date: dt.date | None,
    fields: dict[str, str],
    meta: dict[str, str],
    reasons: list[str],
    urls: list[tuple[str, str, str | None]],
    result: MapperResult,
    existing_key: str | None = None,
    celex_is_master_key: bool = False,
) -> None:
    """Assemble one decision (or a failure) into the result.

    Args:
        loader_file: Loader path.
        row_no: 1-based data-row number.
        celex: Raw CELEX cell (blank allowed).
        cases: Normalised case ids.
        date: Decision date or None.
        fields: title, parties and document_class values.
        meta: Verbatim extra columns.
        reasons: Review reasons collected so far (extended in place).
        urls: Candidate triples (url, format or blank, manifest_row_id).
        result: Result to append to.
        existing_key: Master key used when there is no CELEX.
        celex_is_master_key: When True and the ``celex`` value is not a
            usable CELEX number, it is used as a master key instead (celex_number
            None, review reason added).
    """
    celex_number: str | None = celex or None
    try:
        decision_id, _ = decision_id_for(
            celex_number, existing_key, cases[0] if cases else None, date, set())
    except UnsafeIdentifier as exc:
        if not (celex and celex_is_master_key):
            _fail(result, loader_file, row_no, str(exc))
            return
        try:
            decision_id, _ = decision_id_for(
                None, celex, cases[0] if cases else None, date, set())
        except UnsafeIdentifier as exc2:
            _fail(result, loader_file, row_no, str(exc2))
            return
        celex_number = None
        reasons.append(REASON_NON_CELEX_KEY)
    candidates = _candidates(urls, decision_id, loader_file, row_no, result)
    limit = settings.DOWNLOAD_REVIEW_MAX_CASE_PDFS
    case_pdfs = {c.source_url for c in candidates
                 if c.file_format == PDF_FORMAT and c.origin == EC_ORIGIN}
    if len(case_pdfs) > limit:
        reasons.append(f"more than {limit} distinct {EC_ORIGIN} case PDFs")
    result.decisions.append(DecisionRequest(
        decision_id=decision_id, celex_number=celex_number, case_ids=cases,
        decision_date=date, title=fields.get("title", ""),
        parties=fields.get("parties", ""),
        document_class=fields.get("document_class", ""), meta=meta,
        review_reasons=reasons, candidates=candidates,
        loader_file=loader_file.name, loader_row=row_no))
