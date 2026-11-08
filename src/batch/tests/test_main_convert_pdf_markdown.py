"""Tests for the Main_convert_pdf_markdown CLI wiring (boundaries patched)."""

import signal
import unittest
from unittest.mock import MagicMock, patch

from batch import Main_convert_pdf_markdown as cli
from pipelines.convert.mineru_client import MineruUnavailable
from pipelines.convert.orchestrator import ConversionMode, ConversionSummary
from pipelines.rag.pgvector.conversion_repository import ConversionNotReady

_MOD = "batch.Main_convert_pdf_markdown"


class MainConvertPdfMarkdownTest(unittest.TestCase):
    """Exit codes and argument passing of the CLI."""

    def setUp(self) -> None:
        patches = {
            name: patch(f"{_MOD}.{name}")
            for name in ("make_mineru_client", "make_engine", "ConversionRepository",
                         "MasterDescriptorStore", "ConversionOrchestrator")
        }
        self.mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        self.client = self.mocks["make_mineru_client"].return_value
        self.engine = self.mocks["make_engine"].return_value
        self.repo = self.mocks["ConversionRepository"].return_value
        self.orchestrator = self.mocks["ConversionOrchestrator"].return_value
        self.repo.queue.return_value = [MagicMock()]
        self.orchestrator.run.return_value = ConversionSummary(
            counts={"converted": 1}, failure_reasons={}, aborted=False)

    def test_preflight_failure_exits_2(self) -> None:
        self.client.preflight.side_effect = MineruUnavailable("down")
        self.assertEqual(cli.main([]), 2)
        self.repo.queue.assert_not_called()
        self.client.close.assert_called_once()
        self.engine.dispose.assert_called_once()

    def test_repository_not_ready_exits_2(self) -> None:
        self.repo.probe.side_effect = ConversionNotReady("apply migration")
        self.assertEqual(cli.main([]), 2)
        self.repo.queue.assert_not_called()

    def test_empty_queue_exits_0(self) -> None:
        self.repo.queue.return_value = []
        self.assertEqual(cli.main([]), 0)
        self.orchestrator.run.assert_not_called()

    def test_passes_mode_ids_limit(self) -> None:
        code = cli.main(["--mode", "force", "--ids", "A", "--ids", "B",
                         "--limit", "3", "--dry-run"])
        self.assertEqual(code, 0)
        self.repo.queue.assert_called_once_with(["A", "B"], None)
        items, mode, dry_run = self.orchestrator.run.call_args.args
        self.assertEqual(self.orchestrator.run.call_args.kwargs, {"limit": 3})
        self.assertEqual(items, self.repo.queue.return_value)
        self.assertEqual(mode, ConversionMode.FORCE)
        self.assertTrue(dry_run)

    def test_sigterm_becomes_keyboard_interrupt_and_handler_restored(self) -> None:
        """During the run SIGTERM raises KeyboardInterrupt; the old handler returns."""
        before = signal.getsignal(signal.SIGTERM)

        def fake_run(*args: object, **kwargs: object) -> None:
            handler = signal.getsignal(signal.SIGTERM)
            self.assertIsNot(handler, before)
            handler(signal.SIGTERM, None)

        self.orchestrator.run.side_effect = fake_run
        with self.assertRaises(KeyboardInterrupt):
            cli.main([])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.client.close.assert_called_once()

    def test_failures_exit_1(self) -> None:
        self.orchestrator.run.return_value = ConversionSummary(
            counts={"failed": 1}, failure_reasons={"x": 1}, aborted=False)
        self.assertEqual(cli.main([]), 1)

    def test_start_log_names_server_tier_mode_queue_size(self) -> None:
        with self.assertLogs(_MOD, level="INFO") as logs:
            cli.main([])
        text = "\n".join(logs.output)
        for needle in (cli.settings.MINERU_API_URL, cli.settings.MINERU_TIER,
                       "mode lazy", "1 queue rows"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
