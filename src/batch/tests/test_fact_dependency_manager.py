"""Tests for the fact catalogue checks and dependency graph.

The checks guard the main pipeline's prerequisites against broken manual edits
of ``FACT_EXTRACTOR.yaml``; the graph tells the planner which facts follow a
changed one. No DB, no LLM.
"""

import tempfile
import unittest
from pathlib import Path

from batch.fact_dependency_manager import (
    CatalogueError,
    check_catalogue,
    dependents,
    execution_order,
    load_structs,
)
from config.settings import settings


def _fact(name: str, predecessors: list[str] | None = None, **extra: object) -> dict:
    """Return a minimal valid catalogue entry."""
    return {
        "target_fact_name": name,
        "question": f"what is {name}",
        "output_schema": "",
        "pipeline": extra.pop("pipeline", "universal"),
        "predecessors": predecessors or [],
        **extra,
    }


def _write(text: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as handle:
        handle.write(text)
    return Path(handle.name)


class TestLoadStructs(unittest.TestCase):
    """YAML problems surface as CatalogueError with a location."""

    def test_yaml_syntax_error_names_line(self) -> None:
        path = _write("- target_fact_name: a\n  question: [unclosed\n")
        with self.assertRaises(CatalogueError) as ctx:
            load_structs(path)
        self.assertIn("line", str(ctx.exception))
        self.assertIn(str(path), str(ctx.exception))

    def test_top_level_mapping_rejected(self) -> None:
        with self.assertRaises(CatalogueError):
            load_structs(_write("target_fact_name: a\n"))

    def test_list_loads(self) -> None:
        self.assertEqual(load_structs(_write("- target_fact_name: a\n"))[0]["target_fact_name"], "a")


class TestCheckCatalogue(unittest.TestCase):
    """Each broken declaration yields one message naming the fact."""

    def test_valid_catalogue_has_no_errors(self) -> None:
        self.assertEqual(check_catalogue([_fact("a"), _fact("b", ["a"])]), [])

    def test_unknown_predecessor_reported(self) -> None:
        (msg,) = check_catalogue([_fact("b", ["zz"])])
        self.assertIn("b", msg)
        self.assertIn("zz", msg)

    def test_unknown_na_fallback_fact_reported(self) -> None:
        (msg,) = check_catalogue([_fact("p", na_fallback_fact="case_title")])
        self.assertIn("case_title", msg)

    def test_unknown_context_fact_reported(self) -> None:
        (msg,) = check_catalogue([_fact("s", pipeline="case_summary", context_facts=["gone"])])
        self.assertIn("gone", msg)

    def test_unknown_verify_fact_reported(self) -> None:
        (msg,) = check_catalogue([_fact("a", verify_fact_name="golden")])
        self.assertIn("golden", msg)

    def test_duplicate_name_reported(self) -> None:
        (msg,) = check_catalogue([_fact("a"), _fact("a")])
        self.assertIn("duplicate", msg)

    def test_cycle_reported(self) -> None:
        errors = check_catalogue([_fact("a", ["b"]), _fact("b", ["a"])])
        self.assertTrue(any("cycle" in e for e in errors), errors)

    def test_unknown_pipeline_reported(self) -> None:
        (msg,) = check_catalogue([_fact("a", pipeline="univrsal")])
        self.assertIn("univrsal", msg)

    def test_entry_without_name_reported(self) -> None:
        errors = check_catalogue([{"question": "q"}])
        self.assertTrue(any("target_fact_name" in e for e in errors), errors)

    def test_real_catalogue_is_valid(self) -> None:
        self.assertEqual(check_catalogue(load_structs(settings.FACT_EXTRACTOR_CONFIG_DIR)), [])


class TestGraph(unittest.TestCase):
    """Successors follow predecessors and context_facts."""

    def setUp(self) -> None:
        self.structs = [
            _fact("products"),
            _fact("NACE_group_code", ["products"]),
            _fact("NACE_class_code", ["products", "NACE_group_code"]),
            _fact("case_title"),
            _fact("case_summary", pipeline="case_summary", context_facts=["products", "case_title"]),
        ]

    def test_dependents_follow_context_facts(self) -> None:
        self.assertEqual(
            dependents(self.structs, {"products"}),
            {"NACE_group_code", "NACE_class_code", "case_summary"},
        )

    def test_dependents_exclude_roots(self) -> None:
        self.assertNotIn("NACE_group_code", dependents(self.structs, {"NACE_group_code"}))

    def test_execution_order_respects_predecessors(self) -> None:
        order = execution_order(self.structs)
        self.assertLess(order.index("products"), order.index("NACE_group_code"))
        self.assertLess(order.index("NACE_group_code"), order.index("NACE_class_code"))
        self.assertLess(order.index("case_title"), order.index("case_summary"))

    def test_execution_order_skips_inactive(self) -> None:
        self.structs.append(_fact("old", run_state_active=False))
        self.assertNotIn("old", execution_order(self.structs))


if __name__ == "__main__":
    unittest.main()
