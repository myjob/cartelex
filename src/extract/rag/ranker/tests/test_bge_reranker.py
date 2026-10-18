"""Unit tests for BgeReranker.

The sentence-transformers ``CrossEncoder`` is a system boundary (it downloads a
~2.2 GB model), so it is mocked with a deterministic lexical-overlap scorer.
These tests therefore verify the reranker's *plumbing* — VO marshalling, top_k,
sort order, the Legal Climax boost and its opt-out — not model quality. They
mirror ``test_flash_ranker.py`` for the new backend.
"""

import re
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import torch

from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from pipelines.nace.nace_tree import NaceVo, RankedNaceVo


def _lexical_overlap(query: str, passage: str) -> float:
    """Fraction of *query* word types present in *passage* (deterministic, 0-1)."""
    query_terms = set(re.findall(r"\w+", query.lower()))
    passage_terms = set(re.findall(r"\w+", passage.lower()))
    if not query_terms:
        return 0.0
    return len(query_terms & passage_terms) / len(query_terms)


class FakeCrossEncoder:
    """Stand-in for sentence-transformers CrossEncoder at the model boundary."""

    def __init__(self, *args, **kwargs):
        # Accept model_name_or_path, max_length, cache_folder, activation_fn, ...
        self.init_args = args
        self.init_kwargs = kwargs
        # One entry per predict() call, so a test can assert what the reranker
        # forwards (batch_size in particular) without patching the method.
        self.predict_kwargs: list[dict] = []

    def predict(self, inputs, **kwargs):
        """Score each (query, passage) pair by lexical overlap, order-preserving."""
        self.predict_kwargs.append(kwargs)
        return np.array(
            [_lexical_overlap(query, passage) for query, passage in inputs],
            dtype=np.float32,
        )


