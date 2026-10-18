"""Unit tests for the Stage-1 structural-rejects filter.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/agent_dspy/tests/test_knowledge_stage_filter.py -v
"""

import unittest

from pipelines.agent_dspy.knowledge_stage_filter import (
    StageFilterVerdict,
    apply_stage_filter,
    normalize_prompt,
    should_enqueue_for_knowledge,
)


class TestStageFilterMetaTasks(unittest.TestCase):
    """UI scaffolding prompts must always be rejected."""

    def test_follow_up_questions_template_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="### Task: Suggest 3-5 relevant follow-up questions",
            agent_answer="1. What is X? 2. How does Y work? 3. Where is Z?",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "meta_task")

    def test_generate_title_template_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="Generate a concise title for the following conversation.",
            agent_answer="Cartel Decision Analysis Session",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "meta_task")

    def test_generate_tags_template_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="Generate 1-3 broad tags for this conversation.",
            agent_answer="cartel, fines, leniency",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "meta_task")


class TestStageFilterControlCommands(unittest.TestCase):
    """Reset / control commands carry no knowledge content."""

    def test_reset_command_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="reset case",
            agent_answer="Context cleared, please ask a new question.",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "control_command")

    def test_set_case_all_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="set case all",
            agent_answer="All cases selected.",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "control_command")

    def test_single_word_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="nice",
            agent_answer="The input does not contain a question.",
        )
        self.assertFalse(verdict.kept)
        # Either control_command (short input) or non_answer — both are
        # valid rejection reasons.  Just assert it was rejected.
        self.assertIn(verdict.reason, {"control_command", "non_answer"})

    def test_empty_prompt_rejected(self):
        verdict = apply_stage_filter(user_prompt="", agent_answer="something")
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "control_command")

    def test_commands_with_router_context_suffix_rejected(self):
        """The router appends ``[context: celex_id=...]`` to sticky-case turns."""
        for prompt in (
            "reset  [context: celex_id=CELEX-31998D0273]",
            "unset  [context: celex_id=CELEX-31998D0273]",
            "set all  [context: celex_id=CELEX-31998D0273]",
            "reset  [context: celex_id=39736_1746_5]",
            "unset  [context: celex_id=39736_1746_5]",
        ):
            with self.subTest(prompt=prompt):
                verdict = apply_stage_filter(
                    user_prompt=prompt,
                    agent_answer="CELEX-31998D0273 — IV/35.733—VW, context retained.",
                )
                self.assertFalse(verdict.kept)
                self.assertEqual(verdict.reason, "control_command")

    def test_set_case_id_rejected(self):
        for prompt in ("set IV/26.918", "set 32002D0180", "set  9234234"):
            with self.subTest(prompt=prompt):
                verdict = apply_stage_filter(
                    user_prompt=prompt,
                    agent_answer="CELEX-32002D0180 — COMP/37.859 – De Post-La Poste",
                )
                self.assertFalse(verdict.kept)
                self.assertEqual(verdict.reason, "control_command")

    def test_set_verb_question_kept(self):
        verdict = apply_stage_filter(
            user_prompt="set out the fines imposed in CELEX-31998D0273",
            agent_answer=(
                "The Commission imposed a fine of EUR 102 million on "
                "Volkswagen AG for obstructing parallel imports into Italy."
            ),
        )
        self.assertTrue(verdict.kept)


class TestStageFilterNonAnswers(unittest.TestCase):
    """Error templates and refusals must be rejected."""

    def test_case_not_found_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="What are the fines for IV/99999?",
            agent_answer=(
                "The case identifier 'IV/99999' was not found in the "
                "database. Please check the format."
            ),
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "non_answer")

    def test_pipeline_error_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="Summarise case 38344_5856_3.",
            agent_answer="Pipeline error: connection refused",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "non_answer")


class TestStageFilterNullResults(unittest.TestCase):
    """Null-result answers carry zero positive knowledge."""

    def test_no_occurrences_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="count occurrence of court in CELEX-32003D0600",
            agent_answer="No occurrences of 'court' were found in the corpus.",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "null_result")

    def test_no_references_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="court references in CELEX-32003D0600",
            agent_answer="No court references were found in this case.",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "null_result")


class TestStageFilterCorpusAggregates(unittest.TestCase):
    """Volatile count statistics must not be persisted as knowledge."""

    def test_chunk_count_aggregate_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="How many chunks mention 'court'?",
            agent_answer="'court' appears in 4025 chunks across 497 cases.",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "corpus_aggregate")

    def test_case_count_aggregate_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="How many cases involve leniency?",
            agent_answer="238 cases involve leniency applicants.",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "corpus_aggregate")


