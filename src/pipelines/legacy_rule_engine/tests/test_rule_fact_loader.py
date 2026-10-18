"""Unit tests for the legacy rule-fact loader.

The database is mocked at the engine boundary: ``load_data`` builds its engine
through ``make_engine``, which owns the connection string, so no live
PostgreSQL is required here.
"""

import unittest
from unittest.mock import MagicMock, patch

from pipelines.legacy_rule_engine.rule_fact_loader import load_data, parse_rule_facts

SAMPLE_JSON = {
    "knowledge_base": {
        "CELEX": "CELEX-31993D0082",
        "facts": {
            "application_for_exemption": "False",
            "human_index_year": "1992",
            "conduct_vertical_restraints": {
                "tag": "conduct_vertical_restraints",
                "prompt_num": 0,
                "prompt_question": "is the infringement vertical restraints?",
                "values": "Yes, it is.",
                "meta": {"rule": "rag_conduct_vertical_restraints"},
            },
            "test_value_key": {
                "tag": "test_tag",
                "prompt_question": "question?",
                "value": "answer",
            },
        },
    }
}


class TestParseRuleFacts(unittest.TestCase):
    """Cover both fact shapes accepted by the knowledge-base JSON."""

    def test_parses_simple_and_nested_facts(self):
        records = parse_rule_facts(SAMPLE_JSON)
        self.assertEqual(len(records), 4)

        self.assertTrue(
            any(
                r["fact"] == "application_for_exemption"
                and r["value"] == "False"
                and r["question"] == "NA"
                and r["celex_id"] == "CELEX-31993D0082"
                for r in records
            )
        )
        self.assertTrue(
            any(
                r["fact"] == "human_index_year"
                and r["value"] == "1992"
                and r["question"] == "NA"
                for r in records
            )
        )

        nested_values = next(
            r for r in records if r["fact"] == "conduct_vertical_restraints"
        )
        self.assertEqual(nested_values["celex_id"], "CELEX-31993D0082")
        self.assertEqual(
            nested_values["question"], "is the infringement vertical restraints?"
        )
        self.assertEqual(nested_values["value"], "Yes, it is.")

        nested_value = next(r for r in records if r["fact"] == "test_tag")
        self.assertEqual(nested_value["question"], "question?")
        self.assertEqual(nested_value["value"], "answer")

    def test_missing_celex_id_yields_no_records(self):
        self.assertEqual(parse_rule_facts({"knowledge_base": {"facts": {}}}), [])


class TestLoadData(unittest.TestCase):
    """Exercise the load path with the SQLAlchemy engine mocked out."""

    def _mock_engine(self, existing_fact_count: int) -> MagicMock:
        """Build an engine mock whose existence probe returns a given count.

        Args:
            existing_fact_count: Value returned by the ``SELECT COUNT(*)``
                guard that decides whether the load is skipped.

        Returns:
            A ``MagicMock`` standing in for a SQLAlchemy engine.
        """
        engine = MagicMock()
        insert_connection = MagicMock()
        engine.begin.return_value.__enter__.return_value = insert_connection
        probe_connection = MagicMock()
        engine.connect.return_value.__enter__.return_value = probe_connection
        probe_connection.execute.return_value.scalar.return_value = (
            existing_fact_count
        )
        return engine

    @patch("pipelines.legacy_rule_engine.rule_fact_loader.make_engine")
    def test_inserts_records_when_celex_absent(self, mock_make_engine):
        engine = self._mock_engine(existing_fact_count=0)
        mock_make_engine.return_value = engine
        records = parse_rule_facts(SAMPLE_JSON)

        load_data(records)

        # The connection string is never passed in: make_engine owns it.
        mock_make_engine.assert_called_once_with()
        engine.begin.assert_called_once()
        insert_connection = engine.begin.return_value.__enter__.return_value
        insert_connection.execute.assert_called_once()
        self.assertEqual(insert_connection.execute.call_args.args[1], records)

    @patch("pipelines.legacy_rule_engine.rule_fact_loader.make_engine")
    def test_skips_insert_when_facts_already_exist(self, mock_make_engine):
        engine = self._mock_engine(existing_fact_count=3)
        mock_make_engine.return_value = engine

        load_data(parse_rule_facts(SAMPLE_JSON))

        engine.begin.assert_not_called()

    @patch("pipelines.legacy_rule_engine.rule_fact_loader.make_engine")
    def test_no_records_touches_no_engine(self, mock_make_engine):
        load_data([])

        mock_make_engine.assert_not_called()


if __name__ == "__main__":
    unittest.main()
