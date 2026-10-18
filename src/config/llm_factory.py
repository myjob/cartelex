"""Named LLM profiles, so each consumer picks its own model.

The UI planner and the corp index searcher used to share
``settings.LLM_MODEL_CLOUD_ID`` -- and ``oi_dspy`` built its cloud LM by
importing ``create_deepseek_lm`` out of ``pipelines.corp_index``, which made a
UI concern depend on a batch pipeline. Repointing one at a cheaper model
repointed the other, silently.

A profile is a ``(provider, model_id, thinking_mode, cache)`` selection with a
name. The provider decides how the model id is qualified for LiteLLM and where
the request goes:

* ``deepseek`` -> ``deepseek/<model>``, authenticated with
  :data:`settings.LLM_MODEL_CLOUD_API_KEY`.
* ``ollama`` -> ``openai/<model>`` against ``{OLLAMA_HOST}/v1``, which is how
  every other DSPy call site in this project reaches the local server.

so neither endpoint nor credential is repeated in settings. Thinking mode
travels through :func:`config.llm_thinking.thinking_kwargs`, which translates
the one canonical directive per provider; a profile that names no mode follows
the global :data:`settings.LLM_THINKING_MODE`.

Thinking and caching are per profile for the same reason the model id is. The
UI planner wants a fast non-reasoning answer and a cache that makes a repeated
question free; the corp index searcher wants the model to think, and wants a
re-run to actually re-ask rather than replay a stored answer it is trying to
replace.

Exports:
    LlmProfile -- a named (provider, model) selection.
    create_lm(profile) -- provision the ``dspy.LM`` for a profile.
    lm_runtime_kwargs(model_name) -- the runtime-safety kwargs for any LM.
    ui_planner_profile() / corp_index_profile() / batch_profile() -- the
        configured profiles.
    qualified_model_name(provider, model_id) -- the LiteLLM model string.
"""

import logging
from typing import Any

import dspy
from pydantic import BaseModel, Field, field_validator

from config.llm_thinking import THINKING_DISABLED, THINKING_ENABLED, thinking_kwargs
from config.llm_usage import install_usage_logger, register_local_model_costs
from config.settings import settings

logger = logging.getLogger(__name__)

PROVIDER_OLLAMA = "ollama"
PROVIDER_DEEPSEEK = "deepseek"

SUPPORTED_PROVIDERS = frozenset({PROVIDER_OLLAMA, PROVIDER_DEEPSEEK})

# Thinking modes a profile may name. Anything else is a typo in an env var, and
# a typo that silently fell back to the global default would be invisible.
SUPPORTED_THINKING_MODES = frozenset({THINKING_ENABLED, THINKING_DISABLED})

# Profile names, used in log lines and in the error raised for an unknown
# provider so a misconfiguration says which knob to fix.
PROFILE_UI_PLANNER = "ui_planner"
PROFILE_CORP_INDEX = "corp_index"
PROFILE_BATCH = "batch"


