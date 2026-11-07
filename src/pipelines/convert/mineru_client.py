"""Client for the MinerU 4.1 Tasks API (self-hosted ``mineru-api``).

Live findings (server 4.1.3 started with ``--tier flash``): ``GET /health``
answers 404 (openapi.json names the endpoint ``/v1/health``), so
:meth:`MineruTaskClient.preflight` relies on ``GET /v1/tiers`` only. That
endpoint returns::

    {"object": "list",
     "data": [{"id": "flash", "description": "...", "current_model": "mineru-4.1.3"}]}

The flow is: create a task from a JSON manifest, PUT the bytes to each
returned upload link, poll the task, then download each completed file's
``result_url`` (a ZIP holding ``markdown.md``). The API key is sent only to
the API origin; every redirect hop is re-checked so it never reaches object
storage.
"""

import logging
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError

from config.settings import settings
from pipelines.convert.models import MineruError, MineruTask, MineruUpload

logger = logging.getLogger(__name__)

_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409


class MineruUnavailable(Exception):
    """The MinerU server is unreachable or does not offer the wanted tier."""


class MineruTaskNotFound(Exception):
    """The task id is unknown to the server (never created or collected)."""


class MineruRequestError(Exception):
    """The server answered with a non-2xx status.

    Attributes:
        status: HTTP status code.
        error: Parsed public error object, when the body carried one.
    """

    def __init__(self, status: int, error: MineruError | None = None) -> None:
        """Build the exception.

        Args:
            status: HTTP status code of the response.
            error: Parsed ``{"error": ...}`` body, if present.
        """
        detail = f"{error.code}: {error.message}" if error else "no error body"
        super().__init__(f"MinerU HTTP {status} ({detail})")
        self.status = status
        self.error = error


class UploadSpec(BaseModel):
    """One file to submit: name, content and its SHA-256."""

    file_name: str = Field(description="Logical file name including its extension.")
    data: bytes = Field(description="Raw file bytes.")
    sha256sum: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="Lowercase hex SHA-256 of the bytes.")

    def __init__(self, file_name: str, data: bytes, sha256sum: str) -> None:
        """Build the spec positionally.

        Args:
            file_name: Logical file name including its extension.
            data: Raw file bytes.
            sha256sum: Lowercase hex SHA-256 of the bytes.

        Raises:
            ValidationError: If the digest is not 64 lowercase hex characters.
        """
        super().__init__(file_name=file_name, data=data, sha256sum=sha256sum)


def _origin(url: str) -> tuple[str, str, int | None]:
    """Return (scheme, host, port) of a URL with default ports normalised."""
    parts = urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), port


def _parse_error(response: httpx.Response) -> MineruError | None:
    """Extract the ``{"error": ...}`` body of a response, if well formed."""
    try:
        return MineruError.model_validate(response.json()["error"])
    except (ValueError, KeyError, TypeError, ValidationError):
        return None


