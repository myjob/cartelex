"""Staleness planner of the main pipeline.

Turns the trigger rules of ``docs/Design Main_Main_Pipeline.md`` into a
:class:`RunPlan`:

* added loader rows and rows whose download URL changed -> download, then
  every later chapter for those decisions;
* documents downloaded outside the orchestrator since the last finished run ->
  document preparation onwards;
* a fact whose predecessor changed after the fact was last attempted, or a
  fact the operator asks to refresh -> that fact and its catalogue dependents
  are forced (``case_summary`` excepted);
* a fact missing although it should exist -> an unforced fact run fills it;
* optionally, chunks newer than the facts built from them -> every fact except
  ``case_summary`` is forced.

``case_summary`` never runs forced: chapter 5 calls it plainly and
``FactSummaryModule`` rebuilds a summary only when an input value changed
(``facts.created_at`` moves only on a value change).
"""

import logging
import os
from typing import TYPE_CHECKING
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from batch.fact_dependency_manager import dependents
from batch.main_pipeline.models import Chapter, GpuNeed, RunPlan, StepPlan
from config.settings import settings
from pipelines.graph.entity_graph_mapping import (
    COMPANY_ROSTER_FAMILY,
    FAMILY_EDGE_MAP,
    TEMPORAL_EDGE_FAMILY,
)

if TYPE_CHECKING:
    import polars as pl

    from pipelines.download.models import DecisionRequest

logger = logging.getLogger(__name__)

SUMMARY_FACT = "case_summary"

# Facts Main_materialize_entity_graph reads: the edge families plus the facts
# it looks up by name (decision date for edge validity, the temporal layer).
GRAPH_INPUT_FACTS: frozenset[str] = frozenset(
    set(FAMILY_EDGE_MAP)
    | {COMPANY_ROSTER_FAMILY, TEMPORAL_EDGE_FAMILY, "eu_commission_decision_date"}
)

# (step name, module, extra args, GPU consumer) of chapter 2, in order.
_PREPARATION_STEPS: tuple[tuple[str, str, tuple[str, ...], GpuNeed], ...] = (
    ("convert", "batch.Main_convert_pdf_markdown", (), "mineru"),
    ("decide", "batch.Main_decide_source_document", ("--apply",), "none"),
    ("translate", "batch.Main_translate_markdown", (), "none"),
    ("embed", "batch.Main_ingest_case_markdown2pgvector", (), "none"),
    ("classify", "batch.Main_classify_sections", (), "none"),
    ("enrich", "batch.Main_enrich_chunk_context", (), "none"),
)

# Chapter-2 steps with a lazy/force --mode switch.
_MODE_STEPS: frozenset[str] = frozenset({"convert", "translate", "embed"})

_FACTS_MODULE = "batch.Main_ingest_generic_facts"

# Report sequence fixed by the operator; the zip step is run in-process.
REPORT_STEPS: tuple[tuple[str, str], ...] = (
    ("report_celex_facts", "reporting.report_celex_facts"),
    ("report_eddy_celex_facts", "reporting.report_eddy_celex_facts"),
    ("retrieve_case_summary_md2disk", "reporting.retrieve_case_summary_md2disk"),
    ("zip_case_summary", "builtin.zip_case_summary"),
)


