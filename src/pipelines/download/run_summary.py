"""End-of-run counters of the download orchestrator."""

from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from pipelines.download.models import FetchResult, Outcome


class RunSummary(BaseModel):
    """End-of-run counters (also returned by a dry run, as planned counts).

    ``outcomes`` counts one entry per audited candidate (plus one
    ``no_pdf_source`` per decision without a PDF); in a dry run a planned
    fetch counts as ``downloaded``. Row failures (mapper failures, unsafe
    decision ids) and decision-level errors are counted separately; row
    failures do not affect ``exit_code``.
    """

    run_id: UUID = Field(description="Id of the batch run.")
    dry_run: bool = Field(description="True when nothing was written.")
    outcomes: dict[Outcome, int] = Field(
        default_factory=lambda: dict.fromkeys(Outcome, 0),
        description="Audited candidate outcomes.",
    )
    failure_reasons: dict[str, int] = Field(
        default_factory=dict,
        description="Failed candidates per reason token (text before ':').",
    )
    row_failures: int = Field(default=0, description="Audited row failures.")
    decisions_processed: int = Field(default=0, description="Decisions handled.")
    decisions_skipped: int = Field(
        default=0, description="Decisions dropped because no safe id exists."
    )
    decisions_errored: int = Field(
        default=0, description="Decisions aborted by an unexpected exception."
    )
    review_reasons: int = Field(
        default=0, description="Review reasons on the processed decisions."
    )

    @computed_field(description="1 if any candidate failed or decision errored.")
    @property
    def exit_code(self) -> int:
        """Process exit code of the run.

        Mapper row failures and unsafe decision ids (``row_failures``) are
        audited and reported but do not make the run fail: a loader can carry
        permanent bad rows.

        Returns:
            1 when any candidate failed or any decision errored; else 0.
        """
        return 1 if self.outcomes[Outcome.FAILED] + self.decisions_errored else 0

    def count(self, result: FetchResult) -> None:
        """Count one candidate result (and its failure reason token).

        Args:
            result: Audited or planned candidate result.
        """
        self.outcomes[result.outcome] += 1
        if result.outcome is Outcome.FAILED and result.failure_reason:
            token = result.failure_reason.split(":", 1)[0]
            self.failure_reasons[token] = self.failure_reasons.get(token, 0) + 1
