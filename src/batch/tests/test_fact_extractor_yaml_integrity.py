"""
Structural integrity checks for ``config_main/FACT_EXTRACTOR.yaml``.

Fact names are hand-synchronized across the YAML catalog, the DSPy planner /
router lookup tables, the entity-graph family map and the SQL views.  Nothing in
the runtime enforces that a ``predecessors:`` entry actually names a defined
fact: ``FactIngestorRunner._topological_order`` only logs a warning and the
affected CELEX is skipped at execution time, so a typo or a retired fact stays
invisible until a batch run silently produces nothing.

These tests fail fast on that drift.  They read the live config file (no DB, no
LLM) and are therefore safe to run in isolation.

Run with:
    .venv/bin/python -m unittest batch.tests.test_fact_extractor_yaml_integrity -v
"""
import unittest

import yaml

from config.settings import settings

# Predecessors that are known to be dangling and are tracked in ISSUES.md rather
# than fixed here.  The ``products`` family (predecessor of the NACE / OECD
# sector facts) should point at ``products_name``; repointing it is a separate
# change because it alters the execution order of the sector facts.
KNOWN_DANGLING_PREDECESSORS: frozenset[str] = frozenset({"products"})


def _load_catalog() -> list[dict]:
    """Load the fact catalog from the configured YAML path."""
    with open(settings.FACT_EXTRACTOR_CONFIG_DIR, encoding="utf-8") as handle:
        catalog = yaml.safe_load(handle)
    return catalog if isinstance(catalog, list) else [catalog]


def _predecessors(struct: dict) -> list[str]:
    """Normalize a fact's ``predecessors`` to a list of names."""
    raw = struct.get("predecessors") or []
    return [raw] if isinstance(raw, str) else list(raw)


class TestFactExtractorYamlIntegrity(unittest.TestCase):
    """The catalog is internally consistent and free of retired fact names."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = _load_catalog()
        cls.names = [struct["target_fact_name"] for struct in cls.catalog]

    def test_target_fact_names_are_unique(self):
        duplicates = {name for name in self.names if self.names.count(name) > 1}
        self.assertEqual(duplicates, set(), f"duplicate target_fact_name: {duplicates}")

    def test_every_predecessor_resolves_to_a_defined_fact(self):
        defined = set(self.names)
        dangling = {
            struct["target_fact_name"]: sorted(
                name
                for name in _predecessors(struct)
                if name not in defined and name not in KNOWN_DANGLING_PREDECESSORS
            )
            for struct in self.catalog
        }
        dangling = {fact: missing for fact, missing in dangling.items() if missing}
        self.assertEqual(
            dangling,
            {},
            "predecessors naming facts that do not exist in the catalog "
            f"(add to KNOWN_DANGLING_PREDECESSORS only with an ISSUES.md entry): {dangling}",
        )

    def test_retired_company_fact_names_are_absent(self):
        """The pre-July-2026 company facts must not reappear anywhere."""
        retired = {"companies", "companies_all", "companies_canoncial", "company_country"}
        for struct in self.catalog:
            fact = struct["target_fact_name"]
            self.assertNotIn(fact, retired, f"{fact} is a retired fact name")
            reused = retired.intersection(_predecessors(struct))
            self.assertEqual(reused, set(), f"{fact} depends on retired fact(s) {reused}")

    def test_every_verify_fact_name_resolves_to_a_defined_fact(self):
        """A golden list is looked up by fact name; a dangling one verifies nothing.

        Unlike ``output_schema``, which names a JSON row key, ``verify_fact_name``
        is resolved against ``celex_facts.facts.name``. A name with no catalog
        entry returns an empty golden list and silently skips verification.
        """
        defined = set(self.names)
        dangling = {
            struct["target_fact_name"]: struct["verify_fact_name"]
            for struct in self.catalog
            if struct.get("verify_fact_name")
            and struct["verify_fact_name"] not in defined
        }
        self.assertEqual(
            dangling,
            {},
            f"verify_fact_name naming facts absent from the catalog: {dangling}",
        )

    def test_every_context_fact_resolves_to_a_defined_fact(self):
        """``context_facts`` is an allow-list; a dangling entry summarizes nothing.

        The summary used to read every stored fact except a deny-list, so facts
        retired from this catalog kept leaking in from their old DB rows. The
        allow-list only stays honest if every entry is a live catalog fact.
        """
        defined = set(self.names)
        dangling = {
            struct["target_fact_name"]: sorted(
                set(struct.get("context_facts") or []) - defined
            )
            for struct in self.catalog
        }
        dangling = {fact: missing for fact, missing in dangling.items() if missing}
        self.assertEqual(
            dangling, {}, f"context_facts naming facts absent from the catalog: {dangling}"
        )

    def test_case_summary_reads_context_facts_not_predecessors(self):
        """Predecessors gate execution; the summary's inputs must not."""
        by_name = {s["target_fact_name"]: s for s in self.catalog}
        summary = by_name["case_summary"]
        self.assertTrue(summary.get("context_facts"), "case_summary needs context_facts")
        self.assertEqual(_predecessors(summary), [])
        self.assertNotIn("case_summary", summary["context_facts"])

    def test_full_catalog_runs_case_summary_after_its_context_facts(self):
        """On a decision's first run the summary must see every input fact.

        ``case_summary`` heads the YAML but declares no predecessors; ordering
        it by predecessors alone ran it first and stored ``NA``.
        """
        from batch.Main_ingest_generic_facts import FactIngestionOrchestrator

        order = FactIngestionOrchestrator(description="test")._topological_order(
            self.catalog
        )
        self.assertIsNotNone(order, "catalog ordering has a cycle")
        by_name = {s["target_fact_name"]: s for s in self.catalog}
        late = [
            fact
            for fact in by_name["case_summary"]["context_facts"]
            if order.index(fact) > order.index("case_summary")
        ]
        self.assertEqual(late, [], f"case_summary runs before {late}")

    def test_master_company_facts_are_defined(self):
        """The July 2026 master company set is the authoritative catalog."""
        expected = {
            "companies_investigated",
            "companies_country",
            "companies_address",
            "companies_industry",
            "company_recidivism",
        }
        self.assertTrue(expected.issubset(set(self.names)), expected - set(self.names))

    def test_company_dependents_hang_off_the_roster(self):
        """companies_investigated is the roster every company fact derives from."""
        by_name = {s["target_fact_name"]: s for s in self.catalog}
        for fact in ("companies_country", "companies_address", "company_recidivism"):
            self.assertIn(
                "companies_investigated",
                _predecessors(by_name[fact]),
                f"{fact} must declare companies_investigated as a predecessor",
            )



