"""HTTP contract tests for the OpenAI-compatible REST API served by ``oi_dspy``.

The browser suite (``test_webui_celex_context``) proves Open WebUI renders what
the agent says, and ``test_invalid_celex_gate`` drives ``stream_generator`` --
the SSE producer -- as a plain generator.  Neither exercises the layer in
between: the FastAPI routing, request validation and Server-Sent-Events envelope
that Open WebUI actually parses.  A defect that lives only there (a wrong
``content-type``, a missing terminal ``finish_reason``, an exception escaping as
a 500 halfway through a stream) is invisible to a generator-level test and hangs
the chat client.

Both response envelopes are covered: the SSE stream Open WebUI's chat path asks
for, and the single ``chat.completion`` object returned when ``stream`` is false
or absent -- the shape Open WebUI's background title/tag tasks and a plain
OpenAI SDK call expect.

These tests drive the ASGI app through ``TestClient``, so real routing,
middleware and serialization run, but no socket is opened and no server needs to
be up.  Isolation follows the suite convention: only the system boundaries are
stubbed -- the orchestrator stands in for planner/LLM/retrieval, a fake checker
stands in for the identifier registry lookup, and a stub repository stands in
for the chunk table.  Everything between the HTTP request and those seams is the
real implementation.

For the same contract asserted against a live server on port 7777, see
``test_streaming``.
"""

import json
import unittest
from unittest import mock

import dspy
from fastapi.testclient import TestClient

import ui.backend_server.oi_dspy as oi_dspy
from pipelines.agent_dspy.conversation_context import decode_pinned_context
from pipelines.agent_dspy.dspy_signature import CelexValidationResult
from pipelines.agent_dspy.retrieval_modules import RetrievalEvent
from tools.tests.celex_index_fixture import FakeCelexChecker

# Both aliases are registered by stacked decorators on the same handler, so each
# is an independent route that can drift; every envelope assertion covers both.
_CHAT_ROUTES = ("/chat/completions", "/v1/chat/completions")
_MODEL_ROUTES = ("/models", "/v1/models")

# A case present in the shared registry fixture (REGISTRY_SAMPLE).
_KNOWN_CELEX = "CELEX-32002D0180"

_STATUS_LINE = "Executing retrieval ...\n"
_ANSWER = "The Commission fined three undertakings."

# Open WebUI fires this against the chat endpoint for title generation; it must
# never reach the pipeline.
_TITLE_TASK_PROMPT = (
    "### Task:\nGenerate a concise, 3-5 word title summarizing the chat "
    "history.\n### Chat History:\nUSER: list those cases"
)

# The answer is streamed in fixed-size slices for a typing effect; an answer
# longer than one slice must therefore arrive as several content frames.
_CHUNK_SIZE = 200


def _frames(body: str) -> list[dict]:
    """Decode an SSE response body into its JSON frames.

    Args:
        body: The raw ``text/event-stream`` payload.

    Returns:
        One dict per ``data:`` line, in order, with the terminal ``[DONE]``
        sentinel omitted.

    Raises:
        json.JSONDecodeError: When a frame is not valid JSON, which is itself
            the failure a client would hit.
    """
    decoded: list[dict] = []
    for block in body.split("\n\n"):
        payload = block.strip()
        if not payload.startswith("data: "):
            continue
        payload = payload.removeprefix("data: ")
        if payload == "[DONE]":
            continue
        decoded.append(json.loads(payload))
    return decoded


def _content(frames: list[dict]) -> str:
    """Reassemble the streamed deltas into the text the client displays.

    Args:
        frames: Decoded SSE frames as returned by :func:`_frames`.

    Returns:
        The concatenated ``delta.content`` of every frame that carries one.
    """
    return "".join(f["choices"][0]["delta"].get("content", "") for f in frames)


def _body(
    question: str,
    *,
    history: list[tuple[str, str]] | None = None,
    stream: bool = True,
) -> dict:
    """Build an OpenAI-compatible request body.

    Args:
        question: The current user turn.
        history: Optional ``(role, content)`` pairs preceding it, oldest first.
        stream: Value of the ``stream`` flag. Sent explicitly -- as Open WebUI
            does -- because the server's default is the OpenAI one (``False``),
            so an SSE assertion must ask for SSE.

    Returns:
        A JSON-serialisable request body naming the served model.
    """
    messages = [{"role": role, "content": text} for role, text in (history or [])]
    messages.append({"role": "user", "content": question})
    return {"model": oi_dspy.MODEL_NAME, "messages": messages, "stream": stream}


