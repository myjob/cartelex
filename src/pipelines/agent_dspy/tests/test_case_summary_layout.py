"""Unit tests for ``pipelines.agent_dspy.case_summary_layout`` (ISSUES #355).

The layout is the contract between the fact catalog, the LLM output model and
the rendered summary: every chapter names its source facts, the copy-only
chapters are never written by the LLM, and the status shown is the weakest of
the chapter's sources.
"""

import re
import unittest

import yaml

from config.settings import settings
from pipelines.agent_dspy.case_summary_layout import (
    CHAPTERS,
    ChapterKind,
    ChunkReference,
    StoredFact,
    chapter_status,
    render_summary,
    source_facts,
)
from pipelines.agent_dspy.dspy_signature import CaseSummaryChapters
from pipelines.agent_dspy.evidence_verifier_module import VerificationStatus, weakest_status


def _spec(key: str):
    return next(spec for spec in CHAPTERS if spec.key == key)


def _stored(*facts: tuple[str, str, str | None]) -> dict[str, StoredFact]:
    return {name: StoredFact(name=name, value_md=value, verification_status=status) for name, value, status in facts}


class LayoutContractTest(unittest.TestCase):
    """The layout, the LLM model and the catalog stay in step."""

    def test_llm_writes_exactly_the_generated_and_synthesis_chapters(self) -> None:
        llm_chapters = {spec.key for spec in CHAPTERS if spec.kind is not ChapterKind.VERBATIM}
        self.assertEqual(llm_chapters, set(CaseSummaryChapters.model_fields))

    def test_copy_only_chapters_are_never_asked_of_the_llm(self) -> None:
        for key in ("fines", "cartel_period", "commissioner", "infringement_gravity", "celex_case_id"):
            with self.subTest(key=key):
                self.assertIs(_spec(key).kind, ChapterKind.VERBATIM)
                self.assertNotIn(key, CaseSummaryChapters.model_fields)

    def test_field_descriptions_quote_the_layout_sources(self) -> None:
        for spec in CHAPTERS:
            if spec.kind is not ChapterKind.GENERATED:
                continue
            with self.subTest(key=spec.key):
                description = CaseSummaryChapters.model_fields[spec.key].description or ""
                self.assertEqual(set(re.findall(r"'([A-Za-z_]+)'", description)), set(spec.sources))

    def test_every_context_fact_feeds_a_chapter_and_back(self) -> None:
        with open(settings.FACT_EXTRACTOR_CONFIG_DIR, encoding="utf-8") as handle:
            catalog = yaml.safe_load(handle)
        entry = next(s for s in catalog if s["target_fact_name"] == "case_summary")
        self.assertEqual(set(source_facts()), set(entry["context_facts"]))

    def test_synthesis_chapters_have_no_source(self) -> None:
        synthesis = [spec.key for spec in CHAPTERS if spec.kind is ChapterKind.SYNTHESIS]
        self.assertEqual(synthesis, ["overview", "outcome"])
        self.assertTrue(all(not _spec(key).sources for key in synthesis))


class ChapterStatusTest(unittest.TestCase):
    """The weakest stored source decides; one source keeps its own verdict."""

    def test_one_source_keeps_its_own_verdict(self) -> None:
        stored = _stored(("fines", "x", "unverified"))
        self.assertIs(chapter_status(_spec("fines"), stored), VerificationStatus.UNVERIFIED)

    def test_weakest_source_wins(self) -> None:
        stored = _stored(("leniency_case", "True", "verified_supported"), ("leniency_granted", "x", "contradicted"))
        self.assertIs(chapter_status(_spec("leniency"), stored), VerificationStatus.CONTRADICTED)

    def test_absent_sources_are_ignored_and_none_stored_is_none(self) -> None:
        stored = _stored(("leniency_case", "True", "verified_supported"))
        self.assertIs(chapter_status(_spec("leniency"), stored), VerificationStatus.VERIFIED_SUPPORTED)
        self.assertIsNone(chapter_status(_spec("fines"), {}))


class WeakestStatusTest(unittest.TestCase):
    """The rule shared with the verifier's derived verdict."""

    def test_rule(self) -> None:
        cases = {
            ("verified_supported", "manual_approved"): VerificationStatus.VERIFIED_SUPPORTED,
            ("verified_supported", None): VerificationStatus.WEAK_SUPPORT,
            ("verified_supported", "insufficient_evidence"): VerificationStatus.INSUFFICIENT_EVIDENCE,
            ("insufficient_evidence", "contradicted"): VerificationStatus.CONTRADICTED,
            (): VerificationStatus.WEAK_SUPPORT,
        }
        for statuses, expected in cases.items():
            with self.subTest(statuses=statuses):
                self.assertIs(weakest_status(statuses), expected)


class RenderSummaryTest(unittest.TestCase):
    """Copy-only chapters hold the stored value; every chapter cites its sources."""

    def setUp(self) -> None:
        self.stored = _stored(
            ("fines", "| company | amount |\n|---|---|\n| Acme | EUR 1 |", "verified_supported"),
            ("leniency_case", "True", "verified_supported"),
            ("leniency_granted", "Acme 50%", None),
        )
        self.generated = {"overview": "A cartel.", "leniency": "Acme got 50%.", "fines": "LLM must not win"}

    def test_fines_table_is_copied_not_generated(self) -> None:
        summary = render_summary(self.generated, self.stored)
        self.assertIn("### Fines\n| company | amount |\n|---|---|\n| Acme | EUR 1 |\n", summary)
        self.assertNotIn("LLM must not win", summary)

    def test_source_lines(self) -> None:
        summary = render_summary(self.generated, self.stored)
        self.assertIn(
            "*Sources: leniency_case (verified_supported), leniency_applicants (not stored), "
            "leniency_granted (unverified), leniency_rejected (not stored). Weakest: weak_support*",
            summary)
        self.assertIn("### Overview\nA cartel.\n\n*Generated from the facts of the other chapters", summary)
        self.assertIn("### Cartel Period\nNA\n\n*Sources: cartel_period (not stored)*", summary)

    def test_references_chapter(self) -> None:
        refs = [ChunkReference(fact_name="fines", chunk_id="abc", doc_pos=7),
                ChunkReference(fact_name="leniency_case", chunk_id="def")]
        summary = render_summary(self.generated, self.stored, refs)
        self.assertTrue(summary.endswith("### References\n- fines: chunk 7 (`abc`)\n- leniency_case: chunk (`def`)\n"))

    def test_headings_follow_the_layout_order(self) -> None:
        headings = re.findall(r"^### (.+)$", render_summary({}, {}), re.M)
        self.assertEqual(headings, [spec.title for spec in CHAPTERS] + ["References"])


if __name__ == "__main__":
    unittest.main()
