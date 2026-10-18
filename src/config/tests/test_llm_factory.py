"""Unit tests for the named LLM profiles.

No LM is ever constructed for real: ``dspy.LM`` is patched, so these tests
exercise which model string, endpoint and credential a profile resolves to.

The subject is isolation. The UI planner and the corp index searcher shared one
setting until these profiles existed, so repointing the batch at a cheaper model
silently repointed the UI server too. Most of what is pinned here is that
changing one profile leaves the other alone.
"""

import os
import unittest
from unittest import mock

# Provide a dummy token before importing settings-dependent modules.
os.environ.setdefault("opencorporates_api_token", "dummy")

from config.llm_factory import (  # noqa: E402
    PROVIDER_DEEPSEEK,
    PROVIDER_OLLAMA,
    LlmProfile,
    batch_profile,
    corp_index_profile,
    create_lm,
    lm_runtime_kwargs,
    qualified_model_name,
    ui_planner_profile,
)
from config.settings import settings as real_settings  # noqa: E402


def _stub_runtime_settings(settings: mock.MagicMock) -> None:
    """Give a patched settings object the runtime knobs ``create_lm`` reads.

    ``lm_runtime_kwargs`` reads four numeric settings, and a ``MagicMock``
    attribute is truthy, so a test that patches settings wholesale would
    otherwise smuggle a mock into ``frequency_penalty``.

    Args:
        settings: The patched ``config.llm_factory.settings`` mock.
    """
    settings.LLM_MAX_TOKENS = 8192
    settings.OLLAMA_TIMEOUT = 150
    settings.LLM_NUM_RETRIES = 0
    settings.LLM_FREQUENCY_PENALTY = 0.0


def _stub_profile_settings(settings: mock.MagicMock) -> None:
    """Give a patched settings object the profile knobs a test does not pin.

    A ``MagicMock`` attribute is neither a valid thinking mode nor a bool, so a
    test that patches settings wholesale has to supply every field a profile
    reads, not only the ones it is about.

    Args:
        settings: The patched ``config.llm_factory.settings`` mock.
    """
    settings.UI_LLM_THINKING_MODE = ""
    settings.UI_LLM_CACHE = True
    settings.CORP_INDEX_LLM_THINKING_MODE = ""
    settings.CORP_INDEX_LLM_CACHE = True
    settings.BATCH_LLM_PROVIDER = "ollama"
    settings.BATCH_LLM_MODEL_ID = ""
    settings.BATCH_LLM_THINKING_MODE = ""


class TestBatchProfile(unittest.TestCase):
    """The profile shared by the offline batch pipelines."""

    def test_defaults_to_the_local_model_with_cache_off(self) -> None:
        """A blank model id follows LLM_MODEL_ID; a re-run must really re-ask."""
        with mock.patch("config.llm_factory.settings") as settings:
            _stub_profile_settings(settings)
            settings.LLM_MODEL_ID = "qwen3-coder:30b"

            profile = batch_profile()

        self.assertEqual((profile.provider, profile.model_id), ("ollama", "qwen3-coder:30b"))
        self.assertFalse(profile.cache)
        self.assertIsNone(profile.temperature)
        self.assertIsNone(profile.thinking_mode)

    def test_provider_switch_moves_every_batch_to_the_cloud(self) -> None:
        """One setting repoints the batch profile; the UI profile is untouched."""
        with mock.patch("config.llm_factory.settings") as settings:
            _stub_profile_settings(settings)
            settings.BATCH_LLM_PROVIDER = "deepseek"
            settings.LLM_MODEL_CLOUD_ID = "deepseek-chat"
            settings.UI_LLM_PROVIDER = "ollama"
            settings.UI_LLM_MODEL_ID = "qwen3-coder:30b"

            batch = batch_profile()
            ui = ui_planner_profile()

        self.assertEqual((batch.provider, batch.model_id), ("deepseek", "deepseek-chat"))
        self.assertEqual(ui.provider, "ollama")

    def test_temperature_and_cache_are_per_call(self) -> None:
        """The ingestion batch pins greedy decoding; the QA replay turns the cache on."""
        with mock.patch("config.llm_factory.settings") as settings:
            _stub_profile_settings(settings)
            settings.LLM_MODEL_ID = "qwen3-coder:30b"

            greedy = batch_profile(temperature=0.0)
            cached = batch_profile(cache=True)

        self.assertEqual(greedy.temperature, 0.0)
        self.assertFalse(greedy.cache)
        self.assertIsNone(cached.temperature)
        self.assertTrue(cached.cache)


