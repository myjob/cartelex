"""Tests for the knowledge-extraction queue batch runner.

Covers the fact-id parsing contract and the queue-status outcome it drives:
a row whose ``fact_ids`` cannot be parsed must end ``failed``, never ``done``.

Mocks sit at the system boundaries only (DB session helpers, LLM
configuration, the ingest module), so the tests run in isolation.
"""

import sys
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from batch.Main_ingest_knowledge import KnowledgeIngestionRunner


def _queue_item(fact_ids: list | None) -> SimpleNamespace:
    """Build a queue row that survives the Stage-1 structural filter.

    Parameters
    ----------
    fact_ids:
        The JSONB ``fact_ids`` payload under test.

    Returns
    -------
    SimpleNamespace
        A stand-in for a ``KnowledgeExtractionQueue`` ORM row.
    """
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_prompt="What fines were imposed on the parent undertaking?",
        agent_answer=(
            "The Commission imposed a fine on the parent undertaking for the "
            "full duration of the infringement, applying joint and several "
            "liability with its subsidiary."
        ),
        supporting_facts="fine_amount: EUR 10 000 000",
        fact_ids=fact_ids,
        chunk_ids=["38344_5856_3"],
    )


class TestParseFactIds(unittest.TestCase):
    """Unit tests for the all-or-nothing fact-id parser."""

    def test_parses_well_formed_ids(self):
        """Well-formed UUID strings become UUID objects in input order."""
        first, second = uuid.uuid4(), uuid.uuid4()
        parsed = KnowledgeIngestionRunner._parse_fact_ids([str(first), str(second)])
        self.assertEqual(parsed, [first, second])

    def test_empty_and_none_yield_empty_list(self):
        """An absent or empty payload parses to an empty list, not an error."""
        self.assertEqual(KnowledgeIngestionRunner._parse_fact_ids(None), [])
        self.assertEqual(KnowledgeIngestionRunner._parse_fact_ids([]), [])

    def test_malformed_string_raises_value_error(self):
        """A non-UUID string aborts the whole list."""
        with self.assertRaises(ValueError) as ctx:
            KnowledgeIngestionRunner._parse_fact_ids([str(uuid.uuid4()), "not-a-uuid"])
        self.assertIn("not-a-uuid", str(ctx.exception))

    def test_non_string_element_raises_value_error(self):
        """TypeError/AttributeError from uuid.UUID is normalised to ValueError."""
        for bad in (42, None, ["nested"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    KnowledgeIngestionRunner._parse_fact_ids([bad])


class TestRunFactIdOutcome(unittest.TestCase):
    """The queue status a row ends in, driven by its ``fact_ids`` payload."""

    def _run_with(self, queue_item: SimpleNamespace) -> tuple[MagicMock, ...]:
        """Drive ``run()`` over a single queue row with boundaries mocked.

        Parameters
        ----------
        queue_item:
            The row ``_fetch_pending`` should return.

        Returns
        -------
        tuple[MagicMock, ...]
            The ``_mark_done``, ``_mark_failed`` and module mocks, for
            assertions by the caller.
        """
        runner = KnowledgeIngestionRunner()
        module = MagicMock()
        module.forward.return_value = SimpleNamespace(
            ingested_items=[object()], errors=[]
        )

        with (
            patch.object(sys, "argv", ["Main_ingest_knowledge.py", "--no-judge"]),
            patch.object(KnowledgeIngestionRunner, "_configure_llm"),
            patch.object(
                KnowledgeIngestionRunner, "_fetch_pending", return_value=[queue_item]
            ),
            patch.object(KnowledgeIngestionRunner, "_mark_done") as mark_done,
            patch.object(KnowledgeIngestionRunner, "_mark_failed") as mark_failed,
            patch("batch.Main_ingest_knowledge.KnowledgeRepository"),
            patch("batch.Main_ingest_knowledge.KnowledgeIngestor"),
            patch(
                "batch.Main_ingest_knowledge.KnowledgeIngestModule",
                return_value=module,
            ),
        ):
            runner.run()

        return mark_done, mark_failed, module

    def test_malformed_fact_ids_marked_failed_without_extraction(self):
        """A corrupt payload fails the row and spends no extraction call."""
        item = _queue_item([str(uuid.uuid4()), "not-a-uuid"])

        mark_done, mark_failed, module = self._run_with(item)

        module.forward.assert_not_called()
        mark_done.assert_not_called()
        mark_failed.assert_called_once()
        queue_id, error_msg = mark_failed.call_args.args
        self.assertEqual(queue_id, item.id)
        self.assertIn("unparseable fact_ids", error_msg)

    def test_well_formed_fact_ids_reach_the_module(self):
        """A valid payload is parsed to UUIDs and the row ends done."""
        fact_id = uuid.uuid4()
        item = _queue_item([str(fact_id)])

        mark_done, mark_failed, module = self._run_with(item)

        mark_failed.assert_not_called()
        mark_done.assert_called_once_with(item.id)
        self.assertEqual(
            module.forward.call_args.kwargs["supporting_fact_ids"], [fact_id]
        )


if __name__ == "__main__":
    unittest.main()
