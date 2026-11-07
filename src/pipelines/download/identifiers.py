"""Identifier rules of the HTTP document downloader.

Decision ids double as master JSON key and folder name; document ids are
file stems. Ids that are not filesystem-safe raise ``UnsafeIdentifier``
instead of being silently slugified.

Case number rule (:func:`case_number`): the case number is the LONGEST run
of digits in the case id, where dots and spaces inside a run are ignored
(the first run wins a tie), with leading zeros stripped. So ``IV/32.800`` and
``AT.32800`` are ``32800``; a sub-file suffix or an office prefix is not the
number (``IV/34179-1`` -> ``34179``, ``COMP/E-1/38.069`` -> ``38069``,
``IV/30.373/F3`` -> ``30373``). A CELEX number written in the case field
(``31987D0359``) is not a case id and gives None.
"""

import datetime as dt
import hashlib
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit

from pipelines.download.selector import PDF_FORMAT

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CELEX_PREFIX = "CELEX-"
DEC_PREFIX = "DEC-"
_CELEX_SYNTAX = re.compile(
    r"^(?P<base>[0-9CE]\d{4}[A-Z]{1,2}\d{4,})(?:\((?P<n>\d+)\)|_(?P<u>\d+))?$"
)
_EURLEX_LANG = re.compile(r"/([A-Za-z]{2})/TXT/(?:PDF|HTML)/?", re.IGNORECASE)
_FALLBACK_HEX = 8
_FORMAT_SAFE = re.compile(r"[^a-z0-9]")
_DBCOMP_DOC = re.compile(r"document_(\d+)", re.IGNORECASE)
_EURLEX_HOST = "eur-lex.europa.eu"
_DBCOMP_HOST = "db-comp.eu"
_IGNORED_QUERY_KEYS = frozenset({"from"})
_DIGIT_RUN = re.compile(r"\d(?:[\d. ]*\d)?")
_CELEX_LIKE = re.compile(r"^\s*[0-9CE]\d{4}[A-Z]{1,2}\d{4}", re.IGNORECASE)
_MASTER_DATE = re.compile(r"^\s*(\d{4})[-/](\d{2})[-/](\d{2})")
_SUFFIX = re.compile(r"-(\d+)(?!\d)")


class UnsafeIdentifier(ValueError):
    """Raised when an identifier is not filesystem-safe or cannot be built."""


def _require_safe(value: str) -> str:
    """Validate that an identifier is filesystem-safe.

    Args:
        value: Candidate identifier.

    Returns:
        The unchanged identifier.

    Raises:
        UnsafeIdentifier: If it contains characters outside ``[A-Za-z0-9._-]``
            or does not start with an alphanumeric character.
    """
    if not _SAFE_ID.fullmatch(value) or ".." in value:
        raise UnsafeIdentifier(f"identifier not filesystem-safe: {value!r}")
    return value


def normalize_case_id(raw: str) -> str:
    """Trim and collapse whitespace of a competition case id.

    Args:
        raw: Raw case id, e.g. ``"AT.35388 "``.

    Returns:
        The case id with surrounding whitespace removed and inner runs of
        whitespace collapsed to one space; other variants stay as-is.
    """
    return " ".join(raw.split())


def celex_key(celex: str) -> str:
    """Build the prefixed CELEX key, the decision id spelling of a CELEX number.

    ``31964D0233`` -> ``CELEX-31964D0233``; ``32002D0758(01)`` ->
    ``CELEX-32002D0758_01``. An existing ``CELEX-`` prefix (any case) is
    stripped first, so it is never doubled, and an already converted ``_NN``
    suffix is kept, so the function is idempotent. The number is upper-cased
    and must have real CELEX syntax: sector digit or ``C``/``E``, 4-digit
    year, 1-2 letters, 4 or more digits, optional ``(NN)`` or ``_NN``.
    Master keys such as ``c1417`` or ``35388_9_5`` are rejected, so callers
    can fall back to the master-key path.

    Args:
        celex: CELEX number, bare or prefixed, optionally with a
            parenthesised suffix.

    Returns:
        ``CELEX-<number>[_<suffix>]``.

    Raises:
        UnsafeIdentifier: If the input does not have CELEX syntax or the
            result is not filesystem-safe.
    """
    value = celex.strip().upper()
    if value.startswith(_CELEX_PREFIX):
        value = value[len(_CELEX_PREFIX):]
    match = _CELEX_SYNTAX.fullmatch(value)
    if match is None:
        raise UnsafeIdentifier(f"unusable CELEX number: {celex!r}")
    number = match["n"] or match["u"]
    suffix = f"_{number}" if number else ""
    return _require_safe(f"{_CELEX_PREFIX}{match['base']}{suffix}")