class PlannerRepository:
    """Read-only SQL the planner needs; one method per trigger."""

    def __init__(self, engine: Engine) -> None:
        """Bind the repository to an engine.

        Args:
            engine: SQLAlchemy engine on ``settings.DATABASE_URL``.
        """
        self.engine = engine

    def _rows(self, sql: str, params: dict | None = None, expanding: tuple[str, ...] = ()) -> list:
        stmt = text(sql)
        if expanding:
            stmt = stmt.bindparams(*(bindparam(name, expanding=True) for name in expanding))
        with self.engine.connect() as conn:
            return conn.execute(stmt, params or {}).fetchall()

    def all_cases(self) -> set[str]:
        """Return every decision that has at least one stored fact."""
        return {r[0] for r in self._rows("SELECT DISTINCT celex_id FROM celex_facts.facts")}

    def stale_facts(self, fact_names: list[str], predecessors: dict[str, list[str]]) -> dict[str, set[str]]:
        """Return, per fact, the decisions where an input changed after the fact.

        A fact is stale when one of its inputs' ``created_at`` (last value
        change) is newer than both the fact's own ``created_at`` and its newest
        *successful* audit attempt, so an unchanged re-extraction counts as
        checked while a failed one does not. Predecessors gate like in the
        ingest: a decision where one of them is missing or NA-like is left out,
        because the ingest would skip the fact without an audit row and the plan
        would never become empty. ``case_summary`` inputs never gate.

        Args:
            fact_names: Facts to examine.
            predecessors: Inputs per fact (predecessors, or context_facts for
                ``case_summary``); facts without inputs are never stale.

        Returns:
            Fact name -> decision ids; facts with no stale decision are absent.
        """
        sql = """
            WITH last_attempt AS (
                SELECT celex_id, max(created_at) AS at
                  FROM celex_facts.fact_ingestion_audit_trail
                 WHERE target_fact_name = :fact AND success
                 GROUP BY celex_id
            )
            SELECT DISTINCT d.celex_id
              FROM celex_facts.facts d
              JOIN celex_facts.facts p
                ON p.celex_id = d.celex_id AND p.name IN :inputs
              LEFT JOIN last_attempt a ON a.celex_id = d.celex_id
             WHERE d.name = :fact
               AND p.created_at > GREATEST(d.created_at, COALESCE(a.at, d.created_at))
        """
        # Same emptiness rule as Main_ingest_generic_facts._has_value on
        # ``value_json or value_md``.
        gate = """
               AND (SELECT count(DISTINCT q.name) FROM celex_facts.facts q
                     WHERE q.celex_id = d.celex_id AND q.name IN :inputs
                       AND trim(COALESCE(NULLIF(q.value_json, ''), q.value_md, '')) NOT IN ('', '[]', '{}', 'NA')
                   ) = :n_inputs
        """
        stale: dict[str, set[str]] = {}
        for fact in fact_names:
            inputs = list(dict.fromkeys(predecessors.get(fact) or []))
            if not inputs:
                continue
            query = sql if fact == SUMMARY_FACT else sql + gate
            params = {"fact": fact, "inputs": inputs, "n_inputs": len(inputs)}
            cases = {r[0] for r in self._rows(query, params, ("inputs",))}
            if cases:
                stale[fact] = cases
        return stale

    def new_documents_since(self, ts: datetime | None) -> set[str]:
        """Return decisions with a document downloaded after ``ts``.

        Args:
            ts: Creation time of the last finished plan; None means no baseline.

        Returns:
            Decision ids; empty without a baseline.
        """
        if ts is None:
            return set()
        if ts.tzinfo is None:
            ts = ts.astimezone()  # plans store naive local time; the column is timestamptz
        rows = self._rows(
            """SELECT DISTINCT decision_id FROM celex_facts.document_download_audit
                WHERE outcome = 'downloaded' AND decision_id IS NOT NULL AND created_at > :ts""",
            {"ts": ts},
        )
        return {r[0] for r in rows}

    def loader_changes(self, rows: dict[str, set[str]]) -> tuple[set[str], dict[str, str]]:
        """Compare loader rows with the download audit by URL.

        The audit records every candidate URL a download run saw (downloaded,
        registered, skipped or not selected) under the decision the downloader
        resolved the row to, which may differ from the loader's mapped id (a
        legacy corpus id, or a doublet merged into another decision). URLs are
        therefore the stable link: a row none of whose URLs was ever audited is
        added; a row with audited and unaudited URLs has a changed URL and
        belongs to the decision that owns its audited URLs today (following
        ``decision_merge``).

        Args:
            rows: Mapped loader id -> candidate URLs.

        Returns:
            ``(added, url_changed)``: added loader ids, and URL-changed loader id
            -> current decision key.
        """
        urls = sorted({u for row in rows.values() for u in row})
        if not urls:
            return set(), {}
        owner = dict(self._rows(
            """SELECT DISTINCT ON (source_url) source_url, decision_id
                 FROM celex_facts.document_download_audit
                WHERE source_url IN :urls AND decision_id IS NOT NULL
                ORDER BY source_url, created_at DESC""",
            {"urls": urls},
            ("urls",),
        ))
        merged_into = dict(self._rows(
            "SELECT merged_decision_id, merged_into FROM celex_facts.decision_merge"
        ))
        added: set[str] = set()
        changed: dict[str, str] = {}
        for loader_id, row_urls in rows.items():
            known = sorted(row_urls & owner.keys())
            if not known:
                if row_urls:
                    added.add(loader_id)
                continue
            if len(known) < len(row_urls):
                changed[loader_id] = _follow_merges(owner[known[0]], merged_into)
        return added, changed

    def chunks_newer_than_facts(self) -> set[str]:
        """Return decisions whose newest chunk is newer than a fact's last attempt.

        Returns:
            Decision ids (only used when ``PIPELINE_CHUNK_AGE_TRIGGER`` is on).
        """
        rows = self._rows(
            f"""WITH chunks AS (
                    SELECT celex_id, max(ingestion_timestamp) AS at
                      FROM celex_facts.{settings.EMBEDDING_TABLE} GROUP BY celex_id),
                 attempts AS (
                    SELECT celex_id, target_fact_name, max(created_at) AS at
                      FROM celex_facts.fact_ingestion_audit_trail
                     WHERE target_fact_name <> :summary
                     GROUP BY celex_id, target_fact_name)
               SELECT DISTINCT a.celex_id FROM attempts a JOIN chunks c ON c.celex_id = a.celex_id
                WHERE c.at > (a.at AT TIME ZONE :tz)""",
            {"summary": SUMMARY_FACT, "tz": local_tz_name()},
        )
        return {r[0] for r in rows}

    def mean_duration_s(self, fact_names: list[str]) -> dict[str, float]:
        """Return the mean successful audit duration per fact, in seconds.

        Args:
            fact_names: Facts to look up.

        Returns:
            Fact -> seconds; facts never audited get the ``fact`` default of
            ``settings.PIPELINE_STEP_SECONDS_PER_CASE``.
        """
        default = float(settings.PIPELINE_STEP_SECONDS_PER_CASE.get("fact", 0.0))
        if not fact_names:
            return {}
        rows = self._rows(
            """SELECT target_fact_name, avg(duration) FROM celex_facts.fact_ingestion_audit_trail
                WHERE success AND target_fact_name IN :names GROUP BY target_fact_name""",
            {"names": list(fact_names)},
            ("names",),
        )
        found = {r[0]: float(r[1] or 0.0) for r in rows}
        return {f: found.get(f, default) for f in fact_names}


