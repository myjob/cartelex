"""Tests for the corporate identifier lookup DSPy module."""

import unittest
from unittest.mock import MagicMock, patch

import dspy
from pydantic import ValidationError

from pipelines.corp_index.corp_index_module import (
    CorpIndexModule,
    create_corp_index_lm,
)
from pipelines.corp_index.models import CorpIndexRecord, CorpIndexResult


class TestCorpIndexModule(unittest.TestCase):
    """Unit tests for deterministic corporate identifier lookup behavior."""

    def setUp(self) -> None:
        """Create a module with mocked DSPy dependencies."""
        self.mock_lm = MagicMock()
        self.mock_predictor = MagicMock()
        self.module = CorpIndexModule(lm=self.mock_lm, predictor=self.mock_predictor)

    def test_forward_returns_structured_lookup_record(self) -> None:
        """Forward returns a validated corporate identifier table row."""
        self.mock_predictor.return_value = dspy.Prediction(
            output=CorpIndexResult(
                records=[
                    CorpIndexRecord(
                        corp_canoncical="SCOTTISH & NEWCASTLE LIMITED",
                        BRIS="NA",
                        EUID="NA",
                        LEI="5493008JEWD6T3I5ZJ82",
                        state="lapsed",
                    )
                ]
            )
        )

        prediction = self.module.forward(
            corp_canoncical="SCOTTISH & NEWCASTLE LIMITED",
        )

        record = prediction.output.records[0]
        self.assertEqual(record.corp_canoncical, "SCOTTISH & NEWCASTLE LIMITED")
        self.assertEqual(record.BRIS, "NA")
        self.assertEqual(record.EUID, "NA")
        self.assertEqual(record.LEI, "5493008JEWD6T3I5ZJ82")
        self.assertEqual(record.state, "lapsed")
        self.mock_predictor.assert_called_once_with(
            corp_canoncical="SCOTTISH & NEWCASTLE LIMITED",
        )

    def test_identifier_fields_normalize_missing_values_to_na(self) -> None:
        """Missing identifier values normalize to NA at the output boundary."""
        result = CorpIndexResult(
            records=[
                CorpIndexRecord(
                    corp_canoncical="Example Limited",
                    BRIS="",
                    EUID=None,
                    LEI="not found",
                    state="archived",
                )
            ]
        )

        record = result.records[0]
        self.assertEqual(record.BRIS, "NA")
        self.assertEqual(record.EUID, "NA")
        self.assertEqual(record.LEI, "NA")
        self.assertEqual(record.state, "archived")

    def test_forward_rejects_empty_input(self) -> None:
        """Empty company values fail before calling the LLM."""
        with self.assertRaises(ValidationError):
            self.module.forward(corp_canoncical=" ")

        self.mock_predictor.assert_not_called()

    def test_forward_validates_raw_prediction_dictionary(self) -> None:
        """Raw DSPy output dictionaries are validated into Pydantic models."""
        self.mock_predictor.return_value = dspy.Prediction(
            output={
                "records": [
                    {
                        "corp_canoncical": "SCOTTISH & NEWCASTLE LIMITED",
                        "BRIS": "NA",
                        "EUID": "NA",
                        "LEI": "5493008JEWD6T3I5ZJ82",
                        "state": "lapsed",
                    }
                ]
            }
        )

        prediction = self.module.forward(
            corp_canoncical="SCOTTISH & NEWCASTLE LIMITED",
        )

        self.assertIsInstance(prediction.output, CorpIndexResult)
        self.assertEqual(
            prediction.output.records[0].LEI,
            "5493008JEWD6T3I5ZJ82",
        )

    def test_create_corp_index_lm_uses_the_corp_index_profile(self) -> None:
        """This package provisions from its own profile, not the UI planner's.

        The two were one setting until the profiles were split, so pointing the
        batch at a cheaper model silently repointed the UI server as well.
        """
        with patch("config.llm_factory.settings") as mock_settings:
            mock_settings.CORP_INDEX_LLM_PROVIDER = "deepseek"
            mock_settings.CORP_INDEX_LLM_MODEL_ID = "deepseek-chat"
            mock_settings.CORP_INDEX_LLM_THINKING_MODE = "enabled"
            mock_settings.CORP_INDEX_LLM_CACHE = False
            mock_settings.LLM_MODEL_CLOUD_API_KEY = "key"
            with patch("config.llm_factory.dspy.LM") as mock_lm:
                create_corp_index_lm()

        self.assertEqual(mock_lm.call_args.args[0], "deepseek/deepseek-chat")
        self.assertEqual(mock_lm.call_args.kwargs["api_key"], "key")

    def test_create_corp_index_lm_follows_its_own_provider(self) -> None:
        """Pointing only the corp index at Ollama sends it to the local server."""
        with patch("config.llm_factory.settings") as mock_settings:
            mock_settings.CORP_INDEX_LLM_PROVIDER = "ollama"
            mock_settings.CORP_INDEX_LLM_MODEL_ID = "qwen3-coder:30b"
            mock_settings.CORP_INDEX_LLM_THINKING_MODE = "enabled"
            mock_settings.CORP_INDEX_LLM_CACHE = False
            mock_settings.OLLAMA_HOST = "http://localhost:11434"
            with patch("config.llm_factory.dspy.LM") as mock_lm:
                create_corp_index_lm()

        self.assertEqual(mock_lm.call_args.args[0], "openai/qwen3-coder:30b")
        self.assertEqual(
            mock_lm.call_args.kwargs["api_base"], "http://localhost:11434/v1"
        )