class TestBgeReranker(unittest.TestCase):
    def setUp(self):
        # Patch the model boundary before constructing the ranker.
        self.patcher = patch(
            "extract.rag.ranker.bge_reranker.CrossEncoder", FakeCrossEncoder
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        from extract.rag.ranker.bge_reranker import BgeReranker

        self.ranker = BgeReranker()

        # doc_pos ascending so the last chunk sits deepest in the document.
        self.chunks = [
            ChunkVo(
                id="c1",
                celex_id="32003D0570",
                chunk="wholesale network access and services markets between operators",
                origin="test_mock",
                similarity=0.5,
                doc_pos=1,
            ),
            ChunkVo(
                id="c2",
                celex_id="32003D0570",
                chunk="retail consumer voice and data services for end users",
                origin="test_mock",
                similarity=0.5,
                doc_pos=5,
            ),
            ChunkVo(
                id="c3",
                celex_id="32003D0570",
                chunk="Done at Brussels. For the Commission. Signature and address.",
                origin="test_mock",
                similarity=0.5,
                doc_pos=10,
            ),
        ]

    def _nace_nodes(self) -> list[NaceVo]:
        return [
            NaceVo(type="group", code="26.1", title="Manufacture of electronic components and boards", isic_code="261", childs=[]),
            NaceVo(type="group", code="45.1", title="Sale of motor vehicles", isic_code="451", childs=[]),
            NaceVo(type="group", code="46.7", title="Wholesale of other machinery, equipment and supplies", isic_code="467", childs=[]),
            NaceVo(type="group", code="62.0", title="Computer programming, consultancy and related activities", isic_code="620", childs=[]),
        ]

    # ------------------------------------------------------------------ chunks
    def test_rank_chunkvo_returns_ranked_chunk_vo_instances(self):
        results = self.ranker.rank_chunkvo(
            "wholesale network access markets", self.chunks
        )
        self.assertEqual(len(results), len(self.chunks))
        for result in results:
            self.assertIsInstance(result, RankedChunkVo)
            self.assertIsInstance(result.score, float)
            self.assertIsInstance(result.base_score, float)

    def test_rank_chunkvo_empty_input(self):
        self.assertEqual(self.ranker.rank_chunkvo("any query", []), [])

    def test_rank_chunkvo_top_k_respected(self):
        results = self.ranker.rank_chunkvo(
            "wholesale network access markets", self.chunks, top_k=1
        )
        self.assertEqual(len(results), 1)

    def test_rank_chunkvo_scores_descending(self):
        results = self.ranker.rank_chunkvo(
            "wholesale network access markets", self.chunks
        )
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_rank_chunkvo_base_score_not_exceeds_adjusted(self):
        """The position boost only adds: base_score <= score for every chunk."""
        results = self.ranker.rank_chunkvo(
            "wholesale network access markets", self.chunks
        )
        for result in results:
            self.assertLessEqual(result.base_score, result.score)

    def test_rank_chunkvo_base_score_in_unit_range(self):
        """Sigmoid activation keeps base scores in (0, 1) regime."""
        results = self.ranker.rank_chunkvo(
            "wholesale network access markets", self.chunks
        )
        for result in results:
            self.assertGreaterEqual(result.base_score, 0.0)
            self.assertLessEqual(result.base_score, 1.0)

    def test_rank_chunkvo_disable_position_boost_uses_base_score(self):
        """apply_position_boost=False must collapse score to base_score exactly."""
        results = self.ranker.rank_chunkvo(
            "wholesale network access markets",
            self.chunks,
            apply_position_boost=False,
        )
        self.assertTrue(results)
        for result in results:
            self.assertEqual(result.score, result.base_score)

    def test_rank_chunkvo_best_match_plausible(self):
        """With the boost off, the highest-overlap chunk ranks first."""
        results = self.ranker.rank_chunkvo(
            "wholesale network access services markets operators",
            self.chunks,
            apply_position_boost=False,
        )
        self.assertEqual(results[0].id, "c1")

    def test_rank_chunkvo_boost_lifts_deep_chunk(self):
        """In legal_climax mode the boost lifts an otherwise-tied deep chunk."""
        # Two chunks with identical text (equal base score) at different depths.
        shallow = ChunkVo(id="s", celex_id="x", chunk="article grounds", origin="t", similarity=0.5, doc_pos=1)
        deep = ChunkVo(id="d", celex_id="x", chunk="article grounds", origin="t", similarity=0.5, doc_pos=100)
        with patch(
            "extract.rag.ranker.position_boost.settings.RANKER_BOOST_MODE",
            "legal_climax",
        ):
            results = self.ranker.rank_chunkvo("article grounds", [shallow, deep])
        self.assertEqual(results[0].id, "d")

    def test_rank_chunkvo_section_boost_lifts_fines_chunk(self):
        """Default mode: the section weight lifts an otherwise-tied fines chunk."""
        recital = ChunkVo(
            id="r", celex_id="x", chunk="article grounds", origin="t",
            similarity=0.5, doc_pos=50, section_type="recitals_facts",
        )
        fines = ChunkVo(
            id="f", celex_id="x", chunk="article grounds", origin="t",
            similarity=0.5, doc_pos=10, section_type="fines",
        )
        with patch(
            "extract.rag.ranker.position_boost.settings.RANKER_BOOST_MODE",
            "section_type",
        ):
            results = self.ranker.rank_chunkvo("article grounds", [recital, fines])
        self.assertEqual(results[0].id, "f")
        self.assertEqual(results[0].section_type, "fines")

    # ------------------------------------------------------------------- nace
    def test_rank_nacevo_returns_ranked_nace_vo_instances(self):
        nodes = self._nace_nodes()
        results = self.ranker.rank_nacevo("software development", nodes)
        self.assertEqual(len(results), len(nodes))
        for result in results:
            self.assertIsInstance(result, RankedNaceVo)
            self.assertIsInstance(result.score, float)
            self.assertIsInstance(result.base_score, float)

    def test_rank_nacevo_empty_input(self):
        self.assertEqual(self.ranker.rank_nacevo("any query", []), [])

    def test_rank_nacevo_top_k_respected(self):
        results = self.ranker.rank_nacevo("machinery equipment", self._nace_nodes(), top_k=2)
        self.assertEqual(len(results), 2)

    def test_rank_nacevo_scores_descending(self):
        results = self.ranker.rank_nacevo("sale of motor vehicles", self._nace_nodes())
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_rank_nacevo_no_boost_score_equals_base(self):
        for result in self.ranker.rank_nacevo("sale of vehicles", self._nace_nodes()):
            self.assertEqual(result.score, result.base_score)

    def test_rank_nacevo_best_match_plausible(self):
        results = self.ranker.rank_nacevo(
            "retail sale of cars and motor vehicles", self._nace_nodes()
        )
        self.assertEqual(results[0].code, "45.1")

    def test_rank_nacevo_preserves_vo_reference(self):
        nodes = self._nace_nodes()
        node_map = {vo.code: vo for vo in nodes}
        for result in self.ranker.rank_nacevo("electronic components", nodes):
            self.assertIs(result.vo, node_map[result.code])


CUDA_CAPABILITY_ERROR = RuntimeError(
    "CUDA error: no kernel image is available for execution on the device"
)


def _cross_encoder_failing_on(device_to_fail, error, *, at, attempted_devices):
    """Build a CrossEncoder stand-in that raises *error* on *device_to_fail*.

    Args:
        device_to_fail: Device string whose use must fail; every other device
            behaves like a plain FakeCrossEncoder.
        error: Exception instance to raise.
        at: ``"load"`` to raise during construction, or ``"predict"`` to let the
            model load and raise on the first forward pass instead — which is
            where a CUDA capability mismatch actually surfaces.
        attempted_devices: List appended with each device the ranker tries, in
            order, so a test can assert the retry sequence.

    Returns:
        A callable usable as the patched ``CrossEncoder``.
    """

    class _Probing(FakeCrossEncoder):
        def predict(self, inputs, **kwargs):
            if at == "predict" and self.init_kwargs.get("device") == device_to_fail:
                raise error
            return super().predict(inputs, **kwargs)

    def factory(*args, **kwargs):
        device = kwargs.get("device")
        attempted_devices.append(device)
        if at == "load" and device == device_to_fail:
            raise error
        return _Probing(*args, **kwargs)

    return factory


def _cross_encoder_failing_after_probe(device_to_fail, error, *, attempted_devices):
    """Like :func:`_cross_encoder_failing_on`, but the warm-up probe succeeds.

    This is the shared-GPU case: the model loads and the one-pair probe in
    ``__init__`` runs fine because the card still has room, and only a real
    ranking batch exhausts it. ``_cross_encoder_failing_on(at="predict")``
    cannot express that — it fails the probe too, so the fallback happens during
    construction and never exercises the runtime guard.

    Args:
        device_to_fail: Device string whose ranking calls must fail; every other
            device behaves like a plain FakeCrossEncoder.
        error: Exception instance to raise on every post-probe forward pass.
        attempted_devices: List appended with each device the ranker tries, in
            order, so a test can assert the retry sequence and its stickiness.

    Returns:
        A callable usable as the patched ``CrossEncoder``.
    """

    class _ProbeThenFail(FakeCrossEncoder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.probe_done = False

        def predict(self, inputs, **kwargs):
            failing_device = self.init_kwargs.get("device") == device_to_fail
            if failing_device and self.probe_done:
                raise error
            self.probe_done = True
            return super().predict(inputs, **kwargs)

    def factory(*args, **kwargs):
        attempted_devices.append(kwargs.get("device"))
        return _ProbeThenFail(*args, **kwargs)

    return factory


class TestBgeRerankerDeviceSelection(unittest.TestCase):
    """Device resolution and the CUDA fallbacks, with the model boundary mocked."""

    def setUp(self):
        self.patcher = patch(
            "extract.rag.ranker.bge_reranker.CrossEncoder", FakeCrossEncoder
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        from extract.rag.ranker.bge_reranker import BgeReranker

        self.reranker_cls = BgeReranker

    def test_auto_device_passes_none_to_cross_encoder(self):
        """"auto" must let sentence-transformers pick the device itself."""
        ranker = self.reranker_cls(device="auto")
        self.assertIsNone(ranker.device)
        self.assertIsNone(ranker.model.init_kwargs["device"])

    def test_explicit_device_is_forwarded(self):
        ranker = self.reranker_cls(device="cpu")
        self.assertEqual(ranker.device, "cpu")
        self.assertEqual(ranker.model.init_kwargs["device"], "cpu")

    def test_device_defaults_to_settings(self):
        with patch(
            "extract.rag.ranker.bge_reranker.settings.RERANKER_DEVICE", "cuda:1"
        ):
            self.assertEqual(self.reranker_cls().device, "cuda:1")

    def test_cuda_oom_falls_back_to_cpu(self):
        """A CUDA OOM on load must retry on CPU rather than kill the caller."""
        attempted_devices: list[str | None] = []

        def failing_cross_encoder(*args, **kwargs):
            device = kwargs.get("device")
            attempted_devices.append(device)
            if device != "cpu":
                raise torch.OutOfMemoryError("CUDA out of memory")
            return FakeCrossEncoder(*args, **kwargs)

        with patch(
            "extract.rag.ranker.bge_reranker.CrossEncoder", failing_cross_encoder
        ):
            ranker = self.reranker_cls(device="cuda")

        self.assertEqual(attempted_devices, ["cuda", "cpu"])
        self.assertEqual(ranker.device, "cpu")
        self.assertIsInstance(ranker.model, FakeCrossEncoder)

    def test_cpu_oom_is_not_swallowed(self):
        """Nothing to fall back to when CPU itself is out of memory."""

        def failing_cross_encoder(*args, **kwargs):
            raise torch.OutOfMemoryError("out of memory")

        with patch(
            "extract.rag.ranker.bge_reranker.CrossEncoder", failing_cross_encoder
        ):
            with self.assertRaises(torch.OutOfMemoryError):
                self.reranker_cls(device="cpu")

    def test_cuda_capability_error_on_load_falls_back_to_cpu(self):
        """A CUDA kernel-image failure is not an OOM but is just as recoverable."""
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_on(
            "cuda",
            CUDA_CAPABILITY_ERROR,
            at="load",
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            ranker = self.reranker_cls(device="cuda")

        self.assertEqual(attempted_devices, ["cuda", "cpu"])
        self.assertEqual(ranker.device, "cpu")

    def test_cuda_failure_on_first_forward_pass_falls_back_to_cpu(self):
        """The probe must catch a GPU that loads fine but cannot run kernels.

        Without the warm-up probe this failure escapes ``__init__`` entirely and
        surfaces on a live retrieval request instead.
        """
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_on(
            "cuda",
            CUDA_CAPABILITY_ERROR,
            at="predict",
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            ranker = self.reranker_cls(device="cuda")

        self.assertEqual(attempted_devices, ["cuda", "cpu"])
        self.assertEqual(ranker.device, "cpu")
        # The CPU model must be live, not merely constructed.
        self.assertEqual(len(ranker.model.predict([("a", "a")])), 1)

    def test_non_cuda_runtime_error_is_not_swallowed(self):
        """An unrelated load failure must propagate, not silently retry on CPU."""
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_on(
            "cuda",
            RuntimeError("config.json is malformed"),
            at="load",
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            with self.assertRaises(RuntimeError):
                self.reranker_cls(device="cuda")

        self.assertEqual(attempted_devices, ["cuda"])

    def test_cuda_failure_on_cpu_probe_is_not_swallowed(self):
        """Already on CPU means there is nothing left to fall back to."""
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_on(
            "cpu",
            CUDA_CAPABILITY_ERROR,
            at="predict",
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            with self.assertRaises(RuntimeError):
                self.reranker_cls(device="cpu")

        self.assertEqual(attempted_devices, ["cpu"])

    def test_resolved_device_is_reported_for_auto(self):
        """"auto" leaves .device None, so .resolved_device is the only answer."""
        ranker = self.reranker_cls(device="auto")

        self.assertIsNone(ranker.device)
        self.assertEqual(ranker.resolved_device, "cpu")


class TestBgeRerankerRuntimeCudaFallback(unittest.TestCase):
    """The CUDA guard around ranking calls, not just construction.

    On a GPU shared with the backend container the model loads fine and a later
    forward pass runs out of memory; ``__init__``'s fallback cannot see that.
    """

    def setUp(self):
        from extract.rag.ranker.bge_reranker import BgeReranker

        self.reranker_cls = BgeReranker

    @staticmethod
    def _chunks() -> list[ChunkVo]:
        return [
            ChunkVo(
                id="c1",
                celex_id="32003D0570",
                chunk="wholesale network access and services markets",
                origin="test_mock",
                similarity=0.5,
                doc_pos=1,
            ),
            ChunkVo(
                id="c2",
                celex_id="32003D0570",
                chunk="retail consumer voice and data services",
                origin="test_mock",
                similarity=0.5,
                doc_pos=5,
            ),
        ]

    def test_cuda_oom_during_ranking_falls_back_to_cpu(self):
        """The OOM that actually happened: load succeeds, ranking does not."""
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_after_probe(
            "cuda",
            torch.OutOfMemoryError("CUDA out of memory. Tried to allocate 232.00 MiB"),
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            ranker = self.reranker_cls(device="cuda")
            # Construction must NOT have fallen back — the probe succeeded.
            self.assertEqual(attempted_devices, ["cuda"])
            self.assertEqual(ranker.resolved_device, "cuda")

            chunks = self._chunks()
            results = ranker.rank_chunkvo("wholesale network access", chunks)

        self.assertEqual(attempted_devices, ["cuda", "cpu"])
        self.assertEqual(ranker.device, "cpu")
        self.assertEqual(ranker.resolved_device, "cpu")
        # Every candidate is re-scored on the CPU; nothing is dropped.
        self.assertEqual(len(results), len(chunks))
        self.assertEqual({r.id for r in results}, {c.id for c in chunks})

    def test_cuda_downgrade_is_sticky_across_calls(self):
        """A contended card must not be retried on every later ranking call."""
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_after_probe(
            "cuda",
            torch.OutOfMemoryError("CUDA out of memory"),
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            ranker = self.reranker_cls(device="cuda")
            ranker.rank_chunkvo("wholesale network access", self._chunks())
            ranker.rank_chunkvo("retail consumer services", self._chunks())

        # One CUDA attempt, one CPU reload — no second GPU attempt.
        self.assertEqual(attempted_devices, ["cuda", "cpu"])

    def test_cuda_oom_during_rank_nacevo_falls_back_to_cpu(self):
        """rank_nacevo shares the guard; it is not a chunk-only concern."""
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_after_probe(
            "cuda",
            torch.OutOfMemoryError("CUDA out of memory"),
            attempted_devices=attempted_devices,
        )
        nodes = [
            NaceVo(
                type="group",
                code="26.1",
                title="Manufacture of electronic components and boards",
                isic_code="261",
                childs=[],
            )
        ]

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            ranker = self.reranker_cls(device="cuda")
            results = ranker.rank_nacevo("electronic components", nodes)

        self.assertEqual(attempted_devices, ["cuda", "cpu"])
        self.assertEqual(len(results), 1)

    def test_oom_on_cpu_ranking_is_not_swallowed(self):
        """Already on CPU means there is nothing left to fall back to."""
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_after_probe(
            "cpu",
            torch.OutOfMemoryError("out of memory"),
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            ranker = self.reranker_cls(device="cpu")
            with self.assertRaises(torch.OutOfMemoryError):
                ranker.rank_chunkvo("wholesale network access", self._chunks())

        self.assertEqual(attempted_devices, ["cpu"])

    def test_auto_resolved_cpu_ranking_failure_is_not_swallowed(self):
        """The guard must key off resolved_device, not the None-valued device.

        Under RERANKER_DEVICE=auto ``device`` stays None on a CPU-resident
        model, so a ``device == "cpu"`` check would try to "fall back" to a CPU
        it is already on and loop through a pointless reload.
        """
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_after_probe(
            None,
            torch.OutOfMemoryError("out of memory"),
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            ranker = self.reranker_cls(device="auto")
            self.assertIsNone(ranker.device)
            self.assertEqual(ranker.resolved_device, "cpu")
            with self.assertRaises(torch.OutOfMemoryError):
                ranker.rank_chunkvo("wholesale network access", self._chunks())

        self.assertEqual(attempted_devices, [None])

    def test_non_cuda_runtime_error_during_ranking_is_not_swallowed(self):
        """An unrelated forward-pass bug must propagate, not trigger a reload."""
        attempted_devices: list[str | None] = []
        factory = _cross_encoder_failing_after_probe(
            "cuda",
            RuntimeError("expected a tensor, got NoneType"),
            attempted_devices=attempted_devices,
        )

        with patch("extract.rag.ranker.bge_reranker.CrossEncoder", factory):
            ranker = self.reranker_cls(device="cuda")
            with self.assertRaises(RuntimeError):
                ranker.rank_chunkvo("wholesale network access", self._chunks())

        self.assertEqual(attempted_devices, ["cuda"])


class TestBgeRerankerBatchSize(unittest.TestCase):
    """RERANKER_BATCH_SIZE reaches the CrossEncoder on every scoring path."""

    def setUp(self):
        self.patcher = patch(
            "extract.rag.ranker.bge_reranker.CrossEncoder", FakeCrossEncoder
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        from extract.rag.ranker.bge_reranker import BgeReranker

        self.reranker_cls = BgeReranker

    def test_batch_size_defaults_to_settings(self):
        with patch("extract.rag.ranker.bge_reranker.settings.RERANKER_BATCH_SIZE", 8):
            self.assertEqual(self.reranker_cls().batch_size, 8)

    def test_explicit_batch_size_overrides_settings(self):
        self.assertEqual(self.reranker_cls(batch_size=4).batch_size, 4)

    def test_rank_chunkvo_forwards_batch_size(self):
        ranker = self.reranker_cls(batch_size=4)
        chunks = [
            ChunkVo(
                id="c1",
                celex_id="32003D0570",
                chunk="wholesale network access",
                origin="test_mock",
                similarity=0.5,
                doc_pos=1,
            )
        ]
        ranker.model.predict_kwargs.clear()  # drop the construction probe's call
        ranker.rank_chunkvo("wholesale network access", chunks)

        self.assertEqual(ranker.model.predict_kwargs[-1]["batch_size"], 4)

    def test_rank_nacevo_forwards_batch_size(self):
        ranker = self.reranker_cls(batch_size=4)
        nodes = [
            NaceVo(
                type="group",
                code="26.1",
                title="Manufacture of electronic components and boards",
                isic_code="261",
                childs=[],
            )
        ]
        ranker.model.predict_kwargs.clear()
        ranker.rank_nacevo("electronic components", nodes)

        self.assertEqual(ranker.model.predict_kwargs[-1]["batch_size"], 4)


class TestBgeRerankerOfflineResolution(unittest.TestCase):
    """Model resolution never reaches the Hub when configured for local files."""

    def setUp(self):
        self.patcher = patch(
            "extract.rag.ranker.bge_reranker.CrossEncoder", FakeCrossEncoder
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

        from extract.rag.ranker.bge_reranker import BgeReranker

        self.reranker_cls = BgeReranker

    def test_repo_id_resolves_online_by_default(self):
        """The default stays a Hub-capable load, so nothing breaks unconfigured."""
        with patch(
            "extract.rag.ranker.bge_reranker.settings.RERANKER_LOCAL_FILES_ONLY",
            False,
        ):
            ranker = self.reranker_cls()

        self.assertFalse(ranker.local_files_only)
        self.assertFalse(ranker.model.init_kwargs["local_files_only"])

    def test_setting_forces_local_files_only(self):
        with patch(
            "extract.rag.ranker.bge_reranker.settings.RERANKER_LOCAL_FILES_ONLY", True
        ):
            ranker = self.reranker_cls()

        self.assertTrue(ranker.local_files_only)
        self.assertTrue(ranker.model.init_kwargs["local_files_only"])

    def test_directory_model_id_is_offline_without_the_setting(self):
        """A path to downloaded weights must bypass the Hub on its own."""
        with tempfile.TemporaryDirectory() as model_dir:
            with patch(
                "extract.rag.ranker.bge_reranker.settings.RERANKER_LOCAL_FILES_ONLY",
                False,
            ):
                ranker = self.reranker_cls(model_name=model_dir)

            self.assertTrue(ranker.local_files_only)
            self.assertTrue(ranker.model.init_kwargs["local_files_only"])

    def test_offline_miss_names_the_download_command(self):
        """An unpopulated cache must fail with the way out, not a bare OSError."""

        def missing_weights(*args, **kwargs):
            raise OSError("We couldn't connect to 'https://huggingface.co'")

        with patch(
            "extract.rag.ranker.bge_reranker.CrossEncoder", missing_weights
        ), patch(
            "extract.rag.ranker.bge_reranker.settings.RERANKER_LOCAL_FILES_ONLY", True
        ):
            with self.assertRaises(OSError) as raised:
                self.reranker_cls(model_name="BAAI/bge-reranker-v2-m3")

        message = str(raised.exception)
        self.assertIn("hf download BAAI/bge-reranker-v2-m3", message)
        self.assertIn("RERANKER_LOCAL_FILES_ONLY", message)

    def test_online_resolution_error_is_not_rewritten(self):
        """Without offline mode the original OSError must reach the caller."""

        def unreadable_cache(*args, **kwargs):
            raise OSError("disk is on fire")

        with patch(
            "extract.rag.ranker.bge_reranker.CrossEncoder", unreadable_cache
        ), patch(
            "extract.rag.ranker.bge_reranker.settings.RERANKER_LOCAL_FILES_ONLY",
            False,
        ):
            with self.assertRaises(OSError) as raised:
                self.reranker_cls()

        self.assertEqual(str(raised.exception), "disk is on fire")


if __name__ == "__main__":
    unittest.main()
