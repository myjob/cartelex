"""Per-call LLM token and cost accounting, logged from LiteLLM's own response.

Every LiteLLM response carries ``resp.usage.prompt_tokens``, and nothing in
this project read it -- so the one quantity that decides both the prompt-size
circuit breaker (``settings.OLLAMA_LLM_INFERENCE_PROMPT_TOKEN_LIMIT``) and the
request deadline (``settings.OLLAMA_TIMEOUT``) was never measured. Server-side
timing sweeps say what the hardware does at every prompt size; this module
records which prompt sizes are actually sent, so those two knobs can be set
from a p99 instead of from a two-year-old incident. See
``docs/infra/Performance of LLM with MTP.md``.

Cost is the same story one level further on. LiteLLM computes a per-request
cost only for models in its own price map, and no locally served tag is in it:
``completion_cost(model="openai/qwen3-coder:30b")`` raises "This model isn't
mapped yet", so LiteLLM stores ``response_cost = None`` and a local run is
priceless in the unhelpful sense. :func:`register_local_model_costs` puts the
served models into that map at the rates in ``settings.LOCAL_LLM_*_COST_PER_TOKEN``,
which turns the cost into a number that can be summed and compared against a
DeepSeek run. The rates default to 0.0 because local inference is free at the
margin; the point is that 0.0 is a measurement and ``None`` is not.

The hook is a LiteLLM callback rather than a ``dspy.BaseCallback`` because
DSPy's ``on_lm_end`` receives only the outputs list: usage would have to be
scraped from ``lm.history[-1]``, which attributes to the wrong call once a
batch runs with ``--max-workers`` above 1, and carries no latency. LiteLLM
hands over the response object and both timestamps per request.

A DSPy cache hit never reaches LiteLLM and so logs nothing, which is correct --
no request, no prefill, no tokens.

Attribution to a unit of work (one CELEX) is a :class:`contextvars.ContextVar`
rather than a ``threading.local`` because the request does not run on the
thread that started the CELEX. ``BatchJobRunner`` hands each CELEX to a
``ThreadPoolExecutor`` worker, that worker calls ``asyncio.run``, and the
pipeline then calls ``asyncio.to_thread`` -- three threads deep. Both
``asyncio.run`` and ``asyncio.to_thread`` *copy* the context, so the var travels
down, and because the copies share one :class:`UsageTotals` object by reference,
counts added on the innermost thread are visible to the scope that opened it. A
thread-local would silently record zeros. A pool worker starts with an empty
context, so the scope must be opened inside the worker, not around the pool.

The last hop is LiteLLM's own: it reports a success from its thread pool rather
than inline, and its sync wrapper submits ``ctx.run(success_handler, ...)`` with
a copy of the calling context, which is what carries the scope across. Its async
(``acompletion``) path submits without that copy, so a request made through it
would be counted in the process totals but not attributed to a CELEX. Every call
site in this project is sync -- the pipelines reach DSPy through
``asyncio.to_thread`` -- so this does not currently arise.

Exports:
    PromptUsageLogger -- the LiteLLM callback emitting one line per request.
    UsageTotals -- an accumulator over many requests.
    install_usage_logger() -- register the singleton, idempotently.
    register_local_model_costs() -- price a locally served model for LiteLLM.
    usage_scope() -- accumulate the requests made inside a block.
    drain_usage() -- wait for in-flight accounting before reading a total.
    process_usage_totals() -- the totals for the whole process.
    format_usage_totals() -- the one-line rendering of an accumulator.
    USAGE_LOG_PREFIX -- the fixed line prefix, for greppers and tests.
"""

import contextvars
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import litellm
from litellm.integrations.custom_logger import CustomLogger
from pydantic import BaseModel, Field, PrivateAttr

from config.settings import settings

logger = logging.getLogger(__name__)

# Fixed prefix of every usage line, so a run can be aggregated with
# ``grep -o 'prompt_tokens=[0-9]*' logs/application.log``.
USAGE_LOG_PREFIX = "LLM usage"

# Marker distinguishing the end-of-run total from the per-request lines, which
# share USAGE_LOG_PREFIX so one grep collects both.
USAGE_TOTAL_MARKER = "TOTAL"

# Token count reported when a provider returns no usage block at all. Negative
# rather than 0 so an absent measurement is never mistaken for an empty prompt
# when the distribution is computed.
UNKNOWN_TOKEN_COUNT = -1