def decision_id_for(
    celex_number: str | None,
    existing_key: str | None,
    first_case: str | None,
    decision_date: dt.date | None,
    taken: set[str],
) -> tuple[str, list[str]]:
    """Build the decision id (master key and folder name).

    Order: CELEX, existing master key, then ``DEC-<case>-<YYYYMMDD>``.

    Args:
        celex_number: CELEX number, e.g. ``32002D0758(01)``, or None; built
            into the id by :func:`celex_key`.
        existing_key: Matched existing master key to reuse, or None.
        first_case: First case id for the fallback form, or None.
        decision_date: Decision date for the fallback form, or None.
        taken: Ids assigned earlier in the same run (not the registry, so
            re-runs do not collide with themselves). All three branches are
            collision-checked; a clash gets a ``_2``, ``_3`` suffix (also on
            CELEX ids; the decision_registry.celex_number column keeps the
            unsuffixed ``celex_key`` spelling) and a review reason.

    Returns:
        Tuple of the decision id and a list of review reasons.

    Raises:
        UnsafeIdentifier: If no id can be built or the result is unsafe.
    """
    if celex_number:
        base = celex_key(celex_number)
    elif existing_key:
        base = _require_safe(existing_key)
    else:
        if not first_case or decision_date is None:
            raise UnsafeIdentifier("no CELEX, existing key, or case plus date")
        case_part = re.sub(r"[^A-Za-z0-9]", "", normalize_case_id(first_case))
        if not case_part:
            raise UnsafeIdentifier(f"case id has no alphanumerics: {first_case!r}")
        base = _require_safe(f"{DEC_PREFIX}{case_part}-{decision_date:%Y%m%d}")
    if base not in taken:
        return base, []
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    logger.warning("decision id collision for %s", base)
    return f"{base}_{n}", [f"decision id collision: {base} already taken"]


def pdf_url_key(url: str) -> str:
    """Build the spelling-insensitive comparison key of a PDF URL.

    The scheme is ignored (``http`` and ``https`` compare equal), the host is
    lower-cased and the eur-lex ``from`` query parameter is dropped, so
    ``...?uri=CELEX:X&from=EN`` and ``...?uri=CELEX:X`` are the same document.

    Args:
        url: PDF URL.

    Returns:
        ``<host><path>?<remaining query>``; only for equality checks.
    """
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _IGNORED_QUERY_KEYS]
    return f"{parts.netloc.lower()}{parts.path}?{urlencode(query)}"


def detect_language(url: str) -> str | None:
    """Detect the language from an eur-lex ``/EN/TXT/(PDF|HTML)/`` URL marker.

    Args:
        url: Document URL.

    Returns:
        Upper-case language code, or None for other origins or no marker.
    """
    parsed = urlparse(url)
    if parsed.hostname != _EURLEX_HOST:
        return None
    match = _EURLEX_LANG.search(parsed.path)
    return match.group(1).upper() if match else None