class _RecordingOrchestrator:
    """Planner/retrieval/LLM boundary -- records every call, yields canned events.

    Args:
        answer: Answer text the terminal ``result`` event carries.
        celex_ids: Case ids the turn resolved, pinned into the transcript.
        canonical_celex_id: Active case for the turn, driving the doc footer.
        raises: When set, raised instead of yielding, to model a pipeline crash.
    """

    def __init__(
        self,
        *,
        answer: str = _ANSWER,
        celex_ids: list[str] | None = None,
        canonical_celex_id: str = "",
        raises: Exception | None = None,
    ) -> None:
        self.answer = answer
        self.celex_ids = list(celex_ids or [])
        self.canonical_celex_id = canonical_celex_id
        self.raises = raises
        self.calls: list[dict] = []

    def run_events(
        self,
        question: str,
        *,
        fallback_celex_id: str | None = None,
        history: object = None,
    ):
        """Record the delegation, then emit a status and a terminal result.

        Args:
            question: The current user question.
            fallback_celex_id: Sticky case the UI recovered from the transcript.
            history: Parsed prior turns.

        Yields:
            One ``status`` event followed by a terminal ``result`` event.

        Raises:
            Exception: The instance's ``raises``, when one was configured.
        """
        self.calls.append(
            {
                "question": question,
                "fallback_celex_id": fallback_celex_id,
                "history": history,
            }
        )
        if self.raises is not None:
            raise self.raises
        yield RetrievalEvent("status", _STATUS_LINE)
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                answer=self.answer,
                celex_validation=CelexValidationResult(
                    status="valid" if self.canonical_celex_id else "no_celex",
                    canonical_celex_id=self.canonical_celex_id,
                ),
                celex_ids=self.celex_ids,
            ),
        )


class _OrchestratorTestCase(unittest.TestCase):
    """Base fixture: a recording orchestrator patched in, plus a client."""

    orchestrator_kwargs: dict = {}

    def setUp(self) -> None:
        self.orchestrator = _RecordingOrchestrator(**self.orchestrator_kwargs)
        self._patch = mock.patch.object(oi_dspy, "orchestrator", self.orchestrator)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.client = TestClient(oi_dspy.app)


class ModelDiscoveryTests(unittest.TestCase):
    """``GET /models`` is how Open WebUI discovers the agent."""

    def setUp(self) -> None:
        self.client = TestClient(oi_dspy.app)

    def test_both_routes_return_the_same_listing(self) -> None:
        payloads = [self.client.get(route).json() for route in _MODEL_ROUTES]
        self.assertEqual(payloads[0], payloads[1])

    def test_listing_advertises_the_served_model(self) -> None:
        for route in _MODEL_ROUTES:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertEqual(payload["object"], "list")
                self.assertEqual(
                    [entry["id"] for entry in payload["data"]],
                    [oi_dspy.MODEL_NAME],
                )
                self.assertEqual(payload["data"][0]["object"], "model")

    def test_unknown_route_is_404(self) -> None:
        # The root path has no handler; the browser suite probes /models
        # precisely because of this.
        self.assertEqual(self.client.get("/").status_code, 404)


