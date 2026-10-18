"""Unit tests for the pre-plan knowledge probe.

The anchor cases are the near misses measured against the live store on
20260911: an entity swap and a scope swap that the embedding distance alone
would have let through.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/agent_dspy/tests/test_knowledge_probe.py -v
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.agent_dspy.dspy_signature import RetrievalPlan, RetrievalStep
from pipelines.agent_dspy.knowledge_probe import (
    KnowledgeProbe,
    anchors_compatible,
    extract_anchors,
)
from pipelines.agent_dspy.retrieval_dispatch import EvidenceResult
from pipelines.agent_dspy.retrieval_modules import QAExecutorModule

_NTN_ITEM_TEXT = "Koyo Deutschland GmbH is a cartel peer of NTN Wälzlager."


def _item(text: str) -> SimpleNamespace:
    """A KnowledgeItem stand-in carrying what rendering reads."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_text=text,
        knowledge_type="domain_fact",
        scope="global",
        confidence=0.95,
    )


class TestExtractAnchors(unittest.TestCase):
    """Identifier and name tokens are separated; openers are not names."""

    def test_case_ids_and_names(self):
        anchors = extract_anchors(
            "Which leniency did AU Optronics get in COMP/39.309 [context: celex_id=39309_3643_4]?"
        )
        self.assertEqual(anchors.ids, {"comp/39.309", "39309_3643_4"})
        self.assertEqual(anchors.names, {"au", "optronics"})

    def test_celex_prefix_dropped(self):
        self.assertEqual(
            extract_anchors("fines in CELEX-31994D0823").ids, {"31994d0823"}
        )

    def test_question_words_and_institutions_are_not_names(self):
        self.assertEqual(
            extract_anchors("Which cases did the European Commission decide?").names,
            frozenset(),
        )


class TestAnchorsCompatible(unittest.TestCase):
    """The guard keeps paraphrases and rejects entity / scope swaps."""

    def test_paraphrase_kept(self):
        self.assertTrue(
            anchors_compatible(
                "which companies were in the cartel with NTN Wälzlager",
                "Which companies are part of the cartel with NTN Wälzlager?",
                _NTN_ITEM_TEXT,
            )
        )

    def test_entity_swap_rejected(self):
        self.assertFalse(
            anchors_compatible(
                "list cartel peers of NSK",
                "list cartel peers of NTN Wälzlager",
                _NTN_ITEM_TEXT,
            )
        )

    def test_name_found_in_knowledge_text_kept(self):
        self.assertTrue(
            anchors_compatible(
                "list cartel peers of Koyo",
                "list cartel peers of NTN Wälzlager",
                _NTN_ITEM_TEXT,
            )
        )

    def test_scope_swap_rejected(self):
        self.assertFalse(
            anchors_compatible(
                "list leniency cases",
                "list leniency cases  [context: celex_id=39309_3643_4]",
                "AU Optronics was granted 20% leniency.",
            )
        )

    def test_same_scope_kept(self):
        self.assertTrue(
            anchors_compatible(
                "what infringement happened in 33708_6_7",
                "What infringement was identified in the case with celex_id=33708_6_7?",
                "The infringement in 33708_6_7 is price fixing.",
            )
        )


class TestKnowledgeProbe(unittest.TestCase):
    """The probe filters repository hits and renders the survivors."""

    def _probe(self, rows: list[dict]) -> tuple[KnowledgeProbe, MagicMock]:
        matcher = MagicMock()
        matcher.search_knowledge_by_question.return_value = rows
        matcher.get_knowledge_sources.return_value = []
        probe = KnowledgeProbe(
            matcher=matcher, embed_query=lambda q: [0.1], max_distance=0.1, limit=5
        )
        return probe, matcher

    def test_renders_guarded_matches(self):
        probe, matcher = self._probe(
            [
                {
                    "item": _item(_NTN_ITEM_TEXT),
                    "question_variant": "list cartel peers of NTN Wälzlager",
                    "distance": 0.03,
                }
            ]
        )
        result = probe.probe("who were the cartel peers of NTN Wälzlager?")
        self.assertIsNotNone(result)
        self.assertIn("[knowledge_match]", result.text)
        self.assertIn("'list cartel peers of NTN Wälzlager'", result.text)
        self.assertIn(_NTN_ITEM_TEXT, result.text)
        matcher.search_knowledge_by_question.assert_called_once_with(
            [0.1], max_distance=0.1, limit=5
        )

    def test_none_when_guard_rejects_everything(self):
        probe, _ = self._probe(
            [
                {
                    "item": _item(_NTN_ITEM_TEXT),
                    "question_variant": "list cartel peers of NTN Wälzlager",
                    "distance": 0.09,
                }
            ]
        )
        self.assertIsNone(probe.probe("list cartel peers of NSK"))

    def test_none_when_no_rows(self):
        probe, _ = self._probe([])
        self.assertIsNone(probe.probe("anything"))

    def test_embedding_failure_is_no_match(self):
        matcher = MagicMock()

        def _fail(_question: str) -> list[float]:
            raise ConnectionError("ollama down")

        probe = KnowledgeProbe(matcher=matcher, embed_query=_fail)
        with self.assertLogs("pipelines.agent_dspy.knowledge_probe", level="WARNING"):
            self.assertIsNone(probe.probe("anything"))
        matcher.search_knowledge_by_question.assert_not_called()


class TestExecutorStepZero(unittest.TestCase):
    """The executor prepends a probe hit as step 0 without counting it as a plan hit."""

    def _executor(self, probe_result: EvidenceResult | None) -> QAExecutorModule:
        probe = MagicMock()
        probe.probe.return_value = probe_result
        executor = QAExecutorModule(
            sql_store=MagicMock(),
            kb_searcher=MagicMock(),
            aggregator=MagicMock(),
            hybrid_searcher=MagicMock(),
            doc_searcher=MagicMock(),
            knowledge_probe=probe,
        )
        executor.dispatcher = MagicMock()
        executor.dispatcher.dispatch.return_value = EvidenceResult(
            text="[kb_sql] {'celex_id': '39922_2067_2'}",
            fact_ids=["f1"],
            celex_ids=["39922_2067_2"],
        )
        return executor

    _PLAN = RetrievalPlan(
        question_type="exploratory",
        steps=[RetrievalStep(step_id=1, tool="kb_sql", query="SELECT 1", rationale="r")],
    )

    def test_probe_hit_is_step_zero(self):
        executor = self._executor(
            EvidenceResult(
                text="[knowledge_match] x", chunk_ids=["c1"], celex_ids=["39922_2067_2"]
            )
        )
        pred = executor(plan=self._PLAN, question="list cases of NSK")
        self.assertTrue(pred.evidence.startswith("--- Step 0 (knowledge_match) ---"))
        self.assertIn("--- Step 1 (kb_sql) ---", pred.evidence)
        self.assertEqual(pred.chunk_ids, ["c1"])
        self.assertEqual(pred.fact_ids, ["f1"])
        self.assertEqual(pred.celex_ids, ["39922_2067_2"])

    def test_probe_miss_leaves_evidence_plan_only(self):
        executor = self._executor(None)
        pred = executor(plan=self._PLAN, question="list cases of NSK")
        self.assertNotIn("knowledge_match", pred.evidence)
        self.assertTrue(pred.evidence.startswith("--- Step 1 (kb_sql) ---"))


if __name__ == "__main__":
    unittest.main()
