"""Tests for the progress tracker."""

import unittest
from collections import Counter

from pipelines.convert.models import ConversionItem, ConversionOutcome
from pipelines.convert.progress import ProgressTracker, format_duration


def _item(doc: str) -> ConversionItem:
    return ConversionItem(
        document_id=doc, decision_id="D", is_source=True, local_file_pdf="/x.pdf")


class ProgressTrackerTest(unittest.TestCase):
    """Progress line and heartbeat content."""

    def test_progress_line_format(self) -> None:
        now = [0.0]
        tracker = ProgressTracker(4, lambda: now[0])
        now[0] = 60.0
        tracker.finished(_item("a"), ConversionOutcome.CONVERTED, "advanced, 3 pages, 1m00s")
        now[0] = 120.0
        line = tracker.finished(_item("b"), ConversionOutcome.CONVERTED, "advanced")
        self.assertIn("[2/4]", line)
        self.assertIn("ok 2 failed 0 timeout 0", line)
        self.assertIn("elapsed 2m", line)
        self.assertIn("ETA 2m", line)

    def test_failed_and_timeout_counted(self) -> None:
        tracker = ProgressTracker(3, lambda: 0.0)
        tracker.finished(_item("a"), ConversionOutcome.FAILED, "x")
        line = tracker.finished(_item("b"), ConversionOutcome.TIMEOUT, "y")
        self.assertIn("ok 0 failed 1 timeout 1", line)

    def test_heartbeat_lists_states(self) -> None:
        tracker = ProgressTracker(4, lambda: 0.0)
        line = tracker.heartbeat("t1", Counter({"pending": 3, "running": 1}))
        self.assertIn("t1", line)
        self.assertIn("pending 3, running 1", line)

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(252, precise=True), "4m12s")
        self.assertEqual(format_duration(3120), "52m")
        self.assertEqual(format_duration(13200), "3h40m")
        self.assertEqual(format_duration(45), "45s")


if __name__ == "__main__":
    unittest.main()


class InjectedOutcomeTests(unittest.TestCase):
    """The tracker is reusable by the translation batch."""

    def test_custom_ok_outcomes_and_any_item_with_document_id(self) -> None:
        from types import SimpleNamespace

        from pipelines.translate.models import TranslationOutcome

        tracker = ProgressTracker(
            2, lambda: 0.0, ok_outcomes=frozenset({TranslationOutcome.TRANSLATED}),
            timeout_outcomes=frozenset())
        line = tracker.finished(
            SimpleNamespace(document_id="X_DE"), TranslationOutcome.TRANSLATED, "12 chunks")
        self.assertIn("[1/2] translated X_DE (12 chunks) | ok 1 failed 0", line)
