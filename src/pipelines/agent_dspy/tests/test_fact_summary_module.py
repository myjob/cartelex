import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dspy
import yaml

from config.settings import settings

from pipelines.agent_dspy.universal_modules import FactSummaryModule
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO
from pipelines.agent_dspy.dspy_signature import CaseSummaryChapters
from tools.celex_tools import CelexDatabaseToolkit

T0 = datetime(2026, 8, 1, 12, 0)
T1 = datetime(2026, 9, 1, 12, 0)


def _fact(name: str, value_md: str, created_at: datetime) -> SimpleNamespace:
    """Build a stand-in for a ``Facts`` ORM row."""
    return SimpleNamespace(
        celex_id="12345", name=name, value_md=value_md, value_json=None,
        created_at=created_at,
    )


def _summary_params(**overrides) -> FactExtractorParamVO:
    """Build case_summary params with a small context_facts allow-list."""
    values = dict(
        target_fact_name="case_summary",
        question="Summarize the case",
        celex_id="12345",
        output_schema="",
        pipeline="case_summary",
        context_facts=["companies_investigated", "fines"],
    )
    values.update(overrides)
    return FactExtractorParamVO(**values)


def _case_summary_yaml_struct() -> dict:
    """Return the live ``case_summary`` entry of the fact catalog."""
    with open(settings.FACT_EXTRACTOR_CONFIG_DIR, encoding="utf-8") as handle:
        catalog = yaml.safe_load(handle)
    return next(s for s in catalog if s["target_fact_name"] == "case_summary")


class TestFactSummaryModule(unittest.TestCase):
    def setUp(self):
        self.sql_store_mock = MagicMock()
        self.module = FactSummaryModule(sql_store=self.sql_store_mock)

        # Mock DSPy Prediction Output: every chapter filled with its own name.
        self.dummy_summary = CaseSummaryChapters(
            **{field: field for field in CaseSummaryChapters.model_fields}
        )
        self.mock_summarize = MagicMock(
            return_value=dspy.Prediction(case_summary=self.dummy_summary)
        )
        self.module.summarize = self.mock_summarize

    @patch("pipelines.agent_dspy.universal_modules._check_context_token_limit")
    def test_forward_with_facts(self, _mock_token_check):
        self.sql_store_mock.select_fact4celex.return_value = []  # no stored summary
        self.sql_store_mock.select_named_facts4celex.return_value = [
            _fact("companies_investigated", "Acme Corp", T0),
            _fact("fines", "100M EUR", T0),
        ]

        prediction = self.module(_summary_params())

        self.sql_store_mock.select_named_facts4celex.assert_called_once_with(
            "12345", ["companies_investigated", "fines"]
        )
        context_arg = self.mock_summarize.call_args.kwargs["context"]
        self.assertIn("fines:\n100M EUR", context_arg)
        self.assertIn("companies_investigated:\nAcme Corp", context_arg)
        # Context follows the context_facts order.
        self.assertLess(
            context_arg.index("companies_investigated"), context_arg.index("fines")
        )

        items = prediction.output.items
        self.assertEqual(len(items), 1)
        summary_md = items[0]["case_summary"]
        self.assertIsInstance(summary_md, str)
        # Markdown headings are generated from CaseSummaryChapters field names
        self.assertIn("### Overview", summary_md)
        self.assertIn("### Leniency", summary_md)
        self.assertIn("### Book Reference", summary_md)
        self.assertNotIn("### Legal Assessment Period", summary_md)
        self.assertIn("*(reported:", summary_md)

    @patch("pipelines.agent_dspy.universal_modules._check_context_token_limit")
    def test_empty_input_facts_are_left_out_of_the_context(self, _mock_token_check):
        self.sql_store_mock.select_fact4celex.return_value = []
        self.sql_store_mock.select_named_facts4celex.return_value = [
            _fact("companies_investigated", "Acme Corp", T0),
            _fact("fines", "  ", T0),
        ]

        self.module(_summary_params())

        context_arg = self.mock_summarize.call_args.kwargs["context"]
        self.assertNotIn("fines", context_arg)

    def test_forward_no_facts(self):
        self.sql_store_mock.select_fact4celex.return_value = []
        self.sql_store_mock.select_named_facts4celex.return_value = []

        prediction = self.module(_summary_params())

        self.assertEqual(prediction.output.items, [])
        self.assertEqual(prediction.output.source, "none")
        self.mock_summarize.assert_not_called()

    def test_missing_context_facts_summarizes_nothing(self):
        prediction = self.module(_summary_params(context_facts=[]))

        self.assertEqual(prediction.output.items, [])
        self.sql_store_mock.select_named_facts4celex.assert_not_called()
        self.mock_summarize.assert_not_called()

    def test_current_stored_summary_is_served_from_the_store(self):
        stored = _fact("case_summary", "old", T1)
        stored.value_json = [{"case_summary": "old"}]
        self.sql_store_mock.select_fact4celex.return_value = [stored]
        self.sql_store_mock.select_named_facts4celex.return_value = [
            _fact("fines", "100M EUR", T0),
        ]

        prediction = self.module(_summary_params())

        self.assertEqual(prediction.output.items, [{"case_summary": "old"}])
        self.mock_summarize.assert_not_called()

    @patch("pipelines.agent_dspy.universal_modules._check_context_token_limit")
    def test_stored_summary_older_than_an_input_fact_is_rebuilt(self, _mock_token_check):
        stored = _fact("case_summary", "old", T0)
        stored.value_json = [{"case_summary": "old"}]
        self.sql_store_mock.select_fact4celex.return_value = [stored]
        self.sql_store_mock.select_named_facts4celex.return_value = [
            _fact("fines", "100M EUR", T1),
        ]

        prediction = self.module(_summary_params())

        self.mock_summarize.assert_called_once()
        self.assertEqual(prediction.output.source, "summary")