class ChatCompletionEnvelopeTests(_OrchestratorTestCase):
    """The SSE envelope every Open WebUI response is parsed out of."""

    orchestrator_kwargs = {
        "celex_ids": [_KNOWN_CELEX],
        "canonical_celex_id": _KNOWN_CELEX,
    }

    def test_both_routes_stream_event_stream(self) -> None:
        for route in _CHAT_ROUTES:
            with self.subTest(route=route):
                response = self.client.post(route, json=_body("who was fined?"))
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/event-stream", response.headers["content-type"])

    def test_stream_terminates_with_done_sentinel(self) -> None:
        response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        # A client reads until [DONE]; without it Open WebUI waits forever.
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"), response.text[-80:])

    def test_only_the_last_frame_finishes_the_turn(self) -> None:
        response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        frames = _frames(response.text)
        reasons = [f["choices"][0]["finish_reason"] for f in frames]
        self.assertEqual(reasons[-1], "stop")
        self.assertTrue(
            all(reason is None for reason in reasons[:-1]),
            f"A non-terminal frame closed the turn: {reasons}",
        )

    def test_every_frame_carries_the_openai_envelope(self) -> None:
        response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        frames = _frames(response.text)
        self.assertTrue(frames, "The endpoint streamed no frames at all.")
        for index, frame in enumerate(frames):
            with self.subTest(frame=index):
                self.assertEqual(frame["object"], "chat.completion.chunk")
                self.assertEqual(frame["model"], oi_dspy.MODEL_NAME)
                self.assertEqual(frame["choices"][0]["index"], 0)
                self.assertIsInstance(frame["created"], int)

    def test_all_frames_share_one_completion_id(self) -> None:
        response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        ids = {frame["id"] for frame in _frames(response.text)}
        self.assertEqual(len(ids), 1, f"Frames disagreed on the turn id: {ids}")
        self.assertTrue(ids.pop().startswith("chatcmpl-"))

    def test_status_line_precedes_the_answer(self) -> None:
        response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        rendered = _content(_frames(response.text))
        self.assertIn(_STATUS_LINE.strip(), rendered)
        self.assertIn(_ANSWER, rendered)
        self.assertLess(rendered.index(_STATUS_LINE.strip()), rendered.index(_ANSWER))

    def test_long_answer_arrives_as_several_deltas(self) -> None:
        # The typing effect is part of the contract: one giant frame would make
        # the UI sit blank until the whole answer is ready.
        long_answer = "x" * (_CHUNK_SIZE * 3)
        self.orchestrator.answer = long_answer
        response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        deltas = [
            f["choices"][0]["delta"]["content"]
            for f in _frames(response.text)
            if f["choices"][0]["delta"].get("content")
        ]
        self.assertGreaterEqual(len(deltas), 3)
        self.assertTrue(all(len(d) <= _CHUNK_SIZE for d in deltas))
        self.assertIn(long_answer, "".join(deltas))

    def test_answer_carries_the_source_document_footer(self) -> None:
        response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        rendered = _content(_frames(response.text))
        self.assertIn(f"/doc/{_KNOWN_CELEX}/md", rendered)
        self.assertIn(f"/doc/{_KNOWN_CELEX}/pdf", rendered)

    def test_resolved_cases_are_pinned_into_the_transcript(self) -> None:
        # The marker renders as nothing but is replayed verbatim by the client,
        # which is how the next turn scopes a refinement with no server state.
        response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        rendered = _content(_frames(response.text))
        self.assertEqual(decode_pinned_context(rendered), [_KNOWN_CELEX])