class LlmProfile(BaseModel):
    """One consumer's model selection.

    Args:
        name: Profile name, used in logs and errors (e.g. ``corp_index``).
        provider: Backend serving the model, ``ollama`` or ``deepseek``.
        model_id: Provider-native model id. Blank resolves to the provider's
            shared default at construction time.
        thinking_mode: ``enabled`` or ``disabled`` for this profile alone, or
            None to follow ``settings.LLM_THINKING_MODE``.
        cache: Whether DSPy may serve this profile's answers from its LM cache.
        temperature: Sampling temperature, or None for the provider default.
            Pinned by the ingestion batch (greedy decoding for reproducible
            extractions); left unset elsewhere, and never sent to a reasoning
            model that rejects it.

    Raises:
        ValueError: If the provider is not supported, the resolved model id is
            empty, or the thinking mode is neither ``enabled`` nor ``disabled``.
    """

    model_config = {"frozen": True}

    name: str = Field(description="Profile name used in logs and errors.")
    provider: str = Field(description="Backend serving the model.")
    model_id: str = Field(description="Provider-native model id.")
    thinking_mode: str | None = Field(
        default=None,
        description="Per-profile thinking mode, or None to follow the global one.",
    )
    cache: bool = Field(
        default=True, description="Whether DSPy may serve answers from its LM cache."
    )
    temperature: float | None = Field(
        default=None,
        description="Sampling temperature, or None to leave the provider default.",
    )

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        """Return a supported provider name.

        Args:
            value: Configured provider.

        Returns:
            The trimmed, lowercased provider.

        Raises:
            ValueError: If the provider is not one this project can reach.
        """
        provider = value.strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported LLM provider {value!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}."
            )
        return provider

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        """Return the trimmed model id.

        Args:
            value: Configured model id.

        Returns:
            The trimmed model id.

        Raises:
            ValueError: If the model id is empty after trimming. A blank
                setting is resolved to the provider default before the profile
                is built, so an empty value here means the default is empty too.
        """
        model_id = value.strip()
        if not model_id:
            raise ValueError("LLM model id must not be empty.")
        return model_id

    @field_validator("thinking_mode", mode="before")
    @classmethod
    def validate_thinking_mode(cls, value: str | None) -> str | None:
        """Return a supported thinking mode, or None to follow the global one.

        Args:
            value: Configured mode. Blank and None both mean "not set here".

        Returns:
            The trimmed, lowercased mode, or None when the profile names none.

        Raises:
            ValueError: If a non-blank mode is not one the translation layer
                understands.
        """
        if value is None:
            return None

        thinking_mode = str(value).strip().lower()
        if not thinking_mode:
            return None
        if thinking_mode not in SUPPORTED_THINKING_MODES:
            raise ValueError(
                f"Unsupported thinking mode {value!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_THINKING_MODES))}, "
                "or blank to follow LLM_THINKING_MODE."
            )
        return thinking_mode

    @property
    def qualified_model_name(self) -> str:
        """Return the LiteLLM model string for this profile.

        Returns:
            The model id prefixed with its LiteLLM provider, unless it already
            carries one.
        """
        return qualified_model_name(self.provider, self.model_id)


def qualified_model_name(provider: str, model_id: str) -> str:
    """Return a LiteLLM-compatible model name.

    Args:
        provider: Backend serving the model.
        model_id: Provider-native model id. An id that already carries a
            ``<provider>/`` prefix is passed through untouched, so an explicit
            override always wins.

    Returns:
        A model name suitable for ``dspy.LM`` and LiteLLM.

    Raises:
        ValueError: If the model id is empty, or the provider is unsupported.
    """
    trimmed_model_id = model_id.strip()
    if not trimmed_model_id:
        raise ValueError("LLM model id must not be empty.")
    if "/" in trimmed_model_id:
        return trimmed_model_id

    if provider == PROVIDER_DEEPSEEK:
        return f"{PROVIDER_DEEPSEEK}/{trimmed_model_id}"
    if provider == PROVIDER_OLLAMA:
        # Ollama is reached through its OpenAI-compatible endpoint, as every
        # other DSPy call site in this project does; the native ollama/ provider
        # would need a different thinking parameter and a different base URL.
        return f"openai/{trimmed_model_id}"
    raise ValueError(f"Unsupported LLM provider {provider!r}.")


def _resolve_model_id(provider: str, configured_model_id: str) -> str:
    """Return the model id for a provider, falling back to its shared default.

    Args:
        provider: Backend serving the model.
        configured_model_id: The profile's configured id; blank means "use the
            provider's shared default".

    Returns:
        The model id to run with.
    """
    if configured_model_id.strip():
        return configured_model_id.strip()
    if provider == PROVIDER_DEEPSEEK:
        return settings.LLM_MODEL_CLOUD_ID
    return settings.LLM_MODEL_ID


