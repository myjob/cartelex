"""One test per conversation switch, asserting its documented off-behaviour.

The point of these is that the flags are real rather than decorative: each case
flips exactly one ``settings.CONVERSATION_*`` value and pins down what the
system does instead.  Together they are the safety net that lets the feature be
backed out from the environment without a code change.
"""

import unittest
from unittest.mock import patch

import dspy

from config.settings import settings
from pipelines.agent_dspy.conversation_context import (
    ConversationCondenserModule,
    ConversationTurn,
    decode_pinned_context,
    encode_pinned_context,
    parse_history,
)
from pipelines.agent_dspy.dspy_signature import TurnDependency
from pipelines.agent_dspy.retrieval_modules import RetrievalEvent
from pipelines.agent_dspy.retrieval_router import RetrievalRouterModule
from tools.tests.celex_index_fixture import FakeCelexChecker

_FOLLOW_UP = "list those cases"
_STANDALONE = "list the cartel cases involving Solvay"
_PINNED = ["CELEX-31994D0823", "CELEX-31969D0216"]


class _Message:
    """Minimal stand-in for the UI's ChatMessage."""

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


class _RecordingQAPipeline:
    """Records the question and pinned set the router delegated."""

    def __init__(self) -> None:
        self.questions: list[str] = []
        self.pinned: list[list[str] | None] = []
        self.attach_references = lambda **kw: (kw.get("answer", ""), [])

    def run_events(
        self,
        question,
        *,
        validation=None,
        question_for_planner=None,
        pinned_celex_ids=None,
    ):
        self.questions.append(question_for_planner or question)
        self.pinned.append(pinned_celex_ids)
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                celex_validation=validation,
                plan=None,
                evidence="",
                answer="stub",
                references=[],
                fact_ids=[],
                chunk_ids=[],
                fact_names=[],
                celex_ids=[],
            ),
        )