class NonStreamedCompletionTests(_OrchestratorTestCase):
    """``stream: false`` must yield one JSON completion, not SSE frames.

    Open WebUI sets the flag explicitly on both paths -- ``true`` for chat,
    ``false`` for its background title/tag/follow-up tasks -- and an OpenAI SDK
    client omits it entirely unless it asked to stream.  Serving SSE to any of
    those is a body the client cannot parse.
    """

    orchestrator_kwargs = {
        "celex_ids": [_KNOWN_CELEX],
        "canonical_celex_id": _KNOWN_CELEX,
    }

    def _completion(self, question: str = "who was fined?", **kwargs) -> dict:
        """POST a non-streamed request and return the decoded completion.

        Args:
            question: The user turn to send.
            **kwargs: Extra keyword arguments forwarded to :func:`_body`.

        Returns:
            The parsed JSON response body.
        """
        response = self.client.post(
            _CHAT_ROUTES[1], json=_body(question, stream=False, **kwargs)
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response.headers["content-type"])
        return response.json()

    def test_both_routes_return_one_json_completion(self) -> None:
        for route in _CHAT_ROUTES:
            with self.subTest(route=route):
                response = self.client.post(
                    route, json=_body("who was fined?", stream=False)
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn("application/json", response.headers["content-type"])
                self.assertNotIn("event-stream", response.headers["content-type"])

    def test_omitted_flag_defaults_to_a_json_completion(self) -> None:
        # The OpenAI SDK sends no ``stream`` key at all for a plain call, which
        # is the client the endpoint used to hand unparseable SSE frames to.
        response = self.client.post(
            _CHAT_ROUTES[1],
            json={
                "model": oi_dspy.MODEL_NAME,
                "messages": [{"role": "user", "content": "who was fined?"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "chat.completion")

    def test_completion_carries_the_openai_envelope(self) -> None:
        payload = self._completion()
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["model"], oi_dspy.MODEL_NAME)
        self.assertIsInstance(payload["created"], int)
        self.assertTrue(payload["id"].startswith("chatcmpl-"))
        self.assertEqual(len(payload["choices"]), 1)

    def test_single_choice_closes_the_turn(self) -> None:
        choice = self._completion()["choices"][0]
        self.assertEqual(choice["index"], 0)
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"]["role"], "assistant")
        # A ``delta`` here would be the streamed shape leaking into the
        # aggregate, which is what an OpenAI client chokes on.
        self.assertNotIn("delta", choice)

    def test_message_is_the_concatenation_of_the_streamed_deltas(self) -> None:
        # One pipeline, two envelopes: the aggregate must be exactly what a
        # streaming client renders, or the two paths have drifted.
        streamed = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        aggregated = self._completion()
        self.assertEqual(
            aggregated["choices"][0]["message"]["content"],
            _content(_frames(streamed.text)),
        )

    def test_message_carries_answer_footer_and_pinned_context(self) -> None:
        content = self._completion()["choices"][0]["message"]["content"]
        self.assertIn(_ANSWER, content)
        self.assertIn(f"/doc/{_KNOWN_CELEX}/md", content)
        self.assertEqual(decode_pinned_context(content), [_KNOWN_CELEX])

    def test_long_answer_arrives_whole_rather_than_sliced(self) -> None:
        # The typing-effect slicing is an artefact of the SSE envelope; the
        # aggregate must not show its seams.
        long_answer = "x" * (_CHUNK_SIZE * 3)
        self.orchestrator.answer = long_answer
        content = self._completion()["choices"][0]["message"]["content"]
        self.assertIn(long_answer, content)

    def test_history_still_resolves_the_sticky_case(self) -> None:
        # The envelope is the only thing that changes -- the pipeline call is
        # identical, history parsing included.
        self._checker_patch = mock.patch.object(
            oi_dspy.pipeline.celex_selector,
            "celex_checker",
            FakeCelexChecker({_KNOWN_CELEX: _KNOWN_CELEX}),
        )
        self._checker_patch.start()
        self.addCleanup(self._checker_patch.stop)
        self._completion(
            "and what was the duration?",
            history=[("user", f"set {_KNOWN_CELEX}"), ("assistant", "ok")],
        )
        self.assertEqual(self.orchestrator.calls[0]["fallback_celex_id"], _KNOWN_CELEX)

    def test_rejected_meta_task_returns_an_empty_json_completion(self) -> None:
        # This is the live path: Open WebUI posts its title task with
        # ``stream: false`` and parses the reply as JSON.
        with mock.patch.object(oi_dspy.settings, "WEBUI_REJECT_TASK_PROMPTS", True):
            payload = self._completion(_TITLE_TASK_PROMPT)
        self.assertEqual(self.orchestrator.calls, [])
        choice = payload["choices"][0]
        self.assertEqual(choice["message"]["content"], "")
        self.assertEqual(choice["finish_reason"], "stop")

    def test_reset_prompt_returns_its_acknowledgement_as_json(self) -> None:
        payload = self._completion("reset")
        self.assertIn(
            "`celex_id` set to empty", payload["choices"][0]["message"]["content"]
        )
        self.assertEqual(self.orchestrator.calls, [])

    def test_pipeline_crash_is_delivered_as_a_200_completion(self) -> None:
        """A crash reaches the client as content, not as a 500.

        Unlike the streamed path the status code is not yet committed when the
        pipeline dies, so this asserts the degradation is a deliberate choice
        and not an accident of the response having already started.
        """
        self.orchestrator.raises = RuntimeError("DGX unreachable")

        with self.assertLogs(oi_dspy.logger, level="ERROR") as captured:
            payload = self._completion()

        self.assertIn("DGX unreachable", captured.output[0])
        choice = payload["choices"][0]
        self.assertIn("Pipeline error", choice["message"]["content"])
        self.assertIn("DGX unreachable", choice["message"]["content"])
        self.assertEqual(choice["finish_reason"], "stop")


class ChatCompletionGateTests(_OrchestratorTestCase):
    """Prompts the endpoint must answer without running the pipeline."""

    def test_meta_task_prompt_is_rejected_before_the_pipeline(self) -> None:
        with mock.patch.object(oi_dspy.settings, "WEBUI_REJECT_TASK_PROMPTS", True):
            response = self.client.post(
                _CHAT_ROUTES[1], json=_body(_TITLE_TASK_PROMPT)
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.orchestrator.calls,
            [],
            "A UI background task reached the retrieval pipeline.",
        )

    def test_rejected_meta_task_still_returns_a_valid_completion(self) -> None:
        # Open WebUI must fall back to its own default, which needs a
        # well-formed but contentless completion rather than an error.
        with mock.patch.object(oi_dspy.settings, "WEBUI_REJECT_TASK_PROMPTS", True):
            response = self.client.post(
                _CHAT_ROUTES[1], json=_body(_TITLE_TASK_PROMPT)
            )
        frames = _frames(response.text)
        self.assertEqual(_content(frames), "")
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))

    def test_gate_can_be_disabled_by_settings(self) -> None:
        with mock.patch.object(oi_dspy.settings, "WEBUI_REJECT_TASK_PROMPTS", False):
            self.client.post(_CHAT_ROUTES[1], json=_body(_TITLE_TASK_PROMPT))
        self.assertEqual(len(self.orchestrator.calls), 1)

    def test_reset_prompt_clears_context_without_planning(self) -> None:
        response = self.client.post(_CHAT_ROUTES[1], json=_body("reset"))
        rendered = _content(_frames(response.text))
        # Assert on the ASCII half only -- the decorative prefix is not contract.
        self.assertIn("`celex_id` set to empty", rendered)
        self.assertEqual(self.orchestrator.calls, [])
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))


