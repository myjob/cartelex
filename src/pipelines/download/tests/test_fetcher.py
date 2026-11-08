"""Tests for the validated HTTPS PDF fetcher (no network, MockTransport only)."""

import hashlib
import tempfile
import ssl
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import httpx

from pipelines.download.cellar import cellar_request_for
from pipelines.download.fetcher import HttpDocumentFetcher, make_client
from config.settings import settings
from pipelines.download.models import Outcome

PDF = b"%PDF-1.7\n" + b"x" * 200
URL = "https://example.org/doc.pdf"
EURLEX_EN = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31988D0172"


class _FailingStream(httpx.SyncByteStream):
    """Stream yielding some bytes then raising a read error."""

    def __iter__(self) -> Iterator[bytes]:
        """Yield a PDF prefix, then fail.

        Yields:
            A partial PDF body.

        Raises:
            httpx.ReadError: Always, after the first chunk.
        """
        yield b"%PDF-1.7 partial"
        raise httpx.ReadError("connection reset")


def _fetcher(handler, retries: int = 2) -> HttpDocumentFetcher:
    """Build a fetcher on a mock transport with zero backoff.

    Args:
        handler: Request handler for MockTransport.
        retries: Maximum retries.

    Returns:
        The fetcher under test.
    """
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return HttpDocumentFetcher(client, max_retries=retries, backoff_seconds=0.0)