# Decimal places the cost is logged with. Eight rather than the conventional
# six because a GPU-hour-amortized local rate is small: at 1e-8 USD per token a
# real 17-token request costs 1.9e-07, which six places round to a flat
# 0.000000 and make the knob look broken. Cloud costs stay readable at eight.
# Only the log line is rounded -- the audit trail carries the full float.
COST_DECIMAL_PLACES = 8

# The per-request line, as a %-style template built once rather than spelled
# inline, so the cost precision above cannot drift from the one in
# format_usage_totals. Interpolation stays lazy -- logging does it, and only if
# the line is actually emitted.
_USAGE_LINE_FORMAT = (
    "%s model=%s prompt_tokens=%d completion_tokens=%d total_tokens=%d "
    f"cost_usd=%.{COST_DECIMAL_PLACES}f latency_s=%.2f"
)

# Cost reported when LiteLLM priced nothing. Negative for the same reason, and
# it matters more here: a model missing from the price map and a model that is
# genuinely free both produce 0.0 otherwise, and only one of them is a fact.
UNKNOWN_COST = -1.0

# LiteLLM provider whose transport serves the local models. Ollama is reached
# through its OpenAI-compatible endpoint (see config.llm_factory), so its price
# map entries must declare the same provider the request is made under.
_LOCAL_LITELLM_PROVIDER = "openai"

# Model ids already handed to litellm.register_model. Registration is itself
# idempotent, but it warns once per call about absent cache-cost fields, and
# create_lm runs per LM construction.
_registered_model_ids: set[str] = set()
_registration_lock = threading.Lock()