class ConversationHistoryTests(_OrchestratorTestCase):
    """The active case is recovered from the transcript, not from server state."""

    def setUp(self) -> None:
        super().setUp()
        # Identifier-registry boundary: real detection over the shared sample,
        # in-memory resolution, no database.
        self._checker_patch = mock.patch.object(
            oi_dspy.pipeline.celex_selector,
            "celex_checker",
            FakeCelexChecker({_KNOWN_CELEX: _KNOWN_CELEX}),
        )
        self._checker_patch.start()
        self.addCleanup(self._checker_patch.stop)

    def test_case_from_an_earlier_turn_is_carried_forward(self) -> None:
        response = self.client.post(
            _CHAT_ROUTES[1],
            json=_body(
                "and what was the duration?",
                history=[("user", f"set {_KNOWN_CELEX}"), ("assistant", "ok")],
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.orchestrator.calls[0]["fallback_celex_id"], _KNOWN_CELEX
        )

    def test_reset_in_history_drops_the_earlier_case(self) -> None:
        self.client.post(
            _CHAT_ROUTES[1],
            json=_body(
                "and what was the duration?",
                history=[
                    ("user", f"set {_KNOWN_CELEX}"),
                    ("assistant", "ok"),
                    ("user", "reset"),
                    ("assistant", "cleared"),
                ],
            ),
        )
        self.assertIsNone(self.orchestrator.calls[0]["fallback_celex_id"])

    def test_first_turn_has_no_sticky_case(self) -> None:
        self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))
        self.assertIsNone(self.orchestrator.calls[0]["fallback_celex_id"])


