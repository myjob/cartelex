"""
Manual diagnostic probe -- NOT a unit test, not auto-discovered (no "test_"
prefix, defines no ``unittest.TestCase``), not imported by anything else. It
isolated why 'Andre Dumont N.V., S.A., Brissel' reproducibly 403s against the
live OpenCorporates search endpoint: not any single character (comma
included), not the accented "e", but the literal substring '.,' (a dotted
abbreviation immediately followed by a comma) occurring *twice or more* in one
query. The finding is folded into the ``_QUERY_BLOCKED_CHARS`` comment in
``opencorp_client.py``; this script is kept alongside the other live
OpenCorporates tests in case the WAF behaviour needs re-probing later.

Makes real API calls against real quota -- run it manually, sparingly, never
from CI or ``python -m unittest``. Cases run sequentially with a delay
between calls; if one call unexpectedly throttles (HTML 403 mid-run), stop
and re-run later rather than trusting the remaining rows in that batch.

Usage:
    opencorporates_api_token=<token> PYTHONPATH=src .venv/bin/python \\
        src/pipelines/corp_index/tests/probe_opencorp_comma_isolate.py
"""

import os
import time

# A real token must come from the environment; only default a placeholder so
# `import config.settings` never hard-fails if this module is merely
# collected (never invoked) by tooling that scans the tests/ directory.
os.environ.setdefault("opencorporates_api_token", "dummy")

import requests

from config.settings import settings
from pipelines.corp_index.opencorp_client import _API_TOKEN_HEADER

_SLEEP_SECONDS = 5.0

# Each case is a variant of the known-bad string, isolating one trait at a
# time against the known-good (no comma) and known-bad (both commas) anchors.
_CASES: list[tuple[str, str]] = [
    ("bad (both commas, accent)", "André Dumont N.V., S.A., Brissel"),
    ("good (no commas, accent)", "André Dumont N.V. S.A. Brissel"),
    ("bad, no accent", "Andre Dumont N.V., S.A., Brissel"),
    ("one comma (1st kept)", "André Dumont N.V., S.A. Brissel"),
    ("one comma (2nd kept)", "André Dumont N.V. S.A., Brissel"),
    ("plain two-word comma", "Acme, Corp"),
    ("one abbrev-comma", "Acme N.V., Corp"),
    ("two abbrev-commas, short", "Acme N.V., S.A., Corp"),
    ("abbrev-commas repeated", "N.V., S.A., N.V., S.A."),
    ("length-matched, no dots/commas", "Andre Dumont NV SA Brissel Extra"),
]


def _probe_one(
    session: requests.Session, url: str, query: str
) -> tuple[int | None, str, str]:
    """Issue one raw search request and return (status, content_type, body_snippet).

    Args:
        session: Authenticated session (X-API-TOKEN header already set).
        url: Full /companies/search URL.
        query: Raw, unsanitized query text to send as ``q``.

    Returns:
        A tuple of HTTP status (None on transport failure), the response's
        content-type header, and the first 160 characters of the body.
    """
    try:
        response = session.get(
            url,
            params={"q": query, "per_page": 1},
            timeout=settings.OPENCORP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as transport_error:
        return None, "", f"TRANSPORT ERROR: {transport_error}"
    content_type = response.headers.get("content-type", "")
    snippet = response.text[:160].replace("\n", " ")
    return response.status_code, content_type, snippet


def main() -> None:
    """Probe minimal variants of the known-bad string against the live API."""
    real_token = os.environ.get("opencorporates_api_token", "").strip()
    if not real_token or real_token == "dummy":
        raise SystemExit(
            "Set a real opencorporates_api_token env var before running this "
            "probe; it makes live calls against real quota."
        )

    session = requests.Session()
    session.headers.update({_API_TOKEN_HEADER: settings.OPENCORPORATES_API_TOKEN})
    url = f"{settings.OPENCORP_BASE_URL.rstrip('/')}/companies/search"

    header = f"{'label':<32} {'status':<6} {'content-type':<24} body"
    print(header)
    print("-" * len(header))
    for label, query in _CASES:
        status, content_type, snippet = _probe_one(session, url, query)
        print(f"{label:<32} {str(status):<6} {content_type:<24} {snippet}")
        if status == 403 and "html" in content_type.lower():
            print(
                f"  ^ WARNING: {label!r} hit the HTML throttle page -- the "
                "run may now be IP-throttled; consider stopping and "
                "re-running the remaining cases later."
            )
        time.sleep(_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
