"""Unit tests for the per-call LLM token accounting.

No LiteLLM request is ever made: the callback is driven directly with the
arguments LiteLLM would pass it, so these tests pin the shape of the emitted
line and, above all, that a broken accounting path can never break an LLM call.
"""

import asyncio
import contextvars
import json
import logging
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import litellm
from litellm.utils import Choices, Message, ModelResponse
from litellm.utils import Usage as LiteLlmUsage

# Provide a dummy token before importing settings-dependent modules.
os.environ.setdefault("opencorporates_api_token", "dummy")

from config.settings import settings  # noqa: E402
from config.llm_usage import (  # noqa: E402
    COST_DECIMAL_PLACES,
    UNKNOWN_COST,
    UNKNOWN_TOKEN_COUNT,
    USAGE_LOG_PREFIX,
    PromptUsageLogger,
    UsageTotals,
    format_usage_totals,
    install_usage_logger,
    register_local_model_costs,
    usage_scope,
)

_START = datetime(2026, 8, 20, 12, 0, 0)
_END = _START + timedelta(seconds=23.45)


def _usage(prompt_tokens: int, completion_tokens: int) -> SimpleNamespace:
    """Return a stub response carrying a LiteLLM-shaped usage block.

    Args:
        prompt_tokens: Tokens the provider reports for the prompt.
        completion_tokens: Tokens the provider reports for the completion.

    Returns:
        An object exposing ``usage`` the way a LiteLLM response does.
    """
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )
    )


def _cost_probe_response(model_id: str):
    """Return a real LiteLLM response, for driving completion_cost.

    The SimpleNamespace stub used elsewhere is enough for the callback, which
    only reads attributes; completion_cost validates a genuine ModelResponse.

    Args:
        model_id: Bare model id to stamp on the response.

    Returns:
        A ModelResponse reporting 1000 prompt and 100 completion tokens.
    """
    return ModelResponse(
        id="probe",
        model=model_id,
        object="chat.completion",
        created=0,
        choices=[
            Choices(
                index=0,
                message=Message(role="assistant", content="hi"),
                finish_reason="stop",
            )
        ],
        usage=LiteLlmUsage(
            prompt_tokens=1000, completion_tokens=100, total_tokens=1100
        ),
    )