class TestCartelPeriodInfringementGate(unittest.TestCase):
    """``cartel_period`` is skipped only when no infringement is found.

    Exemption, negative-clearance and Article 9 commitments decisions carry an
    exemption or commitment term, not a cartel period (ISSUES #high 20261105).
    The gate reads ``case_commission_infringement`` and suppresses the period
    only on an explicit False, so an NA or noisy answer keeps it.
    """

    GATE = "case_commission_infringement"

    @classmethod
    def setUpClass(cls) -> None:
        cls.by_name = {s["target_fact_name"]: s for s in _load_catalog()}
        cls.period = cls.by_name["cartel_period"]

    def test_period_is_named_cartel_period_and_built_by_heideltime(self):
        """The fact is ``cartel_period``; ``heideltime_cartel_period`` is only its pipeline.

        The legacy LLM ``cartel_period`` was retired and the HeidelTime fact took
        its name (plan cartel-period-rename-20261106), so no catalog entry may
        reintroduce the prefixed fact name.
        """
        self.assertEqual(self.period["pipeline"], "heideltime_cartel_period")
        prefixed = [n for n in self.by_name if n.startswith("heideltime_cartel")]
        self.assertEqual(prefixed, [])

    def _met(self, context: dict) -> bool:
        """Evaluate the period's YAML condition against a fact context."""
        from pipelines.rule_engine.condition_checker import ConditionValidator

        return ConditionValidator().evaluate(self.period.get("condition"), context)

    def test_gate_fact_follows_the_case_commission_style(self):
        """True/False/NA universal fact, retrieved from the operative part."""
        gate = self.by_name[self.GATE]
        self.assertEqual(gate["pipeline"], "universal")
        self.assertEqual(gate.get("section_scope"), ["operative"])
        self.assertIn("'True' or 'False' or 'NA'", gate["question"])

    def test_only_an_explicit_false_suppresses_the_period(self):
        self.assertFalse(self._met({self.GATE: "False"}))
        self.assertFalse(self._met({self.GATE: "false"}))
        for value in ("True", "NA", "| company | x |"):
            self.assertTrue(self._met({self.GATE: value}), value)
        self.assertTrue(self._met({}), "a decision without the gate fact keeps its period")

    def test_gate_orders_but_never_gates_as_predecessor(self):
        """An NA predecessor is unfulfilled and would skip the period outright."""
        self.assertNotIn(self.GATE, _predecessors(self.period))
        self.assertIn(self.GATE, self.period.get("context_facts") or [])

    def test_case_summary_reads_the_gate_fact(self):
        self.assertIn(self.GATE, self.by_name["case_summary"]["context_facts"])


if __name__ == "__main__":
    unittest.main()
