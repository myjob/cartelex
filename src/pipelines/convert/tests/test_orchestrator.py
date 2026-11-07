"""Tests for the conversion orchestrator with fake client, repository and clock."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from pipelines.convert.mineru_client import MineruTaskNotFound
from pipelines.convert.models import (
    ConversionItem,
    MineruError,
    MineruFile,
    MineruParse,
    MineruTask,
    MineruUpload,
)
from pipelines.convert.orchestrator import ConversionMode, ConversionOrchestrator
from pipelines.convert.result_unpacker import ResultInvalid, ResultMetrics
from pipelines.download.descriptor_store import MasterDescriptorStore

GERMAN = (
    "Die Kommission stellt fest, dass die Unternehmen an einer Zuwiderhandlung "
    "gegen Artikel 101 des Vertrags beteiligt waren. Die Unternehmen haben "
    "Preise abgesprochen und Marktanteile untereinander aufgeteilt, was den "
    "Wettbewerb im Binnenmarkt erheblich beeintraechtigt hat. "
) * 4
METRICS = ResultMetrics(md_chars=10, image_count=1, table_count=2, formula_count=3, pdf_pages=7)


class FakeClock:
    """Manual clock; sleeping advances it."""

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += self.step


class FakeRepository:
    """Records language writes and audit rows."""

    def __init__(self) -> None:
        self.languages: list[tuple[str, str]] = []
        self.audits: list = []

    def set_language(self, document_id: str, language: str) -> None:
        self.languages.append((document_id, language))

    def record(self, audit) -> None:
        self.audits.append(audit)


class FakeClient:
    """Scripted MinerU client; per-task behaviour via ``polls``."""

    def __init__(self, statuses=None, upload_files: bool = False) -> None:
        self.statuses = statuses or {}
        self.upload_files = upload_files
        self.created: list[list[str]] = []
        self.cancelled: list[str] = []
        self.uploaded = 0
        self.poll_script: list = []
        self.polls = 0
        self.create_error: Exception | None = None
        self.status_fn = None
        self.upload_errors: list = []
        self.drop_files = 0

    def create_task(self, files, tier):
        if self.create_error:
            raise self.create_error
        self.created.append([f.file_name for f in files])
        n = len(self.created)
        files = files[:len(files) - self.drop_files] if self.drop_files else files
        upload = MineruUpload(url="http://u", expires_at=1) if self.upload_files else None
        return MineruTask(
            task_id=f"t{n}", status="running",
            files=[MineruFile(file_id=f"t{n}f{i}", file_name=f.file_name,
                              status="waiting-file", upload=upload)
                   for i, f in enumerate(files)])

    def upload(self, upload, data) -> None:
        self.uploaded += 1
        if self.upload_errors:
            step = self.upload_errors.pop(0)
            if step is not None:
                raise step

    def get_task(self, task_id):
        self.polls += 1
        step = self.poll_script.pop(0) if self.poll_script else None
        if isinstance(step, BaseException):
            raise step
        n = int(task_id[1:])
        names = self.created[n - 1][:len(self.created[n - 1]) - self.drop_files]
        files = []
        for i, name in enumerate(names):
            status = self.statuses.get(f"{task_id}f{i}", "completed")
            if self.status_fn:
                status = self.status_fn(task_id, i, self.polls)
            if status == "completed":
                files.append(MineruFile(
                    file_id=f"{task_id}f{i}", file_name=name, status=status,
                    result_url=f"http://r/{task_id}f{i}",
                    parse=MineruParse(tier="advanced", duration_ms=500, parser_version="9")))
            elif status == "failed":
                files.append(MineruFile(
                    file_id=f"{task_id}f{i}", file_name=name, status=status,
                    error=MineruError(code="parse_error", message="bad")))
            else:
                files.append(MineruFile(file_id=f"{task_id}f{i}", file_name=name, status=status))
        return MineruTask(task_id=task_id, status="running", files=files)

    def cancel_task(self, task_id) -> None:
        self.cancelled.append(task_id)

    def download_result(self, result_url) -> bytes:
        return b"zip"


class OrchestratorTest(unittest.TestCase):
    """Run-loop behaviour."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.master_path = self.tmp / "master.json"
        self.repo = FakeRepository()
        self.clock = FakeClock()
        self.client = FakeClient()
        place = patch("pipelines.convert.orchestrator.place_result", side_effect=self._place)
        self.place = place.start()
        self.addCleanup(place.stop)

    def _place(self, zip_bytes: bytes, target: Path) -> ResultMetrics:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n\n".join([GERMAN] * 3), encoding="utf-8")
        return METRICS

    def _master(self, documents: dict) -> MasterDescriptorStore:
        self.master_path.write_text(json.dumps({"documents": documents}), encoding="utf-8")
        return MasterDescriptorStore(self.master_path)

    def _orch(self, master=None, **overrides) -> ConversionOrchestrator:
        kwargs = dict(
            api_url="http://mineru", tier="advanced", files_per_task=10,
            file_timeout_s=100.0, poll_interval_s=1.0, heartbeat_s=60.0,
            max_connection_errors=3, clock=self.clock, sleep=self.clock.sleep)
        kwargs.update(overrides)
        return ConversionOrchestrator(
            self.client, self.repo, master or self._master({}), uuid4(), **kwargs)

    def _item(self, name: str, *, md: bool = True, pdf: bool = True, language=None,
              md_exists: bool = False) -> ConversionItem:
        pdf_path = self.tmp / f"{name}.pdf"
        if pdf:
            pdf_path.write_bytes(b"%PDF-1.4 content")
        md_path = self.tmp / "mineru" / f"{name}.md"
        if md_exists:
            md_path.parent.mkdir(exist_ok=True)
            md_path.write_text("old", encoding="utf-8")
        return ConversionItem(
            document_id=name, decision_id=name, is_source=True,
            local_file_pdf=str(pdf_path), local_file_md=str(md_path) if md else None,
            language=language)

    def test_lazy_skips_existing_markdown(self) -> None:
        summary = self._orch().run(
            [self._item("a", md_exists=True)], ConversionMode.LAZY, dry_run=False)
        self.assertEqual(summary.counts, {"skipped_exists": 1})
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.repo.audits, [])

    def test_force_resubmits_existing(self) -> None:
        summary = self._orch().run(
            [self._item("a", md_exists=True)], ConversionMode.FORCE, dry_run=False)
        self.assertEqual(summary.counts, {"converted": 1})
        self.assertEqual(len(self.client.created), 1)

    def test_no_target_path_audited(self) -> None:
        summary = self._orch().run([self._item("a", md=False)], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"no_target_path": 1})
        self.assertEqual(self.repo.audits[0].outcome.value, "no_target_path")
        self.assertEqual(summary.exit_code(), 1)

    def test_pdf_missing_audited(self) -> None:
        missing = self._item("a", pdf=False)
        bad = self._item("b")
        Path(bad.local_file_pdf).write_bytes(b"<html>")
        summary = self._orch().run([missing, bad], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"pdf_missing": 2})
        self.assertEqual(self.client.created, [])

    def test_converted_flow_writes_registry_then_master(self) -> None:
        item = self._item("a")
        master = self._master({"a": {"local_file_pdf": item.local_file_pdf}})
        summary = self._orch(master).run([item], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"converted": 1})
        self.place.assert_called_once_with(b"zip", Path(item.local_file_md))
        self.assertEqual(self.repo.languages, [("a", "DE")])
        self.assertEqual(master.markdown_entry("a")[1], item.local_file_md)
        saved = json.loads(self.master_path.read_text(encoding="utf-8"))["documents"]["a"]
        self.assertEqual(saved["language_init"], "de")
        audit = self.repo.audits[0]
        self.assertEqual(audit.outcome.value, "converted")
        self.assertEqual(audit.server_duration_ms, 500)
        self.assertGreaterEqual(audit.wall_duration_ms, 0)
        self.assertEqual((audit.pdf_pages, audit.md_chars, audit.table_count), (7, 10, 2))
        self.assertEqual(audit.language_action.value, "set")
        self.assertEqual(audit.mineru_api_url, "http://mineru")
        self.assertEqual(audit.mineru_task_id, "t1")

    def test_candidate_without_master_entry_touches_registry_only(self) -> None:
        item = self._item("a")
        master = self._master({"a": {"local_file_pdf": "/other.pdf"}})
        self._orch(master).run([item], ConversionMode.LAZY, False)
        self.assertEqual(self.repo.languages, [("a", "DE")])
        self.assertIsNone(master.markdown_entry("a")[1])

    def test_failed_file_audits_error_code(self) -> None:
        self.client.statuses = {"t1f0": "failed"}
        summary = self._orch().run([self._item("a")], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"failed": 1})
        self.assertEqual(summary.failure_reasons, {"parse_error": 1})
        self.assertEqual(self.repo.audits[0].error_message, "bad")

    def test_result_invalid_audited(self) -> None:
        self.place.side_effect = ResultInvalid("corrupt")
        summary = self._orch().run([self._item("a")], ConversionMode.LAZY, False)
        self.assertEqual(summary.failure_reasons, {"result_invalid": 1})

    def test_unexpected_error_audited_and_run_continues(self) -> None:
        calls = {"n": 0}

        def flaky(zip_bytes: bytes, target: Path) -> ResultMetrics:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return self._place(zip_bytes, target)

        self.place.side_effect = flaky
        with self.assertLogs("pipelines.convert.orchestrator", "ERROR"):
            summary = self._orch().run(
                [self._item("a"), self._item("b")], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"failed": 1, "converted": 1})
        reason = self.repo.audits[0].failure_reason
        self.assertTrue(reason.startswith("unexpected_error: RuntimeError"))

    def test_timeout_audits_and_cancels_after_others_terminal(self) -> None:
        self.client.statuses = {"t1f1": "running"}
        summary = self._orch(file_timeout_s=3.0).run(
            [self._item("a"), self._item("b")], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"converted": 1, "timeout": 1})
        self.assertEqual(self.client.cancelled, ["t1"])
        self.assertEqual(summary.exit_code(), 1)

    def test_task_not_found_fails_unfinished_and_continues(self) -> None:
        self.client.poll_script = [MineruTaskNotFound("t1")]
        summary = self._orch(files_per_task=1).run(
            [self._item("a"), self._item("b")], ConversionMode.LAZY, False)
        self.assertEqual(summary.failure_reasons, {"task_not_found": 1})
        self.assertEqual(summary.counts, {"failed": 1, "converted": 1})
        self.assertEqual(len(self.client.created), 2)

    def test_consecutive_connection_errors_abort(self) -> None:
        self.client.poll_script = [httpx.ConnectError("down")] * 3
        master = self._master({})
        with patch.object(master, "flush") as flush:
            summary = self._orch(master, files_per_task=1).run(
                [self._item("a"), self._item("b")], ConversionMode.LAZY, False)
        self.assertTrue(summary.aborted)
        self.assertEqual(summary.exit_code(), 1)
        self.assertEqual(summary.failure_reasons, {"connection_error": 1})
        self.assertEqual(len(self.client.created), 1)
        flush.assert_called_once()

    def test_connection_error_counter_resets_on_success(self) -> None:
        self.client.poll_script = [
            httpx.ConnectError("x"), httpx.ConnectError("x"), None,
            httpx.ConnectError("x"), httpx.ConnectError("x")]
        self.client.statuses = {"t1f0": "running"}
        orch = self._orch(file_timeout_s=8.0)
        summary = orch.run([self._item("a")], ConversionMode.LAZY, False)
        self.assertFalse(summary.aborted)
        self.assertEqual(summary.counts, {"timeout": 1})

    def test_dry_run_submits_nothing_writes_nothing(self) -> None:
        master = self._master({})
        with patch.object(master, "flush") as flush:
            summary = self._orch(master).run(
                [self._item("a"), self._item("b", md=False)], ConversionMode.LAZY, True)
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.repo.audits, [])
        self.assertEqual(summary.exit_code(), 0)
        flush.assert_not_called()

    def test_files_per_task_batches_items(self) -> None:
        items = [self._item(f"d{i:02d}") for i in range(25)]
        summary = self._orch(files_per_task=10).run(items, ConversionMode.LAZY, False)
        self.assertEqual([len(c) for c in self.client.created], [10, 10, 5])
        self.assertEqual(summary.counts, {"converted": 25})

    def test_upload_performed_for_files_with_link(self) -> None:
        self.client.upload_files = True
        self._orch().run([self._item("a")], ConversionMode.LAZY, False)
        self.assertEqual(self.client.uploaded, 1)

    def test_heartbeat_emitted_while_waiting(self) -> None:
        self.clock.step = 30.0
        self.client.statuses = {"t1f0": "running"}
        orch = self._orch(file_timeout_s=200.0, heartbeat_s=60.0)
        with self.assertLogs("pipelines.convert.orchestrator", "INFO") as logs:
            orch.run([self._item("a")], ConversionMode.LAZY, False)
        beats = [r for r in logs.records if "task t1" in r.getMessage()]
        self.assertGreaterEqual(len(beats), 3)

    def test_deadline_starts_when_running_not_while_queued(self) -> None:
        """A file queued longer than the timeout still converts once it runs."""
        def status(task_id: str, i: int, polls: int) -> str:
            if i < 2:
                return "running" if polls < 5 else "completed"
            if polls < 120:
                return "pending"
            return "running" if polls < 125 else "completed"

        self.client.status_fn = status
        summary = self._orch(files_per_task=3, file_timeout_s=100.0).run(
            [self._item("a"), self._item("b"), self._item("c")],
            ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"converted": 3})
        self.assertEqual(self.client.cancelled, [])

    def test_queue_wait_cap_times_out_files_never_running(self) -> None:
        """Files still pending at files_per_task * timeout are audited timeout."""
        self.client.statuses = {"t1f1": "pending"}
        summary = self._orch(files_per_task=2, file_timeout_s=3.0).run(
            [self._item("a"), self._item("b")], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"converted": 1, "timeout": 1})
        self.assertEqual(self.client.cancelled, ["t1"])

    def test_stuck_running_file_times_out_while_other_converts(self) -> None:
        """One timeout, one converted, cancel once after both are terminal."""
        self.client.statuses = {"t1f0": "running"}
        summary = self._orch(file_timeout_s=3.0).run(
            [self._item("a"), self._item("b")], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"converted": 1, "timeout": 1})
        self.assertEqual(self.client.cancelled, ["t1"])
        self.assertEqual(self.repo.audits[0].outcome.value, "converted")

    def test_abort_during_upload_audits_all_and_cancels(self) -> None:
        """A connection-error abort mid-upload leaves no unaudited file."""
        self.client.upload_files = True
        err = httpx.ConnectError("down")
        self.client.upload_errors = [None, err, err]
        summary = self._orch(max_connection_errors=2).run(
            [self._item(n) for n in "abcd"], ConversionMode.LAZY, False)
        self.assertTrue(summary.aborted)
        self.assertEqual(summary.counts, {"failed": 4})
        self.assertEqual(summary.failure_reasons, {"connection_error": 4})
        self.assertEqual(sorted(a.document_id for a in self.repo.audits), list("abcd"))
        self.assertEqual(self.client.cancelled, ["t1"])

    def test_master_flushed_after_each_converted_file(self) -> None:
        """A kill after file one keeps its master entry on disk."""
        a, b = self._item("a"), self._item("b")
        master = self._master({"a": {"local_file_pdf": a.local_file_pdf},
                               "b": {"local_file_pdf": b.local_file_pdf}})
        seen: dict = {}

        def killer(zip_bytes: bytes, target: Path) -> ResultMetrics:
            if target.stem == "b":
                seen["flushes"] = flush.call_count
                seen["disk"] = json.loads(
                    self.master_path.read_text(encoding="utf-8"))["documents"]["a"]
                raise KeyboardInterrupt
            return self._place(zip_bytes, target)

        self.place.side_effect = killer
        with patch.object(master, "flush", wraps=master.flush) as flush:
            with self.assertRaises(KeyboardInterrupt):
                self._orch(master).run([a, b], ConversionMode.LAZY, False)
        self.assertEqual(seen["flushes"], 1)
        self.assertIs(seen["disk"]["mineru_flag"], True)
        self.assertEqual(flush.call_count, 2)

    def test_lazy_reconciles_master_for_existing_markdown(self) -> None:
        """Existing md plus an unflagged master entry is repaired, not converted."""
        item = self._item("a", md_exists=True)
        master = self._master({"a": {"local_file_pdf": item.local_file_pdf,
                                     "mineru_flag": False}})
        with self.assertLogs("pipelines.convert.orchestrator", "INFO") as logs:
            summary = self._orch(master).run([item], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"skipped_exists": 1})
        self.assertIs(master.mineru_flag("a"), True)
        self.assertEqual(master.markdown_entry("a")[1], item.local_file_md)
        saved = json.loads(self.master_path.read_text(encoding="utf-8"))["documents"]["a"]
        self.assertIs(saved["mineru_flag"], True)
        self.assertTrue(any("reconciled master" in m for m in logs.output))
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.repo.audits, [])

    def test_candidate_sharing_source_pdf_is_skipped(self) -> None:
        """A candidate pointing at its source's PDF is never converted (ISSUES 20261031)."""
        source = self._item("x")
        phantom = source.model_copy(update={
            "document_id": "x_EN", "is_source": False,
            "local_file_md": str(self.tmp / "mineru" / "x_EN.md"),
            "source_pdf": source.local_file_pdf})
        summary = self._orch().run([phantom], ConversionMode.FORCE, False)
        self.assertEqual(summary.counts, {"skipped_shares_source_pdf": 1})
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.repo.audits, [])
        self.assertEqual(summary.exit_code(), 0)

    def test_translated_source_converts_into_original_not_english(self) -> None:
        """Force-converting a translated decision never overwrites the English file."""
        base = self._item("a")
        english = self.tmp / "mineru" / "a_EN.md"
        english.parent.mkdir(exist_ok=True)
        english.write_text("english", encoding="utf-8")
        item = base.model_copy(update={"local_file_md": str(english), "translated": True})
        master = self._master({"a": {"local_file_pdf": item.local_file_pdf,
                                     "txt_md_file": str(english)}})
        summary = self._orch(master).run([item], ConversionMode.FORCE, False)
        self.assertEqual(summary.counts, {"converted": 1})
        self.place.assert_called_once_with(b"zip", self.tmp / "mineru" / "a.md")
        self.assertEqual(english.read_text(encoding="utf-8"), "english")
        self.assertEqual(master.markdown_entry("a")[1], str(english))

    def test_translated_source_uses_stored_original(self) -> None:
        """A stored original_file_md is the conversion target."""
        original = self.tmp / "orig" / "a.md"
        item = self._item("a").model_copy(update={
            "local_file_md": str(self.tmp / "mineru" / "a_EN.md"),
            "original_file_md": str(original), "translated": True})
        self._orch().run([item], ConversionMode.FORCE, False)
        self.place.assert_called_once_with(b"zip", original)

    def test_master_synced_only_from_source_row(self) -> None:
        """A converted candidate never rewrites the master entry."""
        item = self._item("a").model_copy(update={"is_source": False})
        master = self._master({"a": {"local_file_pdf": item.local_file_pdf,
                                     "txt_md_file": "/m/a_en.md"}})
        summary = self._orch(master).run([item], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"converted": 1})
        self.assertEqual(master.markdown_entry("a")[1], "/m/a_en.md")

    def test_reconcile_ignores_non_source_rows(self) -> None:
        """Lazy reconcile never points master at a candidate's markdown."""
        item = self._item("a", md_exists=True).model_copy(update={"is_source": False})
        master = self._master({"a": {"local_file_pdf": item.local_file_pdf,
                                     "txt_md_file": "/m/a_en.md", "mineru_flag": False}})
        self._orch(master).run([item], ConversionMode.LAZY, False)
        self.assertEqual(master.markdown_entry("a")[1], "/m/a_en.md")
        self.assertIs(master.mineru_flag("a"), False)

    def test_lazy_reconcile_skips_true_none_or_foreign_entries(self) -> None:
        """No write for flag True, flag absent (legacy) or another PDF."""
        flagged = self._item("a", md_exists=True)
        legacy = self._item("c", md_exists=True)
        foreign = self._item("b", md_exists=True)
        master = self._master({
            "a": {"local_file_pdf": flagged.local_file_pdf, "mineru_flag": True},
            "c": {"local_file_pdf": legacy.local_file_pdf},
            "b": {"local_file_pdf": "/other.pdf", "mineru_flag": False}})
        with patch.object(master, "set_markdown") as set_markdown:
            self._orch(master).run([flagged, legacy, foreign], ConversionMode.LAZY, False)
        set_markdown.assert_not_called()

    def test_dry_run_reconcile_only_logs(self) -> None:
        """Dry run reports the reconcile and writes nothing."""
        item = self._item("a", md_exists=True)
        master = self._master({"a": {"local_file_pdf": item.local_file_pdf,
                                     "mineru_flag": False}})
        with self.assertLogs("pipelines.convert.orchestrator", "INFO") as logs:
            self._orch(master).run([item], ConversionMode.LAZY, True)
        self.assertIs(master.mineru_flag("a"), False)
        self.assertTrue(any("Would reconcile master" in m for m in logs.output))

    def test_limit_caps_submissions_after_classification(self) -> None:
        """Skips and pre-decided items do not count toward the limit."""
        items = [self._item("x", md_exists=True), self._item("n", md=False),
                 self._item("a"), self._item("b"), self._item("c")]
        summary = self._orch().run(items, ConversionMode.LAZY, False, limit=2)
        self.assertEqual(self.client.created, [["a.pdf", "b.pdf"]])
        self.assertEqual(summary.counts,
                         {"skipped_exists": 1, "no_target_path": 1, "converted": 2})

    def test_limit_applies_to_dry_run_log(self) -> None:
        """The dry run lists only the capped items."""
        items = [self._item(n) for n in "abcde"]
        with self.assertLogs("pipelines.convert.orchestrator", "INFO") as logs:
            self._orch().run(items, ConversionMode.LAZY, True, limit=3)
        self.assertEqual(sum("Would convert" in m for m in logs.output), 3)

    def test_master_language_filled_even_when_registry_already_set(self) -> None:
        """Registry language set elsewhere does not block master language_init."""
        item = self._item("a", language="EN")
        master = self._master({"a": {"local_file_pdf": item.local_file_pdf}})
        self._orch(master).run([item], ConversionMode.LAZY, False)
        self.assertEqual(self.repo.languages, [])
        saved = json.loads(self.master_path.read_text(encoding="utf-8"))["documents"]["a"]
        self.assertEqual(saved["language_init"], "de")

    def test_flush_failure_raises_on_success_but_not_over_another_error(self) -> None:
        """A flush error surfaces on a clean run and is only logged otherwise."""
        master = self._master({})
        with patch.object(master, "flush", side_effect=OSError("disk")):
            with self.assertLogs("pipelines.convert.orchestrator", "ERROR"):
                with self.assertRaises(OSError):
                    self._orch(master).run([self._item("a")], ConversionMode.LAZY, False)
            self.place.side_effect = KeyboardInterrupt
            with self.assertLogs("pipelines.convert.orchestrator", "ERROR"):
                with self.assertRaises(KeyboardInterrupt):
                    self._orch(master).run([self._item("b")], ConversionMode.LAZY, False)

    def test_fewer_files_than_submitted_audits_file_missing(self) -> None:
        """Unmatched items are audited failed/file_missing, no crash."""
        self.client.drop_files = 1
        summary = self._orch().run(
            [self._item("a"), self._item("b")], ConversionMode.LAZY, False)
        self.assertEqual(summary.counts, {"converted": 1, "failed": 1})
        self.assertEqual(summary.failure_reasons, {"file_missing": 1})

    def test_keyboard_interrupt_while_polling_cancels_task(self) -> None:
        """Ctrl-C cancels the in-flight task and propagates."""
        self.client.poll_script = [KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self._orch().run([self._item("a")], ConversionMode.LAZY, False)
        self.assertEqual(self.client.cancelled, ["t1"])


if __name__ == "__main__":
    unittest.main()
