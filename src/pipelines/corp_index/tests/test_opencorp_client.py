"""Unit tests for the DB-cached OpenCorporates REST client.

Mocks live only at the system boundaries: the HTTP session (a real
``requests.Session`` whose ``get`` is replaced) and the repository DAO.
"""

import os
import unittest
from contextlib import AbstractContextManager
from unittest import mock

import requests

# Provide a dummy token before importing settings-dependent modules so the
# suite runs even when ``opencorporates_api_token`` is absent from the env.
os.environ.setdefault("opencorporates_api_token", "dummy")

from config.settings import settings  # noqa: E402
from pipelines.corp_index.opencorp_client import (  # noqa: E402
    _QUERY_BLOCKED_CHARS,
    OpenCorpApiError,
    OpenCorpAuthError,
    OpenCorpClient,
    OpenCorpRateLimitError,
)
from pipelines.corp_index.opencorp_models import OpenCorpSearchResult  # noqa: E402


def _make_response(
    status_code: int,
    payload: dict | None = None,
    content_type: str = "application/json",
) -> mock.MagicMock:
    """Build a mock ``requests.Response`` with a status and JSON body.

    Args:
        status_code: HTTP status code the response should report.
        payload: JSON body returned by ``.json()``.
        content_type: Value of the ``content-type`` response header; the client
            uses it to tell a token rejection from the HTML throttle page.

    Returns:
        A configured mock response object.
    """
    response = mock.MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.headers = {"content-type": content_type}
    return response


def _search_payload(
    companies: list[dict], total_pages: int, total_count: int
) -> dict:
    """Build a minimal OpenCorporates search response payload.

    Args:
        companies: Company objects to wrap under ``results.companies[].company``.
        total_pages: Value for ``results.total_pages``.
        total_count: Value for ``results.total_count``.

    Returns:
        A search response payload dict.
    """
    return {
        "results": {
            "companies": [{"company": company} for company in companies],
            "total_pages": total_pages,
            "total_count": total_count,
        }
    }


_COMPANY_CH = {
    "name": "Swiss Re Ltd",
    "company_number": "CHE-1",
    "jurisdiction_code": "ch",
    "incorporation_date": "1863-12-19",
    "dissolution_date": None,
    "company_type": "Aktiengesellschaft",
    "registry_url": "https://www.zefix.ch/en/search/entity/list/firm/1",
    "inactive": False,
    "current_status": "Aktive",
    "opencorporates_url": "https://opencorporates.com/companies/ch/1",
    "previous_names": [],
}


