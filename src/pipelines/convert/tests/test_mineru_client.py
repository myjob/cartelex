"""Tests for MineruTaskClient using httpx.MockTransport."""

import json
import unittest

import httpx

from pipelines.convert.mineru_client import (
    MineruTaskClient,
    MineruTaskNotFound,
    MineruUnavailable,
    UploadSpec,
)

BASE = "http://mineru.test:8000"
SHA = "a" * 64


def _client(handler, api_key: str = "secret") -> MineruTaskClient:
    return MineruTaskClient(
        httpx.Client(transport=httpx.MockTransport(handler)), BASE, api_key)


class MineruTaskClientTest(unittest.TestCase):
    """Behaviour of the Tasks API client against a mocked transport."""

    def test_create_task_posts_manifest(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(202, json={
                "task_id": "job_1", "status": "waiting-file",
                "files": [{"file_id": "f1", "file_name": "A.pdf", "status": "waiting-file",
                           "upload": {"url": f"{BASE}/v1/tasks/job_1/uploads/f1",
                                      "headers": {}, "expires_at": 1}}]})

        task = _client(handler).create_task([UploadSpec("A.pdf", b"abc", SHA)], "advanced")
        self.assertEqual(seen[0].url.path, "/v1/tasks")
        self.assertEqual(json.loads(seen[0].content), {
            "files": [{"file_name": "A.pdf", "bytes": 3, "sha256sum": SHA}],
            "tier": "advanced"})
        self.assertEqual(task.files[0].upload.url, f"{BASE}/v1/tasks/job_1/uploads/f1")

    def test_close_closes_http_client(self) -> None:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        MineruTaskClient(http, BASE, "").close()
        self.assertTrue(http.is_closed)

    def test_get_task_404_raises_task_not_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": {"code": "task_not_found", "message": "x"}})

        with self.assertRaises(MineruTaskNotFound):
            _client(handler).get_task("job_1")

    def test_get_task_ignores_unknown_fields(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "task_id": "job_1", "status": "running", "future": 1,
                "progress": {"completed": 0, "failed": 0, "total": 1},
                "files": [{"file_id": "f1", "file_name": "A.pdf", "status": "running",
                           "novel": True}]})

        task = _client(handler).get_task("job_1")
        self.assertEqual(task.status, "running")
        self.assertEqual(task.files[0].status, "running")

    def test_preflight_raises_when_tier_missing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "object": "list", "data": [{"id": "flash"}, {"id": "basic"}]})

        with self.assertRaises(MineruUnavailable) as ctx:
            _client(handler).preflight("advanced")
        self.assertIn("flash", str(ctx.exception))

    def test_preflight_ok_when_tier_offered(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"object": "list", "data": [{"id": "advanced"}]})

        _client(handler).preflight("advanced")

    def test_preflight_raises_on_connect_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with self.assertRaises(MineruUnavailable):
            _client(handler).preflight("advanced")

    def test_preflight_raises_on_non_2xx(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"code": "x", "message": "down"}})

        with self.assertRaises(MineruUnavailable):
            _client(handler).preflight("advanced")

    def test_download_result_drops_auth_on_foreign_redirect(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.host == "mineru.test":
                return httpx.Response(302, headers={"Location": "https://storage.example/z"})
            return httpx.Response(200, content=b"ZIP")

        data = _client(handler).download_result(f"{BASE}/v1/files/f1/content")
        self.assertEqual(data, b"ZIP")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer secret")
        self.assertNotIn("Authorization", seen[1].headers)

    def test_download_result_keeps_auth_on_same_origin(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/v1/files/f1/content":
                return httpx.Response(302, headers={"Location": "/v1/blob/f1"})
            return httpx.Response(200, content=b"ZIP")

        _client(handler).download_result(f"{BASE}/v1/files/f1/content")
        self.assertEqual(seen[1].headers["Authorization"], "Bearer secret")

    def test_api_key_sent_as_bearer(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"task_id": "j", "status": "running", "files": []})

        _client(handler).get_task("j")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer secret")

    def test_no_auth_header_without_api_key(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"task_id": "j", "status": "running", "files": []})

        _client(handler, api_key="").get_task("j")
        self.assertNotIn("Authorization", seen[0].headers)

    def test_cancel_task_ignores_409(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/tasks/job_1/cancel")
            return httpx.Response(409, json={
                "error": {"code": "task_already_terminal", "message": "done"}})

        _client(handler).cancel_task("job_1")

    def test_upload_puts_bytes_with_headers(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"task_id": "j", "file_id": "f", "status": "pending"})

        from pipelines.convert.models import MineruUpload
        _client(handler).upload(
            MineruUpload(url=f"{BASE}/v1/tasks/j/uploads/f", headers={"X-A": "1"}, expires_at=1),
            b"abc")
        self.assertEqual(seen[0].method, "PUT")
        self.assertEqual(seen[0].content, b"abc")
        self.assertEqual(seen[0].headers["X-A"], "1")

    def test_request_error_carries_status_and_error(self) -> None:
        from pipelines.convert.mineru_client import MineruRequestError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={
                "error": {"code": "invalid_request", "message": "bad", "param": "tier"}})

        with self.assertRaises(MineruRequestError) as ctx:
            _client(handler).create_task([UploadSpec("A.pdf", b"a", SHA)], "zzz")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.error.param, "tier")


if __name__ == "__main__":
    unittest.main()