def _follow_merges(key: str, merged_into: dict[str, str]) -> str:
    """Follow the ``decision_merge`` chain to the surviving decision."""
    seen = {key}
    while key in merged_into and merged_into[key] not in seen:
        key = merged_into[key]
        seen.add(key)
    return key


def local_tz_name() -> str:
    """Return the machine's IANA time zone, the zone naive DB timestamps were written in.

    ``facts.created_at`` and the audit trail store naive ``datetime.now()``
    values, while the session time zone of the database is UTC; comparisons
    with ``timestamptz`` columns convert the naive side with this zone.

    Returns:
        ``$TZ`` when set, else the zone ``/etc/localtime`` links to, else ``UTC``.
    """
    if os.environ.get("TZ"):
        return os.environ["TZ"].lstrip(":")
    target = os.path.realpath("/etc/localtime")
    marker = "zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else "UTC"


class LoaderRow(BaseModel):
    """One mapped loader decision as the planner needs it."""

    urls: set[str] = Field(description="Candidate source URLs of the row")
    new_key: str = Field(description="Key the downloader assigns when the row matches no existing decision")


def _new_key(decision: "DecisionRequest") -> str:
    """Return the downloader's key for a row without a master match."""
    from pipelines.download.identifiers import UnsafeIdentifier, celex_key, decision_id_for

    try:
        own = celex_key(decision.celex_number) if decision.celex_number else decision.decision_id
        first_case = decision.case_ids[0] if decision.case_ids else None
        return decision_id_for(None, own, first_case, decision.decision_date, set())[0]
    except UnsafeIdentifier:
        return decision.decision_id


def loader_rows(loader_file: Path) -> dict[str, LoaderRow]:
    """Map the loader file the way the downloader does.

    Args:
        loader_file: The loader spreadsheet (``settings.PIPELINE_LOADER_FILE``).

    Returns:
        Mapped loader decision id (what ``--ids`` filters on) -> URLs and the
        key a new decision would get.
    """
    from batch import Main_download_documents as downloader

    mapped = downloader._load(downloader.parse_args(["--loader-file", str(loader_file)]))
    return {
        d.decision_id: LoaderRow(urls={c.source_url for c in d.candidates if c.source_url}, new_key=_new_key(d))
        for d in mapped.decisions
    }


def _per_case(step: str) -> float:
    return float(settings.PIPELINE_STEP_SECONDS_PER_CASE.get(step, 0.0))


def _inputs(struct: dict) -> list[str]:
    key = "context_facts" if struct.get("pipeline") == SUMMARY_FACT else "predecessors"
    raw = struct.get(key) or []
    return [raw] if isinstance(raw, str) else list(raw)


