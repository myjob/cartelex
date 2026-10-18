"""DB-cached REST client for the OpenCorporates company API.

The client fronts the ``/companies/search`` and
``/companies/{jurisdiction}/{number}`` endpoints with a PostgreSQL cache
(:class:`OpenCorpIndexRepository`). It is a plain REST + DB component: there is
no DSPy involvement, so the DSPy signature rules do not apply here.

Authentication uses the ``X-API-TOKEN`` request header (the officially
supported header form) so the token never appears in URLs or logs. The token
is read from :data:`settings.OPENCORPORATES_API_TOKEN`, which hard-fails at
import time when the ``opencorporates_api_token`` environment variable is
absent.
"""

import logging
import re
import time

import requests

from config.settings import settings
from pipelines.corp_index.opencorp_models import (
    OpenCorpCompany,
    OpenCorpCompanyDetail,
    OpenCorpSearchResult,
)
from pipelines.corp_index.opencorp_repository import OpenCorpIndexRepository

logger = logging.getLogger(__name__)

_API_TOKEN_HEADER = "X-API-TOKEN"
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_UNAUTHORIZED_STATUS = 401
_FORBIDDEN_STATUS = 403
_NOT_FOUND_STATUS = 404
_SEARCH_PATH = "/companies/search"

# Characters removed from the outgoing ``q`` parameter. Bracketing and quoting
# punctuation makes the edge in front of the API answer HTTP 403 with an HTML
# denial page for otherwise legitimate names -- 'Chambers (W. & R.) Ltd' is
# refused while 'Chambers W. & R. Ltd' returns results. Comma is blocked for a
# narrower, confirmed reason: probing live showed no single character here
# (comma included) trips a rejection on its own -- 'Acme, Corp' is accepted,
# and so is one dotted-abbreviation-plus-comma ('Acme N.V., Corp'). What is
# refused is the literal substring '.,' (period immediately followed by
# comma) appearing *twice or more* in one query, independent of accents --
# 'André Dumont N.V., S.A., Brissel' (two: 'N.V.,' and 'S.A.,') 403s (HTML
# page, or the connection is dropped outright) while either comma alone, or
# the same string with both commas removed, returns results. Stripping every
# comma is a superset guard: with no comma left, '.,' can never occur, so this
# is simpler than only blocking a second '.,' occurrence. The characters carry
# no retrieval value in a full-text company search, so they are dropped
# rather than escaped. '&', '+', '-', '/' and '.' are deliberately kept: they
# occur in real registered names and are accepted by the API.
_QUERY_BLOCKED_CHARS = frozenset("()[]{}<>\"\\|;,")

# A single quote is ambiguous, unlike the characters above, so it is not in
# _QUERY_BLOCKED_CHARS and gets its own targeted regex instead of a blanket
# strip. As a mid-word apostrophe ('O'Brien', 'Child's Play', 'd'Obourg') it
# is genuine and accepted. As a scare-quote delimiting a nickname or trade
# name ('Campina', 'de Eendracht', 'Skylge') it produces the same HTML-403
# rejection as the other blocked punctuation -- observed on four consecutive
# NL/BE roster names in one production run, all sharing this exact shape,
# after '_QUERY_BLOCKED_CHARS' alone left them untouched (a lone apostrophe
# was never part of that set). Not yet independently re-isolated with a
# dedicated live probe the way the comma pattern was (see the incident
# report), but implemented preemptively given how consistently it recurred.
# The regex keeps a quote only when it has a letter on *both* sides; a quote
# next to whitespace or a string boundary on either side is dropped.
_SCARE_QUOTE_RE = re.compile(r"(?<!\w)'|'(?!\w)")


class OpenCorpAuthError(RuntimeError):
    """Raised when OpenCorporates rejects the API token (401/403 or empty)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenCorpApiError(RuntimeError):
    """Raised when an OpenCorporates request fails after exhausting retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenCorpRateLimitError(OpenCorpApiError):
    """Raised when OpenCorporates throttles the caller's IP address.

    Signalled by an HTTP 403 carrying the HTML "accessing OpenCorporates at a
    higher than expected rate" page rather than a JSON error body. Distinct from
    :class:`OpenCorpAuthError` because the token is valid and the caller may
    resume later.
    """


