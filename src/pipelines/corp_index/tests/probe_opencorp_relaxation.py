"""
Manual diagnostic probe -- NOT a unit test, not auto-discovered (no "test_"
prefix, defines no ``unittest.TestCase``), not imported by anything else.

Settles the empirical questions that gate the OpenCorporates relaxation ladder
(docs/claude/plans/opencorp-zero-hit-relaxation-ladder.md) before any code
commits to an answer. 3,548 roster rows are marked ``fk_opencorp_idx = 'NA'``
and the plan's whole shape depends on *why*:

    Q1  Does /companies/search return registered_address / locality /
        industry_codes, and at what fill rate per jurisdiction? Decides whether
        town scoring is free or needs a fetch_company_details shortlist.
    Q2  Is ``q`` AND-over-tokens, OR, or a phrase match? A phrase match kills
        every rung that deletes an interior token.
    Q3  Are the 100%-zero jurisdictions (us, it, at, pt, kr, za, il ...)
        genuinely uncovered, or is the jurisdiction_code filter itself broken?
        The local cache already proves 'za' and 'il' companies exist.
    Q4  Halkis: the worked example, with correctly-formed requests. The curl
        snippets in docs/Curl-opencorporates-snippets.txt put
        "q=Halkis&jurisdiction_code=gr" into a single --data-urlencode, so the
        '&' was percent-encoded and no filter was ever applied.
    Q5  How large is an unfiltered result set for real roster names? Sets
        OPENCORP_HIT_CAP_UNFILTERED from data rather than from the single
        cached data point (q=Emirates, 1,447 hits).
    Q6  Does an exact name match rank on page 1 of a broad unfiltered search?
        The ladder's offshore rung fetches page 1 only.

Makes real API calls against real quota -- run it manually, sparingly, never
from CI or ``python -m unittest``. Cases run sequentially with a delay between
calls; if one call unexpectedly throttles (HTML 403 mid-run), stop and re-run
later rather than trusting the remaining rows in that batch.

Usage:
    opencorporates_api_token=<token> PYTHONPATH=src .venv/bin/python \\
        src/pipelines/corp_index/tests/probe_opencorp_relaxation.py
"""

import os
import time
import unicodedata

# A real token must come from the environment; only default a placeholder so
# `import config.settings` never hard-fails if this module is merely collected
# (never invoked) by tooling that scans the tests/ directory.
os.environ.setdefault("opencorporates_api_token", "dummy")

import requests

from config.settings import settings
from pipelines.corp_index.opencorp_client import _API_TOKEN_HEADER

_SLEEP_SECONDS = 5.0

# Fields the search response is believed to carry but OpenCorpCompany discards
# via extra="ignore". Q1 measures how often each is actually populated.
_DISCARDED_FIELDS = (
    "registered_address",
    "registered_address_in_full",
    "industry_codes",
)


class ProbeResult:
    """One search response, reduced to what the probe questions need.

    Attributes:
        status: HTTP status code, or None on a transport failure.
        content_type: Response content-type header, for HTML-403 detection.
        total_count: ``results.total_count``, or None when absent.
        companies: The ``company`` objects from page 1, in response order.
        note: Human-readable failure description, empty on success.
    """

    def __init__(
        self,
        status: int | None,
        content_type: str,
        total_count: int | None,
        companies: list[dict],
        note: str = "",
    ) -> None:
        self.status = status
        self.content_type = content_type
        self.total_count = total_count
        self.companies = companies
        self.note = note

    @property
    def throttled(self) -> bool:
        """Whether this looks like the HTML throttle/WAF rejection page."""
        return self.status == 403 and "html" in self.content_type.lower()


def _search(
    session: requests.Session,
    url: str,
    query: str,
    *,
    jurisdiction_code: str | None = None,
    per_page: int = 30,
) -> ProbeResult:
    """Issue one search request with correctly separated query parameters.

    Args:
        session: Authenticated session (X-API-TOKEN header already set).
        url: Full /companies/search URL.
        query: Raw query text to send as ``q``.
        jurisdiction_code: Optional jurisdiction filter, spelled the way the
            API expects it (``jurisdiction_code``, not the roster's internal
            ``jurisdication_code`` misspelling, which the API would ignore).
        per_page: Page size; 1 when only ``total_count`` is wanted.

    Returns:
        A :class:`ProbeResult`. Transport and decode failures are captured in
        ``note`` rather than raised, so one bad case cannot end the run.
    """
    params: dict[str, str | int] = {"q": query, "per_page": per_page}
    if jurisdiction_code:
        params["jurisdiction_code"] = jurisdiction_code
    try:
        response = session.get(
            url, params=params, timeout=settings.OPENCORP_TIMEOUT_SECONDS
        )
    except requests.RequestException as transport_error:
        return ProbeResult(None, "", None, [], f"TRANSPORT ERROR: {transport_error}")

    content_type = response.headers.get("content-type", "")
    if response.status_code >= 400:
        snippet = response.text[:120].replace("\n", " ")
        return ProbeResult(
            response.status_code, content_type, None, [], f"HTTP body: {snippet}"
        )
    try:
        results = response.json().get("results", {})
    except ValueError as decode_error:
        return ProbeResult(
            response.status_code, content_type, None, [], f"DECODE: {decode_error}"
        )

    companies = [
        entry["company"]
        for entry in results.get("companies", [])
        if isinstance(entry, dict) and isinstance(entry.get("company"), dict)
    ]
    return ProbeResult(
        response.status_code, content_type, results.get("total_count"), companies
    )


