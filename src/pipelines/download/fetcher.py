"""Validated HTTP(S) PDF fetcher.

Streams a URL into a temp file next to the target, hashes it on the fly,
verifies the ``%PDF-`` magic and atomically moves it into place. Failures
are returned as ``FetchResult(outcome=failed)`` with a machine-readable
reason; nothing is ever left at the target path on failure.
"""

import hashlib
import logging
import os
import ssl
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from config.settings import settings
from pipelines.download.cellar import CellarRequest, is_missing_datastream
from pipelines.download.models import FetchResult, Outcome

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
_HTTPS = "https"
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_RETRY_STATUSES = frozenset({429})
HTTP_ACCEPTED = 202
HTTP_NOT_FOUND = 404
# Bytes of a routed 404 body read to recognise Cellar's missing-language message.
_NOT_FOUND_PROBE_BYTES = 4096

Router = Callable[[str], CellarRequest | None]


class _AttemptError(Exception):
    """Internal base: one attempt failed with a stable reason."""

    def __init__(
        self, reason: str, status: int | None = None, content_type: str | None = None
    ) -> None:
        """Store the failure details.

        Args:
            reason: Stable failure reason.
            status: HTTP status if a response was received.
            content_type: Content-Type header if a response was received.
        """
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.content_type = content_type


class _Retryable(_AttemptError):
    """Internal signal: this attempt failed in a way worth retrying."""


class _Rejected(_AttemptError):
    """Internal signal: this attempt failed permanently (no retry)."""