def build_plan(
    repo: PlannerRepository,
    structs: list[dict],
    *,
    refresh_facts: set[str],
    completeness_gaps: dict[str, set[str]],
    loader: dict[str, "LoaderRow"],
    loader_file: str,
    last_done_at: datetime | None,
    only: Chapter | None,
    now: datetime,
    chunk_age_trigger: bool,
) -> RunPlan:
    """Build the run plan from every trigger source.

    Args:
        repo: Staleness queries.
        structs: Catalogue entries (validated by the prerequisites).
        refresh_facts: Facts the operator asked to refresh for every decision.
        completeness_gaps: Fact -> decisions missing it (fact completeness checker).
        loader: Mapped loader id -> row (see :func:`loader_rows`).
        loader_file: Loader path passed to the downloader.
        last_done_at: Creation time of the last finished plan, or None.
        only: Restrict the plan to one chapter (``--only``), or None.
        now: Plan creation time; also the run id.
        chunk_age_trigger: ``settings.PIPELINE_CHUNK_AGE_TRIGGER``.

    Returns:
        The plan; empty when nothing is stale (a report-only plan with ``only``).
    """
    names = [s["target_fact_name"] for s in structs if s.get("target_fact_name")]
    fact_names = [n for n in names if n != SUMMARY_FACT]
    triggers: list[str] = []

    added_ids, changed_ids = repo.loader_changes({i: r.urls for i, r in loader.items()})
    added = {loader[i].new_key if i in loader else i for i in added_ids}
    url_changed = set(changed_ids.values())
    download_ids = sorted(added_ids | set(changed_ids))  # --ids filters on mapped loader ids
    download_cases = sorted(added | url_changed)
    if added:
        triggers.append(f"{len(added)} added loader rows")
    if url_changed:
        triggers.append(f"{len(url_changed)} loader rows with a changed download URL")
    outside = repo.new_documents_since(last_done_at) - set(download_cases)
    if outside:
        triggers.append(f"{len(outside)} documents downloaded since the last run")
    new_docs = sorted(set(download_cases) | outside)

    forced: dict[str, set[str]] = defaultdict(set)
    if refresh_facts:
        triggers.append(f"refresh requested: {', '.join(sorted(refresh_facts))}")
        for case in repo.all_cases():
            forced[case] |= refresh_facts
    inputs = {s["target_fact_name"]: _inputs(s) for s in structs if s.get("target_fact_name")}
    stale = repo.stale_facts(names, inputs)
    summary_stale = stale.pop(SUMMARY_FACT, set())
    for fact, cases in sorted(stale.items()):
        triggers.append(f"{fact}: input changed for {len(cases)} decisions")
        for case in cases:
            forced[case].add(fact)
    if chunk_age_trigger:
        aged = repo.chunks_newer_than_facts()
        if aged:
            triggers.append(f"{len(aged)} decisions with chunks newer than their facts")
        for case in aged:
            forced[case] |= set(fact_names)
    for case in url_changed:  # facts built from the replaced document
        forced[case] |= set(fact_names)
    for case, roots in forced.items():
        forced[case] = (roots | dependents(structs, roots)) - {SUMMARY_FACT}

    gap_cases: dict[str, set[str]] = defaultdict(set)
    for fact, cases in completeness_gaps.items():
        if fact != SUMMARY_FACT:
            for case in cases:
                gap_cases[case].add(fact)
    if gap_cases:
        triggers.append(f"{len(gap_cases)} decisions with missing facts")

    durations = repo.mean_duration_s(names)
    steps: list[StepPlan] = []

    if download_ids:
        steps.append(StepPlan(
            chapter=Chapter.DOWNLOAD, name="download", module="batch.Main_download_documents",
            case_ids=download_ids,
            extra_args=["--loader-file", loader_file, "--force"],
            estimate_s=_per_case("download") * len(download_ids),
        ))
    # A changed URL replaces the document: the lazy modes would keep its old
    # markdown and chunks, so those decisions run the mode-aware steps forced.
    lazy_docs = sorted(set(new_docs) - url_changed)
    forced_docs = sorted(url_changed)
    for name, module, extra, gpu in _PREPARATION_STEPS:
        groups = (
            [(lazy_docs, []), (forced_docs, ["--mode", "force"])]
            if name in _MODE_STEPS
            else [(new_docs, [])]
        )
        for cases, mode in groups:
            if cases:
                steps.append(StepPlan(
                    chapter=Chapter.DOCUMENT_PREPARATION, name=name, module=module, case_ids=list(cases),
                    extra_args=list(extra) + mode, gpu=gpu, estimate_s=_per_case(name) * len(cases),
                ))

    # Unforced runs name their facts explicitly (--select-facts): the
    # catalogue's run_state_active is the operator's manual switch for plain
    # batch runs and must not decide what a new decision gets.
    plain_cases = sorted((set(new_docs) - url_changed) | set(gap_cases))
    selections: dict[frozenset[str], list[str]] = defaultdict(list)
    for case in plain_cases:
        wanted = set(fact_names) if case in new_docs else gap_cases[case]
        selections[frozenset(wanted)].append(case)
    for facts, cases in sorted(selections.items(), key=lambda item: (-len(item[1]), sorted(item[0]))):
        steps.append(StepPlan(
            chapter=Chapter.FACTS, name="facts", module=_FACTS_MODULE, case_ids=cases,
            select_facts=sorted(facts), gpu="bge",
            estimate_s=sum(durations[f] for f in facts) * len(cases),
        ))
    groups: dict[frozenset[str], list[str]] = defaultdict(list)
    for case in sorted(forced):
        if forced[case]:
            groups[frozenset(forced[case])].append(case)
    for facts, cases in sorted(groups.items(), key=lambda item: (-len(item[1]), sorted(item[0]))):
        steps.append(StepPlan(
            chapter=Chapter.FACTS, name="facts", module=_FACTS_MODULE, case_ids=cases,
            force_facts=sorted(facts), gpu="bge",
            estimate_s=sum(durations[f] for f in facts) * len(cases),
        ))

    graph_cases = sorted(
        set(new_docs)
        | {c for c, facts in forced.items() if facts & GRAPH_INPUT_FACTS}
        | {c for c, facts in gap_cases.items() if facts & GRAPH_INPUT_FACTS}
    )
    if graph_cases:
        steps.append(StepPlan(
            chapter=Chapter.ENTITY_GRAPH, name="entity_graph", module="batch.Main_materialize_entity_graph",
            case_ids=graph_cases, estimate_s=_per_case("entity_graph") * len(graph_cases),
        ))

    summary_cases = sorted(set(plain_cases) | {c for cases in groups.values() for c in cases} | summary_stale)
    if summary_cases:
        if summary_stale:
            triggers.append(f"{SUMMARY_FACT}: input changed for {len(summary_stale)} decisions")
        steps.append(StepPlan(
            chapter=Chapter.SUMMARIES, name="summaries", module=_FACTS_MODULE, case_ids=summary_cases,
            extra_args=["--filter-fact", SUMMARY_FACT], gpu="bge",
            estimate_s=durations.get(SUMMARY_FACT, _per_case("fact")) * len(summary_cases),
        ))

    if steps or only == Chapter.REPORT:
        for name, module in REPORT_STEPS:
            steps.append(StepPlan(chapter=Chapter.REPORT, name=name, module=module, estimate_s=_per_case("report")))

    if only is not None:
        steps = [s for s in steps if s.chapter == only]
    return RunPlan(
        run_id=f"{now:%Y%m%d%H%M%S}", created_at=now, status="pending", triggers=triggers, steps=steps, only=only,
    )



