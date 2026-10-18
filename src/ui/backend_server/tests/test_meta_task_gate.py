"""Tests for the endpoint gate that rejects Open WebUI background calls.

Open WebUI fires title, tag and follow-up generation against the same
``/v1/chat/completions`` endpoint as real questions.  Running the retrieval
pipeline on them costs a full plan-execute-synthesize on the DGX and would pin a
bogus case set onto the conversation, so they are rejected before anything runs.

The predicate is tested directly rather than through the FastAPI app: importing
``ui.backend_server.oi_dspy`` provisions repositories, a reranker and the whole
DSPy pipeline at module scope.
"""

import unittest

from pipelines.agent_dspy.knowledge_stage_filter import is_meta_task_prompt


class MetaTaskPromptTests(unittest.TestCase):
    """UI scaffolding is recognised; real questions are not."""

    def test_rejects_title_generation(self):
        prompt = (
            "### Task:\nGenerate a concise, 3-5 word title summarizing the "
            "chat history.\n### Chat History:\nUSER: list those cases"
        )
        self.assertTrue(is_meta_task_prompt(prompt))

    def test_rejects_tag_generation(self):
        self.assertTrue(
            is_meta_task_prompt("Generate 1-3 broad tags categorizing the topics")
        )

    def test_rejects_follow_up_suggestions(self):
        self.assertTrue(
            is_meta_task_prompt("Suggest 3-5 relevant follow-up questions")
        )

    def test_accepts_a_genuine_question(self):
        for prompt in (
            "which companies got investigated in several cartel cases?",
            "list those cases",
            "what fines were imposed in CELEX-31994D0823?",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(is_meta_task_prompt(prompt))

    def test_tolerates_an_empty_prompt(self):
        self.assertFalse(is_meta_task_prompt(""))


if __name__ == "__main__":
    unittest.main()