class TestQualifiedModelName(unittest.TestCase):
    """Translation of a provider-native model id into a LiteLLM model string."""

    def test_deepseek_model_takes_the_deepseek_prefix(self) -> None:
        """An unqualified cloud model id is routed to DeepSeek."""
        self.assertEqual(
            qualified_model_name(PROVIDER_DEEPSEEK, "deepseek-chat"),
            "deepseek/deepseek-chat",
        )

    def test_ollama_model_takes_the_openai_prefix(self) -> None:
        """Ollama is reached through its OpenAI-compatible endpoint."""
        self.assertEqual(
            qualified_model_name(PROVIDER_OLLAMA, "qwen3-coder:30b"),
            "openai/qwen3-coder:30b",
        )

    def test_qualified_id_is_passed_through(self) -> None:
        """An explicit provider prefix always wins over the profile's own."""
        self.assertEqual(
            qualified_model_name(PROVIDER_OLLAMA, "deepseek/deepseek-chat"),
            "deepseek/deepseek-chat",
        )

    def test_empty_model_id_is_rejected(self) -> None:
        """A blank model id is a misconfiguration, not a default."""
        with self.assertRaises(ValueError):
            qualified_model_name(PROVIDER_DEEPSEEK, "   ")

    def test_unknown_provider_is_rejected(self) -> None:
        """An unreachable provider fails loudly rather than at request time."""
        with self.assertRaises(ValueError):
            qualified_model_name("anthropic", "some-model")


class TestLlmProfile(unittest.TestCase):
    """Profile validation."""

    def test_provider_is_normalized(self) -> None:
        """Casing and whitespace in the setting do not break the lookup."""
        profile = LlmProfile(name="test", provider=" DeepSeek ", model_id="m")

        self.assertEqual(profile.provider, "deepseek")

    def test_unsupported_provider_is_rejected(self) -> None:
        """The error names the supported providers."""
        with self.assertRaises(ValueError) as raised:
            LlmProfile(name="test", provider="bedrock", model_id="m")

        self.assertIn("deepseek", str(raised.exception))

    def test_empty_model_id_is_rejected(self) -> None:
        """A profile with no model cannot be built."""
        with self.assertRaises(ValueError):
            LlmProfile(name="test", provider=PROVIDER_DEEPSEEK, model_id=" ")

    def test_blank_thinking_mode_defers_to_the_global_setting(self) -> None:
        """An unset per-profile mode is None, which the translation reads as global."""
        profile = LlmProfile(
            name="test", provider=PROVIDER_DEEPSEEK, model_id="m", thinking_mode="  "
        )

        self.assertIsNone(profile.thinking_mode)

    def test_thinking_mode_is_normalized(self) -> None:
        """Casing and whitespace in the setting do not break the translation."""
        profile = LlmProfile(
            name="test", provider=PROVIDER_DEEPSEEK, model_id="m", thinking_mode=" Enabled "
        )

        self.assertEqual(profile.thinking_mode, "enabled")

    def test_misspelled_thinking_mode_is_rejected(self) -> None:
        """A typo must fail loudly, not fall back to the global default unseen."""
        with self.assertRaises(ValueError) as raised:
            LlmProfile(
                name="test",
                provider=PROVIDER_DEEPSEEK,
                model_id="m",
                thinking_mode="on",
            )

        self.assertIn("enabled", str(raised.exception))


