"""Unit tests for the provider-aware thinking-mode translation."""

import unittest
from unittest.mock import patch

from config.llm_thinking import (
    THINKING_DISABLED,
    THINKING_ENABLED,
    thinking_config,
    thinking_kwargs,
)


class ThinkingConfigTest(unittest.TestCase):
    """The canonical directive shape declared by settings.LLM_THINKING_MODE."""

    def test_explicit_mode_is_returned_verbatim(self) -> None:
        """An explicit mode wins over the configured default."""
        self.assertEqual(
            thinking_config(THINKING_ENABLED), {"thinking": {"type": "enabled"}}
        )
        self.assertEqual(
            thinking_config(THINKING_DISABLED), {"thinking": {"type": "disabled"}}
        )

    def test_default_mode_comes_from_settings(self) -> None:
        """Omitting the mode reads settings.LLM_THINKING_MODE."""
        with patch("config.llm_thinking.settings.LLM_THINKING_MODE", "enabled"):
            self.assertEqual(thinking_config(), {"thinking": {"type": "enabled"}})

    def test_unknown_mode_is_rejected(self) -> None:
        """A typo must fail loudly rather than silently enable thinking."""
        with self.assertRaises(ValueError):
            thinking_config("off")


class ThinkingKwargsTest(unittest.TestCase):
    """Translation of the canonical directive into per-provider LM kwargs."""

    def test_deepseek_receives_the_canonical_parameter(self) -> None:
        """LiteLLM's DeepSeek adapter accepts ``thinking`` in both modes."""
        self.assertEqual(
            thinking_kwargs("deepseek/deepseek-v4-pro", mode=THINKING_DISABLED),
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(
            thinking_kwargs("deepseek/deepseek-v4-pro", mode=THINKING_ENABLED),
            {"thinking": {"type": "enabled"}},
        )

    def test_openai_compatible_ollama_uses_extra_body(self) -> None:
        """The ``openai/`` route is Ollama's /v1 endpoint: reasoning_effort only."""
        self.assertEqual(
            thinking_kwargs("openai/qwen3.6:35b", mode=THINKING_DISABLED),
            {"extra_body": {"reasoning_effort": "none"}},
        )

    def test_native_ollama_uses_reasoning_effort(self) -> None:
        """LiteLLM maps reasoning_effort onto Ollama's ``think`` flag."""
        self.assertEqual(
            thinking_kwargs("ollama/qwen3:32b", mode=THINKING_DISABLED),
            {"reasoning_effort": "none"},
        )
        self.assertEqual(
            thinking_kwargs("ollama_chat/qwen3:32b", mode=THINKING_DISABLED),
            {"reasoning_effort": "none"},
        )

    def test_enabled_sends_no_override_to_ollama(self) -> None:
        """Thinking is the Ollama-side default, so enabling it adds nothing."""
        for model_name in ("openai/qwen3:32b", "ollama/qwen3:32b"):
            with self.subTest(model_name=model_name):
                self.assertEqual(thinking_kwargs(model_name, mode=THINKING_ENABLED), {})

    def test_unknown_provider_leaves_the_request_untouched(self) -> None:
        """An unmapped provider must not receive a parameter it would reject."""
        self.assertEqual(thinking_kwargs("mistral/large", mode=THINKING_DISABLED), {})
        self.assertEqual(thinking_kwargs("qwen3-coder:30b", mode=THINKING_DISABLED), {})

    def test_default_mode_comes_from_settings(self) -> None:
        """Omitting the mode reads settings.LLM_THINKING_MODE."""
        with patch("config.llm_thinking.settings.LLM_THINKING_MODE", "enabled"):
            self.assertEqual(thinking_kwargs("openai/qwen3:32b"), {})
        with patch("config.llm_thinking.settings.LLM_THINKING_MODE", "disabled"):
            self.assertEqual(
                thinking_kwargs("openai/qwen3:32b"),
                {"extra_body": {"reasoning_effort": "none"}},
            )

    def test_empty_model_name_is_rejected(self) -> None:
        """A missing model name is a programming error, not a silent no-op."""
        with self.assertRaises(ValueError):
            thinking_kwargs("   ")


if __name__ == "__main__":
    unittest.main()
