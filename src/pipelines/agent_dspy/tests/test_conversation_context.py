"""Unit tests for conversation-thread parsing, pinning and condensation.

All boundaries are stubbed: no database, no LLM, no FastAPI.  The module under
test is deliberately free of the provisioned pipeline so these run in isolation.
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
    latest_pinned_celex_ids,
    looks_dependent,
    parse_history,
    render_transcript,
    strip_agent_noise,
)
from pipelines.agent_dspy.dspy_signature import TurnDependency


class _Message:
    """Minimal stand-in for the UI's ChatMessage (role/content attributes)."""

    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


# A realistic assistant message: streamed status lines, the answer, the
# provenance block with an expandable chunk, the doc footer, and the pin marker.
_RAW_AGENT_MESSAGE = (
    "\U0001F50D Validating case reference …\n"
    "\U0001F4CB Building retrieval plan …\n"
    "   → 2 step(s): `kb_sql`, `case_facts`\n"
    "⚙️  Executing retrieval …\n"
    "✨ Synthesizing answer …\n\n"
    "Three companies appear in more than one cartel decision:\n"
    "- Solvay\n"
    "- Akzo Nobel\n"
    "\nReferences:\n\n"
    "<details><summary>companies_investigated</summary>\n"
    "<pre>verbatim source chunk text</pre></details>\n"
    "\n---\n"
    "[\U0001F4C4 MD](http://localhost:7777/doc/CELEX-31994D0823/md) "
    "[\U0001F4D5 PDF](http://localhost:7777/doc/CELEX-31994D0823/pdf)\n"
    "\n[celex-context]: #CELEX-31994D0823,CELEX-31969D0216\n"
)


class StripAgentNoiseTests(unittest.TestCase):
    """The transcript must be reduced to the prose the user actually read."""

    def test_removes_every_kind_of_ui_furniture(self):
        cleaned = strip_agent_noise(_RAW_AGENT_MESSAGE)

        self.assertIn("Three companies appear in more than one cartel", cleaned)
        self.assertIn("- Solvay", cleaned)
        # Status lines, provenance, chunk text, footer and marker are all gone.
        self.assertNotIn("Validating case reference", cleaned)
        self.assertNotIn("Building retrieval plan", cleaned)
        self.assertNotIn("2 step(s)", cleaned)
        self.assertNotIn("Executing retrieval", cleaned)
        self.assertNotIn("References:", cleaned)
        self.assertNotIn("verbatim source chunk text", cleaned)
        self.assertNotIn("MD](http", cleaned)
        self.assertNotIn("celex-context", cleaned)

    def test_strips_master_planner_elapsed_status_lines(self):
        raw = "[0.4s] Decomposing into sub-tasks ...\nThe answer.\n"
        self.assertEqual(strip_agent_noise(raw), "The answer.")

    def test_keeps_ordinary_prose_untouched(self):
        raw = "Case CELEX-31994D0823 fined Solvay EUR 3 million."
        self.assertEqual(strip_agent_noise(raw), raw)

    def test_empty_input(self):
        self.assertEqual(strip_agent_noise(""), "")


class PinnedContextTests(unittest.TestCase):
    """The pinned case set must round-trip through the transcript."""

    def test_round_trip(self):
        ids = ["CELEX-31994D0823", "AT_39914", "37766_610_5"]
        self.assertEqual(decode_pinned_context(encode_pinned_context(ids)), ids)

    def test_decodes_from_a_full_agent_message(self):
        self.assertEqual(
            decode_pinned_context(_RAW_AGENT_MESSAGE),
            ["CELEX-31994D0823", "CELEX-31969D0216"],
        )

    def test_marker_is_capped(self):
        many = [f"CELEX-3199{i:04d}" for i in range(200)]
        with patch.object(settings, "CONVERSATION_PIN_MAX_CASES", 5):
            self.assertEqual(len(decode_pinned_context(encode_pinned_context(many))), 5)

    def test_rejects_junk_tokens(self):
        raw = "[celex-context]: #CELEX-31994D0823,drop table facts,x"
        self.assertEqual(decode_pinned_context(raw), ["CELEX-31994D0823"])

    def test_no_marker_when_nothing_resolved(self):
        self.assertEqual(encode_pinned_context([]), "")

    def test_disabled_by_setting(self):
        with patch.object(settings, "CONVERSATION_PIN_CELEX_ENABLED", False):
            self.assertEqual(encode_pinned_context(["CELEX-31994D0823"]), "")
            self.assertEqual(decode_pinned_context(_RAW_AGENT_MESSAGE), [])


