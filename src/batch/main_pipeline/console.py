"""Terminal output of the main pipeline: plan table, compact log lines, step bar.

The orchestrator runs each step as a subprocess whose own tqdm bar goes into the
per-step log, so the operator's terminal would otherwise stay silent for a whole
batch (~40 min for facts). :class:`StepProgress` draws one bar per step on the
orchestrator's terminal instead, advanced by :class:`LogCaseCounter`, which
tails the step log for the ``BatchJobRunner`` per-case marker.

Everything here is presentation: the plan, its persistence and the batch
semantics stay in :mod:`batch.main_pipeline.runner`.
"""

import logging
import os
import shutil
import sys
import textwrap
import threading
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import TextIO

from tqdm import tqdm

from batch.main_pipeline.models import RunPlan, StepPlan

PROGRESS_LOGGER = "batch.main_pipeline.progress"
# Line BatchJobRunner logs when it starts a CELEX (serial mode, which is how
# the pipeline calls it). The count of these lines in a batch's slice of the
# step log, minus the one in flight, is the number of cases already done.
CASE_MARKER = b"Processing CELEX ID: "
DEFAULT_WIDTH = 120
# "HH:MM:SS " in front of every progress line (CompactFormatter).
TIME_PREFIX_WIDTH = 9
_POLL_SECONDS = 2.0
_TIME_FORMAT = "%H:%M:%S"


def terminal_width() -> int:
    """Return the terminal width in columns, 120 when it cannot be detected."""
    return shutil.get_terminal_size((DEFAULT_WIDTH, 40)).columns


def fmt_duration(seconds: float) -> str:
    """Format a duration as 12s, 38m or 1h05m.

    Args:
        seconds: Duration; negative values count as zero.

    Returns:
        The compact duration string.
    """
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def fit_list(items: list[str], width: int) -> str:
    """Join items with commas, cut to ``width`` with a ``+N`` tail for the rest.

    Args:
        items: Values to show, in order.
        width: Maximum length of the result.

    Returns:
        As many items as fit, followed by ``+N`` when some were left out.
    """
    shown: list[str] = []
    for index, item in enumerate(items):
        rest = len(items) - index - 1
        candidate = ",".join([*shown, item])
        tail = f" +{rest}" if rest else ""
        if len(candidate) + len(tail) > width and shown:
            return f"{','.join(shown)} +{len(items) - len(shown)}"
        shown.append(item)
    return ",".join(shown)


def plan_table(plan: RunPlan, width: int) -> list[str]:
    """Render a plan as lines that fit the terminal width.

    One header line per chapter, then one row per step: step name, status,
    cases done/total, estimate and the forced or selected facts, cut to fit.

    Args:
        plan: The plan to show.
        width: Terminal columns available after the time prefix.

    Returns:
        The lines, without trailing newlines.
    """
    lines = [f"plan {plan.run_id} ({plan.status}), estimate {fmt_duration(plan.estimate_s)}"]
    for reason in plan.triggers:
        lines += textwrap.wrap(reason, width, initial_indent="  because: ", subsequent_indent="           ")
    name_width = max((len(step.name) for step in plan.steps), default=4)
    chapter = None
    for step in plan.steps:
        if step.chapter != chapter:
            chapter = step.chapter
            lines.append(f"  chapter {step.chapter} {step.chapter.label}")
        cases = f"{len(step.done_case_ids)}/{len(step.case_ids)}" if step.case_ids else "corpus"
        row = f"    {step.name:<{name_width}} {step.status:<7} {cases:>9} {fmt_duration(step.estimate_s):>6}"
        facts, verb = (step.force_facts, "force") if step.force_facts else (step.select_facts, "fill")
        if facts:
            room = width - len(row) - len(verb) - 2
            row += f" {verb} {fit_list(facts, max(room, 12))}"
        lines.append(row)
    for note in plan.tolerated:
        lines += textwrap.wrap(note, width, initial_indent="  tolerated: ", subsequent_indent="             ")
    if plan.failure:
        lines += textwrap.wrap(plan.failure, width, initial_indent="  stopped: ", subsequent_indent="           ")
    return lines