def fillable_gaps(frame: "pl.DataFrame", structs: list[dict]) -> dict[str, set[str]]:
    """Select the completeness gaps a plain fact run can actually fill.

    A missing, unconditional fact is fillable when each of its predecessors is
    stored with a non-NA value. Behind an empty or missing predecessor the
    ingest skips the fact without an audit row, so planning it would repeat on
    every run without effect. A missing conditional fact is fillable only when
    its last attempt failed (``last_audit_ok`` False, e.g. an LLM timeout): a
    condition that is not met is audited as a success with no fact row, so a
    failed attempt means the condition held.

    Args:
        frame: ``tools.fact_completeness_checker.build_completeness_frame``
            output (columns ``celex_id``, ``fact``, ``present``,
            ``empty_value``, ``conditional``, optionally ``last_audit_ok``).
        structs: Catalogue entries.

    Returns:
        Fact -> decisions to fill.
    """
    predecessors = {
        s["target_fact_name"]: [s["predecessors"]] if isinstance(s.get("predecessors"), str) else list(s.get("predecessors") or [])
        for s in structs
        if s.get("target_fact_name")
    }
    usable = {(r["celex_id"], r["fact"]) for r in frame.iter_rows(named=True) if r["present"] and not r["empty_value"]}
    gaps: dict[str, set[str]] = defaultdict(set)
    for row in frame.iter_rows(named=True):
        if row["present"]:
            continue
        if row["conditional"] and row.get("last_audit_ok") is not False:
            continue
        if all((row["celex_id"], p) in usable for p in predecessors.get(row["fact"], [])):
            gaps[row["fact"]].add(row["celex_id"])
    return dict(gaps)