class _CountingCondenser:
    """Counts LM-boundary invocations and returns a fixed refinement verdict."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, question, turns=None):
        self.calls += 1
        return dspy.Prediction(
            result=TurnDependency(
                dependency="refine_previous",
                standalone_question=_STANDALONE,
                reason="stub",
            )
        )


def _history() -> list[ConversationTurn]:
    """One completed prior turn carrying a resolved case set."""
    return [
        ConversationTurn(
            question="which companies appear in several cartel cases?",
            answer="Solvay and Akzo Nobel.",
            pinned_celex_ids=list(_PINNED),
        )
    ]


def _run(condenser, question=_FOLLOW_UP, history=None) -> _RecordingQAPipeline:
    """Drive the router over stubbed collaborators and return the QA recorder."""
    qa = _RecordingQAPipeline()
    router = RetrievalRouterModule(
        qa_pipeline=qa,
        enumeration=object(),
        celex_checker=FakeCelexChecker({}),
        condenser=condenser,
    )
    for _ in router.run_events(
        question=question, history=_history() if history is None else history
    ):
        pass
    return qa


class HistoryEnabledFlagTests(unittest.TestCase):
    """CONVERSATION_HISTORY_ENABLED=false restores the pre-feature behaviour."""

    def test_router_ignores_the_thread_entirely(self):
        condenser = _CountingCondenser()

        with patch.object(settings, "CONVERSATION_HISTORY_ENABLED", False):
            qa = _run(condenser)

        self.assertEqual(condenser.calls, 0)
        self.assertEqual(qa.questions, [_FOLLOW_UP])
        self.assertEqual(qa.pinned, [[]])

    def test_transport_parses_no_turns(self):
        messages = [
            _Message("user", "first question"),
            _Message("assistant", "an answer"),
            _Message("user", _FOLLOW_UP),
        ]

        with patch.object(settings, "CONVERSATION_HISTORY_ENABLED", False):
            self.assertEqual(parse_history(messages), [])


class CondenseEnabledFlagTests(unittest.TestCase):
    """CONVERSATION_CONDENSE_ENABLED=false drops the rewrite, keeps the rest."""

    def test_no_rewrite_and_no_llm_call(self):
        recorded: list[str] = []

        def _predictor(**kwargs):
            recorded.append(kwargs["question"])
            return dspy.Prediction(
                result=TurnDependency(
                    dependency="refine_previous",
                    standalone_question=_STANDALONE,
                    reason="",
                )
            )

        module = ConversationCondenserModule(predictor=_predictor)

        with patch.object(settings, "CONVERSATION_CONDENSE_ENABLED", False):
            result = module(question=_FOLLOW_UP, turns=_history()).result

        self.assertEqual(recorded, [])
        self.assertEqual(result.dependency, "standalone")
        self.assertEqual(result.standalone_question, _FOLLOW_UP)

    def test_pinning_still_functions(self):
        with patch.object(settings, "CONVERSATION_CONDENSE_ENABLED", False):
            marker = encode_pinned_context(_PINNED)

        self.assertEqual(decode_pinned_context(marker), _PINNED)


class PinCelexFlagTests(unittest.TestCase):
    """CONVERSATION_PIN_CELEX_ENABLED=false leaves the condense-only behaviour."""

    def test_question_is_rewritten_but_nothing_is_scoped(self):
        condenser = _CountingCondenser()
        # With pinning off the transport records no ids, so the parsed history
        # carries none -- the router has nothing to scope to.
        history = [
            ConversationTurn(
                question="which companies appear in several cartel cases?",
                answer="Solvay and Akzo Nobel.",
                pinned_celex_ids=[],
            )
        ]

        with patch.object(settings, "CONVERSATION_PIN_CELEX_ENABLED", False):
            qa = _run(condenser, history=history)

        self.assertEqual(condenser.calls, 1)
        self.assertEqual(qa.questions, [_STANDALONE])
        self.assertEqual(qa.pinned, [[]])

    def test_no_marker_is_written_or_read(self):
        raw = "answer\n\n[celex-context]: #CELEX-31994D0823"

        with patch.object(settings, "CONVERSATION_PIN_CELEX_ENABLED", False):
            self.assertEqual(encode_pinned_context(_PINNED), "")
            self.assertEqual(decode_pinned_context(raw), [])


class CondenseAlwaysFlagTests(unittest.TestCase):
    """CONVERSATION_CONDENSE_ALWAYS=true bypasses the pure pre-gate."""

    def test_condenses_a_question_the_pre_gate_would_pass_through(self):
        calls: list[str] = []

        def _predictor(**kwargs):
            calls.append(kwargs["question"])
            return dspy.Prediction(
                result=TurnDependency(
                    dependency="standalone",
                    standalone_question=kwargs["question"],
                    reason="",
                )
            )

        module = ConversationCondenserModule(predictor=_predictor)
        question = "how many decisions fined companies in the cement sector?"

        module(question=question, turns=_history())
        self.assertEqual(calls, [], "pre-gate should have short-circuited")

        with patch.object(settings, "CONVERSATION_CONDENSE_ALWAYS", True):
            module(question=question, turns=_history())

        self.assertEqual(calls, [question])


class HistoryMaxTurnsFlagTests(unittest.TestCase):
    """CONVERSATION_HISTORY_MAX_TURNS bounds what the condenser is shown."""

    def test_only_the_most_recent_turns_survive(self):
        messages = []
        for i in range(4):
            messages.append(_Message("user", f"question {i}"))
            messages.append(_Message("assistant", f"answer {i}"))
        messages.append(_Message("user", _FOLLOW_UP))

        with patch.object(settings, "CONVERSATION_HISTORY_MAX_TURNS", 1):
            turns = parse_history(messages)

        self.assertEqual([t.question for t in turns], ["question 3"])


if __name__ == "__main__":
    unittest.main()
