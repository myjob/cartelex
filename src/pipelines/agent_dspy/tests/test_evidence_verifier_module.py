"""Unit tests for :class:`EvidenceVerifierModule`.

All system boundaries (fact / chunk / lineage / evidence repositories and the
LLM support judge) are mocked, so these tests touch neither the database nor a
real model. They assert the deterministic-first routing, the complete-ledger
guarantee, and that the LLM is used only on the concept branch.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/agent_dspy/tests/test_evidence_verifier_module.py -v
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config.settings import settings
from pipelines.agent_dspy.evidence_verifier_module import (
    EvidenceSupportResult,
    EvidenceVerifierModule,
    VerificationStatus,
    VerificationStrategy,
    _clean_question,
)


def _fact(name, *, value_json=None, value_md=None, celex_id="CELEX-1", fid="f-1"):
    """Build a minimal stand-in for a ``Facts`` ORM row."""
    return SimpleNamespace(
        id=fid,
        celex_id=celex_id,
        name=name,
        value_json=value_json,
        value_md=value_md,
    )


def _chunk_hit(chunk_id, text, doc_pos=0, similarity=0.9):
    """Build a ChunkVo-like retrieval hit."""
    return SimpleNamespace(id=chunk_id, chunk=text, doc_pos=doc_pos, similarity=similarity)


def _build_module(
    fact, *, predecessors=None, source_type_counts=None, judge=None, chunk_repo=None
):
    """Assemble a module with mocked dependencies around a single fact."""
    fact_repo = MagicMock()
    fact_repo.select_fact4celex.return_value = [fact]
    fact_repo.select_latest_question.return_value = None

    evidence_repo = MagicMock()
    evidence_repo.get_predecessor_facts.return_value = predecessors or []
    evidence_repo.get_source_type_counts.return_value = source_type_counts or {}

    module = EvidenceVerifierModule(
        chunk_repo=chunk_repo or MagicMock(),
        lineage_repo=MagicMock(),
        fact_repo=fact_repo,
        evidence_repo=evidence_repo,
        embed_fn=lambda _t: [0.0, 0.1],
        support_judge=judge or MagicMock(),
    )
    return module, evidence_repo


class TestRouting(unittest.TestCase):
    """The strategy router is fully deterministic from name + lineage."""

    def _strategy(self, name, *, value_md=None, source_type_counts=None):
        fact = _fact(name, value_md=value_md)
        module, _ = _build_module(fact, source_type_counts=source_type_counts)
        return module._route_strategy(fact)

    def test_human_prefix_is_trusted(self):
        self.assertEqual(
            self._strategy("human_index_year"), VerificationStrategy.TRUSTED_HUMAN
        )

    def test_concept_with_chunks_is_llm(self):
        self.assertEqual(
            self._strategy("leniency_granted", source_type_counts={"chunk": 1}),
            VerificationStrategy.LLM_SUPPORT,
        )

    def test_entity_name_is_deterministic_entity(self):
        self.assertEqual(
            self._strategy("court_list"), VerificationStrategy.DETERMINISTIC_ENTITY
        )

    def test_entity_with_chunk_lineage_is_deterministic_entity(self):
        # court_list_raw carries chunk lineage → matched against chunks.
        self.assertEqual(
            self._strategy("court_list_raw", source_type_counts={"chunk": 2}),
            VerificationStrategy.DETERMINISTIC_ENTITY,
        )

    def test_entity_aggregated_from_fact_is_derived(self):
        # court_list aggregates court_list_raw (fact lineage, no chunks of its
        # own): verified against the predecessor, NOT matched against chunks.
        self.assertEqual(
            self._strategy("court_list", source_type_counts={"fact": 1}),
            VerificationStrategy.DERIVED,
        )

    def test_temporal_aggregated_from_fact_is_derived(self):
        # A temporal-named fact with only fact lineage is still derived.
        self.assertEqual(
            self._strategy(
                "eu_commisssion_decision_date",
                value_md="1981-11-01",
                source_type_counts={"fact": 1},
            ),
            VerificationStrategy.DERIVED,
        )

    def test_temporal_name_with_dates_is_deterministic_temporal(self):
        self.assertEqual(
            self._strategy("eu_commisssion_decision_date", value_md="1981-11-01"),
            VerificationStrategy.DETERMINISTIC_TEMPORAL,
        )

    def test_temporal_name_without_dates_is_not_temporal(self):
        # No date tokens → the HeidelTime path does not apply.
        self.assertNotEqual(
            self._strategy("legal_assessment_period"),
            VerificationStrategy.DETERMINISTIC_TEMPORAL,
        )

    def test_fact_predecessors_fall_back_to_derived(self):
        self.assertEqual(
            self._strategy("some_neutral_fact", source_type_counts={"fact": 2}),
            VerificationStrategy.DERIVED,
        )

    def test_concept_with_only_fact_predecessors_is_derived(self):
        # leniency_granted is derived (fact preds, no chunks) — must NOT hit LLM.
        self.assertEqual(
            self._strategy("leniency_granted", source_type_counts={"fact": 3}),
            VerificationStrategy.DERIVED,
        )

    def test_concept_with_chunk_provenance_is_llm(self):
        self.assertEqual(
            self._strategy("leniency_granted",
                           source_type_counts={"chunk": 2, "fact": 3}),
            VerificationStrategy.LLM_SUPPORT,
        )

    def test_no_provenance_when_nothing_matches(self):
        self.assertEqual(
            self._strategy("some_neutral_fact"), VerificationStrategy.NO_PROVENANCE
        )

    def test_boolean_name_routes_to_boolean(self):
        self.assertEqual(
            self._strategy("case_commission_decision", value_md="True"),
            VerificationStrategy.BOOLEAN,
        )

    def test_boolean_court_name_not_mistaken_for_entity(self):
        # is_court_involved contains the 'court' entity marker but is a boolean.
        self.assertEqual(
            self._strategy("is_court_involved", value_md="False"),
            VerificationStrategy.BOOLEAN,
        )

    def test_boolean_aggregated_from_fact_is_derived(self):
        # Provenance shape still beats the boolean name: fact-only lineage.
        self.assertEqual(
            self._strategy(
                "leniency_case_match", value_md="True",
                source_type_counts={"fact": 1},
            ),
            VerificationStrategy.DERIVED,
        )


class TestExcludedAndLedger(unittest.TestCase):
    """Exclude list short-circuits; every other path writes exactly one row."""

    def test_excluded_writes_nothing(self):
        module, evidence_repo = _build_module(_fact("decision_chapter_tokens"))
        result = module.forward("CELEX-1", "decision_chapter_tokens")
        self.assertIsNone(result)
        evidence_repo.upsert_evidence.assert_not_called()
        evidence_repo.set_verification_status.assert_not_called()

    def test_empty_value_md_and_json_records_unverified(self):
        # No verifiable value in either column → explicit unverified row
        # (complete-ledger guarantee), labelled 'empty_value', no chunk.
        for value_md, value_json in ((None, None), ("NA", None), ("NA", "[]")):
            module, evidence_repo = _build_module(
                _fact("court_list", value_md=value_md, value_json=value_json)
            )
            result = module.forward("CELEX-1", "court_list")
            ctx = f"md={value_md!r} json={value_json!r}"
            self.assertIsNotNone(result, ctx)
            self.assertEqual(result.strategy, VerificationStrategy.NO_PROVENANCE, ctx)
            self.assertEqual(result.status, VerificationStatus.UNVERIFIED, ctx)
            self.assertEqual(result.support_label, "empty_value", ctx)
            self.assertIsNone(result.best_chunk_id, ctx)
            evidence_repo.upsert_evidence.assert_called_once()
            self.assertIsNone(
                evidence_repo.upsert_evidence.call_args.kwargs["chunk_id"], ctx
            )
            evidence_repo.set_verification_status.assert_called_once_with(
                result.fact_id, "unverified", None
            )

    def test_semantically_empty_json_records_unverified(self):
        # value_json is structurally non-empty but every scalar leaf is NA:
        # nothing to verify, so it is recorded unverified like a literal NA / [].
        for value_json in (
            '[{"infringement_category_procedural": "NA"}]',
            '[{"a": "NA"}, {"b": "n/a"}]',
            '{"x": null}',
        ):
            module, evidence_repo = _build_module(
                _fact(
                    "infringement_category_procedural",
                    value_md="NA", value_json=value_json,
                )
            )
            result = module.forward("CELEX-1", "infringement_category_procedural")
            self.assertIsNotNone(result, value_json)
            self.assertEqual(result.status, VerificationStatus.UNVERIFIED, value_json)
            self.assertEqual(result.support_label, "empty_value", value_json)
            evidence_repo.upsert_evidence.assert_called_once()

    def test_empty_value_md_but_json_present_is_verified(self):
        # value_md null but value_json carries content → still verified.
        chunk_repo = MagicMock()
        chunk_repo.keyword_search.return_value = [
            _chunk_hit("c-1", "The High Court of Justice ruled on the matter.")
        ]
        fact = _fact(
            "court_list", value_md="NA",
            value_json='[{"court": "High Court of Justice"}]',
        )
        module, evidence_repo = _build_module(fact, chunk_repo=chunk_repo)
        result = module.forward("CELEX-1", "court_list")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        evidence_repo.upsert_evidence.assert_called_once()

    def test_trusted_human_writes_summary_row_no_llm(self):
        judge = MagicMock()
        module, evidence_repo = _build_module(_fact("human_index_year", value_md="1982"), judge=judge)
        result = module.forward("CELEX-1", "human_index_year")
        self.assertEqual(result.status, VerificationStatus.MANUAL_APPROVED)
        self.assertIsNone(result.best_chunk_id)
        judge.assert_not_called()
        evidence_repo.upsert_evidence.assert_called_once()
        # summary row → chunk_id is None
        self.assertIsNone(evidence_repo.upsert_evidence.call_args.kwargs["chunk_id"])
        evidence_repo.set_verification_status.assert_called_once()


class TestDerived(unittest.TestCase):
    """Derived facts aggregate predecessor verdicts (weakest wins)."""

    def test_all_verified_predecessors_verify(self):
        preds = [
            {"name": "a", "verification_status": "verified_supported"},
            {"name": "b", "verification_status": "manual_approved"},
        ]
        module, _ = _build_module(
            _fact("neutral_derived", value_md="aggregated value"), predecessors=preds,
            source_type_counts={"fact": 2},
        )
        result = module.forward("CELEX-1", "neutral_derived")
        self.assertEqual(result.strategy, VerificationStrategy.DERIVED)
        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)

    def test_insufficient_predecessor_propagates(self):
        preds = [
            {"name": "a", "verification_status": "verified_supported"},
            {"name": "b", "verification_status": "insufficient_evidence"},
        ]
        module, _ = _build_module(
            _fact("neutral_derived", value_md="aggregated value"), predecessors=preds,
            source_type_counts={"fact": 2},
        )
        result = module.forward("CELEX-1", "neutral_derived")
        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        self.assertIn("b", result.missing_elements)

    def test_unverified_predecessor_is_verified_on_demand(self):
        # court_list (derived) is processed before its predecessor
        # court_list_raw: the raw fact's verdict is still NULL. It must be
        # verified on demand and NOT reported as a missing/weak element.
        derived = _fact(
            "court_list", celex_id="CELEX-1", fid="f-1",
            value_json='[{"court": "High Court of Justice"}]',
        )
        raw = _fact(
            "court_list_raw",
            celex_id="CELEX-1",
            fid="raw-1",
            value_json='[{"court": "High Court of Justice"}]',
        )

        fact_repo = MagicMock()
        fact_repo.select_fact4celex.side_effect = lambda celex, name: {
            "court_list": [derived],
            "court_list_raw": [raw],
        }[name]

        evidence_repo = MagicMock()
        evidence_repo.get_source_type_counts.side_effect = lambda fid: {
            "f-1": {"fact": 1},     # court_list aggregates a fact → derived
            "raw-1": {"chunk": 1},  # court_list_raw has chunk lineage → entity
        }[fid]
        # First read: raw predecessor not yet verified; second read (after the
        # on-demand pass): verified_supported.
        evidence_repo.get_predecessor_facts.side_effect = [
            [{"fact_id": "raw-1", "name": "court_list_raw",
              "verification_status": None}],
            [{"fact_id": "raw-1", "name": "court_list_raw",
              "verification_status": "verified_supported"}],
        ]

        chunk_repo = MagicMock()
        chunk_repo.keyword_search.return_value = [
            _chunk_hit("c-1", "The High Court of Justice ruled on the matter.")
        ]

        module = EvidenceVerifierModule(
            chunk_repo=chunk_repo,
            lineage_repo=MagicMock(),
            fact_repo=fact_repo,
            evidence_repo=evidence_repo,
            embed_fn=lambda _t: [0.0, 0.1],
            support_judge=MagicMock(),
        )

        result = module.forward("CELEX-1", "court_list")

        self.assertEqual(result.strategy, VerificationStrategy.DERIVED)
        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        self.assertEqual(result.missing_elements, [])
        # The raw predecessor was verified on demand (its own ledger row written).
        self.assertGreaterEqual(evidence_repo.upsert_evidence.call_count, 2)


class TestEntityDeterministic(unittest.TestCase):
    """Entity facts are concluded by SQL surface match — never the LLM."""

    def test_full_coverage_verifies_without_llm(self):
        chunk_repo = MagicMock()
        chunk_repo.keyword_search.return_value = [
            _chunk_hit("c-1", "The High Court of Justice ruled on the matter.")
        ]
        judge = MagicMock()
        fact = _fact("court_list", value_json='[{"court": "High Court of Justice"}]')
        module, evidence_repo = _build_module(fact, judge=judge, chunk_repo=chunk_repo)

        result = module.forward("CELEX-1", "court_list")

        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        self.assertEqual(result.best_chunk_id, "c-1")
        self.assertIsNotNone(result.evidence_quote)
        judge.assert_not_called()
        self.assertEqual(
            evidence_repo.upsert_evidence.call_args.kwargs["chunk_id"], "c-1"
        )

    def test_missing_entity_flags_without_llm(self):
        chunk_repo = MagicMock()
        chunk_repo.keyword_search.return_value = []  # surface absent from corpus
        judge = MagicMock()
        fact = _fact("companies_investigated", value_json='[{"company": "Ghost GmbH"}]')
        module, _ = _build_module(fact, judge=judge, chunk_repo=chunk_repo)

        result = module.forward("CELEX-1", "companies_investigated")

        self.assertEqual(result.status, VerificationStatus.MISSING_REQUIRED_ENTITY)
        judge.assert_not_called()

    def test_case_title_tuple_value_matches_chunk(self):
        # case_title stores a (case_id, title) tuple as a nested list; the docket
        # and title must be matched as surfaces, not the stringified tuple.
        chunk_repo = MagicMock()
        chunk_repo.keyword_search.return_value = [
            _chunk_hit("c-7", "Case AT.39813 — Baltic rail, a proceeding ...")
        ]
        judge = MagicMock()
        fact = _fact(
            "case_title",
            value_json='[{"case_title": ["AT.39813", "Baltic rail"]}]',
            value_md="('AT.39813', 'Baltic rail')",
        )
        module, evidence_repo = _build_module(
            fact, judge=judge, chunk_repo=chunk_repo
        )

        result = module.forward("CELEX-1", "case_title")

        self.assertEqual(result.strategy, VerificationStrategy.DETERMINISTIC_ENTITY)
        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        self.assertEqual(result.best_chunk_id, "c-7")
        judge.assert_not_called()
        self.assertEqual(
            evidence_repo.upsert_evidence.call_args.kwargs["chunk_id"], "c-7"
        )


class TestTemporalDeterministic(unittest.TestCase):
    """Temporal facts match HeidelTime extractions — never the LLM."""

    def test_timex_match_verifies(self):
        chunk_repo = MagicMock()
        chunk_repo.get_chunk_by_id.return_value = {
            "document": "The decision was adopted on 1982-01-01.",
            "doc_pos": 3,
        }
        judge = MagicMock()
        fact = _fact("eu_commisssion_decision_date", value_md="1982-01-01")
        module, _ = _build_module(fact, judge=judge, chunk_repo=chunk_repo)
        module._heideltime_match = lambda celex, iso, years: (
            "c-9", "1 January 1982", "1982-01-01"
        )

        result = module.forward("CELEX-1", "eu_commisssion_decision_date")

        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        self.assertEqual(result.best_chunk_id, "c-9")
        self.assertEqual(result.evidence_quote, "1 January 1982")
        judge.assert_not_called()

    def test_no_timex_match_flags_missing_time(self):
        judge = MagicMock()
        fact = _fact("eu_commisssion_decision_date", value_md="1982-01-01")
        module, _ = _build_module(fact, judge=judge)
        module._heideltime_match = lambda celex, iso, years: None

        result = module.forward("CELEX-1", "eu_commisssion_decision_date")

        self.assertEqual(result.status, VerificationStatus.MISSING_REQUIRED_TIME)
        judge.assert_not_called()


class TestLlmSupport(unittest.TestCase):
    """Concept facts use the judge; quotes must be exact substrings."""

    def _concept_module(self, judge):
        chunk_repo = MagicMock()
        chunk_repo.fulltext_search_in_celex.return_value = [
            _chunk_hit("c-1", "Acme received leniency for the cartel infringement.")
        ]
        chunk_repo.semantic_search_in_celex.return_value = []
        fact = _fact("leniency_granted", value_json='[{"company": "Acme"}]')
        module, evidence_repo = _build_module(
            fact, judge=judge, chunk_repo=chunk_repo,
            source_type_counts={"chunk": 1},  # has chunk evidence → LLM branch
        )
        module.lineage_repo.get_sources_for_fact.return_value = []
        return module, evidence_repo

    def test_supports_with_exact_quote_verifies(self):
        judge = MagicMock(
            return_value=SimpleNamespace(
                output=EvidenceSupportResult(
                    label="supports",
                    support_score=0.9,
                    evidence_quote="Acme received leniency",
                )
            )
        )
        module, evidence_repo = self._concept_module(judge)
        result = module.forward("CELEX-1", "leniency_granted")

        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        self.assertEqual(result.evidence_quote, "Acme received leniency")
        judge.assert_called()
        self.assertEqual(
            evidence_repo.upsert_evidence.call_args.kwargs["chunk_id"], "c-1"
        )

    def test_supports_without_quote_is_downgraded(self):
        judge = MagicMock(
            return_value=SimpleNamespace(
                output=EvidenceSupportResult(
                    label="supports", support_score=0.9, evidence_quote=None
                )
            )
        )
        module, _ = self._concept_module(judge)
        result = module.forward("CELEX-1", "leniency_granted")
        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)

    def test_judge_exception_does_not_crash(self):
        def _boom(**_kwargs):
            raise RuntimeError("LLM timeout")

        judge = MagicMock(side_effect=_boom)
        module, evidence_repo = self._concept_module(judge)
        result = module.forward("CELEX-1", "leniency_granted")
        # Run still completes and records a verdict.
        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        evidence_repo.upsert_evidence.assert_called_once()


class TestBoolean(unittest.TestCase):
    """True/False facts verify by polarity; False is asymmetric (absence)."""

    def _boolean_module(self, fact, *, boolean_judge=None, chunk_repo=None):
        """Assemble a module routed through the boolean branch."""
        fact_repo = MagicMock()
        fact_repo.select_fact4celex.return_value = [fact]
        fact_repo.select_latest_question.return_value = None
        evidence_repo = MagicMock()
        evidence_repo.get_predecessor_facts.return_value = []
        evidence_repo.get_source_type_counts.return_value = {"chunk": 1}
        repo = chunk_repo or MagicMock()
        module = EvidenceVerifierModule(
            chunk_repo=repo,
            lineage_repo=MagicMock(),
            fact_repo=fact_repo,
            evidence_repo=evidence_repo,
            embed_fn=lambda _t: [0.0, 0.1],
            support_judge=MagicMock(),
            boolean_judge=boolean_judge or MagicMock(),
        )
        module.lineage_repo.get_sources_for_fact.return_value = []
        return module, evidence_repo

    def _verdict(self, **kwargs):
        return SimpleNamespace(output=EvidenceSupportResult(**kwargs))

    def test_marker_fast_path_verifies_without_llm(self):
        chunk_repo = MagicMock()
        chunk_repo.keyword_search.return_value = [
            _chunk_hit("c-1", "The Commission HAS ADOPTED THIS DECISION: Article 1...")
        ]
        boolean_judge = MagicMock()
        fact = _fact("case_commission_decision", value_md="True")
        module, evidence_repo = self._boolean_module(
            fact, boolean_judge=boolean_judge, chunk_repo=chunk_repo
        )

        result = module.forward("CELEX-1", "case_commission_decision")

        self.assertEqual(result.strategy, VerificationStrategy.BOOLEAN)
        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        self.assertEqual(result.best_chunk_id, "c-1")
        self.assertIsNotNone(result.evidence_quote)
        boolean_judge.assert_not_called()
        self.assertEqual(
            evidence_repo.upsert_evidence.call_args.kwargs["chunk_id"], "c-1"
        )

    def test_true_via_judge_supports(self):
        # market_oligopoly has no markers → falls through to the polarity judge.
        chunk_repo = MagicMock()
        chunk_repo.fulltext_search_in_celex.return_value = [
            _chunk_hit("c-2", "The market is an oligopoly dominated by three firms.")
        ]
        chunk_repo.semantic_search_in_celex.return_value = []
        boolean_judge = MagicMock(
            return_value=self._verdict(
                label="supports", support_score=0.9,
                evidence_quote="market is an oligopoly",
            )
        )
        fact = _fact("market_oligopoly", value_md="True")
        module, _ = self._boolean_module(
            fact, boolean_judge=boolean_judge, chunk_repo=chunk_repo
        )

        result = module.forward("CELEX-1", "market_oligopoly")

        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        self.assertEqual(result.evidence_quote, "market is an oligopoly")
        boolean_judge.assert_called()

    def test_false_contradicted_when_positive_asserted(self):
        chunk_repo = MagicMock()
        chunk_repo.fulltext_search_in_celex.return_value = [
            _chunk_hit("c-3", "The market is a clear oligopoly.")
        ]
        chunk_repo.semantic_search_in_celex.return_value = []
        boolean_judge = MagicMock(
            return_value=self._verdict(
                label="contradicts", support_score=0.8,
                evidence_quote=None,
            )
        )
        fact = _fact("market_oligopoly", value_md="False")
        module, _ = self._boolean_module(
            fact, boolean_judge=boolean_judge, chunk_repo=chunk_repo
        )

        result = module.forward("CELEX-1", "market_oligopoly")

        self.assertEqual(result.status, VerificationStatus.CONTRADICTED)

    def test_false_unproven_is_weak_not_insufficient(self):
        # No positive evidence found: absence is consistent with False → weak.
        chunk_repo = MagicMock()
        chunk_repo.fulltext_search_in_celex.return_value = [
            _chunk_hit("c-4", "Unrelated procedural background text.")
        ]
        chunk_repo.semantic_search_in_celex.return_value = []
        boolean_judge = MagicMock(
            return_value=self._verdict(
                label="insufficient", support_score=0.1, evidence_quote=None
            )
        )
        fact = _fact("market_oligopoly", value_md="False")
        module, _ = self._boolean_module(
            fact, boolean_judge=boolean_judge, chunk_repo=chunk_repo
        )

        result = module.forward("CELEX-1", "market_oligopoly")

        self.assertEqual(result.status, VerificationStatus.WEAK_SUPPORT)

    def test_true_unproven_is_insufficient(self):
        chunk_repo = MagicMock()
        chunk_repo.fulltext_search_in_celex.return_value = [
            _chunk_hit("c-5", "Unrelated procedural background text.")
        ]
        chunk_repo.semantic_search_in_celex.return_value = []
        boolean_judge = MagicMock(
            return_value=self._verdict(
                label="insufficient", support_score=0.1, evidence_quote=None
            )
        )
        fact = _fact("market_oligopoly", value_md="True")
        module, _ = self._boolean_module(
            fact, boolean_judge=boolean_judge, chunk_repo=chunk_repo
        )

        result = module.forward("CELEX-1", "market_oligopoly")

        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)

    def test_case_commission_no_action_false_with_enforcement_evidence(self):
        # When case_commission_no_action=False (Commission DID take action),
        # evidence of enforcement should support the fact, not return weak_support.
        chunk_repo = MagicMock()
        chunk_repo.fulltext_search_in_celex.return_value = [
            _chunk_hit("c-6", "The Commission shall bring to an end the infringement...")
        ]
        chunk_repo.semantic_search_in_celex.return_value = []
        boolean_judge = MagicMock(
            return_value=self._verdict(
                label="supports", support_score=0.95,
                evidence_quote="Commission shall bring to an end the infringement",
            )
        )
        fact = _fact("case_commission_no_action", value_md="False")
        module, _ = self._boolean_module(
            fact, boolean_judge=boolean_judge, chunk_repo=chunk_repo
        )

        result = module.forward("CELEX-1", "case_commission_no_action")

        # With inverted claim, the judge sees "Commission has taken action" and
        # the evidence supports it → verified_supported, not weak_support.
        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        self.assertEqual(result.evidence_quote, "Commission shall bring to an end the infringement")


class TestExtractionQuestion(unittest.TestCase):
    """The recorded extraction question enriches retrieval and the LLM judge."""

    def _concept_module(self, judge, *, question):
        """A concept fact routed to the LLM branch, with embed-input capture."""
        chunk_repo = MagicMock()
        chunk_repo.fulltext_search_in_celex.return_value = [
            _chunk_hit("c-1", "Acme received leniency for the cartel infringement.")
        ]
        chunk_repo.semantic_search_in_celex.return_value = []
        fact = _fact("leniency_granted", value_json='[{"company": "Acme"}]')
        module, evidence_repo = _build_module(
            fact, judge=judge, chunk_repo=chunk_repo,
            source_type_counts={"chunk": 1},
        )
        module.lineage_repo.get_sources_for_fact.return_value = []
        module.fact_repo.select_latest_question.return_value = question
        embedded: list[str] = []
        module.embed_fn = lambda text: embedded.append(text) or [0.0, 0.1]
        return module, embedded

    def _supporting_judge(self):
        return MagicMock(
            return_value=SimpleNamespace(
                output=EvidenceSupportResult(
                    label="supports", support_score=0.9,
                    evidence_quote="Acme received leniency",
                )
            )
        )

    def test_question_enriches_retrieval_and_judge(self):
        judge = self._supporting_judge()
        module, embedded = self._concept_module(
            judge, question="Did the company receive leniency or immunity?"
        )
        module.forward("CELEX-1", "leniency_granted")

        # The semantic retrieval query carries the question text.
        self.assertTrue(any("leniency or immunity" in text for text in embedded))
        # The judge receives the question as its rubric.
        self.assertIn(
            "leniency or immunity",
            judge.call_args.kwargs["extraction_question"],
        )

    def test_no_question_falls_back_to_statement(self):
        judge = self._supporting_judge()
        module, embedded = self._concept_module(judge, question=None)
        module.forward("CELEX-1", "leniency_granted")

        # Retrieval query is the bare value-derived statement; judge sees no rubric.
        self.assertTrue(all("?" not in text for text in embedded))
        self.assertEqual(judge.call_args.kwargs["extraction_question"], "")

    def test_feature_flag_disables_lookup(self):
        judge = self._supporting_judge()
        module, _ = self._concept_module(
            judge, question="Did the company receive leniency?"
        )
        with patch.object(settings, "EVIDENCE_USE_EXTRACTION_QUESTION", False):
            module.forward("CELEX-1", "leniency_granted")

        module.fact_repo.select_latest_question.assert_not_called()
        self.assertEqual(judge.call_args.kwargs["extraction_question"], "")

    def test_question_cached_across_calls(self):
        judge = self._supporting_judge()
        module, _ = self._concept_module(
            judge, question="Did the company receive leniency?"
        )
        module.forward("CELEX-1", "leniency_granted")
        module.forward("CELEX-1", "leniency_granted")
        # Cached per (celex_id, name): the audit trail is read only once.
        module.fact_repo.select_latest_question.assert_called_once()

    def test_boolean_branch_passes_question_to_judge(self):
        chunk_repo = MagicMock()
        chunk_repo.fulltext_search_in_celex.return_value = [
            _chunk_hit("c-2", "The market is an oligopoly dominated by three firms.")
        ]
        chunk_repo.semantic_search_in_celex.return_value = []
        boolean_judge = MagicMock(
            return_value=SimpleNamespace(
                output=EvidenceSupportResult(
                    label="supports", support_score=0.9,
                    evidence_quote="market is an oligopoly",
                )
            )
        )
        fact = _fact("market_oligopoly", value_md="True")
        fact_repo = MagicMock()
        fact_repo.select_fact4celex.return_value = [fact]
        fact_repo.select_latest_question.return_value = "Is the market an oligopoly?"
        evidence_repo = MagicMock()
        evidence_repo.get_predecessor_facts.return_value = []
        evidence_repo.get_source_type_counts.return_value = {"chunk": 1}
        module = EvidenceVerifierModule(
            chunk_repo=chunk_repo,
            lineage_repo=MagicMock(),
            fact_repo=fact_repo,
            evidence_repo=evidence_repo,
            embed_fn=lambda _t: [0.0, 0.1],
            support_judge=MagicMock(),
            boolean_judge=boolean_judge,
        )
        module.lineage_repo.get_sources_for_fact.return_value = []

        module.forward("CELEX-1", "market_oligopoly")

        self.assertIn(
            "oligopoly", boolean_judge.call_args.kwargs["extraction_question"]
        )

    def test_clean_question_strips_comment_lines(self):
        raw = (
            "the Commission need not take action\n"
            "# Constraint: reply True/False/NA\n"
        )
        self.assertEqual(
            _clean_question(raw), "the Commission need not take action"
        )

    def test_clean_question_returns_none_for_empty_or_comment_only(self):
        self.assertIsNone(_clean_question(None))
        self.assertIsNone(_clean_question("   "))
        self.assertIsNone(_clean_question("# only a constraint comment"))


if __name__ == "__main__":
    unittest.main()
