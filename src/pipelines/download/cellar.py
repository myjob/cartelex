"""Route EUR-Lex PDF URLs to the Publications Office Cellar REST API.

The EUR-Lex website answers automated PDF requests with a bot check (202,
empty ``text/html``). The sanctioned machine route to the same documents is
Cellar: ``<DOWNLOAD_CELLAR_BASE_URL><CELEX>`` with content negotiation, where
``Accept`` names the PDF variants (a bare ``application/pdf`` is not served)
and ``Accept-Language`` carries the language of the EUR-Lex URL. A work that
has no expression in that language answers 404 with a "does not hold a
content datastream of the requested type" body (see ``is_missing_datastream``).

Only EUR-Lex ``/legal-content/<LL>/TXT/PDF/?uri=CELEX:<number>`` URLs are
routed; every other URL is fetched as given.
"""

import re
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings

EURLEX_HOST = "eur-lex.europa.eu"
_PDF_PATH = re.compile(r"^/legal-content/([A-Za-z]{2})/TXT/PDF/?$")
_CELEX_PREFIX = "CELEX:"
# A CELEX number as it may appear in the Cellar path: alphanumerics plus an
# optional "(NN)" suffix. Anything else (path, query or fragment characters
# smuggled in through the spreadsheet URL) is not routed.
_SAFE_CELEX = re.compile(r"[0-9A-Z]+(\([0-9]{2}\))?")
_MISSING_DATASTREAM = b"does not hold a content datastream"


class CellarRequest(BaseModel):
    """The Cellar request replacing one EUR-Lex PDF URL."""

    model_config = ConfigDict(frozen=True)

    url: str = Field(description="Cellar celex resource URL to request.")
    headers: dict[str, str] = Field(
        description="Content negotiation headers (Accept, Accept-Language).")


def cellar_request_for(url: str) -> CellarRequest | None:
    """Return the Cellar request for an EUR-Lex PDF URL, if it is one.

    Args:
        url: Candidate source URL.

    Returns:
        The Cellar request, or None when ``url`` is not an EUR-Lex
        ``/legal-content/<LL>/TXT/PDF/`` URL carrying ``uri=CELEX:<number>``
        with a well-formed CELEX number (upper-cased; ``(NN)`` suffix allowed).
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.hostname or "").lower() != EURLEX_HOST:
        return None
    match = _PDF_PATH.match(parts.path)
    if match is None:
        return None
    uris = parse_qs(parts.query).get("uri", [])
    celex = next((u[len(_CELEX_PREFIX):] for u in uris
                  if u.upper().startswith(_CELEX_PREFIX) and len(u) > len(_CELEX_PREFIX)),
                 None)
    if celex is None:
        return None
    celex = celex.upper()
    if _SAFE_CELEX.fullmatch(celex) is None:
        return None
    return CellarRequest(
        url=f"{settings.DOWNLOAD_CELLAR_BASE_URL}{celex}",
        headers={"Accept": settings.DOWNLOAD_CELLAR_ACCEPT,
                 "Accept-Language": match.group(1).lower()},
    )


def is_missing_datastream(body: bytes) -> bool:
    """Tell whether a Cellar 404 body says the requested format/language is absent.

    Args:
        body: Response body (the first bytes suffice).

    Returns:
        True for Cellar's "does not hold a content datastream" message.
    """
    return _MISSING_DATASTREAM in body