def _fold(text: str) -> str:
    """Normalize a name for comparison: accents, case and punctuation removed.

    Mirrors ``jurisdiction_resolver.fold_match_key`` closely enough for the
    probe's exact-match question without importing scoring code that does not
    exist yet.

    Args:
        text: Arbitrary company name.

    Returns:
        A casefolded ASCII string with non-alphanumeric runs collapsed to one
        space and the ends trimmed.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    kept = [character if character.isalnum() else " " for character in ascii_only]
    return " ".join("".join(kept).casefold().split())


def _report(label: str, result: ProbeResult, extra: str = "") -> None:
    """Print one probe line and warn loudly on an HTML throttle page.

    Args:
        label: Case description.
        result: The probe result to summarize.
        extra: Optional question-specific detail appended to the line.
    """
    total = "-" if result.total_count is None else str(result.total_count)
    print(f"  {label:<46} status={str(result.status):<5} total={total:<7} {extra}")
    if result.note:
        print(f"      note: {result.note}")
    if result.throttled:
        print(
            "      ^ WARNING: HTML throttle page. The run may now be "
            "IP-throttled; stop and re-run the remaining questions later."
        )


def _q1_field_fill_rate(session: requests.Session, url: str) -> None:
    """Q1: how often does a search hit carry address and industry fields?"""
    print("\nQ1  Fill rate of the fields OpenCorpCompany currently discards")
    print("    (decides whether town scoring is free or needs detail fetches)")
    cases = [
        ("gb", "Swiss Re"),
        ("de", "Siemens"),
        ("ch", "Swiss Re"),
        ("us_de", "Anheuser-Busch"),
    ]
    for jurisdiction, query in cases:
        result = _search(session, url, query, jurisdiction_code=jurisdiction)
        counts = []
        for field in _DISCARDED_FIELDS:
            filled = sum(1 for company in result.companies if company.get(field))
            counts.append(f"{field.split('_')[-1]}={filled}/{len(result.companies)}")
        locality = sum(
            1
            for company in result.companies
            if isinstance(company.get("registered_address"), dict)
            and company["registered_address"].get("locality")
        )
        counts.append(f"locality={locality}/{len(result.companies)}")
        _report(f"{query!r} @ {jurisdiction}", result, " ".join(counts))
        time.sleep(_SLEEP_SECONDS)


def _q2_query_semantics(session: requests.Session, url: str) -> None:
    """Q2: AND over tokens, OR, or phrase match?"""
    print("\nQ2  Query semantics under ae_du")
    print("    control hits + 'United Shipping' hits  => AND over tokens")
    print("    control hits + 'United Shipping' zero  => phrase match")
    print("    'United Arab Shipping Zzzzq' hits      => OR")
    cases = [
        ("control: full registered name", "United Arab Shipping Company Services Limited"),
        ("interior token dropped", "United Shipping"),
        ("token order reversed", "Shipping United"),
        ("nonsense token appended", "United Arab Shipping Zzzzq"),
        ("breadth calibration", "Shipping"),
    ]
    for label, query in cases:
        result = _search(session, url, query, jurisdiction_code="ae_du")
        first = result.companies[0]["name"] if result.companies else "-"
        _report(label, result, f"first={first!r}")
        time.sleep(_SLEEP_SECONDS)


def _q3_dead_jurisdictions(session: requests.Session, url: str) -> None:
    """Q3: are the 100%-zero jurisdictions uncovered, or is the filter broken?"""
    print("\nQ3  Dead jurisdictions: genuine absence or broken filter?")
    print("    The cache proves EMIRATES/za company 1995-004426-10 exists, so a")
    print("    zero for that pair means the jurisdiction_code filter is at fault.")
    decisive = _search(session, url, "EMIRATES", jurisdiction_code="za")
    numbers = [company.get("company_number") for company in decisive.companies]
    _report(
        "DECISIVE 'EMIRATES' @ za",
        decisive,
        f"1995-004426-10 present={('1995-004426-10' in numbers)}",
    )
    time.sleep(_SLEEP_SECONDS)

    for query, jurisdiction in [
        ("Fiat", "it"),
        ("Eni", "it"),
        ("OMV", "at"),
        ("Galp", "pt"),
        ("Samsung", "kr"),
    ]:
        filtered = _search(session, url, query, jurisdiction_code=jurisdiction)
        _report(f"{query!r} @ {jurisdiction} (filtered)", filtered)
        time.sleep(_SLEEP_SECONDS)

        unfiltered = _search(session, url, query, per_page=30)
        in_target = sum(
            1
            for company in unfiltered.companies
            if company.get("jurisdiction_code") == jurisdiction
        )
        seen = sorted({company.get("jurisdiction_code") for company in unfiltered.companies})
        _report(
            f"{query!r} unfiltered",
            unfiltered,
            f"page1 in {jurisdiction}={in_target} seen={seen[:8]}",
        )
        time.sleep(_SLEEP_SECONDS)

    print("\n    Authoritative jurisdiction vocabulary:")
    try:
        response = session.get(
            f"{settings.OPENCORP_BASE_URL.rstrip('/')}/jurisdictions",
            timeout=settings.OPENCORP_TIMEOUT_SECONDS,
        )
        payload = response.json().get("results", {}).get("jurisdictions", [])
        codes = sorted(
            entry["jurisdiction"]["code"]
            for entry in payload
            if isinstance(entry, dict) and "jurisdiction" in entry
        )
        print(f"      GET /jurisdictions -> {len(codes)} codes")
        for target in ("it", "at", "pt", "kr", "za", "il", "us", "gr", "cy", "eu"):
            print(f"        {target:<6} indexed={target in codes}")
    except (requests.RequestException, ValueError, KeyError) as jurisdictions_error:
        print(f"      GET /jurisdictions failed: {jurisdictions_error}")
    time.sleep(_SLEEP_SECONDS)


def _q4_halkis(session: requests.Session, url: str) -> None:
    """Q4: the worked example, with correctly separated parameters."""
    print("\nQ4  Halkis (CELEX-31994D0815, roster says GR, believed registered CY)")
    cases: list[tuple[str, str, str | None]] = [
        ("'Halkis' unfiltered", "Halkis", None),
        ("'Halkis' @ gr", "Halkis", "gr"),
        ("'Halkis' @ cy", "Halkis", "cy"),
        ("'Halkis cement' unfiltered", "Halkis cement", None),
        ("'Halkis Athens' unfiltered", "Halkis Athens", None),
    ]
    for label, query, jurisdiction in cases:
        result = _search(session, url, query, jurisdiction_code=jurisdiction)
        detail = "-"
        if result.companies:
            top = result.companies[0]
            detail = (
                f"{top.get('name')!r} @ {top.get('jurisdiction_code')} "
                f"addr={top.get('registered_address_in_full')!r}"
            )
        _report(label, result, detail)
        time.sleep(_SLEEP_SECONDS)


def _q5_unfiltered_breadth(session: requests.Session, url: str) -> None:
    """Q5: how broad is an unfiltered search for real roster names?"""
    print("\nQ5  Unfiltered breadth for real NA roster names (sets the hit cap)")
    for query in [
        "Halkis",
        "Verband Osterreichischer Banken und Bankiers",
        "Veba Ol AG",
        "Nordenia International AG",
        "Iberia",
    ]:
        result = _search(session, url, query, per_page=1)
        _report(f"{query!r}", result)
        time.sleep(_SLEEP_SECONDS)


def _q6_exact_match_on_page_one(session: requests.Session, url: str) -> None:
    """Q6: does an exact folded name match appear on page 1 of a broad search?"""
    print("\nQ6  Does relevance ordering surface the exact name on page 1?")
    print("    (the offshore rung fetches page 1 only)")
    for query in ["Emirates", "Halkis", "Apple Inc."]:
        result = _search(session, url, query, per_page=30)
        wanted = _fold(query)
        rank = next(
            (
                index
                for index, company in enumerate(result.companies, start=1)
                if _fold(str(company.get("name", ""))) == wanted
            ),
            None,
        )
        _report(
            f"{query!r} unfiltered",
            result,
            f"exact-match rank on page 1: {rank if rank else 'ABSENT'}",
        )
        time.sleep(_SLEEP_SECONDS)


def main() -> None:
    """Run every probe question against the live OpenCorporates API."""
    real_token = os.environ.get("opencorporates_api_token", "").strip()
    if not real_token or real_token == "dummy":
        raise SystemExit(
            "Set a real opencorporates_api_token env var before running this "
            "probe; it makes live calls against real quota."
        )

    session = requests.Session()
    session.headers.update({_API_TOKEN_HEADER: settings.OPENCORPORATES_API_TOKEN})
    url = f"{settings.OPENCORP_BASE_URL.rstrip('/')}/companies/search"

    print("OpenCorporates relaxation probe -- ~35 live calls, "
          f"{_SLEEP_SECONDS}s apart")
    _q1_field_fill_rate(session, url)
    _q2_query_semantics(session, url)
    _q3_dead_jurisdictions(session, url)
    _q4_halkis(session, url)
    _q5_unfiltered_breadth(session, url)
    _q6_exact_match_on_page_one(session, url)
    print("\nDone. Record the findings in ISSUES.md before implementing the ladder.")


if __name__ == "__main__":
    main()
