"""Tests for the main pipeline's terminal output: plan table, bar, log tailing."""

import io
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from batch.main_pipeline.console import (
    CASE_MARKER,
    CompactFormatter,
    LogCaseCounter,
    StepProgress,
    fit_list,
    plan_table,
)
from batch.main_pipeline.models import Chapter, RunPlan, StepPlan

_FACTS = [
    "OECD_sector", "companies_address", "companies_country", "company_recidivism", "court_list",
    "court_list_raw", "exemption_applicants", "fines", "leniency_applicants", "leniency_case_match",
    "leniency_granted", "leniency_rejected", "market_outside_eu",
]


def _plan() -> RunPlan:
    steps = [
        StepPlan(chapter=Chapter.FACTS, name="facts", module="m", case_ids=[f"C{i}" for i in range(406)],
                 done_case_ids=["C0"] * 25, force_facts=_FACTS, estimate_s=38400, status="failed"),
        StepPlan(chapter=Chapter.FACTS, name="facts", module="m", case_ids=["C1"], force_facts=_FACTS[:2]),
        StepPlan(chapter=Chapter.REPORT, name="retrieve_case_summary_md2disk", module="m"),
    ]
    return RunPlan(run_id="20261109150856", created_at=datetime(2026, 11, 9), status="failed",
                   triggers=["fines: input changed for 406 decisions " * 6], steps=steps,
                   failure="facts exit 1 at batch 2/17, log ~/a/celex_docs/pipeline_state/logs/x.log")


class TestFitList(unittest.TestCase):

    def test_everything_fits(self) -> None:
        self.assertEqual(fit_list(["a", "b"], 10), "a,b")

    def test_cut_with_count(self) -> None:
        self.assertEqual(fit_list(["alpha", "beta", "gamma"], 12), "alpha +2")

    def test_first_item_always_shown(self) -> None:
        self.assertEqual(fit_list(["a_very_long_fact_name"], 5), "a_very_long_fact_name")


class TestPlanTable(unittest.TestCase):

    def test_lines_fit_width(self) -> None:
        for line in plan_table(_plan(), 111):
            self.assertLessEqual(len(line), 111, line)

    def test_chapter_header_once_per_chapter(self) -> None:
        lines = plan_table(_plan(), 111)
        self.assertEqual(sum(line == "  chapter 3 facts" for line in lines), 1)
        self.assertIn("  chapter 6 report", lines)

    def test_row_content(self) -> None:
        row = next(line for line in plan_table(_plan(), 111) if "failed" in line and "25/406" in line)
        self.assertIn("10h40m", row)
        self.assertIn("force OECD_sector", row)
        self.assertRegex(row, r" \+\d+$")

    def test_corpus_step_and_failure(self) -> None:
        text = "\n".join(plan_table(_plan(), 111))
        self.assertIn("corpus", text)
        self.assertIn("  stopped: facts exit 1", text)


class TestCompactFormatter(unittest.TestCase):

    def _format(self, level: int) -> str:
        record = logging.LogRecord("p", level, __file__, 1, "hello %s", ("x",), None)
        return CompactFormatter().format(record)

    def test_info_has_time_and_message_only(self) -> None:
        self.assertRegex(self._format(logging.INFO), r"^\d\d:\d\d:\d\d hello x$")

    def test_warning_names_level(self) -> None:
        self.assertRegex(self._format(logging.WARNING), r"^\d\d:\d\d:\d\d WARNING hello x$")


class TestLogCaseCounter(unittest.TestCase):

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = Path(tmp.name) / "facts.log"
        self.log.write_bytes(CASE_MARKER + b"OLD\n")

    def test_counts_only_new_markers(self) -> None:
        seen: list[int] = []
        counter = LogCaseCounter(self.log, seen.append, poll_s=60)
        with open(self.log, "ab") as log:
            log.write(CASE_MARKER + b"A\nnoise\n" + CASE_MARKER + b"B\n")
        self.assertEqual(counter.poll(), 2)
        self.assertEqual(seen, [2])

    def test_marker_split_across_reads(self) -> None:
        counter = LogCaseCounter(self.log, lambda n: None, poll_s=60)
        with open(self.log, "ab") as log:
            log.write(CASE_MARKER[:7])
        self.assertEqual(counter.poll(), 0)
        with open(self.log, "ab") as log:
            log.write(CASE_MARKER[7:] + b"A\n")
        self.assertEqual(counter.poll(), 1)

    def test_context_manager_reads_rest_on_exit(self) -> None:
        seen: list[int] = []
        with LogCaseCounter(self.log, seen.append, poll_s=60):
            with open(self.log, "ab") as log:
                log.write(CASE_MARKER + b"A\n")
        self.assertEqual(seen, [1])

    def test_missing_log(self) -> None:
        counter = LogCaseCounter(self.log.with_name("none.log"), lambda n: None, poll_s=60)
        self.assertEqual(counter.poll(), 0)


class TestStepProgress(unittest.TestCase):

    def _bar(self) -> StepProgress:
        step = StepPlan(chapter=Chapter.FACTS, name="facts", module="m",
                        case_ids=[f"C{i}" for i in range(60)], done_case_ids=["C0", "C1"])
        return StepProgress(step, 3600, stream=io.StringIO())  # not a TTY: bar disabled, counts still kept

    def test_in_flight_case_not_counted(self) -> None:
        bar = self._bar()
        bar.batch_started(2)
        bar.cases_started(3)
        self.assertEqual(bar.done, 4)

    def test_batch_done_sets_count(self) -> None:
        bar = self._bar()
        bar.batch_done(27)
        bar.cases_started(1)
        self.assertEqual(bar.done, 27)
        bar.close()


if __name__ == "__main__":
    unittest.main()
