"""Unit tests for the LLM-as-judge gate inside KnowledgeIngestor.

These tests exercise the judge logic in isolation using a fake predictor;
they do not touch the database or the real LLM.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/rag/pgvector/tests/test_knowledge_judge.py -v
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.agent_dspy.knowledge_signatures import KnowHowJudgeResult
from pipelines.graph.entity_graph_models import EdgeRelation, ValidatedRelation
from pipelines.rag.pgvector.knowledge_ingestor import (
    KnowledgeGraphContradiction,
    KnowledgeIngestor,
    KnowledgeJudgeRejected,
)


def _make_fake_judge(verdict: KnowHowJudgeResult):
    """Return a callable that mimics ``dspy.Predict`` semantics.

    DSPy predictors return objects with an ``output`` attribute pointing at
    the pydantic ``OutputField`` model.
    """

    def _judge(**_kwargs):
        return SimpleNamespace(output=verdict)

    return _judge


def _make_ingestor(judge):
    """Build a KnowledgeIngestor with mocked repo + dummy embed + judge."""
    return KnowledgeIngestor(
        knowledge_repo=MagicMock(),
        embed_fn=lambda _t: [0.0] * 4,
        judge=judge,
    )


class TestJudgeGate(unittest.TestCase):
    """Verify the accept/reject behaviour of ``_judge_or_reject``."""

    def test_accept_passes(self):
        verdict = KnowHowJudgeResult(
            accept=True,
            specificity=0.8,
            generality=0.8,
            correctness=0.9,
            non_triviality=0.7,
            rationale="reusable doctrine",
        )
        ing = _make_ingestor(_make_fake_judge(verdict))
        # Should NOT raise
        ing._judge_or_reject(
            knowledge_text="Akzo presumption applies at ~100% capital ownership.",
            knowledge_type="legal_principle",
            user_prompt="When does Akzo apply?",
            supporting_facts_text="evidence ...",
        )

    def test_reject_when_accept_false(self):
        verdict = KnowHowJudgeResult(
            accept=False,
            specificity=0.9,
            generality=0.9,
            correctness=0.9,
            non_triviality=0.9,
            rationale="per-case fact, not doctrine",
        )
        ing = _make_ingestor(_make_fake_judge(verdict))
        with self.assertRaises(KnowledgeJudgeRejected) as ctx:
            ing._judge_or_reject(
                knowledge_text="Case 38344_5856_3 fined Voestalpine 22m EUR.",
                knowledge_type="case_fact",
                user_prompt="What was the fine?",
                supporting_facts_text="...",
            )
        self.assertFalse(ctx.exception.verdict.accept)

    def test_reject_when_any_criterion_below_threshold(self):
        # ``accept=True`` but specificity below 0.5 → must still reject.
        verdict = KnowHowJudgeResult(
            accept=True,
            specificity=0.2,
            generality=0.9,
            correctness=0.9,
            non_triviality=0.9,
            rationale="too vague",
        )
        ing = _make_ingestor(_make_fake_judge(verdict))
        with self.assertRaises(KnowledgeJudgeRejected):
            ing._judge_or_reject(
                knowledge_text="EU law is complex.",
                knowledge_type="domain_fact",
                user_prompt="anything",
                supporting_facts_text="...",
            )

    def test_judge_exception_defaults_to_accept(self):
        """A crashing judge must not block ingestion."""

        def _broken_judge(**_kwargs):
            raise RuntimeError("LLM call timed out")

        ing = _make_ingestor(_broken_judge)
        # Should not raise — silent fallback to accept.
        ing._judge_or_reject(
            knowledge_text="text",
            knowledge_type="type",
            user_prompt="q",
            supporting_facts_text="e",
        )

    def test_disabled_judge_skips_call(self):
        """When ``judge=None`` the gate must be a no-op (no LLM invoked)."""
        ing = KnowledgeIngestor(
            knowledge_repo=MagicMock(),
            embed_fn=lambda _t: [0.0] * 4,
            judge=None,
        )
        self.assertIsNone(ing.judge)


class TestIngestRespectsJudgeRejection(unittest.TestCase):
    """``ingest()`` must short-circuit before any DB write on rejection."""

    def test_ingest_raises_and_does_not_touch_session(self):
        verdict = KnowHowJudgeResult(
            accept=False,
            specificity=0.9,
            generality=0.9,
            correctness=0.9,
            non_triviality=0.9,
            rationale="per-case fact",
        )

        # Wire a Session sentinel that would explode if used.
        repo = MagicMock()
        repo.Session = MagicMock(
            side_effect=AssertionError("Session must not be opened on reject")
        )

        ing = KnowledgeIngestor(
            knowledge_repo=repo,
            embed_fn=lambda _t: [0.0] * 4,
            judge=_make_fake_judge(verdict),
        )

        with self.assertRaises(KnowledgeJudgeRejected):
            ing.ingest(
                knowledge_text="per-case detail",
                knowledge_type="case_fact",
                confidence=0.5,
                question_variants=["q"],
                supporting_fact_ids=[],
                user_prompt="what?",
                supporting_facts_text="...",
            )


class TestEntityGraphGate(unittest.TestCase):
    """The validated entity graph gates the candidate before the judge runs."""

    @staticmethod
    def _fined_relation() -> ValidatedRelation:
        """A validated 'NSK was fined in the bearings cartel' edge."""
        return ValidatedRelation(
            src_name="NSK Deutschland GmbH",
            relation=EdgeRelation.FINED,
            dst_name="Bearings",
            celex_id="39922_2067_2",
            source_fact_id="11111111-1111-1111-1111-111111111111",
            amount="EUR 62 406 000",
        )

    def test_contradiction_raises_before_the_judge_is_called(self):
        """A refuted nugget must cost no inference and no DB write."""
        judge_calls: list[dict] = []

        def _recording_judge(**kwargs):
            judge_calls.append(kwargs)
            raise AssertionError("judge must not run on a graph contradiction")

        repo = MagicMock()
        repo.Session = MagicMock(
            side_effect=AssertionError("Session must not be opened on reject")
        )
        ing = KnowledgeIngestor(
            knowledge_repo=repo,
            embed_fn=lambda _t: [0.0] * 4,
            judge=_recording_judge,
        )

        with self.assertRaises(KnowledgeGraphContradiction) as ctx:
            ing.ingest(
                knowledge_text="NSK was not fined in the bearings cartel.",
                knowledge_type="case_fact",
                confidence=0.5,
                question_variants=["q"],
                supporting_fact_ids=[],
                user_prompt="was NSK fined?",
                supporting_facts_text="...",
                validated_relations=[self._fined_relation()],
            )

        self.assertEqual(judge_calls, [])
        self.assertEqual(ctx.exception.contradiction.trigger, "negated_relation")

    def test_relations_are_rendered_into_the_judge(self):
        """A surviving candidate hands the judge the graph block as evidence."""
        verdict = KnowHowJudgeResult(
            accept=True,
            specificity=0.8,
            generality=0.8,
            correctness=0.9,
            non_triviality=0.7,
            rationale="reusable doctrine",
        )
        captured: list[dict] = []

        def _capturing_judge(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(output=verdict)

        ing = _make_ingestor(_capturing_judge)
        ing._judge_or_reject(
            knowledge_text="A parent is liable at full ownership.",
            knowledge_type="legal_principle",
            user_prompt="when is a parent liable?",
            supporting_facts_text="evidence ...",
            validated_relations=[self._fined_relation()],
        )

        self.assertEqual(len(captured), 1)
        block = captured[0]["validated_relations"]
        self.assertIn("VALIDATED RELATIONS", block)
        self.assertIn("--fined--> Bearings [39922_2067_2]", block)

    def test_no_relations_leaves_the_judge_block_empty(self):
        """Facts that produced no edge must not fabricate a header."""
        verdict = KnowHowJudgeResult(
            accept=True,
            specificity=0.8,
            generality=0.8,
            correctness=0.9,
            non_triviality=0.7,
            rationale="fine",
        )
        captured: list[dict] = []

        def _capturing_judge(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(output=verdict)

        ing = _make_ingestor(_capturing_judge)
        ing._judge_or_reject(
            knowledge_text="text",
            knowledge_type="type",
            user_prompt="q",
            supporting_facts_text="e",
        )

        self.assertEqual(captured[0]["validated_relations"], "")


if __name__ == "__main__":
    unittest.main()
