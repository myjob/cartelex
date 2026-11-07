"""Unit tests for tools.fact_completeness_checker (no DB, no YAML from disk)."""

import tempfile
import unittest
from pathlib import Path

from tools.fact_completeness_checker import (
    AuditRow,
    FactCatalogue,
    FactRow,
    build_completeness_frame,
    load_catalogue,
    read_celex_list,
    summarize_per_decision,
    unconditional_gaps,
)

_YAML = """
- target_fact_name: products
  run_state_active: true
- target_fact_name: NACE_group_code
- target_fact_name: leniency_granted
  condition: |
    leniency_case == "yes"
- target_fact_name: services
  run_state_active: false
"""


class TestReadAndLoad(unittest.TestCase):
    """File parsing helpers."""

    def test_read_celex_list_skips_comments_blanks_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ids.txt"
            path.write_text("# header\nDEC-A\n\nDEC-B\nDEC-A\n", encoding="utf-8")
            self.assertEqual(read_celex_list(path), ["DEC-A", "DEC-B"])

    def test_load_catalogue_keeps_active_and_marks_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.yaml"
            path.write_text(_YAML, encoding="utf-8")
            catalogue = load_catalogue(path)
        self.assertEqual(catalogue.active, ["products", "NACE_group_code", "leniency_granted"])
        self.assertEqual(catalogue.conditional, frozenset({"leniency_granted"}))


class TestFrame(unittest.TestCase):
    """Completeness frame and its summaries."""

    def setUp(self) -> None:
        catalogue = FactCatalogue(
            active=["products", "NACE_group_code", "leniency_granted"],
            conditional=frozenset({"leniency_granted"}),
        )
        facts = [
            FactRow(celex_id="DEC-A", name="products", value_md="raw tobacco"),
            FactRow(celex_id="DEC-A", name="NACE_group_code", value_md="12.0"),
            FactRow(celex_id="DEC-B", name="products", value_md=" NA "),
        ]
        audit = [AuditRow(celex_id="DEC-A", target_fact_name="products", success=True)]
        self.frame = build_completeness_frame(["DEC-A", "DEC-B"], catalogue, facts, audit)

    def test_one_row_per_decision_and_fact(self) -> None:
        self.assertEqual(self.frame.height, 6)

    def test_empty_value_flags_na_products(self) -> None:
        row = self.frame.filter((self.frame["celex_id"] == "DEC-B") & (self.frame["fact"] == "products"))
        self.assertTrue(row["present"][0])
        self.assertTrue(row["empty_value"][0])

    def test_summary_separates_conditional_from_unconditional_gaps(self) -> None:
        summary = {r["celex_id"]: r for r in summarize_per_decision(self.frame).to_dicts()}
        self.assertEqual(summary["DEC-B"]["missing_uncond"], 1)
        self.assertEqual(summary["DEC-B"]["missing_cond"], 1)
        self.assertEqual(summary["DEC-B"]["empty_values"], 1)
        self.assertEqual(summary["DEC-A"]["missing_uncond"], 0)

    def test_unconditional_gaps_lists_only_ungated_missing_facts(self) -> None:
        gaps = unconditional_gaps(self.frame).select("celex_id", "fact").rows()
        self.assertEqual(gaps, [("DEC-B", "NACE_group_code")])

    def test_last_audit_is_null_when_never_attempted(self) -> None:
        row = self.frame.filter((self.frame["celex_id"] == "DEC-B") & (self.frame["fact"] == "NACE_group_code"))
        self.assertIsNone(row["last_audit_ok"][0])


if __name__ == "__main__":
    unittest.main()