class PromptUsageLoggerSuccessTests(unittest.TestCase):
    """The success path reports the counts the provider actually returned."""

    def setUp(self) -> None:
        self.logger = PromptUsageLogger()

    def test_logs_the_reported_prompt_tokens(self):
        """The line carries prompt, completion, total and latency."""
        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event(
                {"model": "openai/qwen3-coder:30b"},
                _usage(12253, 642),
                _START,
                _END,
            )

        self.assertEqual(len(captured.records), 1)
        line = captured.records[0].getMessage()
        self.assertIn(USAGE_LOG_PREFIX, line)
        self.assertIn("model=openai/qwen3-coder:30b", line)
        self.assertIn("prompt_tokens=12253", line)
        self.assertIn("completion_tokens=642", line)
        self.assertIn("total_tokens=12895", line)
        self.assertIn("latency_s=23.45", line)

    def test_usage_may_be_a_plain_dict(self):
        """Providers that return usage as a dict are read the same way."""
        response = SimpleNamespace(usage={"prompt_tokens": 8000})

        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event({"model": "m"}, response, _START, _END)

        line = captured.records[0].getMessage()
        self.assertIn("prompt_tokens=8000", line)
        self.assertIn(f"completion_tokens={UNKNOWN_TOKEN_COUNT}", line)

    def test_missing_usage_logs_unknown_rather_than_raising(self):
        """A response without usage must not be counted as an empty prompt."""
        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event(
                {"model": "m"}, SimpleNamespace(), _START, _END
            )

        line = captured.records[0].getMessage()
        self.assertIn(f"prompt_tokens={UNKNOWN_TOKEN_COUNT}", line)

    def test_unusable_timestamps_do_not_cost_the_line(self):
        """Latency is context for the token count, never a reason to drop it."""
        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event({"model": "m"}, _usage(10, 2), None, None)

        line = captured.records[0].getMessage()
        self.assertIn("prompt_tokens=10", line)
        self.assertIn("latency_s=-1.00", line)

    def test_float_timestamps_are_accepted(self):
        """Epoch floats measure the same elapsed time as datetimes."""
        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event({"model": "m"}, _usage(10, 2), 100.0, 102.5)

        self.assertIn("latency_s=2.50", captured.records[0].getMessage())

    def test_accounting_failure_never_breaks_the_call(self):
        """A logger that raises is swallowed, not propagated to the request."""
        with mock.patch(
            "config.llm_usage.logger.info", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("config.llm_usage", level=logging.WARNING):
                self.logger.log_success_event(
                    {"model": "m"}, _usage(10, 2), _START, _END
                )


class PromptUsageLoggerFailureTests(unittest.TestCase):
    """A timeout is the failure this accounting exists for."""

    def test_failure_reports_the_exception_type_and_elapsed_time(self):
        """The line says what failed and how long it took to fail."""
        logger_under_test = PromptUsageLogger()

        with self.assertLogs("config.llm_usage", level=logging.WARNING) as captured:
            logger_under_test.log_failure_event(
                {"model": "m", "exception": TimeoutError("too slow")},
                None,
                _START,
                _END,
            )

        line = captured.records[0].getMessage()
        self.assertIn("failed=TimeoutError", line)
        self.assertIn("latency_s=23.45", line)


class InstallUsageLoggerTests(unittest.TestCase):
    """Registration is global, so it has to be idempotent."""

    def test_repeated_installation_registers_one_callback(self):
        """Every dspy.LM construction calls this; duplicates would double-log."""
        callbacks: list = []
        with mock.patch("config.llm_usage._usage_logger", None), mock.patch(
            "config.llm_usage.litellm.callbacks", callbacks
        ):
            first = install_usage_logger()
            second = install_usage_logger()

        self.assertIs(first, second)
        self.assertEqual(callbacks, [first])


class ResponseCostTests(unittest.TestCase):
    """Cost comes from the request parameters, not from the response."""

    def setUp(self) -> None:
        self.logger = PromptUsageLogger()

    def test_cost_is_read_from_the_request_kwargs(self):
        """LiteLLM writes response_cost onto the kwargs a callback receives."""
        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event(
                {"model": "deepseek/deepseek-reasoner", "response_cost": 0.00123456},
                _usage(1000, 200),
                _START,
                _END,
            )

        self.assertIn("cost_usd=0.00123456", captured.records[0].getMessage())

    def test_unpriced_model_logs_the_sentinel_not_zero(self):
        """An unmapped model and a free one must stay distinguishable."""
        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event(
                {"model": "openai/qwen3-coder:30b", "response_cost": None},
                _usage(1000, 200),
                _START,
                _END,
            )

        self.assertIn(
            f"cost_usd={UNKNOWN_COST:.{COST_DECIMAL_PLACES}f}",
            captured.records[0].getMessage(),
        )

    def test_a_registered_free_model_logs_a_real_zero(self):
        """0.0 is a measurement; it must not be reported as unknown."""
        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event(
                {"model": "openai/qwen3-coder:30b", "response_cost": 0.0},
                _usage(1000, 200),
                _START,
                _END,
            )

        self.assertIn("cost_usd=0.00000000", captured.records[0].getMessage())

    def test_the_token_fields_keep_their_order(self):
        """Existing greps over the line must survive the added field."""
        with self.assertLogs("config.llm_usage", level=logging.INFO) as captured:
            self.logger.log_success_event(
                {"model": "m", "response_cost": 0.5}, _usage(10, 2), _START, _END
            )

        line = captured.records[0].getMessage()
        self.assertLess(line.index("prompt_tokens="), line.index("cost_usd="))
        self.assertLess(line.index("cost_usd="), line.index("latency_s="))


class UsageTotalsTests(unittest.TestCase):
    """An unknown measurement must not be summed into a total."""

    def test_known_values_accumulate(self):
        """Two requests add up, per model."""
        totals = UsageTotals()
        totals.add_success("qwen3-coder:30b", 100, 20, 0.5, 1.5)
        totals.add_success("qwen3-coder:30b", 200, 30, 0.25, 2.5)

        self.assertEqual(totals.requests, 2)
        self.assertEqual(totals.prompt_tokens, 300)
        self.assertEqual(totals.completion_tokens, 50)
        self.assertEqual(totals.total_tokens, 350)
        self.assertAlmostEqual(totals.cost_usd, 0.75)
        self.assertAlmostEqual(totals.llm_seconds, 4.0)
        self.assertEqual(totals.by_model, {"qwen3-coder:30b": 2})

    def test_unknown_tokens_are_dropped_not_subtracted(self):
        """The -1 sentinel would otherwise corrupt the sum."""
        totals = UsageTotals()
        totals.add_success("m", UNKNOWN_TOKEN_COUNT, UNKNOWN_TOKEN_COUNT, 0.0, -1.0)

        self.assertEqual(totals.requests, 1)
        self.assertEqual(totals.prompt_tokens, 0)
        self.assertEqual(totals.llm_seconds, 0.0)

    def test_unpriced_requests_are_counted_separately(self):
        """A zero cost must be readable as 'free' or 'unpriced', not both."""
        totals = UsageTotals()
        totals.add_success("m", 10, 2, UNKNOWN_COST, 1.0)
        totals.add_success("m", 10, 2, 0.0, 1.0)

        self.assertEqual(totals.cost_usd, 0.0)
        self.assertEqual(totals.unpriced_requests, 1)

    def test_failures_are_counted_with_their_time(self):
        """A run that timed out half its calls must not look cheap silently."""
        totals = UsageTotals()
        totals.add_failure("m", 150.0)

        self.assertEqual(totals.failures, 1)
        self.assertEqual(totals.requests, 0)
        self.assertAlmostEqual(totals.llm_seconds, 150.0)

    def test_totals_serialise_for_the_audit_line(self):
        """BatchJobRunner writes model_dump() into pipeline_audit.jsonl."""
        totals = UsageTotals()
        totals.add_success("m", 10, 2, 0.0, 1.0)
        dumped = totals.model_dump()

        self.assertEqual(dumped["prompt_tokens"], 10)
        self.assertNotIn("_lock", dumped)
        json.dumps(dumped)  # must be JSON-serialisable

    def test_the_summary_line_is_greppable_with_the_request_lines(self):
        """One grep over the prefix has to collect the total too."""
        totals = UsageTotals()
        totals.add_success("qwen3-coder:30b", 100, 20, 0.5, 1.5)
        line = format_usage_totals(totals)

        self.assertIn(USAGE_LOG_PREFIX, line)
        self.assertIn("requests=1", line)
        self.assertIn("cost_usd=0.50000000", line)
        self.assertIn("by_model: qwen3-coder:30b=1", line)


class UsageScopeTests(unittest.TestCase):
    """Attribution of a request to the unit of work that caused it."""

    def setUp(self) -> None:
        self.logger = PromptUsageLogger()

    def _log_one_call(self, prompt_tokens: int = 100) -> None:
        """Drive the callback once, the way LiteLLM would.

        Args:
            prompt_tokens: Prompt tokens the stub response reports.
        """
        with self.assertLogs("config.llm_usage", level=logging.INFO):
            self.logger.log_success_event(
                {"model": "m", "response_cost": 0.25},
                _usage(prompt_tokens, 10),
                _START,
                _END,
            )

    def test_requests_inside_the_block_are_attributed_to_it(self):
        """The scope collects exactly what was requested inside it."""
        with usage_scope() as totals:
            self._log_one_call(100)
            self._log_one_call(200)

        self.assertEqual(totals.requests, 2)
        self.assertEqual(totals.prompt_tokens, 300)

    def test_requests_outside_any_scope_are_not_attributed(self):
        """A closed scope must not keep collecting."""
        with usage_scope() as totals:
            self._log_one_call(100)

        self._log_one_call(999)
        self.assertEqual(totals.prompt_tokens, 100)

    def test_concurrent_scopes_do_not_leak_into_each_other(self):
        """Two CELEX IDs on two pool workers must get their own counts."""
        collected: dict[int, UsageTotals] = {}

        def one_celex(worker: int) -> None:
            with usage_scope() as totals:
                self._log_one_call((worker + 1) * 1000)
                collected[worker] = totals

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(one_celex, range(2)))

        self.assertEqual(collected[0].prompt_tokens, 1000)
        self.assertEqual(collected[1].prompt_tokens, 2000)

    def test_scope_survives_the_pool_asyncio_and_to_thread_hops(self):
        """The regression this contextvar exists for.

        BatchJobRunner opens the scope on a ThreadPoolExecutor worker, the
        ingestion entrypoint then calls asyncio.run, and the pipeline calls
        asyncio.to_thread -- so the request fires two threads below the scope. A
        threading.local would record zero here.
        """

        async def pipeline() -> None:
            await asyncio.to_thread(self._log_one_call, 4242)

        def one_celex(_: int) -> UsageTotals:
            with usage_scope() as totals:
                asyncio.run(pipeline())
            return totals

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(one_celex, range(2)))

        for totals in results:
            self.assertEqual(totals.requests, 1)
            self.assertEqual(totals.prompt_tokens, 4242)