def document_id_for(
    url: str, decision_id: str, file_format: str = PDF_FORMAT
) -> str:
    """Derive the document id (file stem) from its URL.

    Non-pdf formats get a ``_<format>`` suffix so an HTML and a PDF record of
    the same language cannot collide.

    Args:
        url: Document URL.
        decision_id: Id of the owning decision (used for eur-lex documents).
        file_format: Document format, ``pdf`` by default.

    Returns:
        ``<decision_id>_<LANG>`` for eur-lex, ``dbcomp-<n>`` for db-comp,
        otherwise the URL's file name stem; plus ``_<format>`` when the
        format is not pdf. A non-pdf URL with no deterministic id gets
        ``<decision_id>_other_<8 hex of sha256(url)>``.

    Raises:
        UnsafeIdentifier: If no safe id can be derived for a pdf URL (or the
            decision id itself is unsafe).
    """
    fmt = file_format.strip().lower()
    if not fmt or fmt == PDF_FORMAT:
        return _base_document_id(url, decision_id)
    try:
        base = _base_document_id(url, decision_id)
    except UnsafeIdentifier:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:_FALLBACK_HEX]
        return _require_safe(f"{decision_id}_other_{digest}")
    return _require_safe(f"{base}_{_FORMAT_SAFE.sub('', fmt) or 'other'}")


def _base_document_id(url: str, decision_id: str) -> str:
    """Derive the format-independent document id of a URL.

    Args:
        url: Document URL.
        decision_id: Id of the owning decision (used for eur-lex documents).

    Returns:
        The id before any format suffix.

    Raises:
        UnsafeIdentifier: If no safe id can be derived.
    """
    parsed = urlparse(url)
    if parsed.hostname == _EURLEX_HOST:
        language = detect_language(url)
        if language is None:
            raise UnsafeIdentifier(f"eur-lex URL without language marker: {url}")
        return _require_safe(f"{decision_id}_{language}")
    if parsed.hostname == _DBCOMP_HOST:
        match = _DBCOMP_DOC.search(parsed.path)
        if match is not None:
            return _require_safe(f"dbcomp-{match.group(1)}")
        # slug URLs (db-comp.eu/metal-packaging.download) use the file stem
    name = parsed.path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if not stem:
        raise UnsafeIdentifier(f"no file name in URL: {url}")
    return _require_safe(stem)


def case_number(case_id: str) -> str | None:
    """Reduce a competition case id to its case number (digits only).

    Args:
        case_id: Case id in any spelling (``IV/32.800``, ``AT.32800``,
            ``COMP/E-1/38.069``, ``IV/34179-1``, ``36571``).

    Returns:
        The longest digit run (dots and spaces removed, leading zeros
        stripped), or None for a blank, digit-free or CELEX-shaped value.
    """
    if not case_id or _CELEX_LIKE.match(case_id):
        return None
    runs = [re.sub(r"[. ]", "", m.group()) for m in _DIGIT_RUN.finditer(case_id)]
    if not runs:
        return None
    longest = max(runs, key=len)
    return longest.lstrip("0") or "0"


def case_suffix(case_id: str) -> tuple[str, str] | None:
    """Return the case number and its sub-file suffix (``IV/34179-1``).

    Args:
        case_id: Case id in any spelling.

    Returns:
        ``(case number, suffix)`` when ``-<digits>`` directly follows the
        case number's digit run (``IV/34179-1`` -> ``("34179", "1")``), else
        None.
    """
    number = case_number(case_id)
    if number is None:
        return None
    for run in _DIGIT_RUN.finditer(case_id):
        digits = re.sub(r"[. ]", "", run.group()).lstrip("0") or "0"
        suffix = _SUFFIX.match(case_id, run.end())
        if digits == number and suffix:
            return number, suffix.group(1).lstrip("0") or "0"
    return None


def celex_of(value: object) -> str | None:
    """Return the CELEX key of a CELEX-shaped value, else None.

    Args:
        value: A key or a case field.

    Returns:
        The :func:`celex_key` spelling, or None.
    """
    if not isinstance(value, str) or not (
            value.upper().startswith(_CELEX_PREFIX) or _CELEX_LIKE.match(value)):
        return None
    try:
        return celex_key(value)
    except UnsafeIdentifier:
        return None


def master_date(value: object) -> str | None:
    """Normalise a master ``decision_date`` to ``YYYY-MM-DD``.

    Args:
        value: Stored value (``2002-06-11``, ``2002-06-11 00:00:00``,
            ``2002/06/11``), or anything else.

    Returns:
        The ISO date, or None when the value is blank or not a date.
    """
    match = _MASTER_DATE.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        return dt.date(*(int(g) for g in match.groups())).isoformat()
    except ValueError:
        return None
