"""Evidence about a candidate document from its MinerU Markdown (pure).

Reads the title page (decision title, case numbers, subject, date), checks
for a self-declared summary, a letter's closing salutation and the operative
ending ("HAS ADOPTED THIS DECISION", "Done at Brussels"). MinerU places
footnotes after the operative part of long decisions, so footnote blocks are
removed before the ending is read.
"""

import datetime as dt
import re

from config.settings import settings
from pipelines.decide.models import CandidateEvidence, DocumentKind, TitleKey

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_FOOTNOTE = re.compile(r"<small>.*?</small>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_MARKUP = re.compile(r"[#*_]+")
_SPACE = re.compile(r"\s+")

_DECISION_TITLE = re.compile(
    r"COMMISSION\s*DECISION\b|D[ÉE]CISION\s*DE\s*LA\s*COMMISSION\b|(?:BESCHLUSS|ENTSCHEIDUNG)\s*DER\s*KOMMISSION\b",
    re.I)
_SUMMARY = re.compile(
    r"this text is a summary of the decision|summary of commission decision|résumé de la décision|"
    r"zusammenfassung (?:des beschlusses|der entscheidung)", re.I)
_OPERATIVE = re.compile(
    r"has adopted this decision|a adopté la présente décision|a arrêté la présente décision|hat folgenden beschluss erlassen|"
    r"hat folgende entscheidung erlassen|done at brussels|fait à bruxelles|geschehen zu brüssel", re.I)
_LETTER = re.compile(
    r"yours (?:faithfully|sincerely)|veuillez agréer|mit freundlichen grüßen|med venlig hilsen", re.I)
_CASE = re.compile(
    r"(?:\b(?:AT|IV|COMP)[./]+(?:[A-Z]-?\d*[./])?|\b(?:affaire|case|sache|fall)\s+(?:n[o°]s?\.?\s*)?)"
    r"(\d{2}\.?\d{3})\b",
    re.I)
_SUBJECT_STOP = re.compile(
    r"\(text with|\(only the|\(seul|\(nur |table of contents|the commission of|la commission des|"
    r"die kommission der|having regard|vu le traité|gestützt auf", re.I)
_TITLE_BLOCK_CHARS = 800
_DATE_LEAD = re.compile(r"\W{0,6}(?:of the|of|du|vom|dated|de)\s+", re.I)
_PLACE_LEAD = re.compile(r"(?:Brussels|Bruxelles|Brüssel)\s*,?\s*(?:le|den|the)?\s*", re.I)
_VOCABULARY_WORD = re.compile(r"[a-zà-ÿ]{6,}")
_SUBJECT_CHARS = 120

_MONTHS: dict[str, int] = {}
for _names in (
    "january february march april may june july august september october november december",
    "janvier février mars avril mai juin juillet août septembre octobre novembre décembre",
    "januar februar märz april mai juni juli august september oktober november dezember",
):
    for _i, _name in enumerate(_names.split(), start=1):
        _MONTHS.setdefault(_name, _i)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})\s*[./]\s*(\d{1,2})\s*[./]\s*(\d{4})\b")
_ROMAN_MONTHS = {r: i for i, r in enumerate("I II III IV V VI VII VIII IX X XI XII".split(), start=1)}
_ROMAN_DATE = re.compile(r"\b(\d{1,2})\s*[./ ]\s*(XII|XI|X|IX|VIII|VII|VI|V|IV|III|II|I)\s*[./ ]\s*(\d{4})\b")
_WRITTEN_DATE = re.compile(
    r"\b(\d{1,2})(?:er|st|nd|rd|th)?\.?\s+(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\s+(\d{4})\b",
    re.I)


def vocabulary(text: str) -> frozenset[str]:
    """Return the distinct lower-case words of six or more letters.

    Args:
        text: Noise-stripped document text.

    Returns:
        The word set used to compare two documents with the same title.
    """
    return frozenset(_VOCABULARY_WORD.findall(text.lower()))


def strip_noise(markdown: str) -> str:
    """Remove images, footnote blocks, HTML tags and Markdown markers.

    Args:
        markdown: MinerU Markdown.

    Returns:
        Plain text with whitespace collapsed to single spaces.
    """
    text = _FOOTNOTE.sub(" ", _IMAGE.sub(" ", markdown))
    return _SPACE.sub(" ", _MARKUP.sub(" ", _TAG.sub(" ", text))).strip()


def _parse_date(m: re.Match[str]) -> dt.date | None:
    """Turn a numeric, Roman-month or written date match into a date (None if invalid)."""
    day, month, year = m.group(1), m.group(2), m.group(3)
    if month.isdigit():
        number = int(month)
    else:
        number = _ROMAN_MONTHS.get(month) or _MONTHS.get(month.lower(), 0)
    try:
        return dt.date(int(year), number, int(day))
    except ValueError:
        return None


