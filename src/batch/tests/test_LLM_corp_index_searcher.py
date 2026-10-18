"""Unit tests for the LLM company-index batch.

Mocks live only at the boundaries: the DSPy module and the repository. No LLM
call and no DB access occurs, and ``time.sleep`` is patched so the throttle does
not slow the suite.

The subject is the loop's contract with a backlog that costs money to process:
a failure must not close a company permanently (the row is left unwritten, so
the next run retries it), an isolated failure must not end the run, and a streak
of them must.
"""

import os
import sys
import threading
import unittest
from unittest import mock

# Provide a dummy token before importing settings-dependent modules so the suite
# runs even when ``opencorporates_api_token`` is absent from the env.
os.environ.setdefault("opencorporates_api_token", "dummy")

# Ensure src is on the path (src/batch/tests -> src).
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from batch.Main_LLM_corp_index_searcher import (  # noqa: E402
    DEFAULT_MIN_NAME_LENGTH,
    DEFAULT_WORKERS,
    IN_FLIGHT_MULTIPLIER,
    LlmCorpIndexSearcher,
    _build_parser,
)
from pipelines.corp_index.models import (  # noqa: E402
    LlmCompanyIndexProfile,
    LlmCompanyIndexRow,
    LlmCompanyIndexTarget,
)


def _target(name: str = "Brasserie de Wiltz", **overrides: object) -> LlmCompanyIndexTarget:
    """Build a roster target with sensible defaults.

    Args:
        name: The company name.
        **overrides: Attribute values replacing the defaults.

    Returns:
        The target to feed the searcher.
    """
    fields: dict[str, object] = {
        "company_name": name,
        "address": "14 rue Joseph Simon, L-9550 Wiltz",
        "iso_country_code": "LU",
        "jurisdication_code": "LU",
    }
    fields.update(overrides)
    return LlmCompanyIndexTarget(**fields)


# Stands in for whatever the corp_index profile resolves to; the searcher must
# take the label from the module rather than re-reading settings, so the tests
# pin a value that no setting could supply.
TEST_MODEL_ID = "test-model"


def _row(name: str = "Brasserie de Wiltz") -> LlmCompanyIndexRow:
    """Build a persistable profile row for a company.

    Args:
        name: The company name the profile belongs to.

    Returns:
        The row a successful module call would return.
    """
    return LlmCompanyIndexRow(
        company_name=name,
        llm_model_id=TEST_MODEL_ID,
        profile=LlmCompanyIndexProfile(
            canonical_name=f"{name} S.E.C.S.",
            legal_form="S.E.C.S.",
            life_cycle_status="dissolved",
        ),
    )


def _build_searcher(
    targets: list[LlmCompanyIndexTarget],
) -> tuple[LlmCorpIndexSearcher, mock.MagicMock, mock.MagicMock]:
    """Construct a searcher over mocked module and repository boundaries.

    Args:
        targets: The target set the repository should return.

    Returns:
        A tuple of the searcher, the mock module, and the mock repository.
    """
    module = mock.MagicMock()
    module.llm_model_id = TEST_MODEL_ID
    module.side_effect = lambda company_name, **_: mock.Mock(output=_row(company_name))
    repository = mock.MagicMock()
    repository.select_targets.return_value = targets
    return LlmCorpIndexSearcher(module=module, repository=repository), module, repository


