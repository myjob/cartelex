"""Compact, bounded prompt input from a stored ``heideltime_event`` fact.

The aggregate modules (``heideltime_event_agg``, ``heideltime_cartel_period``)
used to send ``str(value_json)`` -- the whole serialised fact, up to 177k
characters / 51k tokens -- as one prompt (#186, #188). The digest turns it into
one ``date | actor | predicate`` line per real event, chronologically, which
halves the token count, and :func:`split_batches` cuts it into contiguous
batches under a token budget so no single prompt is unbounded.

Dropped on the way: rows whose date :func:`parse_partial_date` rejects (#187,
#190) and filler rows whose predicate says nothing happened. A ``redacted``
actor is *kept*: in published decisions the name is blacked out but the act
(a meeting, a price agreement) is real and may bound the cartel period.
"""

import json
import logging
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from pipelines.heideltime.event_dates import parse_partial_date

logger = logging.getLogger(__name__)

_LINE_SEPARATOR = "\n"
_FIELD_SEPARATOR = " | "
# Predicates the event extractor writes when a date carries no activity
# (HeidelTimeEventResult asks for 'no activity described in text').
_FILLER_PREDICATES: frozenset[str] = frozenset({"", "unknown", "none", "na", "n/a"})
_FILLER_PREFIX = "no activity"


class EventLine(BaseModel):
    """One dated event of the digest.

    Attributes:
        date: Validated ISO date at its stated precision.
        actor: Named party, or ``redacted`` / ``unknown``.
        predicate: What happened on that date.
    """

    model_config = ConfigDict(frozen=True)

    date: str = Field(description="Validated ISO date: YYYY, YYYY-MM or YYYY-MM-DD")
    actor: str = Field(description="Named party, or 'redacted' / 'unknown'")
    predicate: str = Field(description="Short verb phrase of what happened")


def _is_filler(predicate: str) -> bool:
    """Return True when the predicate states that nothing happened."""
    text = predicate.strip().casefold()
    return text in _FILLER_PREDICATES or text.startswith(_FILLER_PREFIX)


def digest_events(value_json: str | list | None) -> list[EventLine]:
    """Reduce a stored ``heideltime_event`` payload to its real, dated events.

    Args:
        value_json: The fact's ``value_json`` as JSON text or a decoded list.

    Returns:
        Events sorted chronologically (stable within a date); empty for a
        missing or malformed payload.
    """
    payload = value_json
    if isinstance(value_json, str):
        try:
            payload = json.loads(value_json)
        except ValueError:
            logger.debug("heideltime_event value_json is not JSON; empty digest")
            return []
    if not isinstance(payload, list):
        return []

    dated: list[tuple[object, EventLine]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        date = parse_partial_date(row.get("event_date"))
        predicate = str(row.get("predicate_object") or "")
        if date is None or _is_filler(predicate):
            continue
        actor = str(row.get("actor") or "unknown").strip()
        dated.append((date.first_day, EventLine(date=date.text, actor=actor, predicate=predicate.strip())))
    dated.sort(key=lambda item: item[0])
    return [line for _, line in dated]


def render_digest(lines: list[EventLine]) -> str:
    """Render events as one ``date | actor | predicate`` line each.

    Args:
        lines: Events in the order they should appear.

    Returns:
        The newline-joined prompt text.
    """
    return _LINE_SEPARATOR.join(_FIELD_SEPARATOR.join((e.date, e.actor, e.predicate)) for e in lines)


def split_batches(
    lines: list[EventLine], budget_tokens: int, count_tokens: Callable[[str], int]
) -> list[list[EventLine]]:
    """Cut events into contiguous batches whose rendered text fits a budget.

    Args:
        lines: Chronological events.
        budget_tokens: Maximum tokens of one rendered batch.
        count_tokens: Token counter for a rendered text (tiktoken in
            production, ``len`` in tests).

    Returns:
        Batches in order; a single batch when everything fits, none for no
        input. A lone event above the budget still forms its own batch, so the
        token guard downstream reports it instead of it being dropped.
    """
    batches: list[list[EventLine]] = []
    current: list[EventLine] = []
    current_tokens = 0
    separator_tokens = count_tokens(_LINE_SEPARATOR)
    for line in lines:
        cost = count_tokens(render_digest([line]))
        extra = cost + (separator_tokens if current else 0)
        if current and current_tokens + extra > budget_tokens:
            batches.append(current)
            current, current_tokens, extra = [], 0, cost
        current.append(line)
        current_tokens += extra
    if current:
        batches.append(current)
    return batches
