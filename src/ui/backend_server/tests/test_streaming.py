"""Live smoke test of the ``oi_dspy`` REST API against a running server.

``test_api_contract`` asserts the same envelope in-process through
``TestClient``, which proves the handlers are correct but never opens a socket:
it cannot catch a stale process, a uvicorn misconfiguration, a reverse proxy
that rewrites the response, or a deployment running an older build.  This module
covers that gap by talking HTTP to the real service.

The whole suite skips when nothing answers on the base URL, so it is safe in the
default run.  The base URL is ``OI_DSPY_BASE_URL``, defaulting to
``settings.CHUNK_VIEW_BASE_URL`` -- the address the server already advertises to
the UI for its own deep links.

Inference cost: every test here uses a prompt that terminates *before* any LLM
call (model discovery, a reset command, a rejected UI meta-task), so a full run
costs no DGX inference.  The one test that asks a genuine question is opt-in via
``OI_DSPY_LIVE_QA=1``.

Start the server with ``scripts/run_oi_server.sh``.
"""

import json
import os
import unittest

import httpx

from config.settings import settings

_BASE_URL = os.getenv("OI_DSPY_BASE_URL", settings.CHUNK_VIEW_BASE_URL).rstrip("/")

# The root path has no route, so reachability is probed against /models.
_PROBE_TIMEOUT = 5.0
# A reset or rejected meta-task returns immediately; a real question runs the
# whole plan-execute-synthesize cycle on the DGX.
_FAST_TIMEOUT = 30.0
_QA_TIMEOUT = 180.0

_MODEL_NAME = "celex-dspy-agent"
_RESET_PROMPT = "reset"
_TITLE_TASK_PROMPT = (
    "### Task:\nGenerate a concise, 3-5 word title summarizing the chat "
    "history.\n### Chat History:\nUSER: list those cases"
)
_QA_PROMPT = "What fines were imposed in CELEX-31994D0823?"

_LIVE_QA_ENABLED = os.getenv("OI_DSPY_LIVE_QA", "").lower() in {"1", "true", "yes"}


def _read_sse(response: httpx.Response) -> tuple[list[dict], bool]:
    """Consume a streamed SSE response into frames plus a termination flag.

    Args:
        response: An open streaming response from the chat endpoint.

    Returns:
        A ``(frames, terminated)`` pair: the decoded ``data:`` payloads in
        order, and whether the stream ended with the ``[DONE]`` sentinel.
    """
    frames: list[dict] = []
    terminated = False
    for line in response.iter_lines():
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ").strip()
        if payload == "[DONE]":
            terminated = True
            break
        frames.append(json.loads(payload))
    return frames, terminated


def _content(frames: list[dict]) -> str:
    """Reassemble the streamed deltas into the text the client displays.

    Args:
        frames: Decoded SSE frames.

    Returns:
        The concatenated ``delta.content`` of every frame that carries one.
    """
    return "".join(f["choices"][0]["delta"].get("content", "") for f in frames)