class TestLlmCorpIndexSearcher(unittest.TestCase):
    """The per-company profiling loop."""

    def test_every_target_is_profiled_and_persisted(self) -> None:
        """A clean run writes one profile per company."""
        searcher, module, repository = _build_searcher([_target("A Ltd"), _target("B Ltd")])

        summary = searcher.run()

        self.assertEqual(summary["targets"], 2)
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertFalse(summary["stopped_on_error"])
        self.assertEqual(summary["llm_model_id"], TEST_MODEL_ID)
        self.assertEqual(module.call_count, 2)
        self.assertEqual(repository.upsert_company.call_count, 2)

    def test_known_attributes_are_handed_to_the_module(self) -> None:
        """The roster's hints travel with the company name."""
        searcher, module, _ = _build_searcher([
            _target(lei="5493008JEWD6T3I5ZJ82", handelsregister=None)
        ])

        searcher.run()

        module.assert_called_once_with(
            company_name="Brasserie de Wiltz",
            address="14 rue Joseph Simon, L-9550 Wiltz",
            iso_country_code="LU",
            jurisdication_code="LU",
            lei="5493008JEWD6T3I5ZJ82",
            handelsregister=None,
            use_address=False,
        )

    def test_dry_run_calls_nothing_and_writes_nothing(self) -> None:
        """A dry run reports the target set at zero cost."""
        searcher, module, repository = _build_searcher([_target("A Ltd"), _target("B Ltd")])

        summary = searcher.run(dry_run=True)

        self.assertEqual(summary["targets"], 2)
        self.assertEqual(summary["processed"], 0)
        module.assert_not_called()
        repository.upsert_company.assert_not_called()

    def test_empty_target_set_is_a_no_op(self) -> None:
        """A completed backlog does nothing at all."""
        searcher, module, repository = _build_searcher([])

        summary = searcher.run()

        self.assertEqual(summary["targets"], 0)
        module.assert_not_called()
        repository.upsert_company.assert_not_called()

    def test_selection_arguments_reach_the_repository(self) -> None:
        """The CLI's selection knobs are passed through to the query."""
        searcher, _, repository = _build_searcher([])

        searcher.run(
            marker=None,
            iso_country_code="DE",
            refresh=True,
            min_name_length=5,
            limit=25,
        )

        repository.select_targets.assert_called_once_with(
            marker=None,
            iso_country_code="DE",
            include_existing=True,
            retry_unknown=False,
            min_name_length=5,
            limit=25,
        )

    def test_retry_unknown_reaches_the_repository(self) -> None:
        """The switch that narrows the run to the profiles that answered nothing."""
        searcher, _, repository = _build_searcher([])

        searcher.run(retry_unknown=True)

        self.assertTrue(
            repository.select_targets.call_args.kwargs["retry_unknown"],
        )

    def test_use_address_reaches_the_module(self) -> None:
        """The switch that changes the question, not the target set."""
        searcher, module, _ = _build_searcher([_target("A Ltd")])

        searcher.run(use_address=True, workers=1)

        self.assertTrue(module.call_args.kwargs["use_address"])

    def test_the_two_switches_are_independent(self) -> None:
        """Neither implies the other, which is what makes the A/B possible.

        Running the same rows with the address off and then on is the only way
        to tell whether the address moves the two fields it is meant to move.
        """
        searcher, module, repository = _build_searcher([_target("A Ltd")])

        searcher.run(retry_unknown=True, workers=1)

        self.assertTrue(repository.select_targets.call_args.kwargs["retry_unknown"])
        self.assertFalse(module.call_args.kwargs["use_address"])

    def test_switches_are_recorded_in_the_summary(self) -> None:
        """Nothing on the stored row says which arm produced it."""
        searcher, _, _ = _build_searcher([])

        summary = searcher.run(retry_unknown=True, use_address=True)

        self.assertTrue(summary["retry_unknown"])
        self.assertTrue(summary["use_address"])

    def test_failed_company_is_skipped_not_closed(self) -> None:
        """A failure writes nothing, so the next run retries that company."""
        searcher, module, repository = _build_searcher([_target("A Ltd"), _target("B Ltd")])
        module.side_effect = [
            RuntimeError("provider timeout"),
            mock.Mock(output=_row("B Ltd")),
        ]

        summary = searcher.run(workers=1)

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertFalse(summary["stopped_on_error"])
        self.assertEqual(repository.upsert_company.call_count, 1)
        self.assertEqual(
            repository.upsert_company.call_args.args[0].company_name, "B Ltd"
        )

    def test_error_streak_stops_the_run(self) -> None:
        """Back-to-back failures are a provider outage, not a data problem."""
        targets = [_target(f"Company {index}") for index in range(5)]
        searcher, module, repository = _build_searcher(targets)
        module.side_effect = RuntimeError("invalid api key")

        summary = searcher.run(workers=1, max_consecutive_errors=2)

        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["processed"], 0)
        self.assertTrue(summary["stopped_on_error"])
        self.assertEqual(module.call_count, 2)
        repository.upsert_company.assert_not_called()

    def test_error_streak_resets_on_success(self) -> None:
        """Scattered failures never accumulate into a stop."""
        targets = [_target(f"Company {index}") for index in range(4)]
        searcher, module, repository = _build_searcher(targets)
        module.side_effect = [
            RuntimeError("timeout"),
            mock.Mock(output=_row("Company 1")),
            RuntimeError("timeout"),
            mock.Mock(output=_row("Company 3")),
        ]

        summary = searcher.run(workers=1, max_consecutive_errors=2)

        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["failed"], 2)
        self.assertFalse(summary["stopped_on_error"])

    def test_throttle_is_applied_before_each_call(self) -> None:
        """--sleep-seconds throttles the loop when asked to."""
        searcher, _, _ = _build_searcher([_target("A Ltd"), _target("B Ltd")])

        with mock.patch("batch.Main_LLM_corp_index_searcher.time.sleep") as sleep:
            searcher.run(workers=1, sleep_seconds=0.5)

        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.5)


