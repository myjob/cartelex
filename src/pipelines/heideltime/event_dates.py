"""Partial-precision event dates: the one parser every HeidelTime consumer shares.

A decision states a date at whatever precision its evidence supports -- a
year, a year and month, or a full day -- and the pipeline stores it at that
precision (``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``). HeidelTime and the LLM also
emit strings of that shape that are no date at all: years such as ``0010`` or
``2202`` (#187) and impossible calendar values such as month ``00`` (#190).
:func:`parse_partial_date` accepts the first and rejects the second, so the
extractor, the prompt digest and the lineage matcher agree on what a date is.
"""

import calendar
import datetime
import re

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings

_PARTIAL_ISO_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


class PartialDate(BaseModel):
    """A calendar date known to year, month or day precision.

    Attributes:
        year: Four-digit year.
        month: Month 1-12, or None for a year-only date.
        day: Day of month, or None for a year or year-month date.
    """

    model_config = ConfigDict(frozen=True)

    year: int = Field(description="Four-digit year")
    month: int | None = Field(default=None, ge=1, le=12, description="Month 1-12; None for year precision")
    day: int | None = Field(default=None, ge=1, le=31, description="Day of month; None above day precision")

    @property
    def first_day(self) -> datetime.date:
        """Return the first calendar day the date covers (its start bound)."""
        return datetime.date(self.year, self.month or 1, self.day or 1)

    @property
    def last_day(self) -> datetime.date:
        """Return the last calendar day the date covers (its end bound)."""
        month = self.month or 12
        day = self.day or calendar.monthrange(self.year, month)[1]
        return datetime.date(self.year, month, day)

    @property
    def text(self) -> str:
        """Return the date as ISO text at its stated precision."""
        parts = [f"{self.year:04d}"]
        if self.month is not None:
            parts.append(f"{self.month:02d}")
        if self.day is not None:
            parts.append(f"{self.day:02d}")
        return "-".join(parts)


def parse_partial_date(value: object) -> PartialDate | None:
    """Parse an ISO date of year, month or day precision, or reject it.

    Args:
        value: Candidate date text, e.g. ``"1994"``, ``"1994-03"``,
            ``"1994-03-15"``; surrounding whitespace is ignored.

    Returns:
        The parsed :class:`PartialDate`, or None when ``value`` is not a
        string of that shape, is not a real calendar date, or has a year
        outside ``settings.HEIDELTIME_MIN_YEAR..HEIDELTIME_MAX_YEAR``.
    """
    if not isinstance(value, str):
        return None
    match = _PARTIAL_ISO_DATE.match(value.strip())
    if match is None:
        return None
    year, month, day = (int(group) if group else None for group in match.groups())
    if not settings.HEIDELTIME_MIN_YEAR <= year <= settings.HEIDELTIME_MAX_YEAR:
        return None
    try:
        parsed = PartialDate(year=year, month=month, day=day)
        if day is not None:
            datetime.date(year, month, day)
    except ValueError:
        return None
    return parsed
