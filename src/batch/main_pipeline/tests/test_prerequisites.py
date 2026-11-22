"""Tests for the main pipeline prerequisites.

Every check must pass before anything runs; a failing check names what to fix
and never raises. Boundaries (DB, DGX, process list, GPU) are injected fakes.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from batch.main_pipeline.prerequisites import (
    PrerequisiteDeps,
    gpu_ok,
    run_prerequisites,
)
from config.settings import settings


def _write(text: str) -> Path:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as handle:
        handle.write(text)
    return Path(handle.name)


def _deps(**overrides: object) -> PrerequisiteDeps:
    values: dict = {
        "db_ping": lambda: None,
        "llm_call": lambda: "OK",
        "health_gate": SimpleNamespace(read=lambda: SimpleNamespace(healthy=True, reason="cool")),
        "running_batches": lambda: [],
        "gpu_free_mb": lambda: 10_000,
        "catalogue_path": Path(settings.FACT_EXTRACTOR_CONFIG_DIR),
        "docs_dir": Path(tempfile.gettempdir()),
    }
    values.update(overrides)
    return PrerequisiteDeps(**values)


def _failed(results: list) -> dict[str, str]:
    return {r.name: r.detail for r in results if not r.ok}


class TestRunPrerequisites(unittest.TestCase):
    """Each boundary failure becomes one failed result."""

    def test_all_pass(self) -> None:
        self.assertEqual(_failed(run_prerequisites(_deps(), {"bge"})), {})

    def test_db_down_fails(self) -> None:
        def boom() -> None:
            raise ConnectionError("refused")

        failed = _failed(run_prerequisites(_deps(db_ping=boom), set()))
        self.assertIn("database", failed)
        self.assertIn("refused", failed["database"])

    def test_missing_docs_dir_fails(self) -> None:
        failed = _failed(run_prerequisites(_deps(docs_dir=Path("/nonexistent/celex_docs")), set()))
        self.assertIn("document folder", failed)

    def test_dgx_down_fails(self) -> None:
        def boom() -> str:
            raise TimeoutError("no answer")

        failed = _failed(run_prerequisites(_deps(llm_call=boom), set()))
        self.assertIn("DGX", failed["dgx"])

    def test_dgx_too_hot_fails(self) -> None:
        gate = SimpleNamespace(read=lambda: SimpleNamespace(healthy=False, reason="GPU 92C"))
        failed = _failed(run_prerequisites(_deps(health_gate=gate), set()))
        self.assertIn("92C", failed["dgx"])

    def test_disabled_gate_still_calls_llm(self) -> None:
        self.assertEqual(_failed(run_prerequisites(_deps(health_gate=None), set())), {})

    def test_other_batch_running_fails(self) -> None:
        failed = _failed(run_prerequisites(_deps(running_batches=lambda: ["4711 python -m batch.Main_x"]), set()))
        self.assertIn("4711", failed["running batches"])

    def test_gpu_below_mineru_minimum_fails(self) -> None:
        failed = _failed(run_prerequisites(_deps(gpu_free_mb=lambda: 5000), {"mineru"}))
        self.assertIn("5000", failed["gpu memory"])

    def test_gpu_need_absent_skips_check(self) -> None:
        names = [r.name for r in run_prerequisites(_deps(gpu_free_mb=lambda: 0), set())]
        self.assertNotIn("gpu memory", names)

    def test_yaml_syntax_error_fails_catalogue_check(self) -> None:
        path = _write("- target_fact_name: a\n  question: [unclosed\n")
        failed = _failed(run_prerequisites(_deps(catalogue_path=path), set()))
        self.assertIn("line", failed["catalogue"])

    def test_dangling_predecessor_fails_declaration_check(self) -> None:
        path = _write(
            "- target_fact_name: a\n  question: q\n  output_schema: ''\n  pipeline: universal\n"
            "  predecessors: [ghost]\n"
        )
        failed = _failed(run_prerequisites(_deps(catalogue_path=path), set()))
        self.assertIn("ghost", failed["catalogue declarations"])

    def test_checks_never_raise(self) -> None:
        def boom() -> int:
            raise RuntimeError("nvidia-smi missing")

        failed = _failed(run_prerequisites(_deps(gpu_free_mb=boom), {"bge"}))
        self.assertIn("nvidia-smi missing", failed["gpu memory"])


class TestGpuOk(unittest.TestCase):
    """The runner re-checks GPU memory right before a GPU step."""

    def test_bge_threshold(self) -> None:
        self.assertTrue(gpu_ok(_deps(gpu_free_mb=lambda: settings.PIPELINE_GPU_MIN_FREE_MB_BGE), "bge").ok)
        self.assertFalse(gpu_ok(_deps(gpu_free_mb=lambda: settings.PIPELINE_GPU_MIN_FREE_MB_BGE - 1), "bge").ok)

    def test_none_needs_nothing(self) -> None:
        self.assertTrue(gpu_ok(_deps(gpu_free_mb=lambda: 0), "none").ok)


if __name__ == "__main__":
    unittest.main()