class TestOpenCorpClient(unittest.TestCase):
    """Unit tests for cache behaviour, pagination, auth, and retries."""

    def setUp(self) -> None:
        """Build a client with a real session (mocked ``get``) and mock repo."""
        self.repository = mock.MagicMock()
        self.session = requests.Session()
        self.session.get = mock.MagicMock()
        self.client = OpenCorpClient(
            repository=self.repository,
            session=self.session,
            api_token="secret-token",
        )

    @staticmethod
    def _retries_enabled() -> AbstractContextManager[bool]:
        """Pin ``OPENCORP_SKIP_RETRY`` off for a test that asserts retry behaviour.

        The deployed default has retries skipped, so a test about backoff must
        state the policy it exercises instead of inheriting whatever the
        environment happens to set.

        Returns:
            The patcher, for use as a context manager.
        """
        return mock.patch.object(settings, "OPENCORP_SKIP_RETRY", False)

    def test_empty_token_raises_before_any_request(self) -> None:
        """An empty token raises immediately, without constructing a session."""
        with self.assertRaises(OpenCorpAuthError):
            OpenCorpClient(
                repository=self.repository,
                session=self.session,
                api_token="   ",
            )

    def test_token_sent_as_header_never_in_url(self) -> None:
        """The token rides in the X-API-TOKEN header, not the request URL."""
        self.assertEqual(self.session.headers["X-API-TOKEN"], "secret-token")

        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(
            200, _search_payload([_COMPANY_CH], total_pages=1, total_count=1)
        )

        self.client.search_companies("Swiss Re", jurisdiction_code="ch")

        called_url = self.session.get.call_args.args[0]
        called_params = self.session.get.call_args.kwargs["params"]
        self.assertNotIn("secret-token", called_url)
        self.assertNotIn("api_token", called_params)
        self.assertEqual(called_params["jurisdiction_code"], "ch")
        self.assertEqual(called_params["q"], "Swiss Re")

    def test_cache_hit_makes_no_http_call(self) -> None:
        """A cache hit is returned without touching the HTTP session."""
        cached = OpenCorpSearchResult(
            query_text="Swiss Re",
            normalized_query="swiss re",
            jurisdiction_code="ch",
            fetched_pages=1,
            total_pages=1,
            total_count=1,
            companies=[],
            from_cache=True,
        )
        self.repository.search.return_value = cached

        result = self.client.search_companies("Swiss Re", jurisdiction_code="ch")

        self.assertTrue(result.from_cache)
        self.session.get.assert_not_called()
        self.repository.save_search.assert_not_called()

    def test_force_refresh_bypasses_cache(self) -> None:
        """force_refresh re-fetches even when a cached row exists."""
        self.repository.search.return_value = OpenCorpSearchResult(
            query_text="Swiss Re",
            normalized_query="swiss re",
            fetched_pages=1,
            companies=[],
            from_cache=True,
        )
        self.session.get.return_value = _make_response(
            200, _search_payload([_COMPANY_CH], total_pages=1, total_count=1)
        )

        result = self.client.search_companies("Swiss Re", force_refresh=True)

        self.assertFalse(result.from_cache)
        self.session.get.assert_called_once()
        self.repository.save_search.assert_called_once()

    def test_fetch_all_pages_walks_every_page(self) -> None:
        """With fetch_all_pages, the client requests page 1 through total_pages."""
        self.repository.search.return_value = None
        company_page_2 = {**_COMPANY_CH, "company_number": "CHE-2"}
        company_page_3 = {**_COMPANY_CH, "company_number": "CHE-3"}
        self.session.get.side_effect = [
            _make_response(200, _search_payload([_COMPANY_CH], 3, 3)),
            _make_response(200, _search_payload([company_page_2], 3, 3)),
            _make_response(200, _search_payload([company_page_3], 3, 3)),
        ]

        result = self.client.search_companies("Swiss Re", fetch_all_pages=True)

        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(result.fetched_pages, 3)
        self.assertEqual(len(result.companies), 3)
        requested_pages = [
            call.kwargs["params"]["page"] for call in self.session.get.call_args_list
        ]
        self.assertEqual(requested_pages, [1, 2, 3])

    def test_fetch_all_pages_dedupes_repeat_hits_across_pages(self) -> None:
        """A company repeated on two pages (index drift mid-pagination) is
        collapsed to a single row so the save_search unique constraint on
        (query_id, jurisdiction_code, company_number) is never violated."""
        self.repository.search.return_value = None
        company_page_2 = {**_COMPANY_CH, "company_number": "CHE-2"}
        self.session.get.side_effect = [
            _make_response(200, _search_payload([_COMPANY_CH], 3, 3)),
            _make_response(200, _search_payload([company_page_2], 3, 3)),
            _make_response(200, _search_payload([_COMPANY_CH], 3, 3)),
        ]

        result = self.client.search_companies("Swiss Re", fetch_all_pages=True)

        self.assertEqual(self.session.get.call_count, 3)
        self.assertEqual(len(result.companies), 2)
        self.assertEqual(
            {company.company_number for company in result.companies},
            {"CHE-1", "CHE-2"},
        )
        saved_result = self.repository.save_search.call_args.args[0]
        self.assertEqual(len(saved_result.companies), 2)

    def test_single_page_stops_after_first_page(self) -> None:
        """With fetch_all_pages False, only the first page is requested."""
        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(
            200, _search_payload([_COMPANY_CH], total_pages=3, total_count=3)
        )

        result = self.client.search_companies("Swiss Re", fetch_all_pages=False)

        self.session.get.assert_called_once()
        self.assertEqual(result.fetched_pages, 1)

    def test_retry_on_429_then_success(self) -> None:
        """A 429 is retried with backoff and the subsequent success is used."""
        self.repository.search.return_value = None
        self.session.get.side_effect = [
            _make_response(429),
            _make_response(200, _search_payload([_COMPANY_CH], 1, 1)),
        ]

        with self._retries_enabled(), mock.patch(
            "pipelines.corp_index.opencorp_client.time.sleep"
        ) as mock_sleep:
            result = self.client.search_companies("Swiss Re")

        self.assertEqual(self.session.get.call_count, 2)
        mock_sleep.assert_called_once()
        self.assertEqual(len(result.companies), 1)

    def test_skip_retry_makes_a_retryable_status_fail_on_the_first_attempt(
        self,
    ) -> None:
        """OPENCORP_SKIP_RETRY (the default) drops the retry budget to zero: one
        request, no backoff sleep, the failure raised straight to the caller."""
        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(429)

        with mock.patch.object(settings, "OPENCORP_SKIP_RETRY", True):
            with mock.patch(
                "pipelines.corp_index.opencorp_client.time.sleep"
            ) as mock_sleep:
                with self.assertRaises(OpenCorpApiError):
                    self.client.search_companies("Swiss Re")

        self.session.get.assert_called_once()
        mock_sleep.assert_not_called()

    def test_skip_retry_does_not_back_off_on_the_html_throttle_page(self) -> None:
        """The 60s throttle backoff is a retry too, so skip-retry raises the
        rate-limit error immediately instead of pausing the run."""
        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(
            403, content_type="text/html; charset=utf-8"
        )

        with mock.patch.object(settings, "OPENCORP_SKIP_RETRY", True):
            with mock.patch(
                "pipelines.corp_index.opencorp_client.time.sleep"
            ) as mock_sleep:
                with self.assertRaises(OpenCorpRateLimitError):
                    self.client.search_companies("Swiss Re")

        self.session.get.assert_called_once()
        mock_sleep.assert_not_called()

    def test_401_raises_auth_error(self) -> None:
        """HTTP 401 is mapped to OpenCorpAuthError and is not retried."""
        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(401)

        with self.assertRaises(OpenCorpAuthError) as raised:
            self.client.search_companies("Swiss Re")

        self.session.get.assert_called_once()
        self.assertEqual(raised.exception.status_code, 401)

    def test_403_with_json_body_raises_auth_error(self) -> None:
        """A JSON 403 is a genuine token rejection and is not retried."""
        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(403)

        with self.assertRaises(OpenCorpAuthError) as raised:
            self.client.search_companies("Swiss Re")

        self.session.get.assert_called_once()
        self.assertEqual(raised.exception.status_code, 403)

    def test_403_html_throttle_retries_then_raises_rate_limit_error(self) -> None:
        """The HTML 403 throttle page backs off and is not an auth failure."""
        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(
            403, content_type="text/html; charset=utf-8"
        )

        with self._retries_enabled(), mock.patch(
            "pipelines.corp_index.opencorp_client.time.sleep"
        ) as mock_sleep:
            with self.assertRaises(OpenCorpRateLimitError) as raised:
                self.client.search_companies("Swiss Re")

        self.assertNotIsInstance(raised.exception, OpenCorpAuthError)
        self.assertIsInstance(raised.exception, OpenCorpApiError)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(self.session.get.call_count, 2)
        mock_sleep.assert_called_once_with(
            settings.OPENCORP_THROTTLE_BACKOFF_SECONDS
        )

    def test_403_html_throttle_recovers_after_backoff(self) -> None:
        """A throttle that clears on the retry yields the successful response."""
        self.repository.search.return_value = None
        self.session.get.side_effect = [
            _make_response(403, content_type="text/html"),
            _make_response(200, _search_payload([_COMPANY_CH], 1, 1)),
        ]

        with self._retries_enabled(), mock.patch(
            "pipelines.corp_index.opencorp_client.time.sleep"
        ):
            result = self.client.search_companies("Swiss Re")

        self.assertEqual(len(result.companies), 1)

    def test_bracketed_query_is_sanitized_but_cache_key_is_not(self) -> None:
        """Brackets are stripped from ``q``; the cache key keeps the raw name."""
        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(
            200, _search_payload([_COMPANY_CH], 1, 1)
        )

        result = self.client.search_companies("Chambers (W. & R.) Ltd")

        self.assertEqual(
            self.session.get.call_args.kwargs["params"]["q"],
            "Chambers W. & R. Ltd",
        )
        self.assertEqual(result.query_text, "Chambers (W. & R.) Ltd")
        self.assertEqual(result.normalized_query, "chambers (w. & r.) ltd")

    def test_sanitizer_keeps_punctuation_the_api_accepts(self) -> None:
        """Apostrophes, ampersands, '+' and '/' survive sanitization."""
        for raw, expected in (
            ("Child's Play (International) Ltd", "Child's Play International Ltd"),
            ("Kuehne + Nagel Ltd. (UK)", "Kuehne + Nagel Ltd. UK"),
            ("News International/Sky Channel", "News International/Sky Channel"),
            ("Generics [UK] Limited", "Generics UK Limited"),
            ("Rank Precision Ltd (Rank), London", "Rank Precision Ltd Rank London"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(OpenCorpClient._sanitize_query(raw), expected)

    def test_sanitizer_strips_each_blocked_char_individually(self) -> None:
        """Every char in _QUERY_BLOCKED_CHARS is stripped on its own, not just
        in combination -- each is replaced by a space and the surrounding
        whitespace collapsed, leaving the two words joined by a single space."""
        for blocked_char in sorted(_QUERY_BLOCKED_CHARS):
            raw = f"Acme{blocked_char}Corp"
            with self.subTest(blocked_char=blocked_char):
                sanitized = OpenCorpClient._sanitize_query(raw)
                self.assertEqual(sanitized, "Acme Corp")
                self.assertNotIn(blocked_char, sanitized)

    def test_sanitizer_strips_commas(self) -> None:
        """Commas are stripped: OpenCorporates 403s or drops the connection
        outright on a comma-bearing query that succeeds once commas are
        removed. These four are real BE roster names that previously failed
        with OpenCorpApiError (connection aborted) or OpenCorpRateLimitError
        (HTTP 403) before the comma was the culprit was identified."""
        for raw, expected in (
            (
                "André Dumont N.V., S.A., Brissel",
                "André Dumont N.V. S.A. Brissel",
            ),
            (
                "S.A. Cimenteries C.B.R., Cement Companies N.V., Brussels "
                "— S.A. Ciments d'Obourg, Obourg, Belgium",
                "S.A. Cimenteries C.B.R. Cement Companies N.V. Brussels "
                "— S.A. Ciments d'Obourg Obourg Belgium",
            ),
            (
                "U.C.B. Chemical Union Chemische Bedrijven, S.A., Brussels",
                "U.C.B. Chemical Union Chemische Bedrijven S.A. Brussels",
            ),
            (
                "Zolder N.V., S.A., Zolder",
                "Zolder N.V. S.A. Zolder",
            ),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(OpenCorpClient._sanitize_query(raw), expected)

    def test_sanitizer_strips_scare_quotes_but_keeps_mid_word_apostrophes(
        self,
    ) -> None:
        """A single quote delimiting a nickname/trade name is stripped like a
        double quote; a genuine mid-word apostrophe survives. The first four
        cases are real NL/BE roster names that failed with
        OpenCorpRateLimitError (HTML 403) in production -- all four share the
        scare-quote shape, which _QUERY_BLOCKED_CHARS alone never touched
        since a lone apostrophe was deliberately excluded from it."""
        for raw, expected in (
            (
                "Coöperatieve Biol. Zuivelveren 'Skylge' v/h Coöperatieve "
                "Zuivelfabriek Terscheling",
                "Coöperatieve Biol. Zuivelveren Skylge v/h Coöperatieve "
                "Zuivelfabriek Terscheling",
            ),
            (
                "Coöperatieve Melkproduktenbedrijven 'Domo-(Bedum)'",
                "Coöperatieve Melkproduktenbedrijven Domo- Bedum",
            ),
            ("DMV 'Campina' BV", "DMV Campina BV"),
            (
                "Kartonfabriek 'de Eendracht' NV (trading as BPB de Eendracht)",
                "Kartonfabriek de Eendracht NV trading as BPB de Eendracht",
            ),
            ("O'Brien Ltd", "O'Brien Ltd"),
            ("Child's Play Ltd", "Child's Play Ltd"),
            ("S.A. Ciments d'Obourg", "S.A. Ciments d'Obourg"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(OpenCorpClient._sanitize_query(raw), expected)

    def test_persistent_500_raises_api_error(self) -> None:
        """A 5xx that never recovers raises OpenCorpApiError after retries."""
        self.repository.search.return_value = None
        self.session.get.return_value = _make_response(503)

        with self._retries_enabled(), mock.patch(
            "pipelines.corp_index.opencorp_client.time.sleep"
        ):
            with self.assertRaises(OpenCorpApiError) as raised:
                self.client.search_companies("Swiss Re")

        self.assertEqual(raised.exception.status_code, 503)

    def test_detail_404_returns_none_and_persists_nothing(self) -> None:
        """A detail 404 yields None without persisting a record."""
        self.repository.company_details.return_value = None
        self.session.get.return_value = _make_response(404)

        result = self.client.fetch_company_details("gb", "00000000")

        self.assertIsNone(result)
        self.repository.save_company_details.assert_not_called()

    def test_detail_success_persists_and_returns_model(self) -> None:
        """A detail hit is validated, persisted, and returned."""
        self.repository.company_details.return_value = None
        self.session.get.return_value = _make_response(
            200,
            {
                "results": {
                    "company": {
                        "name": "OPENCORPORATES LTD",
                        "company_number": "07444723",
                        "jurisdiction_code": "gb",
                        "incorporation_date": "2010-11-18",
                        "number_of_employees": None,
                        "officers": [],
                    }
                }
            },
        )

        detail = self.client.fetch_company_details("gb", "07444723")

        self.assertIsNotNone(detail)
        self.assertEqual(detail.company_number, "07444723")
        self.assertFalse(detail.from_cache)
        self.repository.save_company_details.assert_called_once()

    def test_detail_cache_hit_makes_no_http_call(self) -> None:
        """A cached detail record is returned without an HTTP request."""
        self.repository.company_details.return_value = mock.sentinel.cached_detail

        detail = self.client.fetch_company_details("gb", "07444723")

        self.assertIs(detail, mock.sentinel.cached_detail)
        self.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