class TestStageFilterEmptyOrStub(unittest.TestCase):
    """Empty and dashed-stub answers must be rejected."""

    def test_too_short_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="What are the fines in CELEX-31982D0367?",
            agent_answer="",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "empty_or_stub")

    def test_dashed_stub_rejected(self):
        verdict = apply_stage_filter(
            user_prompt="Top cases?",
            agent_answer="38344_5856_3 —\n39612_12448_6 —\n40023_999 —",
        )
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "empty_or_stub")


class TestStageFilterKeeps(unittest.TestCase):
    """Genuine knowledge-bearing answers must survive the filter."""

    def test_legal_principle_kept(self):
        verdict = apply_stage_filter(
            user_prompt=(
                "Under what circumstances is the Akzo presumption applicable?"
            ),
            agent_answer=(
                "Under the Akzo presumption, parental liability for the "
                "subsidiary's cartel conduct is presumed when the parent "
                "company owns 100 percent (or close to 100 percent) of the "
                "subsidiary's capital, unless the parent rebuts the "
                "presumption by demonstrating that the subsidiary acts "
                "independently."
            ),
        )
        self.assertTrue(verdict.kept, msg=verdict.reason)

    def test_procedure_kept(self):
        verdict = apply_stage_filter(
            user_prompt="What is the role of the hearing officer?",
            agent_answer=(
                "The hearing officer safeguards the procedural rights of "
                "parties during the oral hearing. They report directly to "
                "the Commissioner and ensure due process is observed at "
                "every stage of the proceeding."
            ),
        )
        self.assertTrue(verdict.kept, msg=verdict.reason)


class TestStageFilterGrounding(unittest.TestCase):
    """An answer retrieved without any fact cannot become knowledge."""

    _PROMPT = "list all cases related to NSK Deutschland GmbH"
    _ANSWER = (
        "39922_2067_2 — Bearings: NSK Deutschland GmbH was fined and granted "
        "leniency in the bearings cartel."
    )

    def test_empty_fact_ids_rejected(self):
        verdict = apply_stage_filter(self._PROMPT, self._ANSWER, fact_ids=[])
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.reason, "no_fact_ids")

    def test_fact_ids_present_kept(self):
        verdict = apply_stage_filter(
            self._PROMPT, self._ANSWER, fact_ids=["3d1c4224-7ca9-4ff9-aa6e-f50af8b8cc1c"]
        )
        self.assertTrue(verdict.kept, msg=verdict.reason)

    def test_omitted_fact_ids_skip_the_rule(self):
        verdict = apply_stage_filter(self._PROMPT, self._ANSWER)
        self.assertTrue(verdict.kept, msg=verdict.reason)

    def test_structural_reason_wins_over_grounding(self):
        verdict = apply_stage_filter("reset", self._ANSWER, fact_ids=[])
        self.assertEqual(verdict.reason, "control_command")


class TestNormalizePrompt(unittest.TestCase):
    """Re-asked questions collapse to one dedup key."""

    def test_typographic_quotes_match_ascii(self):
        self.assertEqual(
            normalize_prompt("list all cases related to “NSK Deutschland GmbH”"),
            normalize_prompt('list all cases related to "NSK Deutschland GmbH"'),
        )

    def test_context_suffix_case_and_whitespace_ignored(self):
        self.assertEqual(
            normalize_prompt(
                "  List all  cases related to NSK  [context: celex_id=39922_2067_2]"
            ),
            "list all cases related to nsk",
        )

    def test_none_is_empty(self):
        self.assertEqual(normalize_prompt(None), "")


class TestShouldEnqueueWrapper(unittest.TestCase):
    """Boolean wrapper must mirror apply_stage_filter().kept."""

    def test_true_when_kept(self):
        self.assertTrue(
            should_enqueue_for_knowledge(
                user_prompt="What constitutes a horizontal cartel under EU law?",
                agent_answer=(
                    "A horizontal cartel under EU law is an agreement or "
                    "concerted practice between competitors at the same "
                    "level of the supply chain that has the object or "
                    "effect of restricting competition."
                ),
            )
        )

    def test_false_when_rejected(self):
        self.assertFalse(
            should_enqueue_for_knowledge(
                user_prompt="### Task: Suggest follow-up questions",
                agent_answer="What about X?",
            )
        )


class TestVerdictModel(unittest.TestCase):
    """Pydantic verdict model behaves as advertised."""

    def test_default_reason_is_empty_when_kept(self):
        v = StageFilterVerdict(kept=True)
        self.assertTrue(v.kept)
        self.assertEqual(v.reason, "")


if __name__ == "__main__":
    unittest.main()
