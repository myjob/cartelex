"""Unit tests for the section-classification batch (DB and LLM mocked).

Follows the project's batch-test shape: the ChunkRepository is a MagicMock at
the DB boundary, the LLM fallback is a stub module, and the assertions target
the write calls and the stats counters.
"""

import unittest
from unittest.mock import MagicMock

import dspy

from batch.Main_classify_sections import _classify_celex
from extract.rag.models.chunk_vo import SectionType
from pipelines.legal.section_classifier import SectionClassification


def _chunk(chunk_id: str, doc_pos: int, document: str, section_type: str | None = None) -> dict:
    return {
        "id": chunk_id,
        "doc_pos": doc_pos,
        "document": document,
        "section_type": section_type,
    }


def _stats() -> dict[str, int]:
    return {
        "rule_labeled": 0,
        "forward_filled": 0,
        "gate_rejected": 0,
        "llm_labeled": 0,
        "defaulted_other": 0,
        "llm_pending": 0,
        "skipped_already": 0,
        "errors": 0,
    }


# A plausible document: cover, facts, assessment, fines, operative.
_PLAUSIBLE_CHUNKS = [
    _chunk("c0", 0, "Commission Decision of 5 June 2002"),
    _chunk("c1", 1, "# I. THE FACTS\nThe parties met in Brussels."),
    _chunk("c2", 2, "Body text without a heading."),
    _chunk("c3", 3, "# II. LEGAL ASSESSMENT\nArticle 81 applies."),
    _chunk("c4", 4, "# 3.4.4. FINAL AMOUNT OF THE FINES\nEUR 30 million."),
    _chunk("c5", 5, "HAS ADOPTED THIS DECISION:\n\nArticle 1"),
]


class _StubClassifier:
    """SectionClassifierModule stand-in returning a fixed label."""

    def __init__(self, section_type: SectionType) -> None:
        self.calls: list[str] = []
        self._section_type = section_type

    def __call__(self, chunk_text: str, neighbor_context: str = "") -> dspy.Prediction:
        self.calls.append(chunk_text)
        return dspy.Prediction(
            output=SectionClassification(section_type=self._section_type)
        )


class TestClassifyCelex(unittest.TestCase):
    def _repo(self, chunks: list[dict]) -> MagicMock:
        repo = MagicMock()
        repo.select_chunks_for_sections.return_value = chunks
        return repo

    def test_dry_run_writes_nothing(self) -> None:
        repo = self._repo(_PLAUSIBLE_CHUNKS)
        stats = _stats()
        _classify_celex(repo, None, "CELEX-TEST", dry_run=True, stats=stats)
        repo.update_section_type.assert_not_called()
        self.assertGreater(stats["rule_labeled"], 0)

    def test_rules_and_fill_are_persisted(self) -> None:
        repo = self._repo(_PLAUSIBLE_CHUNKS)
        stats = _stats()
        _classify_celex(repo, None, "CELEX-TEST", dry_run=False, stats=stats)

        written = {
            call.args[0]: call.args[1]
            for call in repo.update_section_type.call_args_list
        }
        self.assertEqual(written["c0"], "parties")
        self.assertEqual(written["c1"], "recitals_facts")
        self.assertEqual(written["c2"], "recitals_facts")  # forward-filled
        self.assertEqual(written["c3"], "legal_assessment")
        self.assertEqual(written["c4"], "fines")
        self.assertEqual(written["c5"], "operative")
        self.assertEqual(stats["forward_filled"], 1)
        self.assertEqual(stats["rule_labeled"], 5)

    def test_already_labeled_chunks_skipped_but_anchor_the_fill(self) -> None:
        chunks = [
            _chunk("c0", 0, "cover", section_type="parties"),
            _chunk("c1", 1, "# I. THE FACTS\ntext", section_type="recitals_facts"),
            _chunk("c2", 2, "unheaded body text"),
            _chunk("c3", 3, "# II. LEGAL ASSESSMENT\ntext"),
        ]
        repo = self._repo(chunks)
        stats = _stats()
        _classify_celex(repo, None, "CELEX-TEST", dry_run=False, stats=stats)

        written = {
            call.args[0]: call.args[1]
            for call in repo.update_section_type.call_args_list
        }
        # Pre-labeled rows are never rewritten; the gap inherits their label.
        self.assertNotIn("c0", written)
        self.assertNotIn("c1", written)
        self.assertEqual(written["c2"], "recitals_facts")
        self.assertEqual(stats["skipped_already"], 2)

    def test_skip_llm_leaves_unresolved_chunks_null(self) -> None:
        """Leading chunks before the first label stay NULL without the LLM."""
        chunks = [
            _chunk("c0", 1, "unheaded leading text"),
            _chunk("c1", 2, "more unheaded text"),
            _chunk("c2", 3, "# I. THE FACTS\ntext"),
            _chunk("c3", 4, "# II. LEGAL ASSESSMENT\ntext"),
        ]
        repo = self._repo(chunks)
        stats = _stats()
        _classify_celex(repo, None, "CELEX-TEST", dry_run=False, stats=stats)

        written_ids = {call.args[0] for call in repo.update_section_type.call_args_list}
        self.assertEqual(written_ids, {"c2", "c3"})
        self.assertEqual(stats["llm_pending"], 2)
        self.assertEqual(stats["defaulted_other"], 0)

    def test_llm_fallback_labels_unresolved_chunks(self) -> None:
        chunks = [
            _chunk("c0", 1, "unheaded leading text"),
            _chunk("c1", 2, "# I. THE FACTS\ntext"),
            _chunk("c2", 3, "# II. LEGAL ASSESSMENT\ntext"),
        ]
        repo = self._repo(chunks)
        classifier = _StubClassifier(SectionType.RECITALS_FACTS)
        stats = _stats()
        _classify_celex(repo, classifier, "CELEX-TEST", dry_run=False, stats=stats)

        written = {
            call.args[0]: call.args[1]
            for call in repo.update_section_type.call_args_list
        }
        self.assertEqual(written["c0"], "recitals_facts")
        self.assertEqual(stats["llm_labeled"], 1)
        self.assertEqual(len(classifier.calls), 1)

    def test_gate_rejects_out_of_order_document(self) -> None:
        """OCR-like random heading order: no rule labels are persisted."""
        chunks = [
            _chunk("g0", 1, "HAS ADOPTED THIS DECISION:"),
            _chunk("g1", 2, "# THE PARTIES\ntext"),
            _chunk("g2", 3, "# 3. FINAL AMOUNT OF THE FINES\ntext"),
            _chunk("g3", 4, "# I. THE FACTS\ntext"),
            _chunk("g4", 5, "HAS ADOPTED THIS DECISION:"),
            _chunk("g5", 6, "# THE PARTIES\ntext"),
        ]
        repo = self._repo(chunks)
        stats = _stats()
        _classify_celex(repo, None, "CELEX-TEST", dry_run=False, stats=stats)

        repo.update_section_type.assert_not_called()
        self.assertEqual(stats["gate_rejected"], 1)
        self.assertEqual(stats["llm_pending"], len(chunks))
        self.assertEqual(stats["rule_labeled"], 0)

    def test_empty_document_is_a_noop(self) -> None:
        repo = self._repo([])
        stats = _stats()
        _classify_celex(repo, None, "CELEX-TEST", dry_run=False, stats=stats)
        repo.update_section_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