def _profile_from_settings(
    name: str,
    provider: str,
    configured_model_id: str,
    thinking_mode: str | None = None,
    cache: bool = True,
    temperature: float | None = None,
) -> LlmProfile:
    """Build a validated profile from its configured settings.

    Args:
        name: Profile name for logs and errors.
        provider: Configured provider.
        configured_model_id: Configured model id, possibly blank.
        thinking_mode: Configured thinking mode; blank or None follows the
            global ``settings.LLM_THINKING_MODE``.
        cache: Whether DSPy may serve this profile's answers from its LM cache.
        temperature: Sampling temperature, or None for the provider default.

    Returns:
        The validated profile.

    Raises:
        ValueError: If the provider is unsupported, no model id resolves, or the
            thinking mode is not one the translation layer understands.
    """
    normalized_provider = provider.strip().lower()
    return LlmProfile(
        name=name,
        provider=normalized_provider,
        model_id=_resolve_model_id(normalized_provider, configured_model_id),
        thinking_mode=thinking_mode,
        cache=cache,
        temperature=temperature,
    )


def ui_planner_profile() -> LlmProfile:
    """Return the profile backing the oi_dspy UI planner.

    Returns:
        The profile from ``settings.UI_LLM_PROVIDER`` /
        ``settings.UI_LLM_MODEL_ID``.

    Raises:
        ValueError: If the configured provider or model is unusable.
    """
    return _profile_from_settings(
        PROFILE_UI_PLANNER,
        settings.UI_LLM_PROVIDER,
        settings.UI_LLM_MODEL_ID,
        thinking_mode=settings.UI_LLM_THINKING_MODE,
        cache=settings.UI_LLM_CACHE,
    )


def corp_index_profile() -> LlmProfile:
    """Return the profile backing the corp index searcher.

    Returns:
        The profile from ``settings.CORP_INDEX_LLM_PROVIDER`` /
        ``settings.CORP_INDEX_LLM_MODEL_ID``.

    Raises:
        ValueError: If the configured provider or model is unusable.
    """
    return _profile_from_settings(
        PROFILE_CORP_INDEX,
        settings.CORP_INDEX_LLM_PROVIDER,
        settings.CORP_INDEX_LLM_MODEL_ID,
        thinking_mode=settings.CORP_INDEX_LLM_THINKING_MODE,
        cache=settings.CORP_INDEX_LLM_CACHE,
    )


def batch_profile(
    temperature: float | None = None, cache: bool = False
) -> LlmProfile:
    """Return the profile shared by the offline batch pipelines.

    Args:
        temperature: Sampling temperature to pin (the ingestion batch passes
            ``settings.LLM_TEMPERATURE`` for greedy, reproducible extraction);
            None leaves the provider default.
        cache: Whether DSPy may replay stored answers. Off by default: a batch
            re-run that replays reports work it never did.

    Returns:
        The profile from ``settings.BATCH_LLM_PROVIDER`` /
        ``settings.BATCH_LLM_MODEL_ID`` / ``settings.BATCH_LLM_THINKING_MODE``.

    Raises:
        ValueError: If the configured provider or model is unusable.
    """
    return _profile_from_settings(
        PROFILE_BATCH,
        settings.BATCH_LLM_PROVIDER,
        settings.BATCH_LLM_MODEL_ID,
        thinking_mode=settings.BATCH_LLM_THINKING_MODE,
        cache=cache,
        temperature=temperature,
    )


