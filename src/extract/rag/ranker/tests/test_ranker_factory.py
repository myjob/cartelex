"""Unit tests for the reranker factory and its profiles.

Both model boundaries are mocked (FlashRank's ONNX ``Ranker`` and the
sentence-transformers ``CrossEncoder``) so no models are downloaded/loaded.
The subject is profile isolation: the ui profile and the batch profile resolve
from their own settings, a flashrank profile never carries a model id, and the
factory cache is keyed by (backend, model id) rather than by profile name.

The factory caches instances, so every factory test resets the cache before
and after itself: a ranker built under the mock patch must never leak into
another test (or another test module) that expects a real model.
"""

import gc
import os
import sys
import unittest
import weakref
from unittest.mock import patch

from pydantic import ValidationError

from config.settings import settings
from extract.rag.ranker.bge_reranker import BgeReranker
from extract.rag.ranker.flash_ranker import FlashRanker
from extract.rag.ranker.ranker_factory import (
    PROFILE_BATCH,
    PROFILE_UI,
    RankerProfile,
    RankerProtocol,
    batch_ranker_profile,
    create_ranker,
    reset_ranker_cache,
    ui_ranker_profile,
)

QWEN_MODEL = "Qwen/Qwen3-Reranker-0.6B"
BGE_MODEL = "BAAI/bge-reranker-v2-m3"


class TestRankerProfile(unittest.TestCase):
    """Validation rules of the profile value object, independent of settings."""

    def test_backend_is_normalized(self):
        self.assertEqual(
            RankerProfile(name="t", backend=" BGE ", model_id=BGE_MODEL).backend,
            "bge",
        )
        self.assertEqual(
            RankerProfile(name="t", backend=" FlashRank ").backend, "flashrank"
        )

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(ValueError) as raised:
            RankerProfile(name="t", backend="weaviate")
        self.assertIn("bge", str(raised.exception))
        self.assertIn("flashrank", str(raised.exception))

    def test_flashrank_profile_drops_the_model_id(self):
        """FlashRank ignores the id; a stray value must not split the cache."""
        profile = RankerProfile(name="t", backend="flashrank", model_id=BGE_MODEL)
        self.assertEqual(profile.model_id, "")

    def test_bge_profile_requires_a_model_id(self):
        with self.assertRaises(ValueError):
            RankerProfile(name="t", backend="bge", model_id="   ")

    def test_model_id_is_trimmed(self):
        profile = RankerProfile(name="t", backend="bge", model_id=f"  {QWEN_MODEL} ")
        self.assertEqual(profile.model_id, QWEN_MODEL)

    def test_profile_is_frozen(self):
        profile = RankerProfile(name="t", backend="bge", model_id=BGE_MODEL)
        with self.assertRaises(ValidationError):
            profile.backend = "flashrank"


class TestConfiguredProfiles(unittest.TestCase):
    """The ui and batch profiles resolve from their own settings."""

    @unittest.skipIf(
        os.getenv("UI_RERANKER_BACKEND"), "UI_RERANKER_BACKEND overridden by the environment"
    )
    def test_ui_profile_defaults_to_flashrank(self):
        """The shipped default for the UI is FlashRank; pinned as a literal."""
        profile = ui_ranker_profile()
        self.assertEqual(profile.name, PROFILE_UI)
        self.assertEqual(profile.backend, "flashrank")
        self.assertEqual(profile.model_id, "")

    def test_ui_profile_follows_its_setting(self):
        with patch.object(settings, "UI_RERANKER_BACKEND", "bge"):
            self.assertEqual(ui_ranker_profile().backend, "bge")

    def test_ui_blank_model_id_follows_the_batch_model(self):
        with (
            patch.object(settings, "UI_RERANKER_BACKEND", "bge"),
            patch.object(settings, "UI_RERANKER_MODEL_ID", ""),
            patch.object(settings, "RERANKER_MODEL_ID", QWEN_MODEL),
        ):
            self.assertEqual(ui_ranker_profile().model_id, QWEN_MODEL)

    def test_ui_explicit_model_id_wins(self):
        with (
            patch.object(settings, "UI_RERANKER_BACKEND", "bge"),
            patch.object(settings, "UI_RERANKER_MODEL_ID", QWEN_MODEL),
            patch.object(settings, "RERANKER_MODEL_ID", BGE_MODEL),
        ):
            self.assertEqual(ui_ranker_profile().model_id, QWEN_MODEL)

    def test_ui_flashrank_ignores_the_batch_model_id(self):
        with (
            patch.object(settings, "UI_RERANKER_BACKEND", "flashrank"),
            patch.object(settings, "UI_RERANKER_MODEL_ID", ""),
            patch.object(settings, "RERANKER_MODEL_ID", "whatever"),
        ):
            self.assertEqual(ui_ranker_profile().model_id, "")

    def test_batch_profile_follows_reranker_backend(self):
        with patch.object(settings, "RERANKER_BACKEND", "flashrank"):
            profile = batch_ranker_profile()
        self.assertEqual(profile.name, PROFILE_BATCH)
        self.assertEqual(profile.backend, "flashrank")

        with (
            patch.object(settings, "RERANKER_BACKEND", "bge"),
            patch.object(settings, "RERANKER_MODEL_ID", QWEN_MODEL),
        ):
            profile = batch_ranker_profile()
        self.assertEqual(profile.backend, "bge")
        self.assertEqual(profile.model_id, QWEN_MODEL)

    def test_repointing_the_ui_leaves_the_batch_alone(self):
        with (
            patch.object(settings, "UI_RERANKER_BACKEND", "flashrank"),
            patch.object(settings, "RERANKER_BACKEND", "bge"),
            patch.object(settings, "RERANKER_MODEL_ID", BGE_MODEL),
        ):
            self.assertEqual(ui_ranker_profile().backend, "flashrank")
            self.assertEqual(batch_ranker_profile().backend, "bge")

    def test_misconfigured_ui_backend_is_rejected(self):
        with patch.object(settings, "UI_RERANKER_BACKEND", "weaviate"):
            with self.assertRaises(ValueError):
                ui_ranker_profile()


