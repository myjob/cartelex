"""Unit tests for FactExtractorParamVO's section_scope key (YAML boundary)."""

import unittest

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO


def _struct(**overrides) -> dict:
    struct = {
        "target_fact_name": "fines",
        "question": "List all fines",
        "output_schema": "company, amount",
        "pipeline": "universal",
    }
    struct.update(overrides)
    return struct


class TestSectionScope(unittest.TestCase):
    def test_absent_key_defaults_to_none(self) -> None:
        vo = FactExtractorParamVO.from_yaml_struct("CELEX-1", _struct())
        self.assertIsNone(vo.section_scope)

    def test_valid_values_are_parsed(self) -> None:
        vo = FactExtractorParamVO.from_yaml_struct(
            "CELEX-1", _struct(section_scope=["fines", "operative"])
        )
        self.assertEqual(vo.section_scope, ["fines", "operative"])

    def test_unknown_value_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            FactExtractorParamVO.from_yaml_struct(
                "CELEX-1", _struct(section_scope=["fines", "not_a_section"])
            )

    def test_round_trip_via_yaml_struct(self) -> None:
        vo = FactExtractorParamVO.from_yaml_struct(
            "CELEX-1", _struct(section_scope=["fines", "operative"])
        )
        exported = vo.to_yaml_struct()
        self.assertEqual(exported["section_scope"], ["fines", "operative"])

    def test_unset_scope_not_exported(self) -> None:
        vo = FactExtractorParamVO.from_yaml_struct("CELEX-1", _struct())
        self.assertNotIn("section_scope", vo.to_yaml_struct())


if __name__ == "__main__":
    unittest.main()