def _token_count(usage: Any, field: str) -> int:
    """Return one token count from a LiteLLM usage block.

    Args:
        usage: The response's ``usage`` attribute, which providers return as
            either a pydantic model or a plain dict, or None when absent.
        field: Usage key to read, e.g. ``prompt_tokens``.

    Returns:
        The token count, or :data:`UNKNOWN_TOKEN_COUNT` when the provider
        reported none or reported something that is not an integer.
    """
    if usage is None:
        return UNKNOWN_TOKEN_COUNT

    value = usage.get(field) if isinstance(usage, dict) else getattr(usage, field, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN_TOKEN_COUNT
    return int(value)


def _response_cost(kwargs: dict[str, Any]) -> float:
    """Return the cost LiteLLM computed for a request.

    LiteLLM writes the figure onto its ``model_call_details`` before invoking
    callbacks (``litellm_logging.py``), and that mapping is what a callback
    receives as ``kwargs`` -- so the cost is read from the request parameters,
    not from the response.

    Args:
        kwargs: The request parameters LiteLLM was called with.

    Returns:
        The cost in USD, or :data:`UNKNOWN_COST` when LiteLLM priced nothing --
        which is what happens for any model absent from its price map, until
        :func:`register_local_model_costs` has added it.
    """
    value = kwargs.get("response_cost")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return UNKNOWN_COST
    return float(value)


def _elapsed_seconds(start_time: Any, end_time: Any) -> float:
    """Return the request duration in seconds.

    Args:
        start_time: Request start, a ``datetime`` in every current LiteLLM
            path; a float epoch is accepted too.
        end_time: Request end, same types.

    Returns:
        Seconds elapsed, or ``-1.0`` when the timestamps are unusable. Latency
        is context for the token count, so an odd pair must not cost the line.
    """
    if isinstance(start_time, datetime) and isinstance(end_time, datetime):
        return (end_time - start_time).total_seconds()
    if isinstance(start_time, (int, float)) and isinstance(end_time, (int, float)):
        return float(end_time - start_time)
    return -1.0


class UsageTotals(BaseModel):
    """Token, cost and latency totals over a set of LiteLLM requests.

    One instance covers one unit of work -- a CELEX for the audit trail, the
    process for the end-of-run summary. It is mutated from whichever thread
    LiteLLM reported the request on, so every mutation holds the condition. A
    total is only complete once :meth:`wait_for_pending` has returned true:
    LiteLLM reports a success after the call has already returned to the caller,
    so reading these fields the moment the work finishes reads them too early.

    Unknown measurements are dropped rather than summed. A provider that
    reported no usage would otherwise subtract 1 from the token total, and a
    model LiteLLM cannot price would subtract a dollar from the bill.
    :attr:`unpriced_requests` keeps that omission visible, so a zero cost over a
    hundred requests can be read as either "free" or "unpriced" rather than
    being ambiguous.

    Args:
        requests: Successful requests counted.
        failures: Requests that raised, which report time but no usage.
        prompt_tokens: Sum of the reported prompt tokens.
        completion_tokens: Sum of the reported completion tokens.
        cost_usd: Sum of the costs LiteLLM was able to compute.
        unpriced_requests: Successful requests LiteLLM could not price.
        llm_seconds: Summed request service time. Not wall-clock: concurrent
            requests overlap, so this exceeds the elapsed time of a parallel run
            and is the LLM-time bill rather than the duration.
        by_model: Successful request count per model id.
    """

    requests: int = Field(default=0, description="Successful requests counted.")
    failures: int = Field(default=0, description="Requests that raised.")
    prompt_tokens: int = Field(default=0, description="Sum of prompt tokens.")
    completion_tokens: int = Field(
        default=0, description="Sum of completion tokens."
    )
    cost_usd: float = Field(default=0.0, description="Sum of computable costs.")
    unpriced_requests: int = Field(
        default=0, description="Successful requests LiteLLM could not price."
    )
    llm_seconds: float = Field(
        default=0.0, description="Summed request service time, not wall-clock."
    )
    by_model: dict[str, int] = Field(
        default_factory=dict, description="Successful request count per model id."
    )

    # Guards every mutation and wakes wait_for_pending. A Condition rather than
    # a Lock because closing a scope has to wait for accounting still in flight.
    _condition: threading.Condition = PrivateAttr(
        default_factory=threading.Condition
    )
    # Requests issued but not yet accounted for. Not a field: it is always zero
    # by the time a total is read or reported, so it would only be noise in the
    # audit line.
    _pending: int = PrivateAttr(default=0)

    @property
    def total_tokens(self) -> int:
        """Return the summed prompt and completion tokens.

        Returns:
            Prompt plus completion tokens over every counted request.
        """
        return self.prompt_tokens + self.completion_tokens

    def add_started(self) -> None:
        """Record that a request has been issued but not yet accounted for.

        Called from LiteLLM's pre-call hook, which runs inline on the thread
        making the request -- unlike the success callback, which does not. This
        is what :meth:`wait_for_pending` waits on, and the only reason a total
        read at the end of a unit of work is complete.
        """
        with self._condition:
            self._pending += 1

    def wait_for_pending(self, timeout: float) -> bool:
        """Block until every issued request has been accounted for.

        Args:
            timeout: Seconds to wait before giving up.

        Returns:
            True when nothing is outstanding, False when the timeout expired
            first -- meaning a callback never arrived and this total undercounts.
        """
        with self._condition:
            return self._condition.wait_for(
                lambda: self._pending <= 0, timeout=timeout
            )

    def add_success(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        elapsed_seconds: float,
    ) -> None:
        """Fold one successful request into the totals.

        Args:
            model: Model id the request was served by.
            prompt_tokens: Reported prompt tokens, or
                :data:`UNKNOWN_TOKEN_COUNT`.
            completion_tokens: Reported completion tokens, or
                :data:`UNKNOWN_TOKEN_COUNT`.
            cost_usd: Computed cost, or :data:`UNKNOWN_COST`.
            elapsed_seconds: Request duration, or a negative value when the
                timestamps were unusable.
        """
        with self._condition:
            self.requests += 1
            self.by_model[model] = self.by_model.get(model, 0) + 1
            if prompt_tokens >= 0:
                self.prompt_tokens += prompt_tokens
            if completion_tokens >= 0:
                self.completion_tokens += completion_tokens
            if cost_usd >= 0:
                self.cost_usd += cost_usd
            else:
                self.unpriced_requests += 1
            if elapsed_seconds >= 0:
                self.llm_seconds += elapsed_seconds
            self._settle_one()

    def add_failure(self, model: str, elapsed_seconds: float) -> None:
        """Fold one failed request into the totals.

        A failure consumed prefill time without returning usage, so it is
        counted separately rather than being dropped: a run whose cost looks
        small because half its requests timed out must say so.

        Args:
            model: Model id the request was aimed at.
            elapsed_seconds: Time spent before the failure, or a negative value
                when the timestamps were unusable.
        """
        with self._condition:
            self.failures += 1
            if elapsed_seconds >= 0:
                self.llm_seconds += elapsed_seconds
            self._settle_one()

    def _settle_one(self) -> None:
        """Mark one issued request as accounted for. Call holding the condition.

        Floors at zero rather than going negative: a request already in flight
        when the callback was installed produces a completion with no matching
        start, and that must not leave the counter permanently below zero where
        a later wait would return immediately.
        """
        self._pending = max(0, self._pending - 1)
        self._condition.notify_all()


def format_usage_totals(totals: UsageTotals) -> str:
    """Render an accumulator as one log line.

    Args:
        totals: The accumulator to render.

    Returns:
        A ``key=value`` line in the same shape as the per-request lines, so one
        grep over ``USAGE_LOG_PREFIX`` collects both.
    """
    by_model = " ".join(
        f"{model}={count}" for model, count in sorted(totals.by_model.items())
    )
    return (
        f"{USAGE_LOG_PREFIX} {USAGE_TOTAL_MARKER} "
        f"requests={totals.requests} failures={totals.failures} "
        f"prompt_tokens={totals.prompt_tokens} "
        f"completion_tokens={totals.completion_tokens} "
        f"total_tokens={totals.total_tokens} "
        f"cost_usd={totals.cost_usd:.{COST_DECIMAL_PLACES}f} "
        f"unpriced_requests={totals.unpriced_requests} "
        f"llm_seconds={totals.llm_seconds:.2f} "
        f"by_model: {by_model or 'none'}"
    )


# Totals for every request the process makes, for the end-of-run summary.
_process_totals = UsageTotals()

# Totals for the unit of work currently in scope, or None outside any scope.
# See the module docstring for why this is a ContextVar and not a thread-local.
_scope_totals: contextvars.ContextVar[UsageTotals | None] = contextvars.ContextVar(
    "llm_usage_scope_totals", default=None
)


def process_usage_totals() -> UsageTotals:
    """Return the accumulator covering every request this process made.

    Returns:
        The process-wide singleton, safe to read once the work is done.
    """
    return _process_totals


@contextmanager
def usage_scope() -> Iterator[UsageTotals]:
    """Accumulate the LLM requests made inside the block.

    Open the scope on the thread that owns the unit of work -- in
    ``BatchJobRunner`` that is inside the pool worker, one scope per CELEX. Every
    request made below it, on any thread reached through ``asyncio.run`` or
    ``asyncio.to_thread``, lands in the yielded accumulator.

    Nesting replaces rather than merges: the inner scope collects its own
    requests and the outer one resumes afterwards, having missed them. Only one
    level is used today.

    On exit the scope waits for the accounting of any request still in flight,
    because LiteLLM reports a success on its own thread pool roughly a
    millisecond after the call has already returned. Without the wait the
    yielded totals are read too early and lose the tail of the block's requests.

    Yields:
        A fresh accumulator, complete once the block exits.
    """
    totals = UsageTotals()
    token = _scope_totals.set(totals)
    try:
        yield totals
    finally:
        # Drain before the reset, so the scope is unambiguously still in force
        # for the whole wait. LiteLLM's pool thread holds its own copy of the
        # context taken when the request was issued, so this ordering is belt
        # and braces rather than load-bearing -- but the cheap ordering is the
        # one that stays correct if that copying ever changes.
        drain_usage(totals)
        _scope_totals.reset(token)


def drain_usage(totals: UsageTotals) -> None:
    """Wait for LiteLLM to finish accounting for the requests in a total.

    Args:
        totals: The accumulator to settle. A timeout is logged rather than
            raised: an incomplete count is a reporting problem, and must not
            fail the work that produced it.
    """
    if not totals.wait_for_pending(settings.LLM_USAGE_DRAIN_TIMEOUT):
        logger.warning(
            "LLM usage accounting did not settle within %ss; the reported "
            "totals undercount the requests actually made.",
            settings.LLM_USAGE_DRAIN_TIMEOUT,
        )


class PromptUsageLogger(CustomLogger):
    """Log the token usage and cost of every completed LiteLLM request.

    Emits one line per request at INFO through the root handlers already
    configured by ``Config.setup_logging`` (``logs/application.log``), in a
    fixed ``key=value`` shape so a batch can be aggregated without a parser, and
    folds the same numbers into the process totals and the scope in force.

    Nothing here may break a request: an LLM call must not fail because its
    accounting did. Both handlers therefore swallow their own exceptions.
    """

    def log_pre_api_call(self, model: str, messages: Any, kwargs: Any) -> None:
        """Record a request as issued, before it is sent.

        This is the only hook LiteLLM runs inline on the thread making the
        request; the success callback is dispatched to LiteLLM's own thread pool
        and lands about a millisecond *after* the completion has already been
        returned to the caller. Counting here is what lets a scope know how many
        results it is still waiting for, instead of reading its totals early and
        silently losing the last request of every CELEX.

        Args:
            model: Model id the request is aimed at.
            messages: The request messages. Unused; the prompt is not logged.
            kwargs: The request parameters. Unused at this point -- usage and
                cost do not exist yet.
        """
        try:
            _process_totals.add_started()
            scope = _scope_totals.get()
            if scope is not None:
                scope.add_started()
        except Exception:
            logger.warning("Failed to count an issued LLM request.", exc_info=True)

    def log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Log the token usage and cost of a successful request.

        Args:
            kwargs: The request parameters LiteLLM was called with, carrying
                ``response_cost``.
            response_obj: The provider response, carrying ``usage``.
            start_time: Request start timestamp.
            end_time: Request end timestamp.
        """
        try:
            usage = getattr(response_obj, "usage", None)
            model = kwargs.get("model", "unknown")
            prompt_tokens = _token_count(usage, "prompt_tokens")
            completion_tokens = _token_count(usage, "completion_tokens")
            cost_usd = _response_cost(kwargs)
            elapsed_seconds = _elapsed_seconds(start_time, end_time)

            logger.info(
                _USAGE_LINE_FORMAT,
                USAGE_LOG_PREFIX,
                model,
                prompt_tokens,
                completion_tokens,
                _token_count(usage, "total_tokens"),
                cost_usd,
                elapsed_seconds,
            )
            self._accumulate_success(
                model, prompt_tokens, completion_tokens, cost_usd, elapsed_seconds
            )
        except Exception:
            logger.warning("Failed to log LLM usage for a request.", exc_info=True)

    def log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Log a failed request, which reports no usage but does report time.

        A timeout is the failure this accounting exists for, and how long it
        took to hit is the data point: it says whether
        ``settings.OLLAMA_TIMEOUT`` fired or the server closed the connection
        first.

        Args:
            kwargs: The request parameters LiteLLM was called with.
            response_obj: The exception or partial response LiteLLM captured.
            start_time: Request start timestamp.
            end_time: Request failure timestamp.
        """
        try:
            exception = kwargs.get("exception", response_obj)
            model = kwargs.get("model", "unknown")
            elapsed_seconds = _elapsed_seconds(start_time, end_time)
            logger.warning(
                "%s model=%s failed=%s latency_s=%.2f",
                USAGE_LOG_PREFIX,
                model,
                type(exception).__name__,
                elapsed_seconds,
            )
            self._accumulate_failure(model, elapsed_seconds)
        except Exception:
            logger.warning("Failed to log an LLM failure.", exc_info=True)

    @staticmethod
    def _accumulate_success(
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        elapsed_seconds: float,
    ) -> None:
        """Fold a successful request into the process totals and the scope.

        Args:
            model: Model id the request was served by.
            prompt_tokens: Reported prompt tokens, or the unknown sentinel.
            completion_tokens: Reported completion tokens, or the sentinel.
            cost_usd: Computed cost, or the unknown sentinel.
            elapsed_seconds: Request duration, or a negative sentinel.
        """
        _process_totals.add_success(
            model, prompt_tokens, completion_tokens, cost_usd, elapsed_seconds
        )
        scope = _scope_totals.get()
        if scope is not None:
            scope.add_success(
                model, prompt_tokens, completion_tokens, cost_usd, elapsed_seconds
            )

    @staticmethod
    def _accumulate_failure(model: str, elapsed_seconds: float) -> None:
        """Fold a failed request into the process totals and the scope.

        Args:
            model: Model id the request was aimed at.
            elapsed_seconds: Time spent before the failure, or a negative
                sentinel.
        """
        _process_totals.add_failure(model, elapsed_seconds)
        scope = _scope_totals.get()
        if scope is not None:
            scope.add_failure(model, elapsed_seconds)

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Log a successful request made through LiteLLM's async path.

        Args:
            kwargs: The request parameters LiteLLM was called with.
            response_obj: The provider response, carrying ``usage``.
            start_time: Request start timestamp.
            end_time: Request end timestamp.
        """
        self.log_success_event(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """Log a failed request made through LiteLLM's async path.

        Args:
            kwargs: The request parameters LiteLLM was called with.
            response_obj: The exception or partial response LiteLLM captured.
            start_time: Request start timestamp.
            end_time: Request failure timestamp.
        """
        self.log_failure_event(kwargs, response_obj, start_time, end_time)


_usage_logger: PromptUsageLogger | None = None


def install_usage_logger() -> PromptUsageLogger:
    """Register the usage logger with LiteLLM, once per process.

    Idempotent: ``litellm.callbacks`` is a global list, and every ``dspy.LM``
    in this project is built through
    :func:`config.llm_factory.lm_runtime_kwargs`, so this is called once per
    LM construction and must not accumulate duplicate handlers that would log
    the same request several times.

    Returns:
        The registered singleton.
    """
    global _usage_logger
    if _usage_logger is None:
        _usage_logger = PromptUsageLogger()
        litellm.callbacks.append(_usage_logger)
        logger.debug("Registered PromptUsageLogger with LiteLLM.")
    return _usage_logger


def register_local_model_costs(model_id: str) -> None:
    """Give a locally served model a price, so LiteLLM computes its cost.

    Without this LiteLLM has no entry for the model and stores
    ``response_cost = None`` for every request to it, which is why a local run
    reported no cost at all.

    The id is registered bare, without its provider prefix. Both spellings are
    in play -- ``dspy.LM`` is constructed as ``openai/qwen3-coder:30b`` while the
    callback sees ``model=qwen3-coder:30b`` -- but LiteLLM normalizes a
    prefixed key down to the bare one on registration and strips the prefix again
    on lookup, so one entry prices both. Registering the prefixed spelling as
    well adds nothing.

    The context window is declared from settings as well. It is what LiteLLM's
    own token checks read, and an unregistered model has no window either.

    A model LiteLLM already prices is left alone. This function is called from
    the shared choke point that every ``dspy.LM`` passes through, cloud models
    included, and overwriting ``deepseek-reasoner`` with a local rate of 0.0
    would silently report a paid run as free -- the exact opposite of the point.
    That check, rather than a provider test, is also what keeps a genuinely
    remote OpenAI model safe if one is ever added back.

    Args:
        model_id: The model id to price, with or without its provider prefix.
            Registration is skipped for an id already registered in this process
            or already priced by LiteLLM, and never raises: an LM must still be
            constructible when its pricing is not.
    """
    bare_model_id = model_id.split("/", 1)[-1].strip()
    if not bare_model_id:
        return

    with _registration_lock:
        if bare_model_id in _registered_model_ids:
            return
        if bare_model_id in litellm.model_cost or model_id in litellm.model_cost:
            logger.debug(
                "Model %s is priced by LiteLLM already; keeping its own rates.",
                model_id,
            )
            _registered_model_ids.add(bare_model_id)
            return

        model_info = {
            "max_tokens": settings.LLM_MAX_TOKENS,
            "max_input_tokens": settings.OLLAMA_LLM_INFERENCE_TOKEN_LIMIT,
            "max_output_tokens": settings.LLM_MAX_TOKENS,
            "input_cost_per_token": settings.LOCAL_LLM_INPUT_COST_PER_TOKEN,
            "output_cost_per_token": settings.LOCAL_LLM_OUTPUT_COST_PER_TOKEN,
            "litellm_provider": _LOCAL_LITELLM_PROVIDER,
            "mode": "chat",
        }
        try:
            litellm.register_model({bare_model_id: model_info})
        except Exception:
            logger.warning(
                "Failed to register local costs for %s; its requests will be "
                "logged with an unknown cost.",
                bare_model_id,
                exc_info=True,
            )
            return

        _registered_model_ids.add(bare_model_id)
        logger.debug(
            "Priced local model %s at %s/%s USD per input/output token.",
            bare_model_id,
            settings.LOCAL_LLM_INPUT_COST_PER_TOKEN,
            settings.LOCAL_LLM_OUTPUT_COST_PER_TOKEN,
        )
