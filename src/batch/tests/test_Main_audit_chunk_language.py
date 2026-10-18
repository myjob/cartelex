"""Unit tests for the chunk-language audit batch (DB mocked).

Mirrors ``test_Main_classify_sections.py``: the ChunkRepository is a MagicMock
at the DB boundary, and the assertions target the stats counters, the read
calls, and the report frame. No test touches ``settings.REPORT_PATH``.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import polars as pl

from batch.Main_audit_chunk_language import (
    _REPORT_COLUMNS,
    _audit_celex,
    _write_report,
    run,
)
from pipelines.legal.language_audit import (
    LanguageVerdict,
    aggregate_document_language,
    ChunkLanguageObservation,
    ObservationStatus,
)

_ENGLISH_PARAGRAPH = (
    "The Commission found that the undertakings concerned participated in a single "
    "and continuous infringement of Article 101 of the Treaty covering the entire "
    "territory of the European Economic Area, consisting of price coordination and "
    "the allocation of customers between the cartel members over several years. "
)
_GERMAN_PARAGRAPH = (
    "Die Kommission stellte fest, dass die betroffenen Unternehmen an einer "
    "einheitlichen und fortgesetzten Zuwiderhandlung gegen Artikel 101 des Vertrages "
    "teilgenommen haben, die das gesamte Gebiet des Europaeischen Wirtschaftsraums "
    "umfasste und Preisabsprachen sowie Marktaufteilungen zum Gegenstand hatte. "
)


def _chunk(chunk_id: str, doc_pos: int, document: str) -> dict:
    return {"id": chunk_id, "doc_pos": doc_pos, "document": document}


def _stats() -> dict[str, int]:
    return {
        "documents": 0,
        "chunks": 0,
        "clean": 0,
        "flagged_review": 0,
        "flagged_non_english": 0,
        "insufficient": 0,
        "empty": 0,
        "errors": 0,
    }


_ENGLISH_CHUNKS = [
    _chunk(f"c{i}", i, _ENGLISH_PARAGRAPH * 4) for i in range(6)
]
_GERMAN_CHUNKS = [_chunk(f"g{i}", i, _GERMAN_PARAGRAPH * 4) for i in range(6)]


def _audit(celex_id: str, verdict_share: float) -> object:
    """Build a DocumentLanguageAudit with a chosen char share, for report tests."""
    foreign = int(100_000 * verdict_share)
    observations = [
        ChunkLanguageObservation(
            doc_pos=0,
            char_count=foreign,
            language="de",
            probability=0.99,
            status=ObservationStatus.DETECTED,
        ),
        ChunkLanguageObservation(
            doc_pos=1,
            char_count=100_000 - foreign,
            language="en",
            probability=0.99,
            status=ObservationStatus.DETECTED,
        ),
    ]
    return aggregate_document_language(celex_id, observations, "de", "en")


class TestAuditCelex(unittest.TestCase):
    def _repo(self, chunks: list[dict]) -> MagicMock:
        repo = MagicMock()
        repo.select_chunks_for_language_audit.return_value = chunks
        return repo

    def test_english_document_counts_as_clean(self) -> None:
        repo = self._repo(_ENGLISH_CHUNKS)
        stats = _stats()
        audit = _audit_celex(repo, "CELEX-TEST", {}, stats)
        self.assertIsNotNone(audit)
        self.assertIs(audit.verdict, LanguageVerdict.ENGLISH)
        self.assertEqual(stats["clean"], 1)
        self.assertEqual(stats["documents"], 1)
        self.assertEqual(stats["chunks"], len(_ENGLISH_CHUNKS))

    def test_german_document_is_flagged(self) -> None:
        repo = self._repo(_GERMAN_CHUNKS)
        stats = _stats()
        audit = _audit_celex(repo, "39452_3336_8", {}, stats)
        self.assertIs(audit.verdict, LanguageVerdict.NON_ENGLISH)
        self.assertEqual(audit.dominant_non_english, "de")
        self.assertEqual(stats["flagged_non_english"], 1)

    def test_descriptor_languages_are_attached(self) -> None:
        repo = self._repo(_ENGLISH_CHUNKS)
        audit = _audit_celex(
            repo, "CELEX-TEST", {"CELEX-TEST": ("de", "en")}, _stats()
        )
        self.assertEqual(audit.language_init, "de")
        self.assertEqual(audit.language_translated, "en")

    def test_empty_document_is_a_noop(self) -> None:
        repo = self._repo([])
        stats = _stats()
        self.assertIsNone(_audit_celex(repo, "CELEX-EMPTY", {}, stats))
        self.assertEqual(stats["documents"], 0)

    def test_audit_never_writes(self) -> None:
        repo = self._repo(_GERMAN_CHUNKS)
        _audit_celex(repo, "39452_3336_8", {}, _stats())
        repo.update_section_type.assert_not_called()
        repo.update_chunk_context.assert_not_called()


class TestRun(unittest.TestCase):
    def _repo(self) -> MagicMock:
        repo = MagicMock()
        repo.select_celex_ids_all.return_value = ["A", "B", "C"]
        repo.select_chunks_for_language_audit.return_value = _ENGLISH_CHUNKS
        return repo

    def test_dry_run_writes_no_report(self) -> None:
        repo = self._repo()
        with (
            patch("batch.Main_audit_chunk_language.ChunkRepository", return_value=repo),
            patch("batch.Main_audit_chunk_language._write_report") as write_report,
        ):
            run(dry_run=True)
        write_report.assert_not_called()

    def test_full_run_writes_a_report(self) -> None:
        repo = self._repo()
        with (
            patch("batch.Main_audit_chunk_language.ChunkRepository", return_value=repo),
            patch("batch.Main_audit_chunk_language._write_report") as write_report,
        ):
            run()
        write_report.assert_called_once()
        self.assertEqual(len(write_report.call_args.args[0]), 3)

    def test_celex_filter_skips_the_corpus_query(self) -> None:
        repo = self._repo()
        with (
            patch("batch.Main_audit_chunk_language.ChunkRepository", return_value=repo),
            patch("batch.Main_audit_chunk_language._write_report"),
        ):
            run(celex_filter="A")
        repo.select_celex_ids_all.assert_not_called()
        repo.select_chunks_for_language_audit.assert_called_once_with("A")

    def test_limit_truncates_the_document_list(self) -> None:
        repo = self._repo()
        with (
            patch("batch.Main_audit_chunk_language.ChunkRepository", return_value=repo),
            patch("batch.Main_audit_chunk_language._write_report"),
        ):
            run(limit=2)
        self.assertEqual(repo.select_chunks_for_language_audit.call_count, 2)

    def test_failing_document_is_counted_and_the_run_continues(self) -> None:
        repo = self._repo()
        repo.select_chunks_for_language_audit.side_effect = [
            _ENGLISH_CHUNKS,
            RuntimeError("connection reset"),
            _ENGLISH_CHUNKS,
        ]
        with (
            patch("batch.Main_audit_chunk_language.ChunkRepository", return_value=repo),
            patch("batch.Main_audit_chunk_language._write_report") as write_report,
            self.assertLogs("audit_chunk_language", level="ERROR"),
        ):
            run()
        # Two documents survived the failure and reached the report.
        self.assertEqual(len(write_report.call_args.args[0]), 2)

    def test_verdicts_are_logged(self) -> None:
        repo = self._repo()
        repo.select_celex_ids_all.return_value = ["39452_3336_8"]
        repo.select_chunks_for_language_audit.return_value = _GERMAN_CHUNKS
        with (
            patch("batch.Main_audit_chunk_language.ChunkRepository", return_value=repo),
            patch("batch.Main_audit_chunk_language._write_report"),
            self.assertLogs("audit_chunk_language", level="INFO") as captured,
        ):
            run()
        self.assertTrue(
            any("39452_3336_8" in line and "non_english" in line for line in captured.output)
        )


class TestWriteReport(unittest.TestCase):
    def test_report_columns_and_ordering(self) -> None:
        audits = [_audit("LOW", 0.01), _audit("HIGH", 0.99), _audit("MID", 0.40)]
        with tempfile.TemporaryDirectory() as directory:
            path = _write_report(audits, directory)
            self.assertIsNotNone(path)
            frame = pl.read_csv(path)
        self.assertEqual(frame.columns, list(_REPORT_COLUMNS))
        self.assertEqual(frame["celex_id"].to_list(), ["HIGH", "MID", "LOW"])

    def test_report_creates_a_missing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "reports"
            path = _write_report([_audit("ONLY", 0.5)], target)
            self.assertTrue(path.exists())

    def test_no_audits_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(_write_report([], directory))
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
