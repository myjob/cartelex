"""Router-seam tests for conversation-thread handling.

Covers the reported regression directly: a two-turn thread whose follow-up
("list those cases") must be resolved against the first turn and scoped to the
cases that turn actually resolved -- rather than planned from four words with no
referent, which is what returned 348 unrelated cases.

Every boundary is stubbed: no database, no LLM.
"""

import unittest
from unittest.mock import patch

import dspy

from config.settings import settings
from pipelines.agent_dspy.conversation_context import ConversationTurn
from pipelines.agent_dspy.dspy_signature import (
    RetrievalPlan,
    RetrievalStep,
    TurnDependency,
)
from pipelines.agent_dspy.retrieval_modules import (
    RetrievalEvent,
    _scope_plan_to_celex_ids,
    _scope_sql_to_celex_ids,
)
from pipelines.agent_dspy.retrieval_router import RetrievalRouterModule
from tools.tests.celex_index_fixture import FakeCelexChecker

_FOLLOW_UP = "list those cases"
_PRIOR_QUESTION = (
    "which companies got investigated in several cartel cases by EU commission?"
)
_PINNED = ["CELEX-31994D0823", "CELEX-31969D0216"]
_STANDALONE = (
    "list the cartel cases in which Solvay and Akzo Nobel were investigated"
)


class _RecordingQAPipeline:
    """Records what the router delegated, including the pinned case set."""

    def __init__(self) -> None:
        self.questions: list[str] = []
        self.planner_questions: list[str | None] = []
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
        self.questions.append(question)
        self.planner_questions.append(question_for_planner)
        self.pinned.append(pinned_celex_ids)
        yield RetrievalEvent(
            "result",
            prediction=dspy.Prediction(
                celex_validation=validation,
                plan=None,
                evidence="stub evidence",
                answer="stub answer",
                references=[],
                fact_ids=[],
                chunk_ids=[],
                fact_names=[],
                celex_ids=[],
            ),
        )


class _StubCondenser:
    """Stands in for ConversationCondenserModule at the LM boundary."""

    def __init__(self, verdict: TurnDependency) -> None:
        self.verdict = verdict
        self.calls: list[str] = []

    def __call__(self, question, turns=None):
        self.calls.append(question)
        return dspy.Prediction(result=self.verdict)


def _build_router(condenser) -> tuple[RetrievalRouterModule, _RecordingQAPipeline]:
    """Assemble a router over stubbed collaborators."""
    qa = _RecordingQAPipeline()
    router = RetrievalRouterModule(
        qa_pipeline=qa,
        enumeration=object(),
        celex_checker=FakeCelexChecker({}),
        condenser=condenser,
    )
    return router, qa


def _drain(router, **kwargs) -> tuple[str, dspy.Prediction | None]:
    """Run the router and return the rendered status text and terminal result."""
    rendered: list[str] = []
    terminal = None
    for event in router.run_events(**kwargs):
        if event.kind == "status":
            rendered.append(event.text)
        if event.prediction is not None:
            terminal = event.prediction
    return "".join(rendered), terminal


class DependentFollowUpTests(unittest.TestCase):
    """A refinement is rewritten and pinned to the previous turn's cases."""

    def setUp(self):
        self.history = [
            ConversationTurn(
                question=_PRIOR_QUESTION,
                answer="Solvay and Akzo Nobel appear in more than one decision.",
                pinned_celex_ids=list(_PINNED),
            )
        ]
        self.condenser = _StubCondenser(
            TurnDependency(
                dependency="refine_previous",
                standalone_question=_STANDALONE,
                reason="'those cases' points at the previous answer",
            )
        )

    def test_planner_sees_the_standalone_question(self):
        router, qa = _build_router(self.condenser)

        _drain(router, question=_FOLLOW_UP, history=self.history)

        self.assertEqual(qa.planner_questions, [_STANDALONE])
        self.assertEqual(qa.questions, [_STANDALONE])

    def test_previous_case_set_is_handed_to_the_qa_pipeline(self):
        router, qa = _build_router(self.condenser)

        _drain(router, question=_FOLLOW_UP, history=self.history)

        self.assertEqual(qa.pinned, [_PINNED])

    def test_the_resolved_reading_is_shown_to_the_user(self):
        router, _ = _build_router(self.condenser)

        rendered, _ = _drain(router, question=_FOLLOW_UP, history=self.history)

        # An audit-grade answer must state the assumption it rests on.
        self.assertIn(_STANDALONE, rendered)
        self.assertIn("2 case(s)", rendered)

    def test_history_disabled_reproduces_the_old_behaviour(self):
        router, qa = _build_router(self.condenser)

        with patch.object(settings, "CONVERSATION_HISTORY_ENABLED", False):
            _drain(router, question=_FOLLOW_UP, history=self.history)

        self.assertEqual(self.condenser.calls, [])
        self.assertEqual(qa.questions, [_FOLLOW_UP])
        self.assertEqual(qa.pinned, [[]])


