"""Number of undertakings assessed in a decision (fact ``number_undertaking``).

Pure and DB-free: takes the stored ``companies_investigated`` roster
(``value_json``) and returns an :class:`UndertakingCount`.

The roster lists one row per legal unit whose conduct or agreements the
Commission assessed: companies, subsidiaries named individually, trade and
professional associations. The count is the number of *distinct* units: rows
are deduplicated on :func:`company_stem`, so ``Akzo Chemicals BV`` and
``Akzo Chemicals`` collapse into one unit while ``AZ`` and ``AstraZeneca``
stay two. Placeholder rows (``NA``, empty) are dropped.

This counts legal units, not the economic-unit "undertaking" of EU competition
law (a parent and its subsidiaries can form one undertaking): grouping into
economic units would need ownership data the roster does not carry.
"""

import json
import logging

from pydantic import BaseModel, ConfigDict, Field

from pipelines.graph.heideltime_actor_resolution import company_stem

logger = logging.getLogger(__name__)

# Roster row keys that carry the entity name, in preference order. The schema
# key drifted over time ("company" in older rows, "companies" today); any other
# non-location string key is the fallback.
_NAME_KEYS: tuple[str, ...] = ("companies", "company", "companies_investigated")
_NON_NAME_KEYS: frozenset[str] = frozenset({"location", "country", "address"})
_PLACEHOLDERS: frozenset[str] = frozenset({"", "na", "n/a", "none", "null", "unknown"})


class UndertakingCount(BaseModel):
    """Distinct legal units assessed in one decision.

    Attributes:
        number_undertaking: Count of distinct legal units on the roster.
        undertakings: Display name of each counted unit (first spelling seen),
            in roster order.
        roster_rows: Usable roster rows before deduplication; a gap to
            ``number_undertaking`` shows how many duplicates were merged.
    """

    model_config = ConfigDict(frozen=True)

    number_undertaking: int = Field(ge=0, description="Count of distinct legal units on the roster")
    undertakings: list[str] = Field(
        default_factory=list, description="Display name of each counted unit, in roster order"
    )
    roster_rows: int = Field(ge=0, description="Usable roster rows before deduplication")


def _row_name(row: dict) -> str | None:
    """Return the entity name of one roster row, or None when it has none.

    Args:
        row: One ``companies_investigated`` element.

    Returns:
        The stripped name, or None for a placeholder or nameless row.
    """
    candidates = [row.get(key) for key in _NAME_KEYS]
    candidates += [v for k, v in row.items() if k not in _NAME_KEYS and k not in _NON_NAME_KEYS]
    for value in candidates:
        if isinstance(value, str) and value.strip().casefold() not in _PLACEHOLDERS:
            return value.strip()
    return None


def parse_roster(value_json: str | list | None) -> list[str]:
    """Extract the entity names of a stored roster, in order.

    Args:
        value_json: The fact's ``value_json`` (JSON text, an already decoded
            list, or None).

    Returns:
        One name per usable row; empty when the payload is missing or not a
        JSON list.
    """
    payload = value_json
    if isinstance(value_json, str):
        try:
            payload = json.loads(value_json)
        except ValueError:
            logger.debug("companies_investigated value_json is not JSON; skipping")
            return []
    if not isinstance(payload, list):
        return []
    names = (_row_name(row) for row in payload if isinstance(row, dict))
    return [name for name in names if name]


def count_undertakings(names: list[str]) -> UndertakingCount:
    """Count the distinct legal units of a roster.

    Args:
        names: Roster entity names, as returned by :func:`parse_roster`.

    Returns:
        The count, the kept display names and the pre-dedupe row count. A
        name whose stem is empty (pure legal-form noise) is compared on its
        casefolded text instead, so it is still counted once.
    """
    seen: set[tuple[str, ...]] = set()
    kept: list[str] = []
    for name in names:
        key = company_stem(name) or (name.casefold(),)
        if key in seen:
            continue
        seen.add(key)
        kept.append(name)
    return UndertakingCount(number_undertaking=len(kept), undertakings=kept, roster_rows=len(names))
