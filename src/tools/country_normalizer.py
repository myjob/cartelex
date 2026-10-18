"""Deterministic country-name to ISO 3166-1 alpha-2 normalization.

The ``companies_country`` fact asks the LLM for each company's ISO country
code, but an LLM emits the value inconsistently ("Germany", "DE", "de",
"DEU", "Deutschland"). ISO 3166-1 is a closed vocabulary, so the correct code
is a pure lookup rather than something to trust the model to format. This
module resolves a free-text country token to its canonical alpha-2 code and
normalizes the ``country`` field of extracted fact rows in place.

Values that cannot be resolved are left unchanged: normalization only ever
improves a recognisable value, it never drops data.

The reverse direction (:func:`to_country_name`) and the address-tail test
(:func:`ends_with_country`) live here too, because they draw on the same closed
vocabulary. They exist for the corp index, which composes a postal address out
of a decision's address and its separately stored country code.
"""

import logging
import unicodedata
from typing import Any

import pycountry

logger = logging.getLogger(__name__)

_DEFAULT_COUNTRY_KEY = "country"

# Aliases that ``pycountry.countries.lookup`` does not resolve on its own but
# occur in EU competition-decision text. Keys are matched case-insensitively
# (via ``str.casefold``). ``lookup`` already handles canonical names, alpha-2,
# and alpha-3 codes, so this table only covers the gaps.
_COUNTRY_ALIASES: dict[str, str] = {
    "uk": "GB",
    "u.k.": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "deutschland": "DE",
    "u.s.a.": "US",
    "u.s.": "US",
    "united states of america": "US",
    "south korea": "KR",
    "north korea": "KP",
    "russia": "RU",
}


def _country_name_vocabulary() -> frozenset[str]:
    """Return every country spelling recognised as the tail of an address.

    Combines the ISO 3166-1 names, official names and common names known to
    ``pycountry`` with the alias keys above, all casefolded.

    Returns
    -------
    frozenset[str]
        The accepted lowercase country spellings.
    """
    names: set[str] = set(_COUNTRY_ALIASES)
    for country in pycountry.countries:
        for attribute in ("name", "official_name", "common_name"):
            value = getattr(country, attribute, None)
            if value:
                names.add(value.casefold())
    return frozenset(names)


# Built once at import: pycountry's table is static, and the sweep is the
# expensive part of ``ends_with_country``.
_COUNTRY_NAME_VOCABULARY = _country_name_vocabulary()


def to_iso_alpha2(value: str) -> str | None:
    """Resolve a country name or code to its ISO 3166-1 alpha-2 code.

    Parameters
    ----------
    value:
        A free-text country token: a name ("Germany"), an alpha-2 code
        ("DE"/"de"), an alpha-3 code ("DEU"), or a common alias ("UK").

    Returns
    -------
    str | None
        The upper-case alpha-2 code, or ``None`` when *value* is empty or
        cannot be resolved to a country.
    """
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token:
        return None

    alias = _COUNTRY_ALIASES.get(token.casefold())
    if alias:
        return alias

    try:
        return pycountry.countries.lookup(token).alpha_2
    except LookupError:
        return None


def to_country_name(value: str) -> str | None:
    """Resolve a country name or code to its English country name.

    The reverse of :func:`to_iso_alpha2`, and routed through it, so every alias
    that resolves to a code also resolves to a name. Prefers ``common_name``
    where ISO records one, since that is what an address would carry: ``TW``
    becomes ``Taiwan`` rather than ``Taiwan, Province of China``.

    Parameters
    ----------
    value:
        A free-text country token: a code ("BE"/"be"/"BEL") or a name.

    Returns
    -------
    str | None
        The English country name, or ``None`` when *value* cannot be resolved.
    """
    code = to_iso_alpha2(value)
    if code is None:
        return None

    country = pycountry.countries.get(alpha_2=code)
    if country is None:
        return None
    return getattr(country, "common_name", None) or country.name


def ends_with_country(address: str) -> bool:
    """Report whether an address's last comma-separated segment names a country.

    Used as a guard before appending a country to an address: an address that
    already carries one must not be given a second, and it must keep the one it
    has. That is not merely cosmetic -- the roster holds rows whose country code
    and address disagree, such as ``iso_country_code = 'DE'`` against
    ``111/113, Cours Berriat, F-38019 Grenoble, Cedex 1, France`` -- so the
    address's own country is the more specific claim and wins.

    Parameters
    ----------
    address:
        A postal address, in the roster's comma-separated form.

    Returns
    -------
    bool
        True when the final segment is a recognised country spelling.
    """
    if not isinstance(address, str):
        return False

    normalized = unicodedata.normalize("NFKC", address)
    segments = [segment.strip() for segment in normalized.split(",") if segment.strip()]
    if not segments:
        return False
    return segments[-1].casefold() in _COUNTRY_NAME_VOCABULARY


def normalize_country_fields(
    items: list[Any], key: str = _DEFAULT_COUNTRY_KEY
) -> list[dict[str, Any]]:
    """Return *items* with each row's *key* normalized to an ISO alpha-2 code.

    Rows that are not dicts, lack *key*, or carry an unresolvable value are
    passed through unchanged so no extracted data is lost. Resolvable values
    are replaced with their canonical alpha-2 code.

    Parameters
    ----------
    items:
        Extracted fact rows (a list of dicts, as produced by the extraction
        signatures). Non-dict elements are tolerated and returned untouched.
    key:
        The dict key holding the country value. Defaults to ``"country"``.

    Returns
    -------
    list[dict[str, Any]]
        A new list; dict rows whose value changed are shallow-copied, all
        other elements are returned by reference.
    """
    normalized: list[Any] = []
    for item in items:
        if not isinstance(item, dict) or key not in item:
            normalized.append(item)
            continue

        raw = item[key]
        iso = to_iso_alpha2(raw) if isinstance(raw, str) else None
        if iso is not None and iso != raw:
            normalized.append({**item, key: iso})
        else:
            if iso is None and isinstance(raw, str) and raw.strip():
                logger.debug(
                    "Country value not resolvable to ISO alpha-2, kept as-is: %r",
                    raw,
                )
            normalized.append(item)
    return normalized
