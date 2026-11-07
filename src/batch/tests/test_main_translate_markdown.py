"""Tests for the Main_translate_markdown CLI wiring (boundaries patched)."""

import unittest
from unittest.mock import MagicMock, patch

from batch import Main_translate_markdown as cli
from pipelines.rag.pgvector.translation_repository import TranslationNotReady
from pipelines.translate.orchestrator import TranslationMode, TranslationSummary

_MOD = "batch.Main_translate_markdown"


class MainTranslateMarkdownTest(unittest.TestCase):
    """Exit codes and argument passing of the CLI."""

    def setUp(self) -> None:
        patches = {
            name: patch(f"{_MOD}.{name}")
            for name in ("make_engine", "TranslationRepository", "MasterDescriptorStore",
                         "TranslationOrchestrator", "create_lm", "batch_profile",
                         "MarkdownTranslator", "TokenTargetMarkdownSplitter")
        }
        self.mocks = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        self.engine = self.mocks["make_engine"].return_value
        self.repo = self.mocks["TranslationRepository"].return_value
        self.orchestrator = self.mocks["TranslationOrchestrator"].return_value
        self.repo.queue.return_value = [MagicMock()]
        self.orchestrator.run.return_value = TranslationSummary(
            counts={"translated": 1}, failure_reasons={}, aborted=False)

    def test_parse_defaults(self) -> None:
        args = cli.parse_args([])
        self.assertEqual((args.mode, args.ids, args.limit, args.dry_run),
                         ("lazy", None, None, False))

    def test_not_ready_exits_usage(self) -> None:
        self.repo.probe.side_effect = TranslationNotReady("apply migration")
        self.assertEqual(cli.main([]), 2)
        self.repo.queue.assert_not_called()
        self.engine.dispose.assert_called_once()

    def test_empty_queue_exits_ok(self) -> None:
        self.repo.queue.return_value = []
        self.assertEqual(cli.main([]), 0)
        self.orchestrator.run.assert_not_called()
        self.mocks["create_lm"].assert_not_called()

    def test_passes_mode_ids_limit(self) -> None:
        cli.main(["--mode", "force", "--ids", "A", "--ids", "B", "--limit", "2", "--dry-run"])
        self.repo.queue.assert_called_once_with(["A", "B"], force=True)
        items, mode, dry_run = self.orchestrator.run.call_args.args
        self.assertEqual(mode, TranslationMode.FORCE)
        self.assertTrue(dry_run)
        self.assertEqual(self.orchestrator.run.call_args.kwargs, {"limit": 2})

    def test_summary_exit_code_propagates(self) -> None:
        self.orchestrator.run.return_value = TranslationSummary(
            counts={"failed": 1}, failure_reasons={"chunk_rejected": 1}, aborted=False)
        self.assertEqual(cli.main([]), 1)

    def test_splitter_uses_translate_settings(self) -> None:
        cli.main([])
        kwargs = self.mocks["TokenTargetMarkdownSplitter"].call_args.kwargs
        self.assertEqual(kwargs["chunk_size"], cli.settings.TRANSLATE_CHUNK_TOKENS)
        self.assertEqual(kwargs["separators"], cli.settings.TRANSLATE_SPLIT_SEPARATORS)


if __name__ == "__main__":
    unittest.main()