class CompactFormatter(logging.Formatter):
    """``HH:MM:SS message``; the level name is added only from WARNING up."""

    def __init__(self) -> None:
        """Set the short time format."""
        super().__init__(datefmt=_TIME_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        """Format one record.

        Args:
            record: The log record.

        Returns:
            The formatted line.
        """
        level = f"{record.levelname} " if record.levelno >= logging.WARNING else ""
        return f"{self.formatTime(record, self.datefmt)} {level}{record.getMessage()}"


class TqdmHandler(logging.Handler):
    """Write records through ``tqdm.write`` so an active bar is redrawn below them."""

    def __init__(self, stream: TextIO) -> None:
        """Bind the output stream.

        Args:
            stream: Where the lines go, normally ``sys.stdout``.
        """
        super().__init__()
        self.stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        """Write one record above the bar.

        Args:
            record: The log record.
        """
        try:
            tqdm.write(self.format(record), file=self.stream)
        except Exception:  # noqa: BLE001 -- logging must never raise
            self.handleError(record)


def configure_console(stream: TextIO = sys.stdout) -> None:
    """Give the progress logger compact, bar-safe terminal output.

    The progress logger stops propagating, so its lines no longer carry the
    root console's ``asctime - level - name`` prefix (~60 columns). It keeps
    the root's file handlers, so ``logs/application.log`` still records them.

    Args:
        stream: Terminal stream.
    """
    progress = logging.getLogger(PROGRESS_LOGGER)
    progress.propagate = False
    progress.setLevel(logging.INFO)
    handler = TqdmHandler(stream)
    handler.setFormatter(CompactFormatter())
    progress.handlers = [handler, *(h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler))]


class StepProgress:
    """One terminal bar per step: cases done, rate, step ETA and the run's remaining estimate."""

    def __init__(self, step: StepPlan, later_estimate_s: float, stream: TextIO = sys.stdout) -> None:
        """Open the bar.

        Args:
            step: The step about to run; its ``done_case_ids`` are the start.
            later_estimate_s: Estimate of the plan's steps after this one.
            stream: Terminal stream; the bar is off when it is not a TTY.
        """
        self._base = len(step.done_case_ids)
        self.done: int = self._base
        self._lock = threading.Lock()
        self._bar = tqdm(
            total=len(step.case_ids) or None,
            initial=self._base,
            desc=f"ch{int(step.chapter)} {step.name}",
            unit="case",
            file=stream,
            dynamic_ncols=True,
            disable=not _is_tty(stream),
            bar_format="{desc} {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]{postfix}",
        )
        if later_estimate_s:
            self._bar.set_postfix_str(f"then ~{fmt_duration(later_estimate_s)}", refresh=False)

    def batch_started(self, done: int) -> None:
        """Mark the start of a batch.

        Args:
            done: Cases of the step done before this batch.
        """
        with self._lock:
            self._base = done
            self._set(done)

    def cases_started(self, started: int) -> None:
        """Report how many cases of the current batch have started.

        Args:
            started: Marker count so far; all but the last one are finished.
        """
        with self._lock:
            self._set(self._base + max(started - 1, 0))

    def batch_done(self, done: int) -> None:
        """Set the bar to the step's done count after a finished batch.

        Args:
            done: Cases of the step done so far.
        """
        with self._lock:
            self._base = done
            self._set(done)

    def close(self) -> None:
        """Close the bar, leaving its last state on the terminal."""
        self._bar.close()

    def _set(self, value: int) -> None:
        # Kept here, not read from tqdm: a disabled bar (no TTY) never moves n.
        if value > self.done:
            self._bar.update(value - self.done)
            self.done = value


class LogCaseCounter:
    """Tail a step log from an offset and report the per-case marker count.

    Used as a context manager around one batch's subprocess: a daemon thread
    polls the file, and the count is reported once more on exit.
    """

    def __init__(self, log_path: Path, on_count: Callable[[int], None], poll_s: float = _POLL_SECONDS) -> None:
        """Remember where the batch's slice of the log starts.

        Args:
            log_path: The per-step log the subprocess appends to.
            on_count: Called with the marker count whenever it grows.
            poll_s: Seconds between polls.
        """
        self.log_path = Path(log_path)
        self.on_count = on_count
        self.poll_s = poll_s
        self.offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        self.count = 0
        self._tail = b""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="log-case-counter", daemon=True)

    def __enter__(self) -> "LogCaseCounter":
        """Start polling."""
        self._thread.start()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> None:
        """Stop polling and read the rest of the log once."""
        self._stop.set()
        self._thread.join()
        self.poll()

    def poll(self) -> int:
        """Read what was appended since the last poll and update the count.

        Returns:
            The marker count of this batch so far.
        """
        if not self.log_path.exists():
            return self.count
        with open(self.log_path, "rb") as log:
            log.seek(self.offset)
            chunk = log.read()
        self.offset += len(chunk)
        # Keep a marker-sized tail so a marker split across two reads still counts once.
        data = self._tail + chunk
        found = data.count(CASE_MARKER)
        self._tail = data[-(len(CASE_MARKER) - 1):] if data else b""
        if found:
            self.count += found
            self.on_count(self.count)
        return self.count

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            self.poll()



def _is_tty(stream: TextIO) -> bool:
    try:
        return os.isatty(stream.fileno())
    except (AttributeError, OSError, ValueError):
        return False