class PendingAccountingTests(unittest.TestCase):
    """LiteLLM reports a success after the call has already returned.

    Measured against the DGX: the completion returns to the caller at +0.705s
    and the success callback fires on LiteLLM's own thread pool at +0.704s --
    close enough that reading the totals immediately loses requests. The pre-call
    hook runs inline on the requesting thread, so counting there is what makes
    the wait possible.
    """

    def setUp(self) -> None:
        self.logger = PromptUsageLogger()

    def test_an_issued_request_is_outstanding_until_it_is_accounted(self):
        """wait_for_pending reports the timeout rather than lying."""
        totals = UsageTotals()
        totals.add_started()

        self.assertFalse(totals.wait_for_pending(timeout=0.05))

        totals.add_success("m", 10, 2, 0.0, 1.0)
        self.assertTrue(totals.wait_for_pending(timeout=0.05))

    def test_a_failure_settles_an_issued_request_too(self):
        """A timed-out request must not leave the scope waiting."""
        totals = UsageTotals()
        totals.add_started()
        totals.add_failure("m", 150.0)

        self.assertTrue(totals.wait_for_pending(timeout=0.05))

    def test_a_success_without_a_start_does_not_go_negative(self):
        """A request in flight when the callback was installed has no start.

        A negative counter would make every later wait return immediately.
        """
        totals = UsageTotals()
        totals.add_success("m", 10, 2, 0.0, 1.0)
        totals.add_started()

        self.assertFalse(totals.wait_for_pending(timeout=0.05))

    def test_the_pre_call_hook_counts_into_the_scope(self):
        """The hook LiteLLM runs inline is what the scope waits on."""
        with usage_scope() as totals:
            self.logger.log_pre_api_call(model="m", messages=[], kwargs={})
            self.assertFalse(totals.wait_for_pending(timeout=0.05))
            with self.assertLogs("config.llm_usage", level=logging.INFO):
                self.logger.log_success_event(
                    {"model": "m", "response_cost": 0.0},
                    _usage(10, 2),
                    _START,
                    _END,
                )

        self.assertEqual(totals.requests, 1)

    def test_the_scope_waits_for_a_callback_that_lands_after_the_block(self):
        """The regression: without the wait this scope reports zero requests.

        The background dispatch mirrors LiteLLM's own: its sync wrapper runs
        ``ctx.run(success_handler, ...)`` on a pool thread with a copy of the
        calling context (litellm/utils.py, "Copy the current context to
        propagate it to the background thread"). That copy is why the scope is
        visible over there at all; a bare Thread would start with an empty
        context and record nothing.
        """

        def late_callback() -> None:
            time.sleep(0.2)
            with self.assertLogs("config.llm_usage", level=logging.INFO):
                self.logger.log_success_event(
                    {"model": "m", "response_cost": 0.25},
                    _usage(1234, 10),
                    _START,
                    _END,
                )

        with usage_scope() as totals:
            # What LiteLLM does: count the request inline, answer the caller,
            # and report the usage from another thread a moment later.
            self.logger.log_pre_api_call(model="m", messages=[], kwargs={})
            context = contextvars.copy_context()
            worker = threading.Thread(target=context.run, args=(late_callback,))
            worker.start()
            self.addCleanup(worker.join)

        self.assertEqual(totals.requests, 1)
        self.assertEqual(totals.prompt_tokens, 1234)

    def test_a_callback_that_never_arrives_warns_and_releases(self):
        """A lost callback must not hang the batch that is waiting on it."""
        with mock.patch.object(settings, "LLM_USAGE_DRAIN_TIMEOUT", 0.05):
            with self.assertLogs("config.llm_usage", level=logging.WARNING) as caught:
                with usage_scope() as totals:
                    self.logger.log_pre_api_call(model="m", messages=[], kwargs={})

        self.assertIn("did not settle", caught.records[0].getMessage())
        self.assertEqual(totals.requests, 0)


