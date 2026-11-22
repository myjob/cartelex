"""Adoption date of a Commission decision from its own text (pure, DB-free).

The date a decision was adopted is stated twice in the decision itself: in the
signature line closing the operative part ("Done at Brussels, 31 March 2011" /
"Fait à Bruxelles, le ..." / "Geschehen zu Brüssel am ...") and in the title
("COMMISSION DECISION of 31 March 2011 ..."). The signature line wins; the
title is the fallback because MinerU often keeps "Done at Brussels," with the
date left blank. Dates cited in the body (earlier decisions, the decision an
amending decision amends) are never candidates (ISSUES #369, review A26).

The date embedded in a ``DEC-AT<case>-YYYYMMDD`` id and the human index facts
are independent references. They are compared with the value, never used as
its source: :func:`reference_conflicts` lists every reference that disagrees.

Exports:
    DateSource, AdoptionDate -- where the date was found, and the result.
    DateReference, ReferenceConflict -- a disagreeing reference.
    adoption_date -- the decision's adoption date from its text.
    id_date -- the date embedded in a ``DEC-*-YYYYMMDD`` id.
    parse_date_value, parse_year_value -- stored date / year values.
    reference_conflicts -- the references that disagree with a date.
"""

import datetime as dt
import logging
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from pipelines.decide.evidence import MONTH_NUMBERS, date_starting, strip_noise, title_date

logger = logging.getLogger(__name__)

_PLACE = r"(?:brussels|bruxelles|brüssel|luxembourg|luxemburg|strasbourg|straßburg)"
_PLACE_TAIL = r"\s*,?\s*(?:le|am|den|on|the)?\s*"
# "Done at / Made in / Fait à / Geschehen zu <place>, <date>": "Made in" is how
# machine translations of older decisions render "Fait à".
_DATE_LINE = re.compile(
    r"\b(?:(?:done|made|given)\s+(?:at|in)|fait\s+à|geschehen\s+zu)\s+" + _PLACE + _PLACE_TAIL, re.I)
# Older decisions sign off with a bare "Brussels, <date>" line. A bare place
# counts only when a Commission sign-off follows within _SIGN_OFF_CHARS, which
# keeps out the "Brussels, <date> C(2011) 3640 final" header and addresses.
_BARE_PLACE = re.compile(r"\b" + _PLACE + r"\s*,\s*(?:le|am|den|on|the)?\s*", re.I)
_SIGN_OFF = re.compile(
    r"\b(?:for|by|on\s+behalf\s+of)\s+the\s+commission|für\s+die\s+kommission|"
    r"(?:pour|par)\s+la\s+commission", re.I)
_SIGN_OFF_CHARS = 80
# Title forms the decide parser does not cover: "DECISION OF THE COMMISSION of /
# dated / vom <date>", including translation slips ("you" for "du").
_TITLE_ANY = re.compile(
    r"\b(?:commission\s+decision|decision\s+of\s+the\s+commission|d[ée]cision\s+de\s+la\s+commission|"
    r"(?:entscheidung|beschluss)\s+der\s+kommission)\b", re.I)
_TITLE_LEAD = re.compile(r"\b(?:of|dated|du|vom|you|from|de|on)\s+(?:the\s+)?", re.I)
# Short on purpose: a date further on names another (amended, cited) decision.
_TITLE_LEAD_CHARS = 40
# Month-first dates ("June 1, 1964", "July8,1965") left by machine translation.
_MONTH_FIRST = re.compile(
    r"(" + "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True)) + r")\s*(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})\b",
    re.I)
# Day-month-year with dashes ("26-05-2004"), which the decide parser does not read.
_DASH_DATE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})\b")
# Brackets and parentheses MinerU or redaction leave before a date ("Of [1 October 2008]").
_DATE_OPENERS = "[( "
_QUOTE_TAIL_CHARS = 40
_DEC_ID_DATE = re.compile(r"^DEC-\S+-(\d{4})(\d{2})(\d{2})$")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR = re.compile(r"\b(1[89]\d{2}|2\d{3})\b")


class DateSource(StrEnum):
    """Where in the decision the adoption date was read."""

    DATE_LINE = "date_line"
    TITLE = "title"
    NONE = "none"


class AdoptionDate(BaseModel):
    """The adoption date of one decision with the text it was read from."""

    model_config = ConfigDict(frozen=True)

    date: dt.date | None = Field(default=None, description="Adoption date; None when the text states none.")
    source: DateSource = Field(default=DateSource.NONE, description="Which part of the decision gave the date.")
    quote: str | None = Field(default=None, description="Verbatim noise-stripped text the date was read from.")


class DateReference(StrEnum):
    """An independent reference the adoption date is checked against."""

    DEC_ID_DATE = "dec_id_date"
    HUMAN_INDEX_DECISION_DATE = "human_index_decision_date"
    HUMAN_INDEX_YEAR = "human_index_year"


class ReferenceConflict(BaseModel):
    """A reference whose date disagrees with the adoption date."""

    model_config = ConfigDict(frozen=True)

    reference: DateReference = Field(description="The disagreeing reference.")
    expected: str = Field(description="The reference's value (ISO date, or year for the year reference).")

    def label(self) -> str:
        """Return the conflict as ``reference=expected`` for ledger columns.

        Returns:
            For example ``dec_id_date=2011-03-31``.
        """
        return f"{self.reference.value}={self.expected}"


def _date_at_start(text: str) -> dt.date | None:
    """Return the date the text starts with: day-first, month-first or dashed."""
    text = text.lstrip().lstrip(_DATE_OPENERS)
    date = date_starting(text)
    if date is not None:
        return date
    month_first, dashed = _MONTH_FIRST.match(text), _DASH_DATE.match(text)
    try:
        if month_first is not None:
            return dt.date(int(month_first.group(3)), MONTH_NUMBERS[month_first.group(1).lower()],
                           int(month_first.group(2)))
        if dashed is not None:
            return dt.date(int(dashed.group(3)), int(dashed.group(2)), int(dashed.group(1)))
    except ValueError:
        return None
    return None


