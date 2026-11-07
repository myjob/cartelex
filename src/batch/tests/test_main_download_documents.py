"""Tests for the Main_download_documents batch CLI (fakes only, no DB, no network)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID

import polars as pl
from sqlalchemy.exc import OperationalError

from batch import Main_download_documents as cli
from config.settings import settings
from pipelines.download.models import DecisionRequest, Outcome, RowFailure
from pipelines.download.run_summary import RunSummary

_URLS = (
    "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31990D0645\n"
    "https://example.org/a/second_decision.pdf\n"
    "https://example.org/a/third_decision.pdf\n"
)


class FakeOrchestrator:
    """Records run() calls and returns a configurable summary."""

    def __init__(self, failed: int = 0) -> None:
        """Create the fake.

        Args:
            failed: Number of failed candidates to report.
        """
        self.failed = failed
        self.calls: list[dict] = []

    def run(self, decisions: list[DecisionRequest], **kwargs) -> RunSummary:
        """Record the call and build a summary.

        Args:
            decisions: Mapped decisions.
            **kwargs: Keyword arguments of the real run().

        Returns:
            A summary with ``failed`` failed candidates.
        """
        self.calls.append({"decisions": decisions, **kwargs})
        summary = RunSummary(run_id=kwargs["run_id"], dry_run=kwargs["dry_run"])
        summary.outcomes[Outcome.FAILED] = self.failed
        if self.failed:
            summary.failure_reasons["http_status"] = self.failed
        summary.row_failures = len(kwargs["failures"])
        return summary


class FakeAudit:
    """Returns canned failure rows."""

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        """Create the fake.

        Args:
            rows: Rows returned by failures().
        """
        self.rows = rows or []
        self.run_ids: list[UUID | None] = []

    def failures(self, run_id: UUID | None) -> list[dict[str, object]]:
        """Return the canned rows.

        Args:
            run_id: Requested run id.

        Returns:
            The canned rows.
        """
        self.run_ids.append(run_id)
        return self.rows


def _row(**extra: object) -> dict[str, object]:
    """Build a failures-view row."""
    base: dict[str, object] = {
        "run_id": "r", "decision_id": "CELEX-31990D0645", "celex_number": "31990D0645",
        "title": "T", "document_id": "d", "source_url": "https://x/y.pdf",
        "outcome": "failed", "failure_reason": "http_status: 404",
        "http_status": 404, "loader_file": "l.txt", "loader_row": 1,
        "created_at": "now",
    }
    return base | extra


class MainDownloadDocumentsTest(unittest.TestCase):
    """CLI behaviour with a patched pipeline factory."""

    def setUp(self) -> None:
        """Create a temp dir with a url-list loader."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.loader = self.tmp / "l.txt"
        self.loader.write_text(_URLS, encoding="utf-8")
        self.client = MagicMock()
        self.engine = MagicMock()

    def _run(self, argv: list[str], orch: FakeOrchestrator,
             audit: FakeAudit | None = None) -> int:
        """Run main() with a fake pipeline.

        Args:
            argv: CLI arguments.
            orch: Fake orchestrator.
            audit: Fake audit repository.

        Returns:
            The exit code.
        """
        pipeline = cli.Pipeline(orchestrator=orch, audit=audit or FakeAudit(),
                                client=self.client, engine=self.engine)
        with patch.object(cli, "_build_pipeline", return_value=pipeline):
            return cli.main(argv)

    def test_missing_loader_file_exits_nonzero(self) -> None:
        """An absent loader file is a usage error (2)."""
        orch = FakeOrchestrator()
        code = self._run(["--loader-file", str(self.tmp / "nope.txt")], orch)
        self.assertEqual(code, 2)
        self.assertEqual(orch.calls, [])

    def test_unknown_mapper_rejected(self) -> None:
        """An unknown mapper name exits 2 without running."""
        orch = FakeOrchestrator()
        code = self._run(["--loader-file", str(self.loader), "--mapper", "bogus"],
                         orch)
        self.assertEqual(code, 2)
        self.assertEqual(orch.calls, [])

    def test_dry_run_creates_no_files_and_no_backup(self) -> None:
        """A dry run writes nothing, not even the report or its directory."""
        report = self.tmp / "sub" / "report.csv"
        before = sorted(p.name for p in self.tmp.rglob("*"))
        orch = FakeOrchestrator()
        audit = FakeAudit([_row()])
        code = self._run(["--loader-file", str(self.loader), "--dry-run",
                          "--report-failures", str(report)], orch, audit)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.tmp.rglob("*")), before)
        self.assertFalse(report.parent.exists())
        self.assertEqual(audit.run_ids, [])
        self.assertTrue(orch.calls[0]["dry_run"])

    def test_report_failures_writes_csv_with_loader_row(self) -> None:
        """The failures view rows land in the CSV, parents created."""
        report = self.tmp / "out" / "deep" / "report.csv"
        orch = FakeOrchestrator(failed=1)
        audit = FakeAudit([_row(loader_row=7, title="Café")])
        self._run(["--loader-file", str(self.loader),
                   "--report-failures", str(report)], orch, audit)
        df = pl.read_csv(report)
        self.assertEqual(df.columns, list(cli.REPORT_COLUMNS))
        self.assertEqual(df["loader_row"].to_list(), [7])
        self.assertEqual(df["title"].to_list(), ["Café"])
        self.assertEqual(audit.run_ids, [orch.calls[0]["run_id"]])
        self.assertEqual([p.name for p in report.parent.iterdir()], ["report.csv"])

    def test_exit_code_nonzero_when_candidate_failed(self) -> None:
        """A failed candidate yields exit code 1."""
        self.assertEqual(
            self._run(["--loader-file", str(self.loader)], FakeOrchestrator(failed=2)),
            1)

    def test_row_failures_do_not_change_exit_code(self) -> None:
        """Mapper row failures are passed to the run but keep exit 0."""
        loader = self.tmp / "bad.txt"
        loader.write_text("not a url at all\n", encoding="utf-8")
        orch = FakeOrchestrator()
        code = self._run(["--loader-file", str(loader)], orch)
        self.assertEqual(code, 0)
        failures = orch.calls[0]["failures"]
        self.assertTrue(failures)
        self.assertIsInstance(failures[0], RowFailure)

    def test_file_manifest_default_comes_from_settings(self) -> None:
        """--file-manifest defaults to settings.DOWNLOAD_FILE_MANIFEST_PATH."""
        args = cli.parse_args(["--loader-file", str(self.loader)])
        self.assertEqual(args.file_manifest, Path(settings.DOWNLOAD_FILE_MANIFEST_PATH))
        self.assertEqual(args.max_workers, settings.DOWNLOAD_DEFAULT_MAX_WORKERS)

    def test_limit_applies_after_mapping(self) -> None:
        """--limit truncates the mapped decisions, not the loader rows."""
        orch = FakeOrchestrator()
        self._run(["--loader-file", str(self.loader), "--limit", "2"], orch)
        self.assertEqual(len(orch.calls[0]["decisions"]), 2)

    def test_probe_failure_exits_2(self) -> None:
        """Missing registry tables exit 2 with a clear message."""
        with patch.object(cli, "_build_pipeline",
                          side_effect=cli.RegistryNotReady("apply it")):
            with self.assertLogs(cli.logger, "ERROR") as logs:
                code = cli.main(["--loader-file", str(self.loader), "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("migrate_document_registry.sql", "\n".join(logs.output))

    def test_close_called_on_success_and_failed_run(self) -> None:
        """The client is closed and the engine disposed in both cases."""
        for failed in (0, 1):
            self.client.reset_mock()
            self.engine.reset_mock()
            self._run(["--loader-file", str(self.loader)],
                      FakeOrchestrator(failed=failed))
            self.client.close.assert_called_once()
            self.engine.dispose.assert_called_once()

    def test_close_called_when_run_raises(self) -> None:
        """An exception from the run still releases the resources."""
        orch = FakeOrchestrator()
        with patch.object(orch, "run", side_effect=OSError("flush")):
            with self.assertRaises(OSError):
                self._run(["--loader-file", str(self.loader)], orch)
        self.client.close.assert_called_once()
        self.engine.dispose.assert_called_once()

    def test_close_error_keeps_exit_code(self) -> None:
        """A raising close() after a run does not change the exit code."""
        self.client.close.side_effect = RuntimeError("boom")
        code = self._run(["--loader-file", str(self.loader)],
                         FakeOrchestrator(failed=1))
        self.assertEqual(code, 1)
        self.engine.dispose.assert_called_once()

    def test_close_error_does_not_mask_run_exception(self) -> None:
        """A raising close() keeps the run's own exception."""
        self.client.close.side_effect = RuntimeError("close")
        self.engine.dispose.side_effect = RuntimeError("dispose")
        orch = FakeOrchestrator()
        with patch.object(orch, "run", side_effect=OSError("flush")):
            with self.assertRaises(OSError):
                self._run(["--loader-file", str(self.loader)], orch)

    def test_negative_limit_rejected(self) -> None:
        """A negative --limit is an argparse error (2)."""
        self.assertEqual(
            self._run(["--loader-file", str(self.loader), "--limit", "-1"],
                      FakeOrchestrator()), 2)

    def test_unparseable_loader_exits_2(self) -> None:
        """An unsupported extension and a corrupt xlsx both exit 2."""
        orch = FakeOrchestrator()
        odd = self.tmp / "l.json"
        odd.write_text("{}", encoding="utf-8")
        bad = self.tmp / "l.xlsx"
        bad.write_bytes(b"not a zip")
        for path in (odd, bad):
            self.assertEqual(self._run(["--loader-file", str(path)], orch), 2)
        self.assertEqual(orch.calls, [])

    def test_limit_keeps_row_failures(self) -> None:
        """--limit truncates decisions only; row failures are all passed on."""
        loader = self.tmp / "mixed.txt"
        loader.write_text(_URLS + "not a url\n", encoding="utf-8")
        orch = FakeOrchestrator()
        self._run(["--loader-file", str(loader), "--limit", "1"], orch)
        self.assertEqual(len(orch.calls[0]["decisions"]), 1)
        self.assertEqual(len(orch.calls[0]["failures"]), 1)

    def test_manifest_read_via_class_flag(self) -> None:
        """A mapper with needs_file_manifest gets the per-file manifest read."""
        loader = self.tmp / "m.csv"
        loader.write_text("manifest_row_id,case_numbers\n1,AT.1\n", encoding="utf-8")
        manifest = self.tmp / "files.csv"
        manifest.write_text("manifest_row_id,source_url,file_format\n",
                            encoding="utf-8")
        seen: list[Path] = []
        real = cli.read_loader

        def spy(path: Path) -> pl.DataFrame:
            seen.append(path)
            return real(path)

        with patch.object(cli, "read_loader", spy):
            try:
                self._run(["--loader-file", str(loader),
                           "--file-manifest", str(manifest)], FakeOrchestrator())
            except Exception:  # mapping may fail on the stub data
                pass
        self.assertEqual(seen, [loader, manifest])

    def test_cli_offline_and_initial_loader_flags(self) -> None:
        """--offline reaches run(); the master mapper reads JSON plus --initial-loader."""
        url = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31964D0233"
        master = self.tmp / "master.json"
        master.write_text(json.dumps({"documents": {"CELEX-31964D0233": {
            "language_init": "en", "webDocDescriptor": {"url_pdf": url}}}}),
            encoding="utf-8")
        initial = self.tmp / "initial.csv"
        initial.write_text(f"case.x,url_pdf,title\n61,{url}&from=EN,Grosfillex\n",
                           encoding="utf-8")
        lookup = MagicMock()
        lookup.case_ids_for.return_value = ["IV/61"]
        base = ["--loader-file", str(master), "--mapper", "master_backfill",
                "--offline"]
        with patch.object(cli, "_case_lookup", return_value=lookup), patch.object(
                cli.settings, "MASTER_DOC_DESCRIPTORS_PATH", str(master)):
            orch = FakeOrchestrator()
            code = self._run([*base, "--initial-loader", str(initial)], orch)
            plain = FakeOrchestrator()
            self._run(base, plain)
        self.assertEqual(code, 0)
        self.assertTrue(orch.calls[0]["offline"])
        decision = orch.calls[0]["decisions"][0]
        self.assertEqual((decision.title, decision.case_ids),
                         ("Grosfillex", ["IV/61"]))
        self.assertEqual(plain.calls[0]["decisions"][0].title, "")
        online = FakeOrchestrator()
        self._run(["--loader-file", str(self.loader)], online)
        self.assertFalse(online.calls[0]["offline"])

    def test_master_backfill_requires_offline(self) -> None:
        """master_backfill without --offline is a usage error before any read."""
        orch = FakeOrchestrator()
        with patch.object(cli, "_case_lookup") as lookup:
            with self.assertLogs(cli.logger, "ERROR") as logs:
                code = self._run(["--loader-file", str(self.tmp / "absent.json"),
                                  "--mapper", "master_backfill"], orch)
        self.assertEqual(code, 2)
        self.assertIn("requires --offline", "\n".join(logs.output))
        lookup.assert_not_called()
        self.assertEqual(orch.calls, [])

    def test_run_log_tags_dry_run_and_offline(self) -> None:
        """The run line carries both tags when both flags are given."""
        cases = {(): "", ("--dry-run",): " [dry run]",
                 ("--offline",): " [offline]",
                 ("--dry-run", "--offline"): " [dry run] [offline]"}
        for flags, tags in cases.items():
            with self.subTest(flags=flags):
                with self.assertLogs(cli.logger, "INFO") as logs:
                    self._run(["--loader-file", str(self.loader), *flags],
                              FakeOrchestrator())
                line = next(o for o in logs.output if "mapper row failures)" in o)
                self.assertTrue(line.endswith(f"mapper row failures){tags}"), line)

    def test_master_backfill_unreadable_master_exits_2(self) -> None:
        """A master file without a documents object is a usage error."""
        master = self.tmp / "master.json"
        master.write_text("[]", encoding="utf-8")
        orch = FakeOrchestrator()
        with patch.object(cli, "_case_lookup", return_value=MagicMock()), patch.object(
                cli.settings, "MASTER_DOC_DESCRIPTORS_PATH", str(master)):
            code = self._run(["--loader-file", str(master), "--mapper",
                              "master_backfill", "--offline"], orch)
        self.assertEqual(code, 2)
        self.assertEqual(orch.calls, [])

    def test_master_backfill_refuses_other_master_file(self) -> None:
        """A master file other than the settings path is refused (exit 2)."""
        master = self.tmp / "master.json"
        master.write_text('{"documents": {}}', encoding="utf-8")
        orch = FakeOrchestrator()
        with patch.object(cli, "_case_lookup", return_value=MagicMock()), patch.object(
                cli.settings, "MASTER_DOC_DESCRIPTORS_PATH",
                str(self.tmp / "other.json")):
            with self.assertLogs(cli.logger, "ERROR") as logs:
                code = self._run(["--loader-file", str(master), "--mapper",
                                  "master_backfill", "--offline"], orch)
        self.assertEqual(code, 2)
        self.assertIn("settings master file", "\n".join(logs.output))
        self.assertEqual(orch.calls, [])

    def test_case_lookup_db_error_exits_2(self) -> None:
        """A database error during the case lookup is a logged exit 2."""
        master = self.tmp / "master.json"
        master.write_text('{"documents": {}}', encoding="utf-8")
        orch = FakeOrchestrator()
        with patch.object(cli, "_case_lookup",
                          side_effect=OperationalError("SELECT", {}, Exception("down"))
                          ), patch.object(cli.settings, "MASTER_DOC_DESCRIPTORS_PATH",
                                          str(master)):
            with self.assertLogs(cli.logger, "ERROR") as logs:
                code = self._run(["--loader-file", str(master), "--mapper",
                                  "master_backfill", "--offline"], orch)
        self.assertEqual(code, 2)
        self.assertIn("case lookup failed", "\n".join(logs.output))
        self.assertEqual(orch.calls, [])


class BuildPipelineTest(unittest.TestCase):
    """_build_pipeline wiring with the module boundary patched."""

    def _patches(self, registry: MagicMock) -> tuple:
        """Patch the boundary constructors.

        Args:
            registry: Mock standing in for DocumentRegistryRepository.

        Returns:
            The started patchers (stopped by cleanup) and mocks.
        """
        engine, client = MagicMock(), MagicMock()
        mocks = {}
        for name, value in (("make_engine", MagicMock(return_value=engine)),
                            ("make_client", MagicMock(return_value=client)),
                            ("DocumentRegistryRepository", registry),
                            ("DownloadAuditRepository", MagicMock(return_value=FakeAudit())),
                            ("HttpDocumentFetcher", MagicMock()),
                            ("MasterDescriptorStore", MagicMock()),
                            ("DownloadOrchestrator", MagicMock(return_value=FakeOrchestrator()))):
            patcher = patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
            mocks[name] = value
        return engine, client, mocks

    def test_builds_pipeline_with_resources(self) -> None:
        """The pipeline carries the client and engine and closes them."""
        engine, client, mocks = self._patches(MagicMock())
        pipeline = cli._build_pipeline(MagicMock())
        mocks["DownloadOrchestrator"].assert_called_once()
        self.assertIs(pipeline.engine, engine)
        pipeline.close()
        client.close.assert_called_once()
        engine.dispose.assert_called_once()

    def test_fetcher_routes_eurlex_via_cellar_by_default(self) -> None:
        """The fetcher gets the Cellar router unless DOWNLOAD_CELLAR_ENABLED is off."""
        _, _, mocks = self._patches(MagicMock())
        with patch.object(cli.settings, "DOWNLOAD_CELLAR_ENABLED", True):
            cli._build_pipeline(MagicMock()).close()
        self.assertIs(mocks["HttpDocumentFetcher"].call_args.kwargs["route"],
                      cli.cellar_request_for)
        mocks["HttpDocumentFetcher"].reset_mock()
        with patch.object(cli.settings, "DOWNLOAD_CELLAR_ENABLED", False):
            cli._build_pipeline(MagicMock()).close()
        self.assertIsNone(mocks["HttpDocumentFetcher"].call_args.kwargs["route"])

    def test_runtime_error_maps_to_registry_not_ready(self) -> None:
        """A probe RuntimeError becomes RegistryNotReady and releases the engine."""
        engine, client, _ = self._patches(MagicMock(side_effect=RuntimeError("no")))
        with self.assertRaises(cli.RegistryNotReady):
            cli._build_pipeline(MagicMock())
        engine.dispose.assert_called_once()
        client.close.assert_not_called()

    def test_other_errors_propagate_and_release(self) -> None:
        """A non-probe error is not swallowed; resources are released."""
        engine, _, _ = self._patches(MagicMock(side_effect=KeyError("x")))
        with self.assertRaises(KeyError):
            cli._build_pipeline(MagicMock())
        engine.dispose.assert_called_once()

    def test_main_exits_2_on_registry_not_ready(self) -> None:
        """main() exits 2 when the factory reports missing tables."""
        self._patches(MagicMock(side_effect=RuntimeError("no")))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        loader = Path(tmp.name) / "l.txt"
        loader.write_text(_URLS, encoding="utf-8")
        self.assertEqual(cli.main(["--loader-file", str(loader), "--dry-run"]), 2)


if __name__ == "__main__":
    unittest.main()