class MineruTaskClient:
    """Thin typed wrapper over the Tasks API endpoints."""

    def __init__(self, client: httpx.Client, base_url: str, api_key: str) -> None:
        """Store the injected transport and credentials.

        Args:
            client: httpx client (no redirect following is relied upon).
            base_url: API origin, e.g. ``http://127.0.0.1:8000``.
            api_key: Bearer token; empty means no Authorization header.
        """
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    def close(self) -> None:
        """Close the underlying httpx client and release its connections."""
        self._client.close()

    def _headers_for(self, url: str) -> dict[str, str]:
        """Return the Authorization header only for same-origin URLs."""
        if self._api_key and _origin(url) == _origin(self._base_url):
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Send one request with origin-scoped auth; raise on non-2xx.

        Args:
            method: HTTP method.
            url: Absolute URL.
            **kwargs: Extra httpx request arguments (json, content, headers).

        Returns:
            The 2xx response.

        Raises:
            MineruRequestError: On any non-2xx status.
        """
        extra = dict(kwargs.pop("headers", None) or {})
        response = self._client.request(
            method, url, headers={**extra, **self._headers_for(url)}, **kwargs)
        if not response.is_success:
            raise MineruRequestError(response.status_code, _parse_error(response))
        return response

    def preflight(self, tier: str) -> None:
        """Check that the server is reachable and offers ``tier``.

        Args:
            tier: Tier id the run will request, e.g. ``advanced``.

        Raises:
            MineruUnavailable: On a transport error, a non-2xx answer, or when
                ``tier`` is missing from ``GET /v1/tiers`` ``data[].id``.
        """
        try:
            response = self._send("GET", f"{self._base_url}/v1/tiers")
            offered = [entry["id"] for entry in response.json()["data"]]
        except httpx.HTTPError as exc:
            raise MineruUnavailable(f"MinerU unreachable at {self._base_url}: {exc}") from exc
        except MineruRequestError as exc:
            raise MineruUnavailable(f"MinerU tiers endpoint failed: {exc}") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise MineruUnavailable(f"Unexpected /v1/tiers response: {exc}") from exc
        if tier not in offered:
            raise MineruUnavailable(f"Tier {tier!r} not offered; server offers {offered}")

    def create_task(self, files: list[UploadSpec], tier: str) -> MineruTask:
        """Create a task from a manifest of files.

        Args:
            files: Files to submit, in order.
            tier: Requested quality tier.

        Returns:
            The creation receipt, with upload links for files needing bytes.

        Raises:
            MineruRequestError: On a non-2xx answer (e.g. 400 invalid tier).
        """
        body = {
            "files": [
                {"file_name": f.file_name, "bytes": len(f.data), "sha256sum": f.sha256sum}
                for f in files],
            "tier": tier,
        }
        response = self._send("POST", f"{self._base_url}/v1/tasks", json=body)
        return MineruTask.model_validate(response.json())

    def upload(self, upload: MineruUpload, data: bytes) -> None:
        """PUT raw bytes to an upload link.

        Args:
            upload: Link and headers returned by the server.
            data: Raw file bytes.

        Raises:
            MineruRequestError: On a non-2xx answer.
        """
        self._send("PUT", upload.url, content=data, headers=upload.headers)

    def get_task(self, task_id: str) -> MineruTask:
        """Fetch a task snapshot.

        Args:
            task_id: Server task id.

        Returns:
            The current snapshot.

        Raises:
            MineruTaskNotFound: On 404.
            MineruRequestError: On any other non-2xx answer.
        """
        try:
            response = self._send("GET", f"{self._base_url}/v1/tasks/{task_id}")
        except MineruRequestError as exc:
            if exc.status == _HTTP_NOT_FOUND:
                raise MineruTaskNotFound(task_id) from exc
            raise
        return MineruTask.model_validate(response.json())

    def cancel_task(self, task_id: str) -> None:
        """Cancel a task; an already-terminal task (409) is ignored.

        Args:
            task_id: Server task id.

        Raises:
            MineruRequestError: On a non-2xx answer other than 409.
        """
        try:
            self._send("POST", f"{self._base_url}/v1/tasks/{task_id}/cancel")
        except MineruRequestError as exc:
            if exc.status != _HTTP_CONFLICT:
                raise
            logger.info("Task %s already terminal; cancel ignored", task_id)

    def download_result(self, result_url: str) -> bytes:
        """Download a result, following redirects manually.

        The Bearer header is attached only while the current hop shares the
        origin of ``base_url``.

        Args:
            result_url: ``result_url`` of a completed file.

        Returns:
            The raw response body (a ZIP for the default output format).

        Raises:
            MineruRequestError: On a non-2xx final answer or too many redirects.
        """
        url = result_url
        for _ in range(_MAX_REDIRECTS + 1):
            response = self._client.get(url, headers=self._headers_for(url))
            if response.status_code in _REDIRECT_STATUSES and "location" in response.headers:
                url = urljoin(url, response.headers["location"])
                continue
            if not response.is_success:
                raise MineruRequestError(response.status_code, _parse_error(response))
            return response.content
        raise MineruRequestError(response.status_code, None)


def make_mineru_client() -> MineruTaskClient:
    """Build a client from ``settings.MINERU_*``.

    Returns:
        A client (httpx does not follow redirects by default) with the
        configured HTTP timeout.
    """
    http = httpx.Client(timeout=httpx.Timeout(settings.MINERU_HTTP_TIMEOUT_SECONDS))
    return MineruTaskClient(http, settings.MINERU_API_URL, settings.MINERU_API_KEY)