class TestConfiguredProfiles(unittest.TestCase):
    """What the two settings sets resolve to."""

    def test_profiles_are_independent(self) -> None:
        """Each consumer reads its own provider and model."""
        with mock.patch("config.llm_factory.settings") as settings:
            settings.UI_LLM_PROVIDER = "ollama"
            settings.UI_LLM_MODEL_ID = "qwen3-coder:30b"
            settings.UI_LLM_THINKING_MODE = ""
            settings.UI_LLM_CACHE = True
            settings.CORP_INDEX_LLM_PROVIDER = "deepseek"
            settings.CORP_INDEX_LLM_MODEL_ID = "deepseek-chat"
            settings.CORP_INDEX_LLM_THINKING_MODE = "enabled"
            settings.CORP_INDEX_LLM_CACHE = False

            ui = ui_planner_profile()
            corp_index = corp_index_profile()

        self.assertEqual((ui.provider, ui.model_id), ("ollama", "qwen3-coder:30b"))
        self.assertEqual(
            (corp_index.provider, corp_index.model_id), ("deepseek", "deepseek-chat")
        )

    def test_thinking_and_cache_are_per_profile(self) -> None:
        """The corp index thinks and never replays; the UI planner does neither."""
        with mock.patch("config.llm_factory.settings") as settings:
            settings.UI_LLM_PROVIDER = "ollama"
            settings.UI_LLM_MODEL_ID = "qwen3-coder:30b"
            settings.UI_LLM_THINKING_MODE = "disabled"
            settings.UI_LLM_CACHE = True
            settings.CORP_INDEX_LLM_PROVIDER = "deepseek"
            settings.CORP_INDEX_LLM_MODEL_ID = "deepseek-reasoner"
            settings.CORP_INDEX_LLM_THINKING_MODE = "enabled"
            settings.CORP_INDEX_LLM_CACHE = False

            ui = ui_planner_profile()
            corp_index = corp_index_profile()

        self.assertEqual((ui.thinking_mode, ui.cache), ("disabled", True))
        self.assertEqual((corp_index.thinking_mode, corp_index.cache), ("enabled", False))

    def test_repointing_one_profile_leaves_the_other_alone(self) -> None:
        """The regression that motivated the split: one knob moved both."""
        with mock.patch("config.llm_factory.settings") as settings:
            _stub_profile_settings(settings)
            settings.UI_LLM_PROVIDER = "ollama"
            settings.UI_LLM_MODEL_ID = "qwen3-coder:30b"
            settings.CORP_INDEX_LLM_PROVIDER = "deepseek"
            settings.CORP_INDEX_LLM_MODEL_ID = "deepseek-v4-pro"
            before = ui_planner_profile()

            settings.CORP_INDEX_LLM_MODEL_ID = "deepseek-chat"
            after = ui_planner_profile()

        self.assertEqual(before, after)

    def test_blank_model_falls_back_to_the_provider_default(self) -> None:
        """An unset profile behaves exactly as it did before it existed."""
        with mock.patch("config.llm_factory.settings") as settings:
            _stub_profile_settings(settings)
            settings.LLM_MODEL_ID = "qwen3-coder:30b"
            settings.LLM_MODEL_CLOUD_ID = "deepseek-chat"
            settings.UI_LLM_PROVIDER = "ollama"
            settings.UI_LLM_MODEL_ID = ""
            settings.CORP_INDEX_LLM_PROVIDER = "deepseek"
            settings.CORP_INDEX_LLM_MODEL_ID = ""

            ui = ui_planner_profile()
            corp_index = corp_index_profile()

        self.assertEqual(ui.model_id, "qwen3-coder:30b")
        self.assertEqual(corp_index.model_id, "deepseek-chat")

    def test_blank_model_follows_its_own_provider(self) -> None:
        """A profile switched to ollama defaults to the local model, not the cloud one."""
        with mock.patch("config.llm_factory.settings") as settings:
            _stub_profile_settings(settings)
            settings.LLM_MODEL_ID = "qwen3-coder:30b"
            settings.LLM_MODEL_CLOUD_ID = "deepseek-chat"
            settings.CORP_INDEX_LLM_PROVIDER = "ollama"
            settings.CORP_INDEX_LLM_MODEL_ID = ""

            profile = corp_index_profile()

        self.assertEqual(profile.model_id, "qwen3-coder:30b")


