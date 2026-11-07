"""Unit tests for the translation run loop (temp files, real master store).

The LLM, the registry and language detection are faked at their boundaries.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest pipelines.translate.tests.test_orchestrator
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.translate.models import (
    ChunkRejected,
    TranslationConnectionError,
    TranslationItem,
)
from pipelines.translate.orchestrator import (
    TranslationMode,
    TranslationOrchestrator,
    target_path_for,
)

GERMAN = "# Entscheidung\n\nDie Kommission stellt fest.\n\nDer Antrag wird abgelehnt."


def _detect(markdown: str, document_id: str) -> tuple[str | None, float | None]:
    """Fake detector: English for translated or marked text, German otherwise."""
    if markdown.startswith(("EN:", "ENGLISH")):
        return "en", 0.9
    return "de", 0.9


class FakeTranslator:
    """Prefixes chunks with ``EN:``; scripted errors per call index."""

    def __init__(self, errors: dict[int, BaseException] | None = None) -> None:
        self.errors = errors or {}
        self.chunks: list[str] = []

    def __call__(self, markdown_chunk: str, source_language: str) -> str:
        index = len(self.chunks)
        self.chunks.append(markdown_chunk)
        if index in self.errors:
            raise self.errors[index]
        return "EN: " + markdown_chunk


class FakeSplitter:
    """Splits on blank lines like a paragraph splitter."""

    def split_text(self, text: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(page_content=p) for p in text.split("\n\n")]


class FakeRepository:
    """Records registry writes; can fail the commit after the hook ran."""

    def __init__(self) -> None:
        self.languages: list[tuple[str, str]] = []
        self.commits: list[tuple[str, str, str]] = []
        self.audits: list = []
        self.fail_after_hook: Exception | None = None

    def set_language(self, document_id: str, language: str) -> None:
        self.languages.append((document_id, language))

    def commit_translation(self, item, target_md, original_md, before_commit) -> None:
        before_commit()
        if self.fail_after_hook:
            raise self.fail_after_hook
        self.commits.append((item.document_id, target_md, original_md))

    def record(self, audit) -> None:
        self.audits.append(audit)


class OrchestratorTest(unittest.TestCase):
    """Per-decision atomic translation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.master_path = self.tmp / "master.json"
        self.repo = FakeRepository()
        self.translator = FakeTranslator()
        detect = patch("pipelines.translate.orchestrator.detect_markdown_language",
                       side_effect=_detect)
        detect.start()
        self.addCleanup(detect.stop)

    def _item(self, name: str, *, language: str | None = "DE", text: str = GERMAN,
              original: str | None = None, translated: str = "NA") -> TranslationItem:
        md = self.tmp / name / "mineru" / f"{name}.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(text, encoding="utf-8")
        return TranslationItem(
            document_id=f"{name}_DE", decision_id=name, language=language,
            local_file_md=str(md), original_file_md=original, language_translated=translated)

    def _master(self, *items: TranslationItem) -> MasterDescriptorStore:
        docs = {i.decision_id: {"txt_md_file": i.local_file_md, "language_init": "de",
                                "language_translated": "NA", "facts": "/f.json"} for i in items}
        self.master_path.write_text(json.dumps({"documents": docs}), encoding="utf-8")
        self.master = MasterDescriptorStore(self.master_path)
        return self.master

    def _run(self, items: list[TranslationItem], mode: TranslationMode = TranslationMode.LAZY,
             dry_run: bool = False, master: MasterDescriptorStore | None = None):
        orch = TranslationOrchestrator(
            self.translator, FakeSplitter(), self.repo, master or self._master(*items),
            uuid4(), model_id="ollama/qwen", heartbeat_s=60.0, max_connection_errors=3)
        return orch.run(items, mode, dry_run)

    def _saved(self, key: str) -> dict:
        return json.loads(self.master_path.read_text(encoding="utf-8"))["documents"][key]

    def test_target_path(self) -> None:
        self.assertEqual(target_path_for(Path("/d/X/mineru/X.md")), Path("/d/X/mineru/X_EN.md"))

    def test_translates_and_switches_registry_and_master(self) -> None:
        item = self._item("a")
        summary = self._run([item])
        target = target_path_for(Path(item.local_file_md))
        self.assertEqual(summary.counts, {"translated": 1})
        self.assertEqual(summary.exit_code(), 0)
        self.assertEqual(target.read_text(encoding="utf-8"),
                         "EN: # Entscheidung\n\nEN: Die Kommission stellt fest.\n\n"
                         "EN: Der Antrag wird abgelehnt.\n")
        self.assertEqual(list(target.parent.glob("*.tmp")), [])
        self.assertEqual(self.repo.commits, [("a_DE", str(target), item.local_file_md)])
        saved = self._saved("a")
        self.assertEqual(saved["txt_md_file"], str(target))
        self.assertEqual(saved["original_file_md"], item.local_file_md)
        self.assertEqual(saved["language_translated"], "en")
        self.assertEqual(saved["facts"], "/f.json")
        audit = self.repo.audits[0]
        self.assertEqual((audit.outcome.value, audit.chunk_count), ("translated", 3))
        self.assertEqual(audit.source_language, "DE")
        self.assertEqual(audit.model_id, "ollama/qwen")

    def test_chunk_failure_rolls_back_and_run_continues(self) -> None:
        a, b = self._item("a"), self._item("b")
        self.translator.errors = {1: ChunkRejected("heading count 0 != 1")}
        summary = self._run([a, b])
        self.assertEqual(summary.counts, {"failed": 1, "translated": 1})
        self.assertEqual(summary.failure_reasons, {"chunk_rejected": 1})
        self.assertFalse(target_path_for(Path(a.local_file_md)).exists())
        self.assertEqual(list(Path(a.local_file_md).parent.glob("*.tmp")), [])
        self.assertEqual(self._saved("a")["txt_md_file"], a.local_file_md)
        self.assertEqual([c[0] for c in self.repo.commits], ["b_DE"])
        self.assertEqual(self.repo.audits[0].reason, "chunk_rejected")
        self.assertEqual(summary.exit_code(), 1)

    def test_commit_failure_restores_file_and_master(self) -> None:
        item = self._item("a", original=None)
        target = target_path_for(Path(item.local_file_md))
        target.write_text("previous translation", encoding="utf-8")
        self.repo.fail_after_hook = RuntimeError("commit lost")
        summary = self._run([item], TranslationMode.FORCE)
        self.assertEqual(summary.failure_reasons, {"commit_failed": 1})
        self.assertEqual(target.read_text(encoding="utf-8"), "previous translation")
        self.assertEqual(list(target.parent.glob("*.bak")), [])
        saved = self._saved("a")
        self.assertEqual(saved["txt_md_file"], item.local_file_md)
        self.assertNotIn("original_file_md", saved)
        self.assertEqual(saved["language_translated"], "NA")

    def test_master_flush_failure_rolls_back(self) -> None:
        item = self._item("a")
        master = self._master(item)
        with patch.object(master, "flush", side_effect=OSError("disk full")):
            summary = self._run([item], master=master)
        self.assertEqual(summary.failure_reasons, {"commit_failed": 1})
        self.assertFalse(target_path_for(Path(item.local_file_md)).exists())
        self.assertEqual(self.repo.commits, [])
        self.assertEqual(master.translation_entry("a").txt_md_file, item.local_file_md)

    def test_force_uses_original_file_md(self) -> None:
        original = self._item("a")
        english = Path(original.local_file_md).with_name("a_EN.md")
        english.write_text("ENGLISH old", encoding="utf-8")
        item = original.model_copy(update={
            "local_file_md": str(english), "original_file_md": original.local_file_md,
            "language_translated": "en"})
        self._run([item], TranslationMode.FORCE)
        self.assertEqual(self.translator.chunks[0], "# Entscheidung")
        self.assertEqual(self.repo.commits, [("a_DE", str(english), original.local_file_md)])

    def test_english_input_without_original_is_not_translated(self) -> None:
        """Force on an old-pipeline English file never makes it the 'original'."""
        item = self._item("a", text="ENGLISH decision text", translated="en")
        summary = self._run([item], TranslationMode.FORCE)
        self.assertEqual(summary.counts, {"already_english": 1})
        self.assertEqual(self.translator.chunks, [])
        self.assertEqual(self.repo.commits, [])
        self.assertNotIn("original_file_md", self._saved("a"))
        self.assertFalse(target_path_for(Path(item.local_file_md)).exists())

    def test_stray_target_without_commit_is_retranslated(self) -> None:
        item = self._item("a")
        target = target_path_for(Path(item.local_file_md))
        target.write_text("half written", encoding="utf-8")
        summary = self._run([item])
        self.assertEqual(summary.counts, {"translated": 1})
        self.assertTrue(target.read_text(encoding="utf-8").startswith("EN: "))

    def test_null_language_english_is_already_english(self) -> None:
        item = self._item("a", language=None, text="ENGLISH decision text")
        summary = self._run([item])
        self.assertEqual(summary.counts, {"already_english": 1})
        self.assertEqual(self.repo.languages, [("a_DE", "EN")])
        self.assertEqual(self.translator.chunks, [])
        self.assertEqual(summary.exit_code(), 0)

    def test_null_language_german_sets_language(self) -> None:
        item = self._item("a", language=None)
        summary = self._run([item])
        self.assertEqual(summary.counts, {"translated": 1})
        self.assertEqual(self.repo.languages, [("a_DE", "DE")])
        self.assertEqual(self._saved("a")["language_init"], "de")

    def test_untranslated_result_fails_decision(self) -> None:
        item = self._item("a")
        with patch("pipelines.translate.orchestrator.detect_markdown_language",
                   return_value=("de", 0.9)):
            summary = self._run([item])
        self.assertEqual(summary.failure_reasons, {"not_english": 1})
        self.assertFalse(target_path_for(Path(item.local_file_md)).exists())

    def test_input_missing_fails(self) -> None:
        item = self._item("a")
        Path(item.local_file_md).unlink()
        summary = self._run([item])
        self.assertEqual(summary.failure_reasons, {"input_missing": 1})

    def test_unsupported_language_fails(self) -> None:
        summary = self._run([self._item("a", language="XX")])
        self.assertEqual(summary.failure_reasons, {"unsupported_language": 1})

    def test_undetectable_language_is_skipped(self) -> None:
        item = self._item("a", language=None)
        with patch("pipelines.translate.orchestrator.detect_markdown_language",
                   return_value=(None, None)):
            summary = self._run([item])
        self.assertEqual(summary.counts, {"skipped": 1})
        self.assertEqual(summary.failure_reasons, {})
        self.assertEqual(summary.exit_code(), 0)
        self.assertEqual(self.repo.audits[0].outcome.value, "skipped")
        self.assertEqual(self.repo.audits[0].reason, "language_undetermined")
        self.assertEqual(self.repo.languages, [])

    def test_aborts_after_consecutive_connection_errors(self) -> None:
        items = [self._item(n) for n in "abcd"]
        error = TranslationConnectionError("refused")
        self.translator.errors = {i: error for i in range(10)}
        summary = self._run(items)
        self.assertTrue(summary.aborted)
        self.assertEqual(summary.failure_reasons, {"connection_error": 3})
        self.assertEqual(len(self.repo.audits), 3)
        self.assertEqual(summary.exit_code(), 1)

    def test_validation_failures_do_not_abort(self) -> None:
        items = [self._item(n) for n in "abcde"]
        self.translator.errors = {i: ChunkRejected("empty") for i in range(5)}
        summary = self._run(items)
        self.assertFalse(summary.aborted)
        self.assertEqual(summary.counts, {"failed": 5})
        self.assertEqual(len(self.repo.audits), 5)

    def test_dry_run_writes_nothing(self) -> None:
        item = self._item("a")
        summary = self._run([item], dry_run=True)
        self.assertEqual(self.translator.chunks, [])
        self.assertEqual(self.repo.audits, [])
        self.assertFalse(target_path_for(Path(item.local_file_md)).exists())
        self.assertEqual(summary.counts, {})

    def test_interrupt_cleans_up_and_reraises(self) -> None:
        item = self._item("a")
        self.translator.errors = {1: KeyboardInterrupt()}
        with self.assertRaises(KeyboardInterrupt):
            self._run([item])
        self.assertEqual(list(Path(item.local_file_md).parent.glob("*.tmp")), [])
        self.assertFalse(target_path_for(Path(item.local_file_md)).exists())


if __name__ == "__main__":
    unittest.main()