class TestConcurrency(unittest.TestCase):
    """The threaded path.

    One call costs 20-46s against a provider that reasons whatever it is told,
    so the backlog is latency-bound and the workers exist to overlap that wait.
    These tests pin that the overlap is real, that the bookkeeping survives
    out-of-order completion, and that a stop does not first work through every
    queued company.
    """

    def test_calls_are_genuinely_in_flight_together(self) -> None:
        """Four workers means four calls waiting at once, not four in sequence.

        A barrier is the deterministic form of this assertion: it releases only
        when the fourth thread arrives, so a serial implementation deadlocks
        into ``BrokenBarrierError`` instead of quietly passing a timing check.
        """
        barrier = threading.Barrier(4, timeout=5)
        targets = [_target(f"Company {index}") for index in range(4)]
        searcher, module, _ = _build_searcher(targets)

        def profile_at_the_barrier(company_name: str, **_: object) -> mock.Mock:
            barrier.wait()
            return mock.Mock(output=_row(company_name))

        module.side_effect = profile_at_the_barrier

        summary = searcher.run(workers=4)

        self.assertEqual(summary["processed"], 4)
        self.assertEqual(summary["failed"], 0)

    def test_every_company_is_profiled_exactly_once(self) -> None:
        """Out-of-order completion still writes each company once."""
        targets = [_target(f"Company {index}") for index in range(20)]
        searcher, module, repository = _build_searcher(targets)

        summary = searcher.run(workers=8)

        self.assertEqual(summary["processed"], 20)
        self.assertEqual(module.call_count, 20)
        written = {
            call.args[0].company_name
            for call in repository.upsert_company.call_args_list
        }
        self.assertEqual(written, {f"Company {index}" for index in range(20)})

    def test_stop_abandons_the_queued_backlog(self) -> None:
        """A provider-side stop cancels the rest instead of paying for it.

        Only ``workers * IN_FLIGHT_MULTIPLIER`` companies are submitted at a
        time, topped up one per delivered result, so the ceiling on what a stop
        can waste is that window plus the results the caller took. Without the
        bound this test fails outright: queueing all 60 up front lets the pool
        run every one of them before the second failure is even consumed.
        """
        targets = [_target(f"Company {index}") for index in range(60)]
        searcher, module, repository = _build_searcher(targets)
        module.side_effect = RuntimeError("invalid api key")
        workers = 2

        summary = searcher.run(workers=workers, max_consecutive_errors=2)

        self.assertTrue(summary["stopped_on_error"])
        self.assertEqual(summary["failed"], 2)
        self.assertLessEqual(
            module.call_count, workers * IN_FLIGHT_MULTIPLIER + summary["failed"]
        )
        repository.upsert_company.assert_not_called()


class TestCliDefaults(unittest.TestCase):
    """The argparse surface."""

    def test_defaults_target_the_opencorporates_residue(self) -> None:
        """Out of the box the batch works the fk_opencorp_idx = 'NA' backlog."""
        args = _build_parser().parse_args([])

        self.assertEqual(args.workers, DEFAULT_WORKERS)

        self.assertEqual(args.marker, "NA")
        self.assertFalse(args.all_markers)
        self.assertFalse(args.refresh)
        self.assertFalse(args.retry_unknown)
        self.assertFalse(args.use_address)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.limit, 0)
        self.assertEqual(args.min_name_length, DEFAULT_MIN_NAME_LENGTH)

    def test_flags_are_parsed(self) -> None:
        """The selection flags reach the namespace."""
        args = _build_parser().parse_args(
            ["--all-markers", "--refresh", "--iso-country-code", "DE", "--limit", "25"]
        )

        self.assertTrue(args.all_markers)
        self.assertTrue(args.refresh)
        self.assertEqual(args.iso_country_code, "DE")
        self.assertEqual(args.limit, 25)

    def test_the_retry_flags_parse_together_and_apart(self) -> None:
        """Composable on the command line, not only in the API."""
        both = _build_parser().parse_args(["--retry-unknown", "--use-address"])
        self.assertTrue(both.retry_unknown)
        self.assertTrue(both.use_address)

        control = _build_parser().parse_args(["--retry-unknown"])
        self.assertTrue(control.retry_unknown)
        self.assertFalse(control.use_address)


if __name__ == "__main__":
    unittest.main()