class TestCaseSummaryChapters(unittest.TestCase):
    """The chapter model tolerates an LLM that drops a chapter."""

    def test_omitted_chapter_is_filled_with_na(self):
        payload = {field: field for field in CaseSummaryChapters.model_fields}
        del payload["remedies"]

        with self.assertLogs("pipelines.agent_dspy.dspy_signature", "WARNING"):
            chapters = CaseSummaryChapters.model_validate(payload)

        self.assertEqual(chapters.remedies, "NA")
        self.assertEqual(chapters.fines, "fines")

    def test_chapters_stay_required_in_the_schema(self):
        """The LLM-facing JSON schema still demands every chapter."""
        required = CaseSummaryChapters.model_json_schema()["required"]
        self.assertEqual(set(required), set(CaseSummaryChapters.model_fields))


class TestCaseSummaryCatalogEntry(unittest.TestCase):
    """The YAML entry reaches the module through FactExtractorParamVO."""

    def test_context_facts_flow_from_yaml_into_params(self):
        struct = _case_summary_yaml_struct()
        params = FactExtractorParamVO.from_yaml_struct("12345", struct)

        self.assertEqual(params.context_facts, struct["context_facts"])
        self.assertEqual(params.predecessors, [])

    def test_every_chapter_source_fact_is_a_context_fact(self):
        """A fact quoted in a chapter description must be fed to the LLM."""
        context_facts = set(_case_summary_yaml_struct()["context_facts"])
        quoted = set()
        for field in CaseSummaryChapters.model_fields.values():
            quoted.update(re.findall(r"'([A-Za-z_]+)'", field.description or ""))
        self.assertEqual(quoted - context_facts, set())


class TestFactSummaryModuleIntegration(unittest.TestCase):
    def setUp(self):
        from config.llm_thinking import thinking_kwargs
        from config.settings import settings

        model_name = f"openai/{settings.LLM_MODEL_ID}"
        lm = dspy.LM(
            model_name,
            api_base=f"{settings.OLLAMA_HOST}/v1",
            api_key="ollama",
            cache=True,
            **thinking_kwargs(model_name),
        )
        dspy.configure(lm=lm)
        self.toolkit = CelexDatabaseToolkit()
        self.module = FactSummaryModule(sql_store=self.toolkit.fact_repo)

    def x_test_integration_39525_1916_7(self):
        """
        Duration: ~2523 seconds (~42 minutes) — the local Ollama LLM inference was very slow,
        likely due to the large fact context for celex 39525_1916_7
        """
        celex_id = "39525_1916_7"
        params = FactExtractorParamVO(
            target_fact_name="case_summary",
            question="Summarize the case into chapters",
            celex_id=celex_id,
            output_schema="",
            pipeline="summary",
            context_facts=_case_summary_yaml_struct()["context_facts"],
            force_override=True,  # Bypass cache to test full extraction
        )
        prediction = self.module(params)

        items = prediction.output.items
        print(
            f"\nIntegration test Summary Result for {celex_id}:\n{items[0]['case_summary'][:200]}..."
        )

        self.assertGreater(len(items), 0)
        self.assertIsInstance(items[0]["case_summary"], str)
        self.assertIn("###", items[0]["case_summary"])
        self.assertEqual(prediction.output.source, "summary")

    def test_integration_small_context_CELEX_31969D0477(self):
        """CELEX-31969D0477 has a sufficiently small fact context that fits
        within the prompt-token budget. The module should produce a valid
        markdown summary without raising."""
        celex_id = "CELEX-31969D0477"
        params = FactExtractorParamVO(
            target_fact_name="case_summary",
            question="Summarize the case into chapters",
            celex_id=celex_id,
            output_schema="",
            pipeline="summary",
            context_facts=_case_summary_yaml_struct()["context_facts"],
            force_override=True,
        )
        prediction = self.module(params)

        items = prediction.output.items
        print(
            f"\nIntegration test Summary Result for {celex_id}:\n{items[0]['case_summary'][:200]}..."
        )

        self.assertGreater(len(items), 0)
        self.assertIn("case_summary", items[0])
        self.assertIsInstance(items[0]["case_summary"], str)
        # Verify markdown heading structure
        self.assertIn("###", items[0]["case_summary"])
        self.assertEqual(prediction.output.source, "summary")

    @unittest.skip("test succeed but taked 40min" )
    def test_integration_exceeds_token_limit_36756_68_16(self):
        """36756_68_16 has an excessively large aggregated fact context that
        exceeds ``OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT``. The module must
        raise a ``ValueError`` from ``_check_context_token_limit``."""
        celex_id = "36756_68_16"
        params = FactExtractorParamVO(
            target_fact_name="case_summary",
            question="Summarize the case into chapters",
            celex_id=celex_id,
            output_schema="",
            pipeline="summary",
            context_facts=_case_summary_yaml_struct()["context_facts"],
            force_override=True,
        )
        with self.assertRaises(ValueError) as ctx:
            self.module(params)

        self.assertIn("Context too long", str(ctx.exception))
        print(f"\nExpected ValueError for {celex_id}: {ctx.exception}")


if __name__ == "__main__":
    unittest.main()
