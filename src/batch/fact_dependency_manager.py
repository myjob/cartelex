"""Fact catalogue checks and dependency graph for ``FACT_EXTRACTOR.yaml``.

The catalogue is a directed acyclic graph: ``predecessors`` are hard edges
(a fact needs them), ``context_facts`` are soft edges (``case_summary`` reads
them but is never gated by them). Two consumers use this module:

* the main pipeline's prerequisites, which refuse to start on a catalogue that
  does not parse or declares something that does not exist (manual edits have
  crashed whole batch runs), and
* the main pipeline's planner, which walks the graph to find every fact that
  follows a changed one and orders them with a topological sort.
"""

from graphlib import CycleError, TopologicalSorter
from pathlib import Path

import yaml

from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO

_CHECK_CELEX_ID = "CATALOGUE-CHECK"


class CatalogueError(ValueError):
    """The catalogue file cannot be read as a list of fact entries.

    Attributes:
        messages: One human-readable problem per entry.
    """

    def __init__(self, messages: list[str]) -> None:
        """Store the problems and build the exception text from them.

        Args:
            messages: Problems found, each naming the file and location.
        """
        super().__init__("; ".join(messages))
        self.messages = messages


def load_structs(path: Path) -> list[dict]:
    """Read the catalogue YAML as a list of fact entries.

    Args:
        path: Path of ``FACT_EXTRACTOR.yaml`` (or a copy).

    Returns:
        The raw entries in file order.

    Raises:
        CatalogueError: On a YAML syntax error (with line and column) or when
            the top level is not a list.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "unknown position"
        raise CatalogueError([f"{path}: YAML syntax error at {where}: {exc.problem}"]) from exc
    except yaml.YAMLError as exc:
        raise CatalogueError([f"{path}: YAML error: {exc}"]) from exc
    if not isinstance(data, list):
        raise CatalogueError([f"{path}: top level must be a list of facts, got {type(data).__name__}"])
    return data


def _as_list(raw: object) -> list[str]:
    """Normalize a string-or-list YAML value to a list of names."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw] if isinstance(raw, list) else [str(raw)]


def _known_pipelines() -> set[str]:
    """Return the pipeline names the fact batch can dispatch.

    Imported lazily: ``Main_ingest_generic_facts`` is heavy (DSPy modules) and
    imports this module's neighbours, so a module-level import would be
    circular.
    """
    from batch.Main_ingest_generic_facts import PIPELINE_DSPY_MODULE_CLASSES

    return set(PIPELINE_DSPY_MODULE_CLASSES)


def check_catalogue(structs: list[dict]) -> list[str]:
    """Return every declaration problem of the catalogue.

    Checks, per entry: it loads through ``FactExtractorParamVO.from_yaml_struct``
    (the batch's own loader); its name is unique; its ``pipeline`` is known; every
    name in ``predecessors``, ``context_facts``, ``verify_fact_name`` and
    ``na_fallback_fact`` is a defined fact. Then the whole graph must be acyclic.

    Args:
        structs: Entries as returned by :func:`load_structs`.

    Returns:
        Problem messages, each naming the fact; empty when the catalogue is valid.
    """
    errors: list[str] = []
    named = [s for s in structs if isinstance(s, dict) and s.get("target_fact_name")]
    for index, struct in enumerate(structs):
        if not (isinstance(struct, dict) and struct.get("target_fact_name")):
            errors.append(f"entry {index + 1}: missing target_fact_name")
    defined = {s["target_fact_name"] for s in named}
    pipelines = _known_pipelines()

    seen: set[str] = set()
    for struct in named:
        name = struct["target_fact_name"]
        if name in seen:
            errors.append(f"{name}: duplicate target_fact_name")
        seen.add(name)
        try:
            FactExtractorParamVO.from_yaml_struct(celex_id=_CHECK_CELEX_ID, struct=struct)
        except Exception as exc:  # noqa: BLE001 - any loader failure is a catalogue error
            errors.append(f"{name}: does not load as a fact config: {exc}")
        pipeline = struct.get("pipeline", "universal")
        if pipeline not in pipelines:
            errors.append(f"{name}: unknown pipeline '{pipeline}'")
        references = {
            "predecessor": _as_list(struct.get("predecessors")),
            "context fact": _as_list(struct.get("context_facts")),
            "verify_fact_name": _as_list(struct.get("verify_fact_name")),
            "na_fallback_fact": _as_list(struct.get("na_fallback_fact")),
        }
        for kind, names in references.items():
            for ref in names:
                if ref not in defined:
                    errors.append(f"{name}: unknown {kind} '{ref}'")

    graph = {s["target_fact_name"]: set(_as_list(s.get("predecessors"))) & defined for s in named}
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        errors.append(f"dependency cycle: {' -> '.join(exc.args[1])}")
    return errors


def _edges(structs: list[dict]) -> dict[str, set[str]]:
    """Map each fact to the facts it reads (predecessors plus context_facts)."""
    return {
        s["target_fact_name"]: set(_as_list(s.get("predecessors"))) | set(_as_list(s.get("context_facts")))
        for s in structs
        if s.get("target_fact_name")
    }


def dependents(structs: list[dict], roots: set[str]) -> set[str]:
    """Return every fact that directly or transitively reads one of ``roots``.

    Both ``predecessors`` and ``context_facts`` count, so a change of
    ``products`` reaches ``case_summary``.

    Args:
        structs: Catalogue entries.
        roots: Changed facts.

    Returns:
        The successor facts, excluding ``roots`` themselves.
    """
    readers: dict[str, set[str]] = {}
    for fact, inputs in _edges(structs).items():
        for source in inputs:
            readers.setdefault(source, set()).add(fact)
    found: set[str] = set()
    stack = list(roots)
    while stack:
        for reader in readers.get(stack.pop(), set()):
            if reader not in found:
                found.add(reader)
                stack.append(reader)
    return found - set(roots)


def execution_order(structs: list[dict]) -> list[str]:
    """Return the active facts in dependency order.

    Args:
        structs: Catalogue entries; ``run_state_active: false`` entries are left out.

    Returns:
        Fact names with every input before its readers.

    Raises:
        graphlib.CycleError: When the catalogue has a cycle (see
            :func:`check_catalogue`).
    """
    active = [s for s in structs if s.get("target_fact_name") and s.get("run_state_active", True) is not False]
    names = {s["target_fact_name"] for s in active}
    graph = {fact: inputs & names for fact, inputs in _edges(active).items()}
    return list(TopologicalSorter(graph).static_order())
