"""Unit tests for entity-graph evidence in the knowledge-ingestion pipeline.

Pure-Python: rendering, relation narrowing and the deterministic contradiction
detector are exercised without a database or an LLM.

The detector is deliberately high-precision, so the negative cases below carry
as much weight as the positive ones: a false contradiction discards a sound
nugget without any LLM seeing it.
"""

import unittest

from pipelines.agent_dspy.knowledge_graph_evidence import (
    find_graph_contradiction,
    relations_for_fact_ids,
    render_validated_relations,
)
from pipelines.graph.entity_graph_models import EdgeRelation, ValidatedRelation

_FACT_A = "11111111-1111-1111-1111-111111111111"
_FACT_B = "22222222-2222-2222-2222-222222222222"


def _relation(
    relation: EdgeRelation,
    src_name: str = "NSK Deutschland GmbH",
    celex_id: str = "39922_2067_2",
    source_fact_id: str = _FACT_A,
    amount: str | None = None,
) -> ValidatedRelation:
    """Build a validated relation with test-friendly defaults."""
    return ValidatedRelation(
        src_name=src_name,
        relation=relation,
        dst_name="Bearings",
        celex_id=celex_id,
        source_fact_id=source_fact_id,
        amount=amount,
    )


class TestRenderValidatedRelations(unittest.TestCase):
    """The prompt block handed to the extractor and the judge."""

    def test_empty_renders_empty_string(self):
        """No relations must yield no header, not a dangling title."""
        self.assertEqual(render_validated_relations([]), "")

    def test_renders_one_line_per_relation_with_amount(self):
        """Each relation renders with its decision, and its amount when set."""
        block = render_validated_relations(
            [
                _relation(EdgeRelation.PARTICIPATED_IN),
                _relation(EdgeRelation.FINED, amount="EUR 62 406 000"),
            ]
        )
        lines = block.splitlines()
        self.assertIn("VALIDATED RELATIONS", lines[0])
        self.assertIn("--participated_in--> Bearings [39922_2067_2]", lines[1])
        self.assertIn("(amount: EUR 62 406 000)", lines[2])

    def test_cap_truncates_and_says_so(self):
        """A partial list must announce itself, never read as exhaustive."""
        relations = [
            _relation(EdgeRelation.PARTICIPATED_IN, src_name=f"Company {i} Werke")
            for i in range(5)
        ]
        block = render_validated_relations(relations, max_lines=2)
        lines = block.splitlines()
        self.assertEqual(len(lines), 4)  # header + 2 relations + notice
        self.assertIn("3 further relation(s) not shown", lines[-1])
        self.assertIn("absence from it proves nothing", lines[-1])

    def test_no_notice_when_everything_fits(self):
        """Below the cap the block carries no truncation line."""
        block = render_validated_relations(
            [_relation(EdgeRelation.FINED)], max_lines=10
        )
        self.assertNotIn("not shown", block)


class TestRelationsForFactIds(unittest.TestCase):
    """Narrowing the resolved set to the facts one nugget cited."""

    def test_narrows_to_cited_facts(self):
        """A nugget receives only the relations behind the facts it cited."""
        relations = [
            _relation(EdgeRelation.PARTICIPATED_IN, source_fact_id=_FACT_A),
            _relation(EdgeRelation.FINED, source_fact_id=_FACT_B),
        ]
        narrowed = relations_for_fact_ids(relations, [_FACT_B])
        self.assertEqual(len(narrowed), 1)
        self.assertEqual(narrowed[0].relation, EdgeRelation.FINED)

    def test_no_citation_yields_nothing(self):
        """Citing no fact must not inherit the whole trace's relations."""
        relations = [_relation(EdgeRelation.PARTICIPATED_IN)]
        self.assertEqual(relations_for_fact_ids(relations, []), ())