class RegisterLocalModelCostsTests(unittest.TestCase):
    """Pricing a model LiteLLM does not know, without touching one it does."""

    def setUp(self) -> None:
        self.model_id = "test-only-qwen:1b"
        self.registered = set()
        # Patch the module's memo so registration is not skipped as a duplicate
        # and no state leaks into another test.
        self.memo_patch = mock.patch(
            "config.llm_usage._registered_model_ids", self.registered
        )
        self.memo_patch.start()
        self.addCleanup(self.memo_patch.stop)
        self.addCleanup(
            lambda: [
                litellm.model_cost.pop(key, None)
                for key in (self.model_id, f"openai/{self.model_id}")
            ]
        )

    def test_both_spellings_of_the_id_become_priceable(self):
        """dspy.LM uses openai/<id>; the callback reports the bare <id>.

        Asserted through completion_cost rather than through the dict keys:
        LiteLLM stores one normalised entry and strips the prefix on lookup, so
        what matters is that neither spelling raises "isn't mapped yet".
        """
        register_local_model_costs(f"openai/{self.model_id}")

        for spelling in (self.model_id, f"openai/{self.model_id}"):
            self.assertEqual(
                litellm.completion_cost(
                    completion_response=_cost_probe_response(self.model_id),
                    model=spelling,
                ),
                0.0,
            )

    def test_an_unregistered_model_is_what_the_gap_looked_like(self):
        """The bug being fixed: LiteLLM refuses to price an unmapped model."""
        with self.assertRaises(Exception):
            litellm.completion_cost(
                completion_response=_cost_probe_response(self.model_id),
                model=f"openai/{self.model_id}",
            )

    def test_the_declared_context_window_comes_from_settings(self):
        """An unregistered model has no window either."""
        register_local_model_costs(self.model_id)

        self.assertEqual(
            litellm.model_cost[self.model_id]["max_input_tokens"],
            settings.OLLAMA_LLM_INFERENCE_TOKEN_LIMIT,
        )

    def test_configured_rates_are_applied(self):
        """The knob is what makes a local run comparable to a cloud one."""
        with mock.patch.object(
            settings, "LOCAL_LLM_INPUT_COST_PER_TOKEN", 1e-8
        ), mock.patch.object(settings, "LOCAL_LLM_OUTPUT_COST_PER_TOKEN", 2e-8):
            register_local_model_costs(self.model_id)

        # 1000 prompt tokens at 1e-8 plus 100 completion tokens at 2e-8.
        self.assertAlmostEqual(
            litellm.completion_cost(
                completion_response=_cost_probe_response(self.model_id),
                model=self.model_id,
            ),
            1.2e-05,
        )

    def test_a_model_litellm_already_prices_is_left_alone(self):
        """Overwriting deepseek with 0.0 would report a paid run as free."""
        before = dict(litellm.model_cost["deepseek/deepseek-chat"])

        register_local_model_costs("deepseek/deepseek-chat")

        self.assertEqual(litellm.model_cost["deepseek/deepseek-chat"], before)

    def test_registration_never_raises(self):
        """An LM must stay constructible when its pricing is not."""
        with mock.patch(
            "config.llm_usage.litellm.register_model",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("config.llm_usage", level=logging.WARNING):
                register_local_model_costs(self.model_id)

        self.assertNotIn(self.model_id, self.registered)


if __name__ == "__main__":
    unittest.main()