class FetcherTests(unittest.TestCase):
    """Behaviour of HttpDocumentFetcher.fetch."""

    def setUp(self) -> None:
        """Create a temp directory and target path."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "sub" / "doc.pdf"

    def _assert_clean(self) -> None:
        """Assert nothing was left in the target directory."""
        self.assertFalse(self.target.exists())
        if self.target.parent.exists():
            self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_valid_pdf_is_moved_into_place_with_sha256(self) -> None:
        """Test valid pdf is moved into place with sha256."""
        f = _fetcher(
            lambda r: httpx.Response(
                200, content=PDF, headers={"content-type": "application/pdf"}
            )
        )
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.DOWNLOADED)
        self.assertEqual(self.target.read_bytes(), PDF)
        self.assertEqual(res.sha256, hashlib.sha256(PDF).hexdigest())
        self.assertEqual(res.size_bytes, len(PDF))
        self.assertEqual(res.local_path, str(self.target))
        self.assertEqual(res.http_status, 200)
        self.assertEqual(res.content_type, "application/pdf")
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["doc.pdf"])

    def test_html_body_with_pdf_content_type_fails_not_a_pdf_and_leaves_no_file(
        self,
    ) -> None:
        """Test html body with pdf content type fails not a pdf and leaves no file."""
        f = _fetcher(
            lambda r: httpx.Response(
                200,
                content=b"<html>err</html>",
                headers={"content-type": "application/pdf"},
            )
        )
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(res.failure_reason, "not_a_pdf")
        self._assert_clean()

    def test_empty_body_fails(self) -> None:
        """Test empty body fails."""
        f = _fetcher(lambda r: httpx.Response(200, content=b""))
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(res.failure_reason, "empty_body")
        self._assert_clean()

    def test_202_empty_html_is_access_challenge_and_not_retried(self) -> None:
        """EUR-Lex answers a bot check with 202, an empty text/html body."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                202, content=b"", headers={"content-type": "text/html; charset=UTF-8"}
            )

        res = _fetcher(handler, retries=3).fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(res.failure_reason, "access_challenge")
        self.assertEqual(res.http_status, 202)
        self.assertEqual(res.content_type, "text/html; charset=UTF-8")
        self.assertEqual(len(calls), 1)
        self._assert_clean()

    def test_202_html_page_is_access_challenge(self) -> None:
        """A 202 carrying a challenge page instead of a PDF is the same failure."""
        f = _fetcher(
            lambda r: httpx.Response(
                202,
                content=b"<html><script>challenge()</script></html>",
                headers={"content-type": "text/html"},
            )
        )
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.failure_reason, "access_challenge")
        self._assert_clean()

    def test_200_empty_body_stays_empty_body(self) -> None:
        """Only a 202 is an access challenge; a plain empty 200 keeps its reason."""
        f = _fetcher(
            lambda r: httpx.Response(
                200, content=b"", headers={"content-type": "text/html"}
            )
        )
        self.assertEqual(f.fetch("d1", URL, self.target).failure_reason, "empty_body")

    def test_routed_url_fetches_from_cellar_with_headers(self) -> None:
        """A router sends the request to Cellar; the result keeps the source URL."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200, content=PDF, headers={"content-type": "application/pdf"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        f = HttpDocumentFetcher(
            client, max_retries=0, backoff_seconds=0.0, route=cellar_request_for
        )
        res = f.fetch("d1", EURLEX_EN, self.target)
        self.assertEqual(res.outcome, Outcome.DOWNLOADED)
        self.assertEqual(res.source_url, EURLEX_EN)
        self.assertEqual(len(seen), 1)
        self.assertEqual(
            str(seen[0].url), f"{settings.DOWNLOAD_CELLAR_BASE_URL}31988D0172"
        )
        self.assertEqual(seen[0].headers["Accept"], settings.DOWNLOAD_CELLAR_ACCEPT)
        self.assertEqual(seen[0].headers["Accept-Language"], "en")

    def test_routed_redirect_to_http_keeps_headers_ua_and_source_url(self) -> None:
        """Cellar's https -> http DOC_1 redirect keeps the negotiation headers."""
        seen: list[httpx.Request] = []
        file_url = "http://publications.europa.eu/resource/cellar/e537.0006.02/DOC_1"

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if str(request.url) == file_url:
                return httpx.Response(
                    200, content=PDF,
                    headers={"content-type": "application/pdf;type=pdfa1b"})
            return httpx.Response(303, headers={"location": file_url})

        client = httpx.Client(transport=httpx.MockTransport(handler),
                              follow_redirects=True,
                              headers={"User-Agent": settings.DOWNLOAD_USER_AGENT})
        f = HttpDocumentFetcher(
            client, max_retries=0, backoff_seconds=0.0, route=cellar_request_for
        )
        with self.assertLogs("pipelines.download.fetcher", level="INFO") as logs:
            res = f.fetch("d1", EURLEX_EN, self.target)
        self.assertEqual(res.outcome, Outcome.DOWNLOADED)
        self.assertEqual(res.source_url, EURLEX_EN)
        self.assertEqual([str(r.url) for r in seen],
                         [f"{settings.DOWNLOAD_CELLAR_BASE_URL}31988D0172", file_url])
        for request in seen:
            self.assertEqual(request.headers["Accept-Language"], "en")
            self.assertEqual(request.headers["User-Agent"],
                             settings.DOWNLOAD_USER_AGENT)
        plain_http = [r for r in logs.records if "plain http" in r.getMessage()]
        self.assertEqual([r.levelname for r in plain_http], ["INFO"])

    def test_cellar_404_probe_reads_a_bounded_prefix(self) -> None:
        """A huge routed 404 body is not read to the end."""
        served: list[int] = []

        class _Endless(httpx.SyncByteStream):
            """A 404 body that would never end."""

            def __iter__(self) -> Iterator[bytes]:
                """Yield 1 KiB chunks forever, counting them.

                Yields:
                    Filler bytes.
                """
                while True:
                    served.append(1)
                    yield b"x" * 1024

        client = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(404, stream=_Endless())))
        f = HttpDocumentFetcher(
            client, max_retries=0, backoff_seconds=0.0, route=cellar_request_for
        )
        self.assertEqual(f.fetch("d1", EURLEX_EN, self.target).failure_reason,
                         "http_404")
        self.assertLessEqual(len(served), 8)

    def test_unrouted_url_is_fetched_unchanged(self) -> None:
        """A URL the router declines is fetched as is, without extra headers."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=PDF)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        f = HttpDocumentFetcher(
            client, max_retries=0, backoff_seconds=0.0, route=cellar_request_for
        )
        self.assertEqual(f.fetch("d1", URL, self.target).outcome, Outcome.DOWNLOADED)
        self.assertEqual(str(seen[0].url), URL)
        self.assertNotIn("Accept-Language", seen[0].headers)

    def test_cellar_missing_language_is_not_available_and_not_retried(self) -> None:
        """Cellar's 404 'no content datastream' is not_available_in_language."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(
                404,
                content=b"None of the requests returned successfully a redirection."
                b" The following exception was thrown: [cellar identifier "
                b"cellar:67f2 does not hold a content datastream of the "
                b"requested type]",
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        f = HttpDocumentFetcher(
            client, max_retries=3, backoff_seconds=0.0, route=cellar_request_for
        )
        res = f.fetch("d1", EURLEX_EN, self.target)
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(res.failure_reason, "not_available_in_language")
        self.assertEqual(res.http_status, 404)
        self.assertEqual(len(calls), 1)
        self._assert_clean()

    def test_other_cellar_404_stays_http_404(self) -> None:
        """A 404 without the datastream marker keeps the generic reason."""
        client = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(404, content=b"<html>Not Found</html>")))
        f = HttpDocumentFetcher(
            client, max_retries=0, backoff_seconds=0.0, route=cellar_request_for
        )
        self.assertEqual(
            f.fetch("d1", EURLEX_EN, self.target).failure_reason, "http_404"
        )

    def test_no_route_fetches_eurlex_url_directly(self) -> None:
        """Without a router (Cellar disabled) the EUR-Lex URL is requested as is."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, content=PDF)

        _fetcher(handler).fetch("d1", EURLEX_EN, self.target)
        self.assertEqual(seen, [EURLEX_EN])

    def test_202_with_a_real_pdf_still_downloads(self) -> None:
        """The challenge rule never rejects a genuine PDF body."""
        f = _fetcher(
            lambda r: httpx.Response(
                202, content=PDF, headers={"content-type": "application/pdf"}
            )
        )
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.DOWNLOADED)
        self.assertEqual(self.target.read_bytes(), PDF)

    def test_retries_on_503_then_succeeds(self) -> None:
        """Test retries on 503 then succeeds."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(503)
            return httpx.Response(200, content=PDF)

        res = _fetcher(handler, retries=3).fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.DOWNLOADED)
        self.assertEqual(len(calls), 3)

    def test_404_not_retried_and_failed_with_status(self) -> None:
        """Test 404 not retried and failed with status."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(404)

        res = _fetcher(handler).fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(res.failure_reason, "http_404")
        self.assertEqual(res.http_status, 404)
        self.assertEqual(len(calls), 1)
        self._assert_clean()

    def test_persistent_503_fails_after_retries(self) -> None:
        """Test persistent 503 fails after retries."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503)

        res = _fetcher(handler, retries=2).fetch("d1", URL, self.target)
        self.assertEqual(res.failure_reason, "http_503")
        self.assertEqual(len(calls), 3)

    def test_stream_error_removes_temp_file(self) -> None:
        """Test stream error removes temp file."""
        f = _fetcher(lambda r: httpx.Response(200, stream=_FailingStream()), retries=1)
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(res.failure_reason, "transport_error")
        self._assert_clean()

    def test_timeout_is_failure(self) -> None:
        """Test timeout is failure."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        res = _fetcher(handler, retries=1).fetch("d1", URL, self.target)
        self.assertEqual(res.failure_reason, "timeout")
        self._assert_clean()

    def test_tls_error_is_failure_not_exception(self) -> None:
        """A certificate verify failure is a non-retried tls_error result."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            err = httpx.ConnectError("handshake failed", request=request)
            err.__cause__ = ssl.SSLCertVerificationError("verify failed")
            raise err

        res = _fetcher(handler).fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(res.failure_reason, "tls_error")
        self.assertEqual(len(calls), 1)

    def test_other_ssl_error_stays_retryable(self) -> None:
        """A non-certificate SSL error is retried as transport_error."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            err = httpx.ConnectError("eof", request=request)
            err.__cause__ = ssl.SSLError("UNEXPECTED_EOF")
            raise err

        res = _fetcher(handler, retries=2).fetch("d1", URL, self.target)
        self.assertEqual(res.failure_reason, "transport_error")
        self.assertEqual(len(calls), 3)

    def test_redirect_loop_is_failure(self) -> None:
        """A redirect loop yields too_many_redirects, not an exception."""
        f = _fetcher(lambda r: httpx.Response(302, headers={"location": URL}))
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.failure_reason, "too_many_redirects")
        self._assert_clean()

    def test_broken_content_encoding_is_failure(self) -> None:
        """A broken gzip body yields decoding_error."""
        f = _fetcher(
            lambda r: httpx.Response(
                200, content=b"not gzip", headers={"content-encoding": "gzip"}
            )
        )
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.failure_reason, "decoding_error")
        self._assert_clean()

    def test_generic_request_error_is_failure(self) -> None:
        """An unclassified RequestError yields request_error, not retried."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.RequestError("odd", request=request)

        res = _fetcher(handler).fetch("d1", URL, self.target)
        self.assertEqual(res.failure_reason, "request_error")
        self.assertEqual(len(calls), 1)

    def test_invalid_url_is_failure(self) -> None:
        """A malformed URL yields request_error without raising."""
        res = _fetcher(lambda r: httpx.Response(200)).fetch(
            "d1", "https://example.org/\x00", self.target
        )
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(res.failure_reason, "request_error")

    def test_unsupported_protocol_not_retried(self) -> None:
        """An unsupported scheme from a redirect is not retried."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            raise httpx.UnsupportedProtocol("bad", request=request)

        res = _fetcher(handler).fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.FAILED)
        self.assertEqual(len(calls), 1)

    def test_http_url_with_valid_pdf_downloads(self) -> None:
        """A plain http:// URL serving a PDF downloads and logs a warning."""
        f = _fetcher(lambda r: httpx.Response(200, content=PDF))
        with self.assertLogs("pipelines.download.fetcher", "WARNING"):
            res = f.fetch("d1", "http://example.org/d.pdf", self.target)
        self.assertEqual(res.outcome, Outcome.DOWNLOADED)
        self.assertEqual(self.target.read_bytes(), PDF)

    def test_https_to_http_redirect_with_pdf_downloads_and_warns(self) -> None:
        """A downgrade redirect serving a PDF downloads and logs a warning."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.scheme == "https":
                return httpx.Response(
                    302, headers={"location": "http://example.org/d.pdf"}
                )
            return httpx.Response(200, content=PDF)

        with self.assertLogs("pipelines.download.fetcher", "WARNING") as logs:
            res = _fetcher(handler).fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.DOWNLOADED)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("http://example.org/d.pdf", logs.output[0])

    def test_http_url_serving_html_fails_not_a_pdf(self) -> None:
        """An http:// URL serving HTML fails and stores nothing."""
        f = _fetcher(lambda r: httpx.Response(200, content=b"<html>x</html>"))
        res = f.fetch("d1", "http://example.org/d.pdf", self.target)
        self.assertEqual(res.failure_reason, "not_a_pdf")
        self._assert_clean()

    def test_non_http_schemes_fail_without_request(self) -> None:
        """file:// and ftp:// URLs fail as unsupported_protocol, no request."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=PDF)

        for url in ("file:///etc/passwd", "ftp://example.org/d.pdf"):
            res = _fetcher(handler).fetch("d1", url, self.target)
            self.assertEqual(res.failure_reason, "unsupported_protocol")
        self.assertEqual(calls, [])

    def test_existing_target_kept_when_refetch_fails(self) -> None:
        """A failed re-fetch leaves a previously stored file untouched."""
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(PDF)
        f = _fetcher(lambda r: httpx.Response(200, content=b"<html>"))
        res = f.fetch("d1", URL, self.target)
        self.assertEqual(res.failure_reason, "not_a_pdf")
        self.assertEqual(self.target.read_bytes(), PDF)
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["doc.pdf"])

    def test_429_retried_then_succeeds(self) -> None:
        """HTTP 429 is retried."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429)
            return httpx.Response(200, content=PDF)

        res = _fetcher(handler).fetch("d1", URL, self.target)
        self.assertEqual(res.outcome, Outcome.DOWNLOADED)
        self.assertEqual(len(calls), 2)

    def test_make_client_verifies_tls(self) -> None:
        """make_client builds a verifying, redirect-following client."""
        with mock.patch("pipelines.download.fetcher.httpx.Client") as client_cls:
            make_client()
        kwargs = client_cls.call_args.kwargs
        self.assertIs(kwargs["verify"], True)
        self.assertIs(kwargs["follow_redirects"], True)
        self.assertEqual(kwargs["timeout"], settings.DOWNLOAD_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