class TestCreateRanker(unittest.TestCase):
    """Backend construction and the (backend, model id) cache."""

    def setUp(self):
        # Patch the two model boundaries so construction is free.
        self._flash_patcher = patch("extract.rag.ranker.flash_ranker.Ranker")
        self._ce_patcher = patch("extract.rag.ranker.bge_reranker.CrossEncoder")
        self._flash_patcher.start()
        self._ce_patcher.start()
        self.addCleanup(self._flash_patcher.stop)
        self.addCleanup(self._ce_patcher.stop)

        # Mocked instances must not survive this test in the factory cache.
        reset_ranker_cache()
        self.addCleanup(reset_ranker_cache)

    @staticmethod
    def _bge(name: str = "t", model_id: str = BGE_MODEL) -> RankerProfile:
        return RankerProfile(name=name, backend="bge", model_id=model_id)

    @staticmethod
    def _flashrank(name: str = "t") -> RankerProfile:
        return RankerProfile(name=name, backend="flashrank")

    def test_default_uses_the_batch_profile(self):
        with (
            patch.object(settings, "RERANKER_BACKEND", "bge"),
            patch.object(settings, "RERANKER_MODEL_ID", BGE_MODEL),
        ):
            self.assertIsInstance(create_ranker(), BgeReranker)
        with patch.object(settings, "RERANKER_BACKEND", "flashrank"):
            self.assertIsInstance(create_ranker(), FlashRanker)

    def test_ui_flashrank_profile_builds_flashrank(self):
        self.assertIsInstance(create_ranker(self._flashrank("ui")), FlashRanker)

    def test_bge_profile_carries_its_model_id(self):
        ranker = create_ranker(self._bge(model_id=QWEN_MODEL))
        self.assertIsInstance(ranker, BgeReranker)
        self.assertEqual(ranker.model_name, QWEN_MODEL)

    def test_returned_rankers_satisfy_protocol(self):
        self.assertIsInstance(create_ranker(self._flashrank()), RankerProtocol)
        self.assertIsInstance(create_ranker(self._bge()), RankerProtocol)

    # ------------------------------------------------------------------ cache
    def test_same_profile_returns_same_instance(self):
        """Two call sites asking for the same profile share one loaded model."""
        self.assertIs(create_ranker(self._bge()), create_ranker(self._bge()))

    def test_profiles_with_the_same_backend_and_model_share_one_instance(self):
        """The cache key is (backend, model id), never the profile name."""
        ui = create_ranker(self._bge(name="ui"))
        batch = create_ranker(self._bge(name="batch"))
        self.assertIs(ui, batch)

    def test_default_and_explicit_batch_profile_share_one_instance(self):
        with (
            patch.object(settings, "RERANKER_BACKEND", "bge"),
            patch.object(settings, "RERANKER_MODEL_ID", BGE_MODEL),
        ):
            self.assertIs(create_ranker(), create_ranker(batch_ranker_profile()))

    def test_different_model_ids_are_cached_separately(self):
        alpha = create_ranker(self._bge(model_id=QWEN_MODEL))
        beta = create_ranker(self._bge(model_id=BGE_MODEL))
        self.assertIsNot(alpha, beta)
        self.assertIs(alpha, create_ranker(self._bge(model_id=QWEN_MODEL)))

    def test_different_backends_are_cached_separately(self):
        self.assertIsNot(create_ranker(self._bge()), create_ranker(self._flashrank()))

    def test_use_cache_false_builds_a_fresh_instance(self):
        cached = create_ranker(self._bge())
        uncached = create_ranker(self._bge(), use_cache=False)
        self.assertIsNot(cached, uncached)
        # ... and the throwaway instance must not replace the cached one.
        self.assertIs(cached, create_ranker(self._bge()))

    def test_reset_clears_the_cache(self):
        first = create_ranker(self._bge())
        reset_ranker_cache()
        self.assertIsNot(first, create_ranker(self._bge()))

    def test_cache_holds_only_a_weak_reference(self):
        """Dropping every caller reference must free the model, not pin it."""
        ranker = create_ranker(self._bge())
        observer = weakref.ref(ranker)
        del ranker
        gc.collect()
        self.assertIsNone(observer())

    def test_bge_without_the_torch_stack_raises_a_named_error(self):
        """A torch-less image must say which lock to rebuild, not just ImportError.

        Simulates the requirements-ui.txt serving image, where BgeReranker is
        absent because sentence-transformers is not installed. A ``None`` entry
        in ``sys.modules`` makes the lazy import inside ``_build_ranker`` raise
        ImportError exactly as a missing dependency would.
        """
        with patch.dict(sys.modules, {"extract.rag.ranker.bge_reranker": None}):
            with self.assertRaises(RuntimeError) as caught:
                create_ranker(self._bge(name="ui"))

        message = str(caught.exception)
        self.assertIn("ui", message)
        self.assertIn("sentence-transformers", message)
        self.assertIn("requirements-cpu.txt", message)
        self.assertIsInstance(caught.exception.__cause__, ImportError)

    def test_flashrank_still_builds_without_the_torch_stack(self):
        """The lean image's own backend must be unaffected by bge's absence."""
        with patch.dict(sys.modules, {"extract.rag.ranker.bge_reranker": None}):
            self.assertIsInstance(create_ranker(self._flashrank()), FlashRanker)


if __name__ == "__main__":
    unittest.main()
