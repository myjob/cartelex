"""
Unit tests for the markdown-to-pgvector batch's source-document wiring.

Run from ``src/``:
    ../.venv/bin/python -m unittest batch.tests.test_Main_ingest_case_markdown2pgvector
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from batch.Main_ingest_case_markdown2pgvector import IngestState, process_single_celex
from pipelines.rag.pgvector.source_document_resolver import SourceDocumentResolver


class TestProcessSingleCelexDocumentId(unittest.TestCase):
    """``process_single_celex`` resolves the source document and passes it on."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.md = Path(self._tmp.name) / "doc.md"
        self.md.write_text("# x\n", encoding="utf-8")
        self.master = {"documents": {"C1": {"txt_md_file": str(self.md)}}}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolved_document_id_is_passed_to_ingest_doc(self) -> None:
        ingestor = MagicMock()
        resolver = SourceDocumentResolver({"C1": "C1_EN"}.get)
        self.assertTrue(process_single_celex("C1", (ingestor, self.master, resolver, IngestState.force())))
        ingestor.ingest_doc.assert_called_once_with("C1", str(self.md), document_id="C1_EN")

    def test_unregistered_decision_passes_null(self) -> None:
        ingestor = MagicMock()
        resolver = SourceDocumentResolver(lambda decision_id: None)
        process_single_celex("C1", (ingestor, self.master, resolver, IngestState.force()))
        ingestor.ingest_doc.assert_called_once_with("C1", str(self.md), document_id=None)


class TestLazyMode(unittest.TestCase):
    """Lazy mode never re-chunks a decision that already has chunks."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.md = Path(self._tmp.name) / "C1_en.md"
        self.md.write_text("# x\n", encoding="utf-8")
        self.master = {"documents": {"C1": {"txt_md_file": str(self.md)},
                                     "C2": {"txt_md_file": str(self.md)}}}
        self.resolver = SourceDocumentResolver(lambda decision_id: None)

    def test_lazy_skips_ingested_decision(self) -> None:
        ingestor = MagicMock()
        state = IngestState(ingested_paths={"C1": {"/old/location/C1_en.md"}})
        self.assertTrue(process_single_celex("C1", (ingestor, self.master, self.resolver, state)))
        ingestor.ingest_doc.assert_not_called()

    def test_lazy_ingests_new_decision(self) -> None:
        ingestor = MagicMock()
        state = IngestState(ingested_paths={"C1": {"/x/C1_en.md"}})
        process_single_celex("C2", (ingestor, self.master, self.resolver, state))
        ingestor.ingest_doc.assert_called_once()

    def test_force_reingests(self) -> None:
        ingestor = MagicMock()
        process_single_celex("C1", (ingestor, self.master, self.resolver, IngestState.force()))
        ingestor.ingest_doc.assert_called_once()

    def test_lazy_warns_when_markdown_changed_since_ingest(self) -> None:
        """Same file name under another folder is no change; another file name is."""
        ingestor = MagicMock()
        state = IngestState(ingested_paths={"C1": {"/x/C1_de.md"}})
        with self.assertLogs("batch.Main_ingest_case_markdown2pgvector", "WARNING") as logs:
            process_single_celex("C1", (ingestor, self.master, self.resolver, state))
        self.assertTrue(any("markdown changed since ingest" in m for m in logs.output))
        ingestor.ingest_doc.assert_not_called()

    def test_lazy_same_file_other_folder_is_quiet(self) -> None:
        ingestor = MagicMock()
        state = IngestState(ingested_paths={"C1": {"/usr/old/docs/C1_en.md"}})
        with self.assertNoLogs("batch.Main_ingest_case_markdown2pgvector", "WARNING"):
            process_single_celex("C1", (ingestor, self.master, self.resolver, state))


if __name__ == "__main__":
    unittest.main()
