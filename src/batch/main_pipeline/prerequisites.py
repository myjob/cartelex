"""Prerequisite checks of the main pipeline (chapter 0).

All checks must pass before anything is computed. Each check turns its
boundary failure into a :class:`CheckResult` with a one-line fix hint and never
raises, so the operator sees every problem at once instead of the first.
"""

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from batch.fact_dependency_manager import CatalogueError, check_catalogue, load_structs
from config.settings import settings

logger = logging.getLogger(__name__)

_GPU_MINIMUM_MB: dict[str, Callable[[], int]] = {
    "mineru": lambda: settings.PIPELINE_GPU_MIN_FREE_MB_MINERU,
    "bge": lambda: settings.PIPELINE_GPU_MIN_FREE_MB_BGE,
}


class CheckResult(BaseModel):
    """Outcome of one prerequisite check."""

    name: str = Field(description="Check name, e.g. 'database', 'dgx', 'catalogue'")
    ok: bool = Field(description="True when the check passed")
    detail: str = Field(description="What was found, or what to fix when it failed")


class PrerequisiteDeps(BaseModel):
    """Boundaries the checks talk to, injected so tests need no infrastructure."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    db_ping: Callable[[], None] = Field(description="Raises when PostgreSQL is unreachable")
    llm_call: Callable[[], str] = Field(description="One minimal completion on settings.LLM_MODEL_ID")
    health_gate: Any = Field(description="OllamaHealthGate (has .read()), or None when the thermal gate is off")
    running_batches: Callable[[], list[str]] = Field(description="Command lines of other running batch.Main_* processes")
    gpu_free_mb: Callable[[], int] = Field(description="Free local GPU memory in MiB")
    catalogue_path: Path = Field(description="FACT_EXTRACTOR.yaml to validate")
    docs_dir: Path = Field(description="Document folder (settings.CELEX_DOCS_DIR)")


def _guard(name: str, check: Callable[[], str]) -> CheckResult:
    """Run ``check``; its return is the success detail, an exception a failure."""
    try:
        return CheckResult(name=name, ok=True, detail=check())
    except Exception as exc:  # noqa: BLE001 - every failure becomes a result
        return CheckResult(name=name, ok=False, detail=str(exc) or type(exc).__name__)


def _check_database(deps: PrerequisiteDeps) -> str:
    deps.db_ping()
    return "reachable"


def _check_docs_dir(deps: PrerequisiteDeps) -> str:
    if not deps.docs_dir.is_dir():
        raise FileNotFoundError(f"{deps.docs_dir} does not exist; set CELEX_DOCS_DIR in config_main/.env")
    return str(deps.docs_dir)


def _check_dgx(deps: PrerequisiteDeps) -> str:
    if deps.health_gate is not None:
        verdict = deps.health_gate.read()
        if not verdict.healthy:
            raise RuntimeError(f"DGX not healthy: {verdict.reason}")
    try:
        answer = deps.llm_call()
    except Exception as exc:
        raise RuntimeError(
            f"DGX not serving {settings.LLM_MODEL_ID} ({exc}); after a hard boot wait for Ollama"
        ) from exc
    if not (answer or "").strip():
        raise RuntimeError(f"DGX returned an empty answer from {settings.LLM_MODEL_ID}")
    return f"{settings.LLM_MODEL_ID} answers"


def _check_running(deps: PrerequisiteDeps) -> str:
    others = deps.running_batches()
    if others:
        raise RuntimeError("other batch running, wait or stop it: " + " | ".join(others))
    return "none"


def _check_catalogue(deps: PrerequisiteDeps) -> str:
    structs = load_structs(deps.catalogue_path)
    return f"{len(structs)} facts parsed"


def _check_declarations(deps: PrerequisiteDeps) -> str:
    try:
        structs = load_structs(deps.catalogue_path)
    except CatalogueError as exc:
        raise RuntimeError(f"not checked, catalogue does not parse: {exc}") from exc
    errors = check_catalogue(structs)
    if errors:
        raise RuntimeError("; ".join(errors))
    return "all references resolve, no cycles"


def gpu_ok(deps: PrerequisiteDeps, need: str) -> CheckResult:
    """Check that the local GPU has the free memory ``need`` requires.

    Args:
        deps: Boundaries; only ``gpu_free_mb`` is used.
        need: ``"mineru"``, ``"bge"`` or ``"none"``.

    Returns:
        The result named ``gpu memory``; always ok for ``"none"``.
    """
    if need not in _GPU_MINIMUM_MB:
        return CheckResult(name="gpu memory", ok=True, detail="not needed")
    minimum = _GPU_MINIMUM_MB[need]()

    def check() -> str:
        free = deps.gpu_free_mb()
        if free < minimum:
            raise RuntimeError(f"{free} MiB free, {need} needs {minimum} MiB; free the GPU (nvidia-smi)")
        return f"{free} MiB free, {need} needs {minimum} MiB"

    return _guard("gpu memory", check)


def run_prerequisites(deps: PrerequisiteDeps, gpu_needs: set[str]) -> list[CheckResult]:
    """Run every prerequisite check in the design's order.

    Args:
        deps: Boundaries to check.
        gpu_needs: GPU consumers in the plan (subset of ``{"mineru", "bge"}``);
            the GPU check is skipped when empty and uses the largest minimum.

    Returns:
        One result per check; the caller stops when any is not ok.
    """
    results = [
        _guard("database", lambda: _check_database(deps)),
        _guard("document folder", lambda: _check_docs_dir(deps)),
        _guard("dgx", lambda: _check_dgx(deps)),
        _guard("running batches", lambda: _check_running(deps)),
    ]
    needs = [n for n in gpu_needs if n in _GPU_MINIMUM_MB]
    if needs:
        results.append(gpu_ok(deps, max(needs, key=lambda n: _GPU_MINIMUM_MB[n]())))
    results.append(_guard("catalogue", lambda: _check_catalogue(deps)))
    results.append(_guard("catalogue declarations", lambda: _check_declarations(deps)))
    return results


def _db_ping() -> None:
    from sqlalchemy import text

    from pipelines.rag.pgvector.session import make_engine

    with make_engine().connect() as conn:
        conn.execute(text("SELECT 1"))


def _llm_call() -> str:
    from config.llm_factory import batch_profile, create_lm

    lm = create_lm(batch_profile(temperature=0.0))
    answers = lm("Reply with the single word OK.")
    return str(answers[0]) if answers else ""


def _running_batches() -> list[str]:
    out = subprocess.run(
        ["pgrep", "-af", r"python.* -m batch\.[M]ain_"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    own = {str(os.getpid()), str(os.getppid())}
    return [line for line in out.splitlines() if line.split(" ", 1)[0] not in own and "Main_Main_Pipeline" not in line]


def _gpu_free_mb() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return int(out.split()[0])


def default_deps() -> PrerequisiteDeps:
    """Build the production boundaries from settings.

    Returns:
        Deps talking to the real DB, DGX, process table and GPU.
    """
    from config.ollama_health_gate import build_thermal_gate

    return PrerequisiteDeps(
        db_ping=_db_ping,
        llm_call=_llm_call,
        health_gate=build_thermal_gate(dry_run=False, disabled=False),
        running_batches=_running_batches,
        gpu_free_mb=_gpu_free_mb,
        catalogue_path=Path(settings.FACT_EXTRACTOR_CONFIG_DIR),
        docs_dir=Path(settings.CELEX_DOCS_DIR),
    )