class ErrorHandlingTests(_OrchestratorTestCase):
    """Malformed bodies and pipeline crashes must not break the client."""

    def test_pipeline_crash_is_delivered_as_a_complete_stream(self) -> None:
        """A crash degrades inside the stream instead of becoming a 500.

        The status line is already on the wire when the pipeline dies, so the
        HTTP status is committed to 200 and the failure has to reach the user
        as content.  The injected error is caught by ``stream_generator``'s own
        handler, which logs it -- ``assertLogs`` asserts the operator still
        gets that traceback and keeps the expected ``logger.exception`` call
        from printing into the test report, where it would read as a failure.
        """
        self.orchestrator.raises = RuntimeError("DGX unreachable")

        with self.assertLogs(oi_dspy.logger, level="ERROR") as captured:
            response = self.client.post(_CHAT_ROUTES[1], json=_body("who was fined?"))

        self.assertIn("Pipeline failed", captured.output[0])
        self.assertIn("DGX unreachable", captured.output[0])
        self.assertEqual(response.status_code, 200)
        frames = _frames(response.text)
        rendered = _content(frames)
        self.assertIn("Pipeline error", rendered)
        self.assertIn("DGX unreachable", rendered)
        self.assertEqual(frames[-1]["choices"][0]["finish_reason"], "stop")
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))

    def test_empty_message_list_is_rejected_at_the_boundary(self) -> None:
        response = self.client.post(
            _CHAT_ROUTES[1],
            json={"model": oi_dspy.MODEL_NAME, "messages": [], "stream": True},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.orchestrator.calls, [])

    def test_missing_fields_are_rejected(self) -> None:
        for body in (
            {"model": oi_dspy.MODEL_NAME},
            {"messages": [{"role": "user", "content": "hi"}]},
            {"model": oi_dspy.MODEL_NAME, "messages": [{"role": "user"}]},
        ):
            with self.subTest(body=body):
                response = self.client.post(_CHAT_ROUTES[1], json=body)
                self.assertEqual(response.status_code, 422)


class CorsTests(unittest.TestCase):
    """Open WebUI calls this API from the browser, so preflight must pass."""

    def setUp(self) -> None:
        self.client = TestClient(oi_dspy.app)

    def test_preflight_allows_a_cross_origin_post(self) -> None:
        response = self.client.options(
            _CHAT_ROUTES[1],
            headers={
                "Origin": "https://webui.localhost",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"], "https://webui.localhost"
        )
        self.assertIn("POST", response.headers["access-control-allow-methods"])


class _StubChunkRepo:
    """Chunk-table boundary for the provenance deep-link route.

    Args:
        chunk: Row to return, or ``None`` to model an unknown chunk id.
    """

    def __init__(self, chunk: dict | None) -> None:
        self.chunk = chunk
        self.calls: list[str] = []

    def get_chunk_by_id(self, chunk_id: str) -> dict | None:
        """Return the configured row regardless of *chunk_id*.

        Args:
            chunk_id: The requested chunk id, recorded for assertions.

        Returns:
            The configured row, or ``None``.
        """
        self.calls.append(chunk_id)
        return self.chunk


class ChunkEndpointTests(unittest.TestCase):
    """``GET /chunk/{id}`` backs the ``view chunk`` provenance links."""

    def _client_for(self, chunk: dict | None) -> TestClient:
        """Patch the chunk repository and return a client bound to the app.

        Args:
            chunk: Row the stub repository serves.

        Returns:
            A ``TestClient`` whose chunk lookups hit the stub.
        """
        patch = mock.patch.object(oi_dspy, "chunk_repo", _StubChunkRepo(chunk))
        patch.start()
        self.addCleanup(patch.stop)
        return TestClient(oi_dspy.app)

    def test_unknown_chunk_returns_404_page(self) -> None:
        client = self._client_for(None)
        response = client.get("/chunk/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Chunk not found", response.text)

    def test_known_chunk_renders_its_source_text(self) -> None:
        client = self._client_for(
            {
                "celex_id": _KNOWN_CELEX,
                "doc_pos": 17,
                "document": "The undertakings agreed on prices.",
            }
        )
        response = client.get("/chunk/abc-123")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn(_KNOWN_CELEX, response.text)
        self.assertIn("doc_pos 17", response.text)
        self.assertIn("The undertakings agreed on prices.", response.text)

    def test_document_markup_is_escaped(self) -> None:
        # Decision text is ingested from MinerU markdown and reaches the page
        # verbatim; unescaped angle brackets would be parsed as markup.
        client = self._client_for(
            {
                "celex_id": _KNOWN_CELEX,
                "doc_pos": None,
                "document": "<script>alert(1)</script>",
            }
        )
        response = client.get("/chunk/abc-123")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;", response.text)


if __name__ == "__main__":
    unittest.main()