if __name__ == "__main__":
    unittest.main()



"""
====================================
ERROR: test_forward_returns_structured_lookup_record (__main__.TestCorpIndexModule.test_forward_returns_structured_lookup_record)
Forward returns a validated corporate identifier table row.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "~/a/celex-facts/src/pipelines/corp_index/tests/test_corp_index_module.py", line 30, in test_forward_returns_structured_lookup_record
    CorpIndexRecord(
  File "~/a/celex-facts/.venv/lib/python3.12/site-packages/pydantic/main.py", line 250, in __init__
    validated_self = self.__pydantic_validator__.validate_python(data, self_instance=self)
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
pydantic_core._pydantic_core.ValidationError: 4 validation errors for CorpIndexRecord
native_company_number
  Field required [type=missing, input_value={'corp_canoncical': 'SCOT...J82', 'state': 'lapsed'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
SIREN
  Field required [type=missing, input_value={'corp_canoncical': 'SCOT...J82', 'state': 'lapsed'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
SIRET
  Field required [type=missing, input_value={'corp_canoncical': 'SCOT...J82', 'state': 'lapsed'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
description
  Field required [type=missing, input_value={'corp_canoncical': 'SCOT...J82', 'state': 'lapsed'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing

======================================================================
ERROR: test_forward_validates_raw_prediction_dictionary (__main__.TestCorpIndexModule.test_forward_validates_raw_prediction_dictionary)
Raw DSPy output dictionaries are validated into Pydantic models.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "~/a/celex-facts/src/pipelines/corp_index/tests/test_corp_index_module.py", line 98, in test_forward_validates_raw_prediction_dictionary
    prediction = self.module.forward(
                 ^^^^^^^^^^^^^^^^^^^^
  File "~/a/celex-facts/src/pipelines/corp_index/corp_index_module.py", line 96, in forward
    validated_output = CorpIndexResult.model_validate(output)
                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "~/a/celex-facts/.venv/lib/python3.12/site-packages/pydantic/main.py", line 716, in model_validate
    return cls.__pydantic_validator__.validate_python(
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
pydantic_core._pydantic_core.ValidationError: 4 validation errors for CorpIndexResult
records.0.native_company_number
  Field required [type=missing, input_value={'corp_canoncical': 'SCOT...J82', 'state': 'lapsed'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
records.0.SIREN
  Field required [type=missing, input_value={'corp_canoncical': 'SCOT...J82', 'state': 'lapsed'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
records.0.SIRET
  Field required [type=missing, input_value={'corp_canoncical': 'SCOT...J82', 'state': 'lapsed'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
records.0.description
  Field required [type=missing, input_value={'corp_canoncical': 'SCOT...J82', 'state': 'lapsed'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing

======================================================================
ERROR: test_identifier_fields_normalize_missing_values_to_na (__main__.TestCorpIndexModule.test_identifier_fields_normalize_missing_values_to_na)
Missing identifier values normalize to NA at the output boundary.
----------------------------------------------------------------------
Traceback (most recent call last):
  File "~/a/celex-facts/src/pipelines/corp_index/tests/test_corp_index_module.py", line 59, in test_identifier_fields_normalize_missing_values_to_na
    CorpIndexRecord(
  File "~/a/celex-facts/.venv/lib/python3.12/site-packages/pydantic/main.py", line 250, in __init__
    validated_self = self.__pydantic_validator__.validate_python(data, self_instance=self)
                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
pydantic_core._pydantic_core.ValidationError: 4 validation errors for CorpIndexRecord
native_company_number
  Field required [type=missing, input_value={'corp_canoncical': 'Exam...d', 'state': 'archived'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
SIREN
  Field required [type=missing, input_value={'corp_canoncical': 'Exam...d', 'state': 'archived'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
SIRET
  Field required [type=missing, input_value={'corp_canoncical': 'Exam...d', 'state': 'archived'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing
description
  Field required [type=missing, input_value={'corp_canoncical': 'Exam...d', 'state': 'archived'}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.12/v/missing

----------------------------------------------------------------------
Ran 6 tests in 0.128s

FAILED (errors=3)


"""