class OpenCorpClient:
    """DB-cached client over the OpenCorporates company endpoints.

    Follows the Provisioner pattern: the repository and HTTP session are
    injected in ``__init__`` (defaults built from settings) rather than created
    inside request methods.
    """

    def __init__(
        self,
        repository: OpenCorpIndexRepository | None = None,
        session: requests.Session | None = None,
        api_token: str = settings.OPENCORPORATES_API_TOKEN,
    ) -> None:
        """Provision the client with a repository, HTTP session, and token.

        Args:
            repository: Persistence layer for the lookup cache. Defaults to a
                fresh :class:`OpenCorpIndexRepository`.
            session: HTTP session carrying the auth header. Defaults to a new
                :class:`requests.Session`.
            api_token: OpenCorporates API token. Defaults to
                :data:`settings.OPENCORPORATES_API_TOKEN`.

        Raises:
            OpenCorpAuthError: If ``api_token`` is empty after trimming.
        """
        if not api_token or not api_token.strip():
            raise OpenCorpAuthError(
                "OpenCorporates API token is empty; set opencorporates_api_token."
            )
        self._api_token = api_token
        self._repository = repository or OpenCorpIndexRepository()
        self._session = session or requests.Session()
        self._session.headers.update({_API_TOKEN_HEADER: api_token})
        self._base_url = settings.OPENCORP_BASE_URL.rstrip("/")

    def search_companies(
        self,
        query: str,
        *,
        jurisdiction_code: str | None = None,
        fetch_all_pages: bool = True,
        force_refresh: bool = False,
        max_hits: int | None = None,
    ) -> OpenCorpSearchResult:
        """Search companies by name, serving from cache when possible.

        Args:
            query: Free-text company name to search for.
            jurisdiction_code: Optional jurisdiction filter (e.g. ``ch``).
            fetch_all_pages: When ``True`` (default), walk every result page;
                when ``False``, fetch only the first page.
            force_refresh: When ``True``, bypass the cache and re-fetch,
                replacing any cached result for the key.
            max_hits: Abandon paging when the reported ``total_count`` exceeds
                this, keeping only page 1. Page 1 already carries
                ``total_count``, so an over-budget query costs exactly one call
                instead of hundreds -- an unfiltered search for ``Apple Inc.``
                reports 12,821 hits across 428 pages and is not worth walking.
                The returned result still carries the true ``total_count``, so
                the caller can tell "too broad to use" from "nothing found".

        Returns:
            The :class:`OpenCorpSearchResult`, with ``from_cache`` reflecting
            whether it was served from the local cache.
        """
        normalized_query = self.normalize_query(query)
        if not force_refresh:
            cached = self._repository.search(
                normalized_query,
                jurisdiction_code,
                require_complete=fetch_all_pages and max_hits is None,
            )
            if cached is not None:
                logger.debug("OpenCorporates search cache hit for %r", normalized_query)
                return cached

        # Only the outgoing parameter is sanitized. The cache key
        # (``normalized_query``) stays derived from the caller's original text so
        # callers can look the result back up by the name they hold.
        api_query = self._sanitize_query(query)
        if api_query != query:
            logger.debug(
                "Sanitized OpenCorporates query %r -> %r", query, api_query
            )
        base_params: dict[str, str | int] = {
            "q": api_query,
            "per_page": settings.OPENCORP_PER_PAGE,
        }
        if jurisdiction_code:
            base_params["jurisdiction_code"] = jurisdiction_code

        first_response = self._get(_SEARCH_PATH, {**base_params, "page": 1})
        results = first_response.json().get("results", {})
        companies = self._parse_companies(results)
        total_pages = results.get("total_pages")
        total_count = results.get("total_count")
        fetched_pages = 1

        over_budget = (
            max_hits is not None
            and total_count is not None
            and int(total_count) > max_hits
        )
        if over_budget:
            logger.info(
                "Not paging %r: %s hit(s) exceeds the %d budget; keeping page 1 only.",
                normalized_query,
                total_count,
                max_hits,
            )

        if fetch_all_pages and total_pages and not over_budget:
            for page in range(2, int(total_pages) + 1):
                page_response = self._get(_SEARCH_PATH, {**base_params, "page": page})
                page_results = page_response.json().get("results", {})
                companies.extend(self._parse_companies(page_results))
                fetched_pages += 1

        deduped_companies = self._dedupe_companies(companies)
        if len(deduped_companies) != len(companies):
            logger.info(
                "Dropped %d duplicate company hit(s) for %r across %d page(s)",
                len(companies) - len(deduped_companies),
                normalized_query,
                fetched_pages,
            )

        result = OpenCorpSearchResult(
            query_text=query,
            normalized_query=normalized_query,
            jurisdiction_code=jurisdiction_code,
            fetched_pages=fetched_pages,
            total_pages=total_pages,
            total_count=total_count,
            companies=deduped_companies,
            from_cache=False,
        )
        self._repository.save_search(result)
        return result

    def fetch_company_details(
        self,
        jurisdiction_code: str,
        company_number: str,
        *,
        force_refresh: bool = False,
    ) -> OpenCorpCompanyDetail | None:
        """Fetch a company's full detail record, serving from cache when possible.

        Args:
            jurisdiction_code: OpenCorporates jurisdiction code.
            company_number: Registry-assigned company number.
            force_refresh: When ``True``, bypass the cache and re-fetch.

        Returns:
            The :class:`OpenCorpCompanyDetail`, or ``None`` when the company is
            unknown to OpenCorporates (HTTP 404), in which case nothing is
            persisted.
        """
        if not force_refresh:
            cached = self._repository.company_details(jurisdiction_code, company_number)
            if cached is not None:
                logger.debug(
                    "OpenCorporates detail cache hit for %s/%s",
                    jurisdiction_code,
                    company_number,
                )
                return cached

        path = f"/companies/{jurisdiction_code}/{company_number}"
        response = self._get(path, None)
        if response.status_code == _NOT_FOUND_STATUS:
            logger.info(
                "OpenCorporates has no company %s/%s",
                jurisdiction_code,
                company_number,
            )
            return None

        company_json = response.json().get("results", {}).get("company", {})
        detail = OpenCorpCompanyDetail(**company_json, from_cache=False)
        self._repository.save_company_details(detail)
        return detail

    @staticmethod
    def normalize_query(query: str) -> str:
        """Build the cache key: casefolded with collapsed whitespace.

        Public so callers (e.g. the batch ingestor) can derive the same key to
        record a failed search against, without duplicating the normalization
        rule.

        Args:
            query: Raw query text.

        Returns:
            The normalized cache key.
        """
        return " ".join(query.split()).casefold()

    @staticmethod
    def _sanitize_query(query: str) -> str:
        """Strip punctuation that makes the API answer with an HTML 403 page.

        Bracketing, quoting, and comma characters (:data:`_QUERY_BLOCKED_CHARS`)
        are replaced with a space, whitespace is collapsed, and a space left
        hanging in front of a period is closed up. Word content is preserved,
        so ``'Chambers (W. & R.) Ltd'`` becomes ``'Chambers W. & R. Ltd'`` and
        ``'André Dumont N.V., S.A., Brissel'`` becomes
        ``'André Dumont N.V. S.A. Brissel'``.

        A single quote is handled separately from the blocklist because it is
        ambiguous: as a mid-word apostrophe (``"O'Brien"``, ``"Child's Play"``,
        ``"d'Obourg"``) it is a genuine part of the name and must stay, but as
        a scare-quote delimiting a nickname or trade name (``"'Campina'"``,
        ``"'de Eendracht'"``) it is dropped the same as a double quote.
        :data:`_SCARE_QUOTE_RE` tells the two apart by requiring a letter on
        *both* sides to keep it -- a quote adjacent to whitespace or a string
        boundary on either side is stripped.

        Args:
            query: Raw query text.

        Returns:
            The text safe to send as the ``q`` request parameter.
        """
        stripped = "".join(
            " " if character in _QUERY_BLOCKED_CHARS else character
            for character in query
        )
        stripped = _SCARE_QUOTE_RE.sub(" ", stripped)
        collapsed = " ".join(stripped.split())
        return re.sub(r"\s+(\.)", r"\1", collapsed)

    @staticmethod
    def _parse_companies(results: dict) -> list[OpenCorpCompany]:
        """Validate the ``company`` objects in a search result page.

        Args:
            results: The ``results`` object from a search response page.

        Returns:
            The validated company hits from that page.
        """
        companies: list[OpenCorpCompany] = []
        for entry in results.get("companies", []):
            company_json = entry.get("company")
            if company_json:
                companies.append(OpenCorpCompany(**company_json))
        return companies

    @staticmethod
    def _dedupe_companies(
        companies: list[OpenCorpCompany],
    ) -> list[OpenCorpCompany]:
        """Drop repeat hits for the same registry company across pages.

        OpenCorporates' underlying search index can shift between page
        fetches (e.g. results re-ranking or new entries appearing), so the
        same ``(jurisdiction_code, company_number)`` company sometimes
        surfaces on more than one page. The repository's unique constraint
        rejects a second row for that pair within one query, so duplicates
        must be collapsed before the batch reaches :meth:`save_search`.

        Args:
            companies: Company hits collected across all fetched pages.

        Returns:
            The hits with later duplicates by ``(jurisdiction_code,
            company_number)`` removed; first occurrence wins.
        """
        seen: set[tuple[str, str]] = set()
        deduped: list[OpenCorpCompany] = []
        for company in companies:
            key = (company.jurisdiction_code, company.company_number)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(company)
        return deduped

    def _get(
        self, path: str, params: dict[str, str | int] | None
    ) -> requests.Response:
        """Issue a GET request with retry/backoff and auth-error mapping.

        Retries on 429 and 5xx up to ``settings.OPENCORP_MAX_RETRIES`` with
        exponential backoff. Returns the response for 2xx and 404 (the caller
        decides how to treat 404).

        When :data:`settings.OPENCORP_SKIP_RETRY` is set, the retry budget is
        zero: the first failure of any kind is logged and raised immediately,
        with no backoff sleep, so a long batch spends no time waiting on a
        request that is going to fail anyway.

        Args:
            path: API path relative to the base URL (leading slash).
            params: Query parameters, or ``None``.

        Returns:
            The successful (or 404) :class:`requests.Response`.

        Raises:
            OpenCorpAuthError: On HTTP 401/403.
            OpenCorpApiError: On a non-retryable error, or after exhausting
                retries on a retryable status, or on a transport error.
        """
        url = f"{self._base_url}{path}"
        max_retries = 0 if settings.OPENCORP_SKIP_RETRY else settings.OPENCORP_MAX_RETRIES
        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = self._session.get(
                    url, params=params, timeout=settings.OPENCORP_TIMEOUT_SECONDS
                )
            except requests.RequestException as transport_error:
                last_error = transport_error
                if attempt < max_retries:
                    self._sleep_backoff(attempt)
                    continue
                logger.warning(
                    "OpenCorporates request to %s failed: %s", path, transport_error
                )
                raise OpenCorpApiError(
                    f"OpenCorporates request to {path} failed: {transport_error}"
                ) from transport_error

            status = response.status_code
            # OpenCorporates overloads 403: a JSON body means the token was
            # rejected, while the HTML denial page means the IP is being
            # throttled. Only the former is a permanent, non-retryable failure.
            if status == _FORBIDDEN_STATUS and self._is_throttle_page(response):
                if attempt < max_retries:
                    logger.warning(
                        "OpenCorporates throttled this IP on %s; backing off "
                        "%.1fs (attempt %d).",
                        path,
                        settings.OPENCORP_THROTTLE_BACKOFF_SECONDS,
                        attempt + 1,
                    )
                    time.sleep(settings.OPENCORP_THROTTLE_BACKOFF_SECONDS)
                    continue
                raise OpenCorpRateLimitError(
                    f"OpenCorporates throttled this IP on {path} (HTTP {status}: "
                    "'accessing OpenCorporates at a higher than expected rate'). "
                    "The API token is valid; retry later or slow the run down.",
                    status_code=status,
                )
            if status in (_UNAUTHORIZED_STATUS, _FORBIDDEN_STATUS):
                raise OpenCorpAuthError(
                    f"OpenCorporates rejected the API token (HTTP {status}).",
                    status_code=status,
                )
            if status < 400 or status == _NOT_FOUND_STATUS:
                return response
            if status in _RETRYABLE_STATUS_CODES and attempt < max_retries:
                logger.warning(
                    "OpenCorporates %s returned HTTP %s; retrying (attempt %d).",
                    path,
                    status,
                    attempt + 1,
                )
                self._sleep_backoff(attempt)
                continue
            logger.warning(
                "OpenCorporates %s returned HTTP %s; giving up on this request.",
                path,
                status,
            )
            raise OpenCorpApiError(
                f"OpenCorporates request to {path} failed with HTTP {status}.",
                status_code=status,
            )

        raise OpenCorpApiError(
            f"OpenCorporates request to {path} failed: {last_error}"
        )

    @staticmethod
    def _is_throttle_page(response: requests.Response) -> bool:
        """Report whether a 403 is the HTML throttle page rather than an auth error.

        The throttle response is served as ``text/html``; a genuine token
        rejection comes back as the API's JSON error body.

        Args:
            response: The 403 response to classify.

        Returns:
            ``True`` when the body is HTML, i.e. an IP-level throttle.
        """
        return "html" in response.headers.get("content-type", "").casefold()

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        """Sleep for the exponential backoff interval of a retry attempt.

        Args:
            attempt: Zero-based retry attempt index.
        """
        time.sleep(settings.OPENCORP_RETRY_BACKOFF_SECONDS * (2 ** attempt))