class LiveApiTests(unittest.TestCase):
    """End-to-end checks against the server listening on the base URL."""

    @classmethod
    def setUpClass(cls) -> None:
        """Skip the whole class unless the server answers on ``/models``.

        Raises:
            unittest.SkipTest: When the service is unreachable.
        """
        try:
            response = httpx.get(f"{_BASE_URL}/models", timeout=_PROBE_TIMEOUT)
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            raise unittest.SkipTest(
                f"No oi_dspy server at {_BASE_URL} ({exc}). "
                "Start it with scripts/run_oi_server.sh to run these tests."
            ) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _post_chat(
        self, prompt: str, *, timeout: float = _FAST_TIMEOUT
    ) -> tuple[httpx.Response, list[dict], bool]:
        """Stream a one-turn chat completion and decode it.

        Args:
            prompt: The user message to send.
            timeout: Read timeout for the streamed response.

        Returns:
            The response, its decoded frames, and whether it terminated cleanly.
        """
        # ``stream`` is sent explicitly: the server's default is the OpenAI one
        # (a single JSON completion), so SSE has to be asked for.  A backend
        # predating that flag ignores the extra field, so this body works
        # against an old container too.
        body = {
            "model": _MODEL_NAME,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        with httpx.stream(
            "POST",
            f"{_BASE_URL}/v1/chat/completions",
            json=body,
            timeout=timeout,
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            frames, terminated = _read_sse(response)
        return response, frames, terminated

    # ------------------------------------------------------------------
    # Tests -- no inference
    # ------------------------------------------------------------------

    def test_models_endpoint_advertises_the_agent(self) -> None:
        """The endpoint Open WebUI polls must list the agent model."""
        payload = httpx.get(f"{_BASE_URL}/v1/models", timeout=_PROBE_TIMEOUT).json()
        self.assertEqual(payload["object"], "list")
        self.assertIn(_MODEL_NAME, [entry["id"] for entry in payload["data"]])

    def test_reset_prompt_streams_a_terminated_completion(self) -> None:
        """A reset command short-circuits and still closes the stream properly."""
        _, frames, terminated = self._post_chat(_RESET_PROMPT)

        self.assertTrue(terminated, "Stream did not end with the [DONE] sentinel.")
        self.assertIn("`celex_id` set to empty", _content(frames))
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(
            {f["model"] for f in frames}, {_MODEL_NAME}, "Frames named another model."
        )
        self.assertEqual(
            len({f["id"] for f in frames}), 1, "Frames disagreed on the turn id."
        )

    def test_meta_task_prompt_returns_an_empty_completion(self) -> None:
        """A UI background task is answered without running the pipeline.

        Skipped when the gate is disabled on this deployment.
        """
        if not settings.WEBUI_REJECT_TASK_PROMPTS:
            self.skipTest("WEBUI_REJECT_TASK_PROMPTS is off on this deployment.")
        _, frames, terminated = self._post_chat(_TITLE_TASK_PROMPT)

        self.assertTrue(terminated)
        self.assertEqual(_content(frames), "")
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")

    def test_non_streamed_reset_returns_one_json_completion(self) -> None:
        """``stream: false`` is answered with JSON, not an SSE body.

        Uses the reset command so no inference is spent.  Fails against a
        server still running a build from before the endpoint honoured the
        flag; recreate the backend container in that case.
        """
        response = httpx.post(
            f"{_BASE_URL}/v1/chat/completions",
            json={
                "model": _MODEL_NAME,
                "messages": [{"role": "user", "content": _RESET_PROMPT}],
                "stream": False,
            },
            timeout=_FAST_TIMEOUT,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers["content-type"])
        payload = response.json()
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["model"], _MODEL_NAME)
        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["role"], "assistant")
        self.assertIn("`celex_id` set to empty", choice["message"]["content"])

    def test_empty_message_list_is_rejected(self) -> None:
        """An empty transcript is a 422, not a server error.

        Fails against a server still running a build from before the request
        model constrained ``messages``; restart it in that case.
        """
        response = httpx.post(
            f"{_BASE_URL}/v1/chat/completions",
            json={"model": _MODEL_NAME, "messages": []},
            timeout=_FAST_TIMEOUT,
        )
        self.assertEqual(response.status_code, 422)

    def test_unknown_route_is_404(self) -> None:
        """The service exposes no root handler."""
        self.assertEqual(
            httpx.get(f"{_BASE_URL}/", timeout=_PROBE_TIMEOUT).status_code, 404
        )

    # ------------------------------------------------------------------
    # Tests -- real inference, opt-in
    # ------------------------------------------------------------------

    @unittest.skipUnless(
        _LIVE_QA_ENABLED, "Set OI_DSPY_LIVE_QA=1 to run a real inference round-trip."
    )
    def test_question_streams_a_grounded_answer(self) -> None:
        """A genuine question runs the pipeline and streams a non-empty answer."""
        _, frames, terminated = self._post_chat(_QA_PROMPT, timeout=_QA_TIMEOUT)
        rendered = _content(frames)

        self.assertTrue(terminated)
        self.assertNotIn("Pipeline error", rendered)
        self.assertGreater(len(rendered.strip()), 0, "The agent streamed no text.")
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