class TestCreateLm(unittest.TestCase):
    """How a profile is wired into ``dspy.LM``."""

    def test_deepseek_profile_carries_the_api_key(self) -> None:
        """The cloud arm authenticates and never sets a base URL."""
        profile = LlmProfile(
            name="corp_index", provider=PROVIDER_DEEPSEEK, model_id="deepseek-chat"
        )

        with mock.patch("config.llm_factory.settings") as settings:
            settings.LLM_MODEL_CLOUD_API_KEY = "sk-test"
            _stub_runtime_settings(settings)
            with mock.patch("config.llm_factory.dspy.LM") as mock_lm:
                create_lm(profile)

        self.assertEqual(mock_lm.call_args.args[0], "deepseek/deepseek-chat")
        self.assertEqual(mock_lm.call_args.kwargs["api_key"], "sk-test")
        self.assertNotIn("api_base", mock_lm.call_args.kwargs)
        self.assertTrue(mock_lm.call_args.kwargs["cache"])

    def test_ollama_profile_points_at_the_local_server(self) -> None:
        """The local arm sets the base URL and a key the endpoint ignores."""
        profile = LlmProfile(
            name="ui_planner", provider=PROVIDER_OLLAMA, model_id="qwen3-coder:30b"
        )

        with mock.patch("config.llm_factory.settings") as settings:
            settings.OLLAMA_HOST = "http://localhost:11434"
            _stub_runtime_settings(settings)
            with mock.patch("config.llm_factory.dspy.LM") as mock_lm:
                create_lm(profile)

        self.assertEqual(mock_lm.call_args.args[0], "openai/qwen3-coder:30b")
        self.assertEqual(
            mock_lm.call_args.kwargs["api_base"], "http://localhost:11434/v1"
        )

    def test_temperature_is_sent_only_when_pinned(self) -> None:
        """A pinned temperature reaches the LM; an unpinned one is not sent at all."""
        pinned = LlmProfile(
            name="batch", provider=PROVIDER_OLLAMA, model_id="m", temperature=0.0
        )
        unpinned = LlmProfile(name="batch", provider=PROVIDER_OLLAMA, model_id="m")

        with mock.patch("config.llm_factory.settings") as settings:
            settings.OLLAMA_HOST = "http://localhost:11434"
            _stub_runtime_settings(settings)
            with mock.patch("config.llm_factory.dspy.LM") as mock_lm:
                create_lm(pinned)
                pinned_kwargs = mock_lm.call_args.kwargs
                create_lm(unpinned)
                unpinned_kwargs = mock_lm.call_args.kwargs

        self.assertEqual(pinned_kwargs["temperature"], 0.0)
        self.assertNotIn("temperature", unpinned_kwargs)

    def test_thinking_directive_is_translated_per_provider(self) -> None:
        """The canonical toggle reaches the LM in the provider's own spelling."""
        profile = LlmProfile(
            name="corp_index", provider=PROVIDER_DEEPSEEK, model_id="deepseek-chat"
        )

        with mock.patch("config.llm_thinking.settings.LLM_THINKING_MODE", "disabled"):
            with mock.patch("config.llm_factory.dspy.LM") as mock_lm:
                create_lm(profile)

        self.assertEqual(mock_lm.call_args.kwargs["thinking"], {"type": "disabled"})

    def test_profile_thinking_mode_overrides_the_global_one(self) -> None:
        """The corp index thinks without the global toggle having to move."""
        profile = LlmProfile(
            name="corp_index",
            provider=PROVIDER_DEEPSEEK,
            model_id="deepseek-reasoner",
            thinking_mode="enabled",
        )

        with mock.patch("config.llm_thinking.settings.LLM_THINKING_MODE", "disabled"):
            with mock.patch("config.llm_factory.dspy.LM") as mock_lm:
                create_lm(profile)

        self.assertEqual(mock_lm.call_args.kwargs["thinking"], {"type": "enabled"})

    def test_cache_can_be_turned_off_for_one_profile(self) -> None:
        """A profile whose purpose is to re-ask must not be served from cache."""
        profile = LlmProfile(
            name="corp_index",
            provider=PROVIDER_DEEPSEEK,
            model_id="deepseek-reasoner",
            cache=False,
        )

        with mock.patch("config.llm_factory.dspy.LM") as mock_lm:
            create_lm(profile)

        self.assertFalse(mock_lm.call_args.kwargs["cache"])

    def test_runtime_kwargs_reach_the_lm(self) -> None:
        """A profile-built LM inherits the cap and the deadline, not just the model.

        The UI planner and the corp index searcher used to be built with neither,
        so they ran at DSPy's default ``num_retries=3`` with no client deadline
        at all.
        """
        profile = LlmProfile(
            name="ui_planner", provider=PROVIDER_OLLAMA, model_id="qwen3-coder:30b"
        )

        with mock.patch("config.llm_factory.dspy.LM") as mock_lm:
            create_lm(profile)

        self.assertEqual(
            mock_lm.call_args.kwargs["max_tokens"], real_settings.LLM_MAX_TOKENS
        )
        self.assertEqual(
            mock_lm.call_args.kwargs["timeout"], real_settings.OLLAMA_TIMEOUT
        )