_DATE_PATTERNS = (_NUMERIC_DATE, _ROMAN_DATE, _WRITTEN_DATE)


def _date_starting(text: str) -> dt.date | None:
    """Return the date the text starts with (after whitespace), if any."""
    head = text.lstrip()
    for pattern in _DATE_PATTERNS:
        m = pattern.match(head)
        if m is not None:
            return _parse_date(m)
    return None


def _title_date(head: str, title_end: int) -> dt.date | None:
    """Return the decision's own date.

    Only a date directly after "of/du/vom" following the title counts (an
    amending decision names the amended decision's date later on); else the
    last "Brussels/Bruxelles/Brüssel, <date>" before the title; else None.
    """
    lead = _DATE_LEAD.match(head, title_end)
    if lead is not None:
        own = _date_starting(head[lead.end():])
        if own is not None:
            return own
    place = None
    for m in _PLACE_LEAD.finditer(head, 0, title_end):
        place = _date_starting(head[m.end():]) or place
    return place


def _subject(block: str, case_end: int) -> str:
    """Return the normalised subject following the first case number."""
    rest = block[case_end:case_end + 2 * _SUBJECT_CHARS]
    stop = _SUBJECT_STOP.search(rest)
    rest = rest[:stop.start()] if stop else rest
    words = re.sub(r"[^\w]+", " ", rest.lower()).split()
    return " ".join(words)[:_SUBJECT_CHARS]


def parse_title(head: str) -> tuple[DocumentKind, TitleKey | None]:
    """Classify a title page and build the decision identity.

    Args:
        head: Noise-stripped beginning of the document.

    Returns:
        ``(kind, title_key)``; summary wins over a decision title, a decision
        title naming several cases is ``COMBINED``; the key is None when no
        decision title with a date was found.
    """
    if _SUMMARY.search(head):
        return DocumentKind.SUMMARY, None
    title = _DECISION_TITLE.search(head)
    if title is None:
        return DocumentKind.UNKNOWN, None
    block = head[title.start():title.start() + _TITLE_BLOCK_CHARS]
    doc_date = _title_date(head, title.end())
    cases = list(dict.fromkeys(m.group(1).replace(".", "") for m in _CASE.finditer(block)))
    if doc_date is None:
        # A decision whose title states no date: no identity, the ladder
        # sends it to review instead of guessing a date from the body.
        return DocumentKind.DECISION, None
    first_case = _CASE.search(block)
    key = TitleKey(
        case_numbers=tuple(cases),
        subject=_subject(block, first_case.end()) if first_case else "",
        doc_date=doc_date)
    return (DocumentKind.COMBINED if len(cases) > 1 else DocumentKind.DECISION), key


def has_operative_ending(text: str) -> bool:
    """Return True when the ending of the noise-stripped text holds the operative part.

    Args:
        text: Noise-stripped document text.

    Returns:
        True when an operative marker occurs in the last
        ``DECIDE_ENDING_FRACTION`` of the text.
    """
    start = int(len(text) * (1 - settings.DECIDE_ENDING_FRACTION))
    return _OPERATIVE.search(text[start:]) is not None


def extract_evidence(
    markdown: str,
    *,
    document_id: str,
    decision_id: str,
    origin: str,
    sha256: str | None,
    local_file_pdf: str,
    local_file_md: str,
    language: str | None,
) -> CandidateEvidence:
    """Build the evidence of one candidate.

    Args:
        markdown: The candidate's MinerU Markdown.
        document_id: Registry document id.
        decision_id: Manifest decision id.
        origin: Source site.
        sha256: PDF hash.
        local_file_pdf: Stored PDF path.
        local_file_md: Markdown path.
        language: Upper-case detected language (the caller detects it).

    Returns:
        The evidence.
    """
    text = strip_noise(markdown)
    kind, key = parse_title(text[:settings.DECIDE_TITLE_REGION_CHARS])
    if kind is DocumentKind.UNKNOWN and _LETTER.search(text[-settings.DECIDE_LETTER_TAIL_CHARS:]):
        kind = DocumentKind.LETTER
    return CandidateEvidence(
        document_id=document_id, decision_id=decision_id, origin=origin, sha256=sha256,
        local_file_pdf=local_file_pdf, local_file_md=local_file_md, language=language,
        kind=kind, title_key=key, operative=has_operative_ending(text), md_chars=len(markdown),
        text_chars=len(text),
        vocabulary=vocabulary(text))