class ParseHistoryTests(unittest.TestCase):
    """The reported two-turn thread must parse into one usable prior turn."""

    def setUp(self):
        self.messages = [
            _Message(
                "user",
                "which companies got investigated in several cartel cases "
                "by EU commission?",
            ),
            _Message("assistant", _RAW_AGENT_MESSAGE),
            _Message("user", "list those cases"),
        ]

    def test_pairs_prior_turns_and_excludes_the_current_one(self):
        turns = parse_history(self.messages)

        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0].question.startswith("which companies"))
        self.assertIn("Solvay", turns[0].answer)
        self.assertNotIn("Validating", turns[0].answer)
        self.assertEqual(
            turns[0].pinned_celex_ids,
            ["CELEX-31994D0823", "CELEX-31969D0216"],
        )

    def test_first_turn_of_a_thread_has_no_history(self):
        self.assertEqual(parse_history(self.messages[-1:]), [])

    def test_respects_max_turns(self):
        messages = []
        for i in range(5):
            messages.append(_Message("user", f"question {i}"))
            messages.append(_Message("assistant", f"answer {i}"))
        messages.append(_Message("user", "now this"))

        turns = parse_history(messages, max_turns=2)

        self.assertEqual([t.question for t in turns], ["question 3", "question 4"])

    def test_unanswered_user_message_still_yields_a_turn(self):
        messages = [
            _Message("user", "first"),
            _Message("user", "second"),
            _Message("assistant", "answered second"),
            _Message("user", "current"),
        ]
        turns = parse_history(messages)
        self.assertEqual([t.question for t in turns], ["first", "second"])
        self.assertEqual(turns[0].answer, "")
        self.assertEqual(turns[1].answer, "answered second")

    def test_disabled_by_setting(self):
        with patch.object(settings, "CONVERSATION_HISTORY_ENABLED", False):
            self.assertEqual(parse_history(self.messages), [])


class LatestPinnedTests(unittest.TestCase):
    """An intervening turn that resolved nothing must not erase the case set."""

    def test_scans_backwards_past_an_empty_turn(self):
        turns = [
            ConversationTurn(question="q1", answer="a1", pinned_celex_ids=["A", "B"]),
            ConversationTurn(question="q2", answer="a2", pinned_celex_ids=[]),
        ]
        self.assertEqual(latest_pinned_celex_ids(turns), ["A", "B"])

    def test_prefers_the_newest_non_empty(self):
        turns = [
            ConversationTurn(question="q1", answer="a1", pinned_celex_ids=["A"]),
            ConversationTurn(question="q2", answer="a2", pinned_celex_ids=["C"]),
        ]
        self.assertEqual(latest_pinned_celex_ids(turns), ["C"])

    def test_empty_history(self):
        self.assertEqual(latest_pinned_celex_ids([]), [])