class TestContradictionPositive(unittest.TestCase):
    """Claims the graph refutes."""

    def test_denied_relation_is_a_contradiction(self):
        """A negated relation the graph holds is refuted."""
        found = find_graph_contradiction(
            "NSK was not fined in the bearings cartel.",
            [_relation(EdgeRelation.FINED)],
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.trigger, "negated_relation")
        self.assertEqual(found.relation.relation, EdgeRelation.FINED)

    def test_denied_participation_is_a_contradiction(self):
        """The participation surface forms are covered too."""
        found = find_graph_contradiction(
            "NSK never took part in the bearings cartel.",
            [_relation(EdgeRelation.PARTICIPATED_IN)],
        )
        self.assertIsNotNone(found)

    def test_mutually_exclusive_leniency_is_a_contradiction(self):
        """Asserting leniency_rejected where the graph holds granted."""
        found = find_graph_contradiction(
            "NSK's leniency application was rejected by the Commission.",
            [_relation(EdgeRelation.LENIENCY_GRANTED)],
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.trigger, "mutually_exclusive")

    def test_abbreviation_in_company_name_does_not_hide_the_denial(self):
        """Real canonical names carry 'Inc.'; splitting there would hide it.

        Regression from a live probe: naive sentence splitting shattered
        "Hitachi-LG Data Storage, Inc. and ... Korea, Inc. was not fined."
        into three fragments, leaving the denial in a fragment that named no
        company, so the gate silently passed.
        """
        name = "Hitachi-LG Data Storage, Inc. and Hitachi-LG Data Storage Korea, Inc."
        found = find_graph_contradiction(
            f"{name} was not fined in this decision.",
            [_relation(EdgeRelation.FINED, src_name=name)],
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.trigger, "negated_relation")

    def test_reports_the_offending_sentence(self):
        """The audit trail needs the sentence that carried the conflict."""
        found = find_graph_contradiction(
            "The Akzo presumption applies at full ownership. "
            "NSK was not fined here.",
            [_relation(EdgeRelation.FINED)],
        )
        self.assertEqual(found.sentence, "NSK was not fined here.")
        self.assertIn("negated_relation", found.describe())


class TestContradictionNegative(unittest.TestCase):
    """Claims that must NOT be refuted -- the precision guards."""

    def test_negation_after_the_relation_does_not_fire(self):
        """'participated but was not fined' does not deny participation."""
        found = find_graph_contradiction(
            "NSK participated in the cartel but was not fined.",
            [_relation(EdgeRelation.PARTICIPATED_IN)],
        )
        self.assertIsNone(found)

    def test_hedge_between_negation_and_relation_does_not_fire(self):
        """Uncertainty about a relation is not a denial of it."""
        found = find_graph_contradiction(
            "It is not clear whether NSK was fined in this decision.",
            [_relation(EdgeRelation.FINED)],
        )
        self.assertIsNone(found)

    def test_other_company_negation_does_not_fire(self):
        """A denial about a different company leaves the relation alone."""
        found = find_graph_contradiction(
            "SKF was not fined in the bearings cartel.",
            [_relation(EdgeRelation.FINED)],
        )
        self.assertIsNone(found)

    def test_word_boundary_prevents_prefix_collision(self):
        """'Bayerische' must not stand in for 'Bayer'."""
        found = find_graph_contradiction(
            "Bayerische Hypo- und Vereinsbank AG was not fined.",
            [_relation(EdgeRelation.FINED, src_name="Bayer AG")],
        )
        self.assertIsNone(found)

    def test_nugget_scoped_to_another_case_does_not_fire(self):
        """A claim about CELEX-31994D0823 cannot refute a 39922_2067_2 edge."""
        found = find_graph_contradiction(
            "NSK was not fined in CELEX-31994D0823.",
            [_relation(EdgeRelation.FINED, celex_id="39922_2067_2")],
        )
        self.assertIsNone(found)

    def test_matching_case_scope_still_fires(self):
        """The scope guard must not swallow an in-scope denial."""
        found = find_graph_contradiction(
            "NSK was not fined in 39922_2067_2.",
            [_relation(EdgeRelation.FINED, celex_id="39922_2067_2")],
        )
        self.assertIsNotNone(found)

    def test_generic_only_name_is_skipped(self):
        """A name reducing to corporate-form words cannot be matched safely."""
        found = find_graph_contradiction(
            "The company was not fined.",
            [_relation(EdgeRelation.FINED, src_name="Holding GmbH")],
        )
        self.assertIsNone(found)

    def test_relation_without_surface_form_is_skipped(self):
        """dated_in has no reliable prose form, so it never contradicts."""
        found = find_graph_contradiction(
            "NSK was not active in this period.",
            [_relation(EdgeRelation.DATED_IN)],
        )
        self.assertIsNone(found)

    def test_no_relations_or_empty_text_is_none(self):
        """Both empty inputs short-circuit."""
        self.assertIsNone(find_graph_contradiction("", [_relation(EdgeRelation.FINED)]))
        self.assertIsNone(find_graph_contradiction("NSK was not fined.", []))

    def test_consistent_nugget_passes(self):
        """A nugget agreeing with the graph must survive."""
        found = find_graph_contradiction(
            "NSK was fined in the bearings cartel and participated in it.",
            [
                _relation(EdgeRelation.FINED),
                _relation(EdgeRelation.PARTICIPATED_IN),
            ],
        )
        self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