def _is_cert_error(exc: BaseException) -> bool:
    """Tell whether an exception chain contains a certificate verify failure.

    Args:
        exc: Exception to inspect.

    Returns:
        True if ``ssl.SSLCertVerificationError`` is in the cause/context chain.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


def make_client() -> httpx.Client:
    """Create the HTTP client used for downloads (TLS verification on).

    Returns:
        A configured ``httpx.Client`` following redirects.
    """
    return httpx.Client(
        verify=True,
        follow_redirects=True,
        timeout=settings.DOWNLOAD_TIMEOUT_SECONDS,
        headers={"User-Agent": settings.DOWNLOAD_USER_AGENT},
    )


class HttpDocumentFetcher:
    """Downloads one PDF with validation and bounded retries."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_retries: int,
        backoff_seconds: float,
        route: Router | None = None,
    ) -> None:
        """Create the fetcher.

        Args:
            client: Injected HTTP client.
            max_retries: Retries after the first attempt (transport errors,
                timeouts, 429 and 5xx only).
            backoff_seconds: Fixed wait between attempts.
            route: Optional router mapping a source URL to the request
                actually sent (for example ``cellar.cellar_request_for`` for
                EUR-Lex PDFs); None fetches every URL as given. A routed
                request is interpreted with Cellar semantics: its 404 body is
                probed for the missing-datastream message.
        """
        self._client = client
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._route = route

    def fetch(self, document_id: str, url: str, target: Path) -> FetchResult:
        """Download ``url`` to ``target`` if it is a real PDF.

        Args:
            document_id: Document id recorded in the result.
            url: Absolute http or https URL (https certificates are verified).
                The result always carries this URL, also when the router
                sends the request elsewhere.
            target: Final file path; parents are created.

        Returns:
            A ``FetchResult``: downloaded, or failed with a reason. Never
            raises for HTTP, validation, TLS or transport problems.
        """
        start = time.monotonic()
        routed = self._route(url) if self._route is not None else None
        request_url = routed.url if routed is not None else url
        headers = routed.headers if routed is not None else {}
        try:
            scheme = httpx.URL(request_url).scheme
        except httpx.InvalidURL:
            return self._failed(document_id, url, start, "request_error")
        if scheme not in _ALLOWED_SCHEMES:
            return self._failed(document_id, url, start, "unsupported_protocol")
        if routed is not None:
            logger.info("document %s: %s fetched via %s", document_id, url,
                        request_url)
        retrying = Retrying(
            retry=retry_if_exception_type(_Retryable),
            stop=stop_after_attempt(self._max_retries + 1),
            wait=wait_fixed(self._backoff),
            reraise=True,
        )
        try:
            sha, size, ctype, status, final_url = retrying(
                self._attempt, request_url, target, headers, routed is not None
            )
        except _AttemptError as exc:
            logger.warning("fetch failed %s %s (requested %s): %s", document_id,
                           url, request_url, exc.reason)
            return self._failed(
                document_id, url, start, exc.reason, exc.status, exc.content_type
            )
        if scheme != _HTTPS or httpx.URL(final_url).scheme != _HTTPS:
            # A routed (Cellar) request always ends on an http file URL after
            # an https start; that expected hop is logged at info, not warning.
            level = (logging.INFO if routed is not None and scheme == _HTTPS
                     else logging.WARNING)
            logger.log(
                level,
                "document %s fetched over plain http: url=%s final_url=%s",
                document_id,
                request_url,
                final_url,
            )
        return FetchResult(
            document_id=document_id,
            source_url=url,
            outcome=Outcome.DOWNLOADED,
            local_path=str(target),
            sha256=sha,
            size_bytes=size,
            content_type=ctype,
            http_status=status,
            duration_ms=self._elapsed(start),
        )

    def _failed(
        self,
        document_id: str,
        url: str,
        start: float,
        reason: str,
        status: int | None = None,
        content_type: str | None = None,
    ) -> FetchResult:
        """Build a failed result.

        Args:
            document_id: Document id.
            url: Source URL.
            start: ``time.monotonic()`` at fetch start.
            reason: Stable failure reason.
            status: HTTP status if known.
            content_type: Content-Type header if known.

        Returns:
            A ``FetchResult`` with outcome failed.
        """
        return FetchResult(
            document_id=document_id,
            source_url=url,
            outcome=Outcome.FAILED,
            content_type=content_type,
            http_status=status,
            failure_reason=reason,
            duration_ms=self._elapsed(start),
        )

    @staticmethod
    def _elapsed(start: float) -> int:
        """Return elapsed milliseconds since ``start``.

        Args:
            start: ``time.monotonic()`` value.

        Returns:
            Elapsed whole milliseconds.
        """
        return int((time.monotonic() - start) * 1000)

    def _attempt(
        self, url: str, target: Path, headers: dict[str, str], routed: bool
    ) -> tuple[str, int, str | None, int, str]:
        """Make one download attempt.

        Args:
            url: URL to fetch.
            target: Final path.
            headers: Extra request headers (content negotiation of a routed URL).
            routed: True when ``url`` came from the router (Cellar).

        Returns:
            Tuple of sha256, size, content type, HTTP status and final URL.

        Raises:
            _Retryable: On transport error, timeout, 429 or 5xx.
            _Rejected: On other HTTP errors, empty or non-PDF body, TLS error.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".part"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                result = self._stream(url, handle, headers, routed)
            os.replace(tmp, target)
            return result
        except httpx.TimeoutException as exc:
            raise _Retryable("timeout") from exc
        except httpx.UnsupportedProtocol as exc:
            raise _Rejected("unsupported_protocol") from exc
        except httpx.TransportError as exc:
            if _is_cert_error(exc):
                raise _Rejected("tls_error") from exc
            raise _Retryable("transport_error") from exc
        except httpx.TooManyRedirects as exc:
            raise _Rejected("too_many_redirects") from exc
        except httpx.DecodingError as exc:
            raise _Rejected("decoding_error") from exc
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            raise _Rejected("request_error") from exc
        finally:
            tmp.unlink(missing_ok=True)

    def _stream(
        self, url: str, handle: BinaryIO, headers: dict[str, str], routed: bool
    ) -> tuple[str, int, str | None, int, str]:
        """Stream the response body into ``handle`` and validate it.

        Args:
            url: URL to fetch.
            handle: Open binary file receiving the body.
            headers: Extra request headers.
            routed: True when ``url`` came from the router (Cellar).

        Returns:
            Tuple of sha256, size, content type, HTTP status and final URL.

        Raises:
            _Retryable: On 429 or 5xx.
            _Rejected: On other non-2xx (a routed 404 saying the requested
                language/format is absent is ``not_available_in_language``),
                a 202 without a PDF (``access_challenge``), an empty body or
                missing PDF magic.
        """
        digest = hashlib.sha256()
        size = 0
        head = b""
        with self._client.stream("GET", url, headers=headers) as response:
            status = response.status_code
            ctype = response.headers.get("content-type")
            if status in _RETRY_STATUSES or status >= 500:
                raise _Retryable(f"http_{status}", status, ctype)
            if routed and status == HTTP_NOT_FOUND:
                probe = b""
                for chunk in response.iter_bytes():
                    probe += chunk
                    if len(probe) >= _NOT_FOUND_PROBE_BYTES:
                        break
                if is_missing_datastream(probe[:_NOT_FOUND_PROBE_BYTES]):
                    raise _Rejected("not_available_in_language", status, ctype)
            if not response.is_success:
                raise _Rejected(f"http_{status}", status, ctype)
            for chunk in response.iter_bytes():
                if len(head) < len(PDF_MAGIC):
                    head = (head + chunk)[: len(PDF_MAGIC)]
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
        if head != PDF_MAGIC and status == HTTP_ACCEPTED:
            # 202 Accepted without a PDF is a bot check (EUR-Lex answers an
            # empty text/html body), not a broken file: say so, never retry.
            raise _Rejected("access_challenge", status, ctype)
        if size == 0:
            raise _Rejected("empty_body", status, ctype)
        if head != PDF_MAGIC:
            raise _Rejected("not_a_pdf", status, ctype)
        return digest.hexdigest(), size, ctype, status, str(response.url)