class UnrelatedTurnTests(unittest.TestCase):
    """A new topic drops the history entirely."""

    def setUp(self):
        self.history = [
            ConversationTurn(
                question=_PRIOR_QUESTION,
                answer="Solvay and Akzo Nobel.",
                pinned_celex_ids=list(_PINNED),
            )
        ]

    def test_standalone_verdict_passes_the_question_through_unpinned(self):
        question = "how many decisions fined companies in the cement sector?"
        condenser = _StubCondenser(
            TurnDependency(
                dependency="standalone",
                standalone_question=question,
                reason="new topic",
            )
        )
        router, qa = _build_router(condenser)

        rendered, _ = _drain(router, question=question, history=self.history)

        self.assertEqual(qa.questions, [question])
        self.assertEqual(qa.pinned, [[]])
        self.assertNotIn("Follow-up resolved", rendered)

    def test_no_history_means_the_condenser_is_never_consulted(self):
        condenser = _StubCondenser(
            TurnDependency(
                dependency="standalone", standalone_question="x", reason=""
            )
        )
        router, qa = _build_router(condenser)

        _drain(router, question=_PRIOR_QUESTION, history=[])

        self.assertEqual(condenser.calls, [])
        self.assertEqual(qa.questions, [_PRIOR_QUESTION])


class ScopeSqlTests(unittest.TestCase):
    """Pinned ids must reach the SQL, whether or not a placeholder is present."""

    def test_substitutes_the_placeholder(self):
        sql = "SELECT celex_id FROM facts WHERE celex_id IN ({celex_ids})"

        scoped = _scope_sql_to_celex_ids(sql, ["CELEX-A", "CELEX-B"])

        self.assertIn("'CELEX-A', 'CELEX-B'", scoped)
        self.assertNotIn("{celex_ids}", scoped)

    def test_wraps_a_query_without_a_placeholder(self):
        scoped = _scope_sql_to_celex_ids(
            "SELECT celex_id, name FROM facts;", ["CELEX-A"]
        )

        self.assertTrue(scoped.startswith("SELECT * FROM ("))
        self.assertIn("WHERE chained.celex_id IN ('CELEX-A')", scoped)
        self.assertNotIn(";", scoped)

    def test_escapes_quotes(self):
        scoped = _scope_sql_to_celex_ids("SELECT celex_id FROM facts", ["a'b"])

        self.assertIn("'a''b'", scoped)

    def test_empty_ids_leave_the_sql_untouched(self):
        sql = "SELECT celex_id FROM facts"
        self.assertEqual(_scope_sql_to_celex_ids(sql, []), sql)


class ScopePlanTests(unittest.TestCase):
    """The whole plan is pinned before any step runs."""

    def test_scopes_sql_steps_and_fills_a_missing_case_id(self):
        plan = RetrievalPlan(
            question_type="cross_case",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="kb_sql",
                    query="SELECT celex_id FROM facts",
                    rationale="r",
                ),
                RetrievalStep(
                    step_id=2, tool="case_facts", query="", rationale="r"
                ),
            ],
        )

        _scope_plan_to_celex_ids(plan, ["CELEX-A", "CELEX-B"])

        self.assertIn("chained.celex_id IN ('CELEX-A', 'CELEX-B')", plan.steps[0].query)
        self.assertEqual(plan.steps[1].case_id, "CELEX-A")

    def test_intra_turn_chaining_wins_over_the_carried_set(self):
        plan = RetrievalPlan(
            question_type="cross_case",
            steps=[
                RetrievalStep(
                    step_id=1, tool="kb_sql", query="SELECT celex_id FROM facts",
                    rationale="r",
                ),
                RetrievalStep(
                    step_id=2,
                    tool="kb_sql",
                    query="SELECT name FROM facts",
                    input_from_step=1,
                    rationale="r",
                ),
            ],
        )

        _scope_plan_to_celex_ids(plan, ["CELEX-A"])

        self.assertIn("CELEX-A", plan.steps[0].query)
        self.assertEqual(plan.steps[1].query, "SELECT name FROM facts")

    def test_an_explicit_case_id_is_never_overwritten(self):
        plan = RetrievalPlan(
            question_type="single_case",
            steps=[
                RetrievalStep(
                    step_id=1,
                    tool="case_facts",
                    query="",
                    case_id="CELEX-EXPLICIT",
                    rationale="r",
                )
            ],
        )

        _scope_plan_to_celex_ids(plan, ["CELEX-A"])

        self.assertEqual(plan.steps[0].case_id, "CELEX-EXPLICIT")

    def test_respects_the_case_cap(self):
        plan = RetrievalPlan(
            question_type="cross_case",
            steps=[
                RetrievalStep(
                    step_id=1, tool="kb_sql", query="SELECT celex_id FROM facts",
                    rationale="r",
                )
            ],
        )

        with patch.object(settings, "CONVERSATION_PIN_MAX_CASES", 2):
            _scope_plan_to_celex_ids(plan, ["A", "B", "C", "D"])

        self.assertIn("'A', 'B'", plan.steps[0].query)
        self.assertNotIn("'C'", plan.steps[0].query)

    def test_no_pinned_ids_leaves_the_plan_alone(self):
        step = RetrievalStep(
            step_id=1, tool="kb_sql", query="SELECT celex_id FROM facts", rationale="r"
        )
        plan = RetrievalPlan(question_type="cross_case", steps=[step])

        _scope_plan_to_celex_ids(plan, [])

        self.assertEqual(plan.steps[0].query, "SELECT celex_id FROM facts")


if __name__ == "__main__":
    unittest.main()