def _signature_date(text: str) -> AdoptionDate | None:
    """Return the last dated signature line ("Done at Brussels, <date>").

    A bare "Brussels, <date>" counts only when a Commission sign-off follows.
    """
    found: AdoptionDate | None = None
    candidates = [(m, False) for m in _DATE_LINE.finditer(text)]
    full_ends = {m.end() for m, _ in candidates}
    # The place inside "Done at Brussels, <date>" is not a second, bare line.
    candidates += [(m, True) for m in _BARE_PLACE.finditer(text) if m.end() not in full_ends]
    for m, bare in sorted(candidates, key=lambda c: c[0].start()):
        if bare and _SIGN_OFF.search(text, m.end(), m.end() + _SIGN_OFF_CHARS) is None:
            continue
        date = _date_at_start(text[m.end():])
        if date is not None:
            quote = text[m.start():m.end() + _QUOTE_TAIL_CHARS].strip()
            found = AdoptionDate(date=date, source=DateSource.DATE_LINE, quote=quote)
    return found


def _any_title_date(head: str) -> dt.date | None:
    """Return the date right after any decision title form, or after its first "of/dated/vom"."""
    first = _TITLE_ANY.search(head)
    if first is None:
        return None
    # Garbled titles repeat the form ("COMMISSION DECISION OF THE COMMISSION
    # 6 November 1968"): try each form starting close to the first one, never a
    # decision cited further down.
    title = first
    while title is not None:
        direct = _date_at_start(head[title.end():])
        if direct is not None:
            return direct
        for lead in _TITLE_LEAD.finditer(head, title.end(), title.end() + _TITLE_LEAD_CHARS):
            date = _date_at_start(head[lead.end():])
            if date is not None:
                return date
        title = _TITLE_ANY.search(head, title.start() + 1, first.start() + _TITLE_LEAD_CHARS)
    return None


def adoption_date(markdown: str) -> AdoptionDate:
    """Read the adoption date of a decision from its text.

    Args:
        markdown: The decision's MinerU Markdown, or its chunks joined in
            ``doc_pos`` order. Footnote blocks are removed before reading.

    Returns:
        The date of the last dated signature line ("Done at / Made in / Fait à
        / Geschehen zu <place>, <date>", or a bare "Brussels, <date>" followed
        by the Commission sign-off); else the date in the decision's title;
        else an empty result.
    """
    text = strip_noise(markdown)
    found = _signature_date(text)
    if found is not None:
        return found
    head = text[:settings.DECIDE_TITLE_REGION_CHARS]
    # The first title form in the text anchors the search; the decide parser
    # only knows "COMMISSION DECISION" and would take a decision cited later
    # in the head when the title reads "DECISION OF THE COMMISSION".
    date = _any_title_date(head) or title_date(head)
    if date is not None:
        return AdoptionDate(date=date, source=DateSource.TITLE, quote=head[:_QUOTE_TAIL_CHARS * 4].strip())
    return AdoptionDate()


def id_date(decision_id: str) -> dt.date | None:
    """Return the date embedded in a ``DEC-AT<case>-YYYYMMDD`` decision id.

    Args:
        decision_id: Any decision key (CELEX ids and case keys carry no date).

    Returns:
        The embedded date, or None when the id has none or it is invalid.
    """
    m = _DEC_ID_DATE.match(decision_id or "")
    if m is None:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_date_value(value: str | None) -> dt.date | None:
    """Parse a stored date value: ISO first, then a numeric or written date.

    Args:
        value: A fact's ``value_md`` (human index date, LLM answer); ``NA``,
            empty and unparseable values give None.

    Returns:
        The first ISO date in the value, else the date the value starts with.
    """
    if not value:
        return None
    m = _ISO_DATE.search(value)
    if m is not None:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return date_starting(value)


def parse_year_value(value: str | None) -> int | None:
    """Parse a stored year value such as the human index year.

    Args:
        value: A fact's ``value_md``; ``NA`` and empty values give None.

    Returns:
        The first four-digit year in the value, or None.
    """
    m = _YEAR.search(value or "")
    return int(m.group(1)) if m is not None else None


def reference_conflicts(
    value: dt.date,
    decision_id: str,
    human_decision_date: str | None = None,
    human_year: str | None = None,
) -> list[ReferenceConflict]:
    """List the references that disagree with an adoption date.

    A reference that is absent or unparseable (``NA``) is no conflict.

    Args:
        value: The adoption date to check.
        decision_id: The decision key; only ``DEC-*-YYYYMMDD`` ids carry a date.
        human_decision_date: ``value_md`` of the human index decision date fact.
        human_year: ``value_md`` of the human index year fact.

    Returns:
        One conflict per disagreeing reference, in ``DateReference`` order.
    """
    conflicts: list[ReferenceConflict] = []
    from_id = id_date(decision_id)
    if from_id is not None and from_id != value:
        conflicts.append(ReferenceConflict(reference=DateReference.DEC_ID_DATE, expected=from_id.isoformat()))
    human_date = parse_date_value(human_decision_date)
    if human_date is not None and human_date != value:
        conflicts.append(ReferenceConflict(
            reference=DateReference.HUMAN_INDEX_DECISION_DATE, expected=human_date.isoformat()))
    year = parse_year_value(human_year)
    if year is not None and year != value.year:
        conflicts.append(ReferenceConflict(reference=DateReference.HUMAN_INDEX_YEAR, expected=str(year)))
    return conflicts