def lm_runtime_kwargs(
    model_name: str, thinking_mode: str | None = None
) -> dict[str, Any]:
    """Return the runtime-safety keyword arguments every ``dspy.LM`` needs.

    These are the knobs that decide how a request *fails*, as opposed to what it
    asks for, and they are centralized here for the same reason
    :func:`config.llm_thinking.thinking_kwargs` is: repeated per call site they
    drifted into four different spellings, and four call sites carried none at
    all.

    Two of them are not obvious and must not be "simplified":

    * ``max_tokens`` is mandatory. DSPy declares ``max_tokens: int | None = None``
      and LiteLLM drops ``None`` parameters, so omitting it makes Ollama decode
      at ``num_predict=-1`` until it fills the slot. A greedy model in a
      repetition loop then never returns and the client times out instead.
    * ``num_retries`` and ``max_retries`` are deliberately the same value.
      LiteLLM aliases one onto the other -- ``litellm/main.py:5117`` runs
      ``elif num_retries is not None: max_retries = num_retries`` -- so a call
      site that sets ``max_retries=0`` alongside a non-zero ``num_retries`` has
      its zero silently discarded and gets a retry it asked not to have, at the
      cost of another full ``timeout``. Passing both keeps the aliasing a no-op.

    Truncation caused by ``max_tokens`` needs no handling here: DSPy warns on it
    natively in ``LM._check_truncation``, which reaches ``logs/application.log``
    through the root logger.

    ``temperature`` and ``cache`` are deliberately absent. Both are per-call-site
    semantic choices rather than failure modes, and ``deepseek-reasoner`` does
    not accept a temperature at all.

    This function also has one side effect, and it lives here for the same
    reason the kwargs do: every ``dspy.LM`` in this project is constructed by
    splatting this function's result, so registering
    :func:`config.llm_usage.install_usage_logger` here is what makes token
    accounting cover every call site without nine separate edits. The
    registration is idempotent.

    :func:`config.llm_usage.register_local_model_costs` is called here for the
    same reason, and specifically not from :func:`create_lm`: the batch
    pipelines build their ``dspy.LM`` directly and never reach ``create_lm``, so
    pricing installed there would miss every ingestion run -- the one path whose
    cost anybody wants. It declines to overwrite a model LiteLLM already prices,
    which is what keeps the cloud tariffs intact.

    Args:
        model_name: The ``dspy.LM`` model name the kwargs will be passed with,
            including its provider prefix (for example ``"openai/qwen3:32b"``).
        thinking_mode: Explicit thinking mode, or None to follow
            ``settings.LLM_THINKING_MODE``.

    Returns:
        Keyword arguments to splat into ``dspy.LM``, including the provider's
        spelling of the thinking mode.

    Raises:
        ValueError: If the model name is empty, or the thinking mode is neither
            ``"enabled"`` nor ``"disabled"``.
    """
    install_usage_logger()
    register_local_model_costs(model_name)

    runtime_kwargs: dict[str, Any] = {
        "max_tokens": settings.LLM_MAX_TOKENS,
        "timeout": settings.OLLAMA_TIMEOUT,
        "num_retries": settings.LLM_NUM_RETRIES,
        "max_retries": settings.LLM_NUM_RETRIES,
    }
    if settings.LLM_FREQUENCY_PENALTY:
        # Omitted at the default 0.0 so the request stays byte-identical to the
        # greedy baseline unless a run is confirmed to loop.
        runtime_kwargs["frequency_penalty"] = settings.LLM_FREQUENCY_PENALTY
    runtime_kwargs.update(thinking_kwargs(model_name, thinking_mode))
    return runtime_kwargs


def create_lm(profile: LlmProfile) -> dspy.LM:
    """Provision the DSPy language model for a profile.

    Args:
        profile: The profile to build an LM for.

    Returns:
        A configured ``dspy.LM``, carrying the profile's thinking mode in its
        provider's spelling and its own cache switch. Caching makes a repeated
        identical question free, which matters most on the cloud providers, but
        a profile whose whole point is to re-ask a question turns it off.

    Raises:
        ValueError: If the profile's provider is unsupported.
    """
    model_name = profile.qualified_model_name
    provider_kwargs: dict[str, object] = {}
    if profile.provider == PROVIDER_DEEPSEEK:
        provider_kwargs["api_key"] = settings.LLM_MODEL_CLOUD_API_KEY
    elif profile.provider == PROVIDER_OLLAMA:
        provider_kwargs["api_base"] = f"{settings.OLLAMA_HOST}/v1"
        # The OpenAI-compatible endpoint requires a key it never checks.
        provider_kwargs["api_key"] = PROVIDER_OLLAMA
    else:
        raise ValueError(f"Unsupported LLM provider {profile.provider!r}.")
    if profile.temperature is not None:
        provider_kwargs["temperature"] = profile.temperature

    logger.info(
        "Provisioning LM for profile %s: %s (thinking=%s, cache=%s)",
        profile.name,
        model_name,
        profile.thinking_mode or f"global:{settings.LLM_THINKING_MODE}",
        profile.cache,
    )
    return dspy.LM(
        model_name,
        cache=profile.cache,
        **provider_kwargs,
        **lm_runtime_kwargs(model_name, profile.thinking_mode),
    )