class TestLmRuntimeKwargs(unittest.TestCase):
    """The runtime-safety knobs shared by every ``dspy.LM`` in the project."""

    def test_both_retry_spellings_are_disarmed(self) -> None:
        """``num_retries`` and ``max_retries`` must agree, not merely be set.

        LiteLLM aliases one onto the other (``litellm/main.py:5117``), so a
        config that pins only ``max_retries=0`` has its zero overwritten and
        silently retries. That is what turned one 6-minute timeout into three.
        Asserting only one of the two is exactly the check that missed it.
        """
        kwargs = lm_runtime_kwargs("openai/qwen3-coder:30b")

        self.assertEqual(kwargs["num_retries"], 0)
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertEqual(kwargs["num_retries"], kwargs["max_retries"])

    def test_retry_counts_stay_paired_when_retries_are_enabled(self) -> None:
        """Re-enabling a retry moves both spellings together."""
        with mock.patch("config.llm_factory.settings.LLM_NUM_RETRIES", 2):
            kwargs = lm_runtime_kwargs("openai/qwen3-coder:30b")

        self.assertEqual(kwargs["num_retries"], 2)
        self.assertEqual(kwargs["max_retries"], 2)

    def test_output_length_is_capped(self) -> None:
        """An absent ``max_tokens`` is what lets Ollama decode without end."""
        kwargs = lm_runtime_kwargs("openai/qwen3-coder:30b")

        self.assertEqual(kwargs["max_tokens"], real_settings.LLM_MAX_TOKENS)
        self.assertGreater(kwargs["max_tokens"], 0)

    def test_request_deadline_is_set(self) -> None:
        """Every LM carries the client-side deadline, not only the batch ones."""
        kwargs = lm_runtime_kwargs("openai/qwen3-coder:30b")

        self.assertEqual(kwargs["timeout"], real_settings.OLLAMA_TIMEOUT)

    def test_frequency_penalty_is_absent_by_default(self) -> None:
        """The default run must be byte-identical to the greedy baseline."""
        with mock.patch("config.llm_factory.settings.LLM_FREQUENCY_PENALTY", 0.0):
            kwargs = lm_runtime_kwargs("openai/qwen3-coder:30b")

        self.assertNotIn("frequency_penalty", kwargs)

    def test_frequency_penalty_is_forwarded_when_configured(self) -> None:
        """The repetition-loop mitigation is reachable without a code change."""
        with mock.patch("config.llm_factory.settings.LLM_FREQUENCY_PENALTY", 0.2):
            kwargs = lm_runtime_kwargs("openai/qwen3-coder:30b")

        self.assertAlmostEqual(kwargs["frequency_penalty"], 0.2)

    def test_temperature_and_cache_are_left_to_the_call_site(self) -> None:
        """Both are semantic choices; ``deepseek-reasoner`` rejects a temperature."""
        kwargs = lm_runtime_kwargs("deepseek/deepseek-reasoner", "enabled")

        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("cache", kwargs)

    def test_thinking_translation_is_preserved_per_provider(self) -> None:
        """Folding the runtime knobs in must not drop the thinking directive."""
        with mock.patch("config.llm_thinking.settings.LLM_THINKING_MODE", "disabled"):
            openai_kwargs = lm_runtime_kwargs("openai/qwen3-coder:30b")
            ollama_kwargs = lm_runtime_kwargs("ollama/qwen3-coder:30b")
            deepseek_kwargs = lm_runtime_kwargs("deepseek/deepseek-chat")

        self.assertEqual(
            openai_kwargs["extra_body"], {"reasoning_effort": "none"}
        )
        self.assertEqual(ollama_kwargs["reasoning_effort"], "none")
        self.assertEqual(deepseek_kwargs["thinking"], {"type": "disabled"})

    def test_empty_model_name_is_rejected(self) -> None:
        """The thinking translation validates the model name; do not swallow it."""
        with self.assertRaises(ValueError):
            lm_runtime_kwargs("")

    def test_usage_accounting_is_installed(self) -> None:
        """Every LM is built from these kwargs, so this is what makes token
        accounting cover all nine construction sites."""
        with mock.patch("config.llm_factory.install_usage_logger") as install:
            lm_runtime_kwargs("openai/qwen3-coder:30b")

        install.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