class LooksDependentTests(unittest.TestCase):
    """The pre-gate decides whether a turn needs the history at all."""

    def test_anaphoric_follow_ups(self):
        for prompt in (
            "list those cases",
            "what fines were imposed on them?",
            "and the same period for the other parties",
            "which of these had leniency applicants?",
            "give me more details",
            "why?",
            "what about that decision",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(looks_dependent(prompt))

    def test_self_contained_questions(self):
        for prompt in (
            "which companies got investigated in several cartel cases by EU commission?",
            "list all leniency applicants in CELEX-31994D0823",
            "how many cartel decisions mention market sharing in the chemicals sector?",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(looks_dependent(prompt))

    def test_empty_prompt(self):
        self.assertFalse(looks_dependent(""))


class RenderTranscriptTests(unittest.TestCase):
    """The condenser sees a compact transcript, not whole case listings."""

    def test_renders_pairs_and_truncates_long_answers(self):
        turns = [ConversationTurn(question="q", answer="x" * 3000)]
        rendered = render_transcript(turns, max_answer_chars=100)

        self.assertTrue(rendered.startswith("User: q\nAgent: "))
        self.assertIn("[…]", rendered)
        self.assertLess(len(rendered), 200)

    def test_omits_an_empty_answer(self):
        turns = [ConversationTurn(question="q", answer="")]
        self.assertEqual(render_transcript(turns), "User: q")


class _RecordingPredictor:
    """Stub predictor recording its calls, standing in for the LM boundary."""

    def __init__(self, result: TurnDependency | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or TurnDependency(
            dependency="refine_previous",
            standalone_question=(
                "list the cartel cases in which Solvay and Akzo Nobel were "
                "investigated"
            ),
            reason="'those cases' refers to the previous answer",
        )

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return dspy.Prediction(result=self.result)


class ConversationCondenserTests(unittest.TestCase):
    """Condensation rewrites a dependent turn and skips the LM otherwise."""

    def setUp(self):
        self.turns = [
            ConversationTurn(
                question="which companies got investigated in several cartel cases?",
                answer="Solvay and Akzo Nobel appear in more than one decision.",
                pinned_celex_ids=["CELEX-31994D0823"],
            )
        ]

    def test_rewrites_a_dependent_turn(self):
        predictor = _RecordingPredictor()
        module = ConversationCondenserModule(predictor=predictor)

        result = module(question="list those cases", turns=self.turns).result

        self.assertEqual(result.dependency, "refine_previous")
        self.assertIn("Solvay", result.standalone_question)
        self.assertEqual(len(predictor.calls), 1)
        self.assertIn("Solvay", predictor.calls[0]["conversation"])

    def test_pre_gate_skips_the_llm_for_a_self_contained_question(self):
        predictor = _RecordingPredictor()
        module = ConversationCondenserModule(predictor=predictor)
        question = "how many decisions fined companies in the cement sector?"

        result = module(question=question, turns=self.turns).result

        self.assertEqual(result.dependency, "standalone")
        self.assertEqual(result.standalone_question, question)
        self.assertEqual(predictor.calls, [])

    def test_no_history_means_no_llm_call(self):
        predictor = _RecordingPredictor()
        module = ConversationCondenserModule(predictor=predictor)

        result = module(question="list those cases", turns=[]).result

        self.assertEqual(result.dependency, "standalone")
        self.assertEqual(predictor.calls, [])

    def test_condense_always_overrides_the_pre_gate(self):
        predictor = _RecordingPredictor()
        module = ConversationCondenserModule(predictor=predictor)
        question = "how many decisions fined companies in the cement sector?"

        with patch.object(settings, "CONVERSATION_CONDENSE_ALWAYS", True):
            module(question=question, turns=self.turns)

        self.assertEqual(len(predictor.calls), 1)

    def test_llm_may_still_drop_the_history(self):
        predictor = _RecordingPredictor(
            result=TurnDependency(
                dependency="standalone",
                standalone_question="list those cases",
                reason="new topic",
            )
        )
        module = ConversationCondenserModule(predictor=predictor)

        result = module(question="list those cases", turns=self.turns).result

        self.assertEqual(result.dependency, "standalone")

    def test_a_condenser_failure_never_costs_the_user_an_answer(self):
        def _explode(**kwargs):
            raise RuntimeError("LM unreachable")

        module = ConversationCondenserModule(predictor=_explode)

        result = module(question="list those cases", turns=self.turns).result

        self.assertEqual(result.dependency, "standalone")
        self.assertEqual(result.standalone_question, "list those cases")

    def test_empty_rewrite_falls_back_to_the_original_wording(self):
        predictor = _RecordingPredictor(
            result=TurnDependency(
                dependency="refine_previous", standalone_question="   ", reason=""
            )
        )
        module = ConversationCondenserModule(predictor=predictor)

        result = module(question="list those cases", turns=self.turns).result

        self.assertEqual(result.standalone_question, "list those cases")


if __name__ == "__main__":
    unittest.main()
