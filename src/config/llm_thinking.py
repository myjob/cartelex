"""Provider-aware translation of the configured LLM thinking mode.

The thinking toggle is declared once, in the canonical form
``{"thinking": {"type": "enabled" | "disabled"}}`` (``settings.LLM_THINKING_MODE``),
and translated here into the request parameter the target provider actually
honours. Three providers are reachable through ``dspy.LM`` in this project and
all three spell the same intent differently:

* ``deepseek/<model>`` accepts the canonical ``thinking`` parameter. LiteLLM
  forwards it when enabled and omits it when disabled.
* ``openai/<model>`` is, in this project, always Ollama's OpenAI-compatible
  endpoint (every call site passes ``api_base=<OLLAMA_HOST>/v1``). That
  endpoint honours a top-level ``reasoning_effort``, but LiteLLM refuses to
  forward the field for a model id it does not know, so it travels in
  ``extra_body``. Ollama ignores a top-level ``think`` field here.
* ``ollama/<model>`` and ``ollama_chat/<model>`` reject ``thinking`` outright;
  LiteLLM maps ``reasoning_effort="none"`` onto Ollama's ``think: false``.

Passing the canonical parameter to a provider that does not support it raises
``litellm.UnsupportedParamsError`` before the request is sent, which is why the
translation is centralized instead of repeated at each ``dspy.LM`` call site.
"""

import logging
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

THINKING_ENABLED = "enabled"
THINKING_DISABLED = "disabled"

_VALID_THINKING_MODES = frozenset({THINKING_ENABLED, THINKING_DISABLED})

# Providers whose LiteLLM adapter accepts the canonical ``thinking`` parameter.
_NATIVE_THINKING_PROVIDERS = frozenset({"deepseek", "anthropic"})

# LiteLLM's native Ollama providers: ``reasoning_effort`` maps to ``think``.
_OLLAMA_PROVIDERS = frozenset({"ollama", "ollama_chat"})

# Ollama behind its OpenAI-compatible endpoint: same field, but smuggled past
# LiteLLM's per-model parameter validation.
_OPENAI_COMPATIBLE_PROVIDERS = frozenset({"openai"})

# Ollama's "do not think" effort level. The enabled case sends nothing at all,
# because thinking is the default for every model that supports it.
_NO_THINKING_EFFORT = "none"


def _resolve_mode(mode: str | None) -> str:
    """Return a validated thinking mode, defaulting to the configured one.

    Args:
        mode: Explicit mode, or ``None`` to read ``settings.LLM_THINKING_MODE``.

    Returns:
        Either ``"enabled"`` or ``"disabled"``.

    Raises:
        ValueError: If the mode is neither ``"enabled"`` nor ``"disabled"``.
    """
    resolved_mode = (mode if mode is not None else settings.LLM_THINKING_MODE).strip()
    if resolved_mode not in _VALID_THINKING_MODES:
        raise ValueError(
            f"Invalid thinking mode {resolved_mode!r}; "
            f"expected one of {sorted(_VALID_THINKING_MODES)}."
        )
    return resolved_mode


def _provider_of(model_name: str) -> str:
    """Return the LiteLLM provider prefix of a model name.

    Args:
        model_name: A ``dspy.LM`` model name such as ``"openai/qwen3-coder:30b"``.

    Returns:
        The provider prefix, or an empty string when the name carries none.

    Raises:
        ValueError: If the model name is empty.
    """
    trimmed_model_name = model_name.strip()
    if not trimmed_model_name:
        raise ValueError("Model name must not be empty.")
    provider, separator, _ = trimmed_model_name.partition("/")
    return provider if separator else ""


def thinking_config(mode: str | None = None) -> dict[str, dict[str, str]]:
    """Return the canonical thinking directive for the configured mode.

    Args:
        mode: Explicit mode, or ``None`` to read ``settings.LLM_THINKING_MODE``.

    Returns:
        ``{"thinking": {"type": "enabled"}}`` or ``{"thinking": {"type": "disabled"}}``.

    Raises:
        ValueError: If the mode is neither ``"enabled"`` nor ``"disabled"``.
    """
    return {"thinking": {"type": _resolve_mode(mode)}}


def thinking_kwargs(model_name: str, mode: str | None = None) -> dict[str, Any]:
    """Return the ``dspy.LM`` keyword arguments that realize the thinking mode.

    Args:
        model_name: The ``dspy.LM`` model name the kwargs will be passed with,
            including its provider prefix (for example ``"openai/qwen3:32b"``).
        mode: Explicit mode, or ``None`` to read ``settings.LLM_THINKING_MODE``.

    Returns:
        Keyword arguments to splat into ``dspy.LM``. Empty when the provider
        needs no parameter to reach the requested mode, so that a call site
        that splats the result is unchanged in that case.

    Raises:
        ValueError: If the model name is empty, or the mode is neither
            ``"enabled"`` nor ``"disabled"``.
    """
    resolved_mode = _resolve_mode(mode)
    provider = _provider_of(model_name)

    if provider in _NATIVE_THINKING_PROVIDERS:
        return thinking_config(resolved_mode)

    if resolved_mode == THINKING_ENABLED:
        # Thinking is the provider-side default for every Ollama model that
        # supports it, so enabling it means sending no override.
        return {}

    if provider in _OLLAMA_PROVIDERS:
        return {"reasoning_effort": _NO_THINKING_EFFORT}

    if provider in _OPENAI_COMPATIBLE_PROVIDERS:
        return {"extra_body": {"reasoning_effort": _NO_THINKING_EFFORT}}

    logger.debug(
        "No thinking-mode translation for provider %r (model %r); "
        "leaving the provider default in place.",
        provider,
        model_name,
    )
    return {}
