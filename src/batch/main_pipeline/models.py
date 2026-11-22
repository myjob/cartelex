"""Run plan models of the main pipeline.

A :class:`RunPlan` is the unit the operator approves, defers or resumes: the
ordered steps (chapter, script, cases) plus their progress. It is persisted as
JSON by :mod:`batch.main_pipeline.state_store` after every finished batch.
"""

from datetime import datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, Field


class Chapter(IntEnum):
    """Pipeline chapters in execution order."""

    PREREQUISITES = 0
    DOWNLOAD = 1
    DOCUMENT_PREPARATION = 2
    FACTS = 3
    ENTITY_GRAPH = 4
    SUMMARIES = 5
    REPORT = 6

    @property
    def label(self) -> str:
        """Human-readable chapter name for progress lines."""
        return self.name.lower().replace("_", " ")


StepStatus = Literal["pending", "running", "done", "failed"]
PlanStatus = Literal["pending", "running", "failed", "done", "superseded"]
GpuNeed = Literal["none", "mineru", "bge"]


class StepPlan(BaseModel):
    """One script invocation over a set of cases, run in batches."""

    chapter: Chapter = Field(description="Chapter the step belongs to")
    name: str = Field(description="Step name, e.g. 'convert', 'facts', 'report_celex_facts'")
    module: str = Field(description="Python module run with -m, e.g. 'batch.Main_convert_pdf_markdown'")
    case_ids: list[str] = Field(default_factory=list, description="Decisions the step processes; empty for corpus-wide steps")
    force_facts: list[str] = Field(default_factory=list, description="Facts passed to --force-facts (fact steps only)")
    select_facts: list[str] = Field(default_factory=list, description="Facts passed to --select-facts: extracted where missing, never forced")
    extra_args: list[str] = Field(default_factory=list, description="Further CLI arguments appended to every call")
    estimate_s: float = Field(default=0.0, description="Estimated duration of the whole step in seconds")
    gpu: GpuNeed = Field(default="none", description="Local GPU consumer the step starts, checked before it runs")
    done_case_ids: list[str] = Field(default_factory=list, description="Cases whose batch finished with exit code 0")
    status: StepStatus = Field(default="pending", description="Execution state of the step")

    @property
    def remaining_case_ids(self) -> list[str]:
        """Cases not yet done, in plan order."""
        done = set(self.done_case_ids)
        return [case for case in self.case_ids if case not in done]


class RunPlan(BaseModel):
    """Everything one orchestrator run will do, and how far it got."""

    run_id: str = Field(description="Creation time as YYYYMMDDHHMMSS; names the state and log files")
    created_at: datetime = Field(description="When the plan was built")
    status: PlanStatus = Field(default="pending", description="pending = waiting for the operator's go")
    triggers: list[str] = Field(default_factory=list, description="Why the plan exists, one reason per line")
    steps: list[StepPlan] = Field(default_factory=list, description="Steps in execution order")
    failure: str | None = Field(default=None, description="Reason of the stop: step, exit code, batch and log path")
    tolerated: list[str] = Field(
        default_factory=list,
        description="Batches that exited with LLM timeouts only and were let through; the next plan re-plans them",
    )
    finished_at: datetime | None = Field(default=None, description="When the run finished; the next plan's baseline")
    only: Chapter | None = Field(default=None, description="Chapter the plan was restricted to (--only); such plans set no baseline")

    @property
    def estimate_s(self) -> float:
        """Total estimated duration in seconds."""
        return sum(step.estimate_s for step in self.steps)

    @property
    def is_empty(self) -> bool:
        """True when nothing is stale."""
        return not self.steps
