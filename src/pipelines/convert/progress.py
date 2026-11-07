"""Progress line and heartbeat formatting for the conversion batch.

One PDF can take minutes, so the batch logs after every finished file (any
outcome) with running counters and an ETA derived from the mean wall time per
finished file, plus a heartbeat while it waits for the server.
"""

from collections import Counter
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from pipelines.convert.models import ConversionOutcome

_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
_OK_OUTCOMES = frozenset({ConversionOutcome.CONVERTED})
_TIMEOUT_OUTCOMES = frozenset({ConversionOutcome.TIMEOUT})


class _HasDocumentId(Protocol):
    """Any queue item with a registry document id."""

    document_id: str


def format_duration(seconds: float, *, precise: bool = False) -> str:
    """Format a duration as ``3h40m``, ``52m`` or ``45s``.

    Args:
        seconds: Duration in seconds (negative values are clamped to 0).
        precise: When True, minute-range values keep their seconds (``4m12s``).

    Returns:
        Compact human-readable duration.
    """
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, _SECONDS_PER_HOUR)
    minutes, secs = divmod(rest, _SECONDS_PER_MINUTE)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s" if precise else f"{minutes}m"
    return f"{secs}s"


class ProgressTracker:
    """Counts finished files and renders the progress and heartbeat lines."""

    def __init__(
        self,
        total: int,
        clock: Callable[[], float],
        *,
        ok_outcomes: frozenset[StrEnum] = _OK_OUTCOMES,
        timeout_outcomes: frozenset[StrEnum] = _TIMEOUT_OUTCOMES,
    ) -> None:
        """Start the tracker; elapsed time counts from construction.

        Args:
            total: Number of files expected to finish in this run.
            clock: Monotonic clock returning seconds.
            ok_outcomes: Outcomes counted as ok (default: converted).
            timeout_outcomes: Outcomes counted as timeout (default: timeout).
        """
        self._ok_outcomes = ok_outcomes
        self._timeout_outcomes = timeout_outcomes
        self._total = total
        self._clock = clock
        self._start = clock()
        self._done = 0
        self._ok = 0
        self._failed = 0
        self._timeout = 0

    def finished(self, item: _HasDocumentId, outcome: StrEnum, detail: str) -> str:
        """Register one finished file and return its progress line.

        Args:
            item: The finished queue item.
            outcome: Its outcome; converted counts as ok, timeout as timeout,
                everything else as failed.
            detail: Free text shown in parentheses (tier, pages, duration or reason).

        Returns:
            ``[n/total] outcome id (detail) | ok a failed b timeout c | elapsed X ETA Y``.
        """
        self._done += 1
        if outcome in self._ok_outcomes:
            self._ok += 1
        elif outcome in self._timeout_outcomes:
            self._timeout += 1
        else:
            self._failed += 1
        elapsed = self._clock() - self._start
        remaining = max(0, self._total - self._done)
        eta = elapsed / self._done * remaining
        return (
            f"[{self._done}/{self._total}] {outcome.value} {item.document_id} ({detail}) "
            f"| ok {self._ok} failed {self._failed} timeout {self._timeout} "
            f"| elapsed {format_duration(elapsed)} ETA {format_duration(eta)}"
        )

    def heartbeat(self, task_id: str, states: Counter[str]) -> str:
        """Render the heartbeat line for a task that is still running.

        Args:
            task_id: MinerU task id being polled.
            states: Count of unfinished files per MinerU file status.

        Returns:
            ``task <id>: pending 3, running 1`` plus progress and elapsed time.
        """
        shown = ", ".join(f"{name} {count}" for name, count in sorted(states.items()))
        elapsed = format_duration(self._clock() - self._start)
        return f"task {task_id}: {shown or 'no files'} | {self._done}/{self._total} finished | elapsed {elapsed}"
