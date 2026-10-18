import logging
import os
from typing import List, Optional, Tuple

import torch
from sentence_transformers import CrossEncoder

from config.settings import settings
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from extract.rag.ranker.position_boost import structural_boost
from pipelines.nace.nace_tree import NaceVo, RankedNaceVo

logger = logging.getLogger(__name__)


class BgeReranker:
    """CrossEncoder-backed reranker, drop-in for :class:`FlashRanker`.

    Uses a sentence-transformers ``CrossEncoder`` (default
    ``BAAI/bge-reranker-v2-m3``): a *true* cross-encoder that scores each
    ``(query, passage)`` pair in a single forward pass, multilingual with an 8k
    context window. This is a strict upgrade over FlashRank's 2021-era
    ``ms-marco-MiniLM`` (English web-QA, 512-token) for the legal EN/DE decision
    text this system reranks (review Weakness 3).

    The backend is model-agnostic: ``settings.RERANKER_MODEL_ID`` may point at
    any CrossEncoder-compatible reranker (e.g. a ``Qwen/Qwen3-Reranker-*`` size)
    without touching this class. Scores are squashed to ``(0, 1)`` with a sigmoid
    so they share FlashRank's 0-1 regime and the shared structural boost stays
    comparable across backends.

    The model is loaded once in ``__init__`` (Provisioner pattern); the same
    ``rank_chunkvo`` / ``rank_nacevo`` interface as ``FlashRanker`` is preserved
    so every injection site is unchanged.

    Device selection is automatic by default: ``settings.RERANKER_DEVICE`` is
    ``"auto"``, which lets sentence-transformers take a visible CUDA device and
    fall back to CPU when there is none, so the same image runs unchanged on a
    GPU workstation and a GPU-less laptop. Construction then *verifies* that
    choice with a throwaway forward pass and retries on CPU if the GPU cannot
    actually execute the model.

    The same CUDA fallback also guards every *ranking* call, not just
    construction. On a GPU shared between the backend container and a batch CLI
    run, the model loads successfully and only a later forward pass runs out of
    memory; :meth:`_predict` catches that, reloads the model on the CPU and
    re-scores the failed batch. The downgrade is **sticky** — once a CUDA
    failure has been seen, the instance stays on the CPU for the rest of its
    life rather than retrying a card that is still contended. Because
    ``ranker_factory.create_ranker`` shares one instance per backend across
    every call site in the process, a single downgrade covers them all.

    Model resolution is offline-capable. ``settings.RERANKER_MODEL_ID`` may be a
    directory of downloaded weights instead of a Hub repo id, and
    ``settings.RERANKER_LOCAL_FILES_ONLY`` forces a repo id to resolve from
    ``settings.RERANKER_CACHE_DIR`` alone. Either way no Hub call is made, so a
    host with no egress (or a slow Hub) neither stalls startup nor logs the
    ``modules.json`` 404 and anonymous-request warning that a normal load emits.

    Attributes:
        local_files_only: Whether the weights are resolved from disk only. True
            when ``settings.RERANKER_LOCAL_FILES_ONLY`` is set or the model name
            is an existing directory.
        device: The requested torch device, or ``None`` for automatic selection.
            Rewritten to ``"cpu"`` when a CUDA attempt fails.
        resolved_device: The device the loaded model actually sits on, logged at
            INFO during construction. Unlike ``device`` this is never ``None``,
            and it is the attribute the runtime fallback keys off. Rewritten to
            ``"cpu"`` by a downgrade.
        batch_size: ``(query, passage)`` pairs per forward pass. Bounds peak
            activation memory; see ``settings.RERANKER_BATCH_SIZE``.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        max_length: Optional[int] = None,
        device: Optional[str] = None,
        batch_size: Optional[int] = None,
    ):
        """Load the CrossEncoder reranker.

        Args:
            model_name: HuggingFace/CrossEncoder model id. Defaults to
                ``settings.RERANKER_MODEL_ID``.
            max_length: Max tokens per ``(query, passage)`` pair. Defaults to
                ``settings.RERANKER_MAX_LENGTH``.
            device: torch device to load on ("cpu", "cuda", "cuda:1"), or
                ``"auto"``/``None`` to let sentence-transformers choose.
                Defaults to ``settings.RERANKER_DEVICE``.
            batch_size: ``(query, passage)`` pairs scored per forward pass.
                Defaults to ``settings.RERANKER_BATCH_SIZE``.

        Raises:
            torch.OutOfMemoryError: If the model fits on neither the requested
                CUDA device nor, after the fallback, the CPU.
            RuntimeError: If a non-CUDA failure occurs while loading or probing
                the model, or if a CUDA failure repeats on the CPU fallback.
            OSError: If offline resolution is in force and the weights are in
                neither the cache nor the configured directory.
        """
        self.model_name = model_name or settings.RERANKER_MODEL_ID
        self.cache_dir = settings.RERANKER_CACHE_DIR
        # A directory path is offline by construction — sentence-transformers
        # loads straight from disk — but local_files_only is still set for it so
        # nothing downstream (tokenizer/config resolution) can fall through to a
        # Hub call for a file the directory happens to be missing.
        self.local_files_only = settings.RERANKER_LOCAL_FILES_ONLY or os.path.isdir(
            self.model_name
        )
        self.max_length = max_length or settings.RERANKER_MAX_LENGTH
        self.batch_size = batch_size or settings.RERANKER_BATCH_SIZE
        requested_device = device or settings.RERANKER_DEVICE
        self.device = None if requested_device.strip().lower() == "auto" else requested_device

        # On CPU torch pins to physical cores by default; use every logical CPU
        # so the heavy cross-encoder forward pass is not thread-starved. Only the
        # CrossEncoder is affected here (Qwen3 embeddings run via Ollama).
        torch.set_num_threads(settings.RERANKER_TORCH_THREADS)

        # Held separately because the CUDA fallback below rewrites self.device;
        # without it the "ready" log would claim CPU was what the caller asked
        # for and hide the fact that a GPU attempt was made and failed.
        requested_device_label = self.device or "auto"

        # Provisioner Pattern — the model is a heavy, reusable dependency built
        # at construction, never inside a ranking call.
        logger.debug(
            "Initializing BgeReranker with model '%s' (cache '%s', max_length %d, "
            "batch_size %d, torch_threads %d, device %s, local_files_only %s)",
            self.model_name,
            self.cache_dir,
            self.max_length,
            self.batch_size,
            settings.RERANKER_TORCH_THREADS,
            requested_device_label,
            self.local_files_only,
        )
        try:
            self.model = self._load_model(self.device)
            self._probe_model()
        except (torch.OutOfMemoryError, RuntimeError) as cuda_error:
            # Two distinct CUDA failures land here, both of which the CPU can
            # absorb:
            #   * OutOfMemoryError — the GPU may be shared (Ollama serves the
            #     Qwen3 embeddings on some deployments), so a CUDA load fails
            #     even though the reranker runs perfectly well on CPU, which is
            #     this backend's documented default sizing anyway.
            #   * RuntimeError "no kernel image is available for execution" —
            #     torch reports the device as available but the wheel carries no
            #     kernels for its compute capability (the +cu130 build on the
            #     sm_61 workstation card did exactly this). Only _probe_model
            #     surfaces it; loading alone can succeed.
            if not self._is_cuda_failure(cuda_error):
                raise
            if self.device == "cpu":
                raise
            logger.warning(
                "BgeReranker: CUDA unusable for '%s' (%s); falling back to CPU. "
                "Set RERANKER_DEVICE=cpu to skip the GPU attempt.",
                self.model_name,
                cuda_error,
            )
            # No-op guard: on a CPU-only host there is no CUDA context to drop,
            # and empty_cache() would raise before the fallback could happen.
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            self.device = "cpu"
            self.model = self._load_model(self.device)
            self._probe_model()

        # With device="auto" the requested device stays None, so this is the only
        # way an operator can tell from the logs whether the cross-encoder ended
        # up on the GPU or the CPU.
        self.resolved_device = str(
            getattr(self.model, "device", None) or self.device or "cpu"
        )
        logger.info(
            "BgeReranker ready: model '%s' on device '%s' (requested %s, "
            "batch_size %d)",
            self.model_name,
            self.resolved_device,
            requested_device_label,
            self.batch_size,
        )

    @staticmethod
    def _is_cuda_failure(error: BaseException) -> bool:
        """Whether *error* is a CUDA failure that a CPU retry could survive.

        ``torch.OutOfMemoryError`` is always treated as one. A bare
        ``RuntimeError`` is only treated as one when its message names CUDA, so
        unrelated model-loading bugs keep propagating untouched.

        Args:
            error: The exception raised while loading or probing the model.

        Returns:
            ``True`` if the caller should retry the load on the CPU.
        """
        if isinstance(error, torch.OutOfMemoryError):
            return True
        return "cuda" in str(error).lower()

    def _probe_model(self) -> None:
        """Score one throwaway pair to force the device to prove it works.

        Loading a model onto CUDA allocates memory but does not necessarily run
        a kernel, so a capability mismatch stays hidden until the first real
        forward pass — which would be a live request. This pulls that failure
        into ``__init__``, where the CPU fallback can still catch it. On CPU the
        cost is a single short pair; on GPU it is the CUDA context
        initialisation the first request would have paid anyway.

        Raises:
            RuntimeError: Propagated from the forward pass; the caller decides
                whether it is a CUDA failure worth retrying on the CPU.
        """
        # show_progress_bar=False: a one-pair tqdm bar on every startup is pure
        # log noise, and this runs before the "ready" line an operator reads.
        self.model.predict(
            [("device probe", "device probe")], show_progress_bar=False
        )

    def _load_model(self, device: Optional[str]) -> CrossEncoder:
        """Instantiate the CrossEncoder on *device* (``None`` lets it choose).

        Args:
            device: torch device string, or ``None`` for automatic selection.

        Returns:
            The loaded ``CrossEncoder``.

        Raises:
            OSError: If offline resolution was requested and the weights are in
                neither the cache nor the configured directory. Re-raised with
                the command that populates them.
        """
        # cache_folder is the documented CrossEncoder parameter for the download
        # location. sentence-transformers 5.x logs a benign "cache_dir is
        # deprecated" notice because it forwards this internally, but routing
        # cache_dir via *_kwargs instead collides with its own AutoConfig call
        # (duplicate cache_dir), so the supported parameter is kept as-is.
        try:
            return CrossEncoder(
                self.model_name,
                max_length=self.max_length,
                cache_folder=self.cache_dir,
                # Resolve from disk only: no Hub metadata call, hence no 404 on
                # the absent modules.json, no anonymous-request warning and no
                # stall when huggingface.co is unreachable. See
                # settings.RERANKER_LOCAL_FILES_ONLY.
                local_files_only=self.local_files_only,
                device=device,
                # Explicit sigmoid: map raw logits to a (0, 1) relevance
                # probability, independent of the model's config default, so base
                # scores stay in FlashRank's regime for the Legal Climax boost.
                activation_fn=torch.nn.Sigmoid(),
            )
        except OSError as resolution_error:
            # Offline resolution against an unpopulated cache is the one failure
            # this mode introduces, and huggingface_hub's own message does not
            # name the setting that caused it or the way out.
            if not self.local_files_only:
                raise
            raise OSError(
                f"BgeReranker: '{self.model_name}' could not be resolved offline "
                f"from cache '{self.cache_dir}'. Populate it once with "
                f"`hf download {self.model_name} --local-dir <dir>` (then point "
                "RERANKER_MODEL_ID at <dir>), or unset "
                "RERANKER_LOCAL_FILES_ONLY to allow the download. "
                f"Original error: {resolution_error}"
            ) from resolution_error

    def _predict(self, pairs: List[Tuple[str, str]]):
        """Score ``(query, passage)`` *pairs*, surviving a CUDA failure.

        The single scoring entry point for both ranking methods, so the CUDA
        guard exists in exactly one place. ``__init__``'s fallback only covers
        loading and the warm-up probe; on a GPU shared with another process the
        model loads fine and runs out of memory here instead, which is the
        failure this guard exists for. Recovery reloads the model on the CPU and
        re-scores the same pairs, so no candidate is dropped.

        Args:
            pairs: ``(query, passage)`` tuples to score, in caller order.

        Returns:
            A numpy array of scores aligned positionally with *pairs*.

        Raises:
            torch.OutOfMemoryError: If the CPU itself runs out of memory, or the
                instance had already been downgraded to CPU.
            RuntimeError: If the failure is unrelated to CUDA, or repeats after
                the downgrade.
        """
        try:
            return self.model.predict(
                pairs, batch_size=self.batch_size, convert_to_numpy=True
            )
        except (torch.OutOfMemoryError, RuntimeError) as cuda_error:
            # Mirrors __init__'s classification: only a CUDA fault is
            # recoverable, and only while the model is still on the GPU. Note
            # this keys off resolved_device, not device — under
            # RERANKER_DEVICE=auto the latter stays None even on a CPU model.
            if not self._is_cuda_failure(cuda_error):
                raise
            if self.resolved_device.startswith("cpu"):
                raise
            logger.warning(
                "BgeReranker: CUDA failed during ranking for '%s' (%s); "
                "downgrading this instance to CPU for the rest of the process "
                "and re-scoring %d pair(s). The GPU is most likely shared with "
                "another process (e.g. the backend container). Set "
                "RERANKER_DEVICE=cpu to skip the GPU entirely, or lower "
                "RERANKER_BATCH_SIZE (currently %d) to shrink the peak "
                "allocation.",
                self.model_name,
                cuda_error,
                len(pairs),
                self.batch_size,
            )
            self._downgrade_to_cpu()
            return self.model.predict(
                pairs, batch_size=self.batch_size, convert_to_numpy=True
            )

    def _downgrade_to_cpu(self) -> None:
        """Reload the model on the CPU, permanently for this instance.

        Drops the GPU-resident model before reloading so its VRAM is released
        rather than held alongside the CPU copy, then rewrites ``device`` and
        ``resolved_device`` so :meth:`_predict` never attempts CUDA again.
        """
        # Release the GPU copy first: reloading with the old model still
        # referenced would need both resident at once, which is exactly the
        # memory we do not have.
        self.model = None
        # No-op guard for CPU-only hosts, matching __init__: there is no CUDA
        # context to drop and empty_cache() would raise.
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self.device = "cpu"
        self.model = self._load_model("cpu")
        self.resolved_device = "cpu"

    def rank_chunkvo(
        self,
        query: str,
        chunks: List[ChunkVo],
        top_k: Optional[int] = None,
        apply_position_boost: bool = True,
    ) -> List[RankedChunkVo]:
        """Rerank ``chunks`` against ``query`` using the CrossEncoder.

        Mirrors :meth:`FlashRanker.rank_chunkvo` exactly, including the
        structural boost and its opt-out.

        Args:
            query: Free-text query to rerank against.
            chunks: Candidate chunks to score.
            top_k: If given, return only the top *k* results.
            apply_position_boost: When ``True`` (default) the structural boost
                selected by ``settings.RANKER_BOOST_MODE`` (section-type
                weighting by default; the Legal Climax depth boost for A/B) is
                added to the model score. Pass ``False`` for enumeration /
                recall tasks where structural priors must not penalise rare
                mentions.

        Returns:
            ``RankedChunkVo`` list sorted descending by score.
        """
        if not chunks:
            return []

        # 1. Score every (query, chunk) pair in one batch. predict preserves the
        #    input order, so scores align positionally with ``chunks``.
        pairs = [(query, chunk.chunk) for chunk in chunks]
        scores = self._predict(pairs)

        # 2. Depth denominator relative to the retrieved slice of the document.
        local_max = max((c.doc_pos for c in chunks), default=1)

        ranked_chunks: List[RankedChunkVo] = []
        for chunk, raw_score in zip(chunks, scores):
            base_score = float(raw_score)

            if apply_position_boost:
                # Structural prior per settings.RANKER_BOOST_MODE; see
                # position_boost.py.
                adjusted_score = base_score + structural_boost(chunk, local_max)
            else:
                # Enumeration / recall mode: raw model score only.
                adjusted_score = base_score

            ranked_chunks.append(
                RankedChunkVo(
                    id=chunk.id,
                    celex_id=chunk.celex_id,
                    chunk=chunk.chunk,
                    origin=chunk.origin,
                    similarity=chunk.similarity,
                    score=adjusted_score,
                    base_score=base_score,
                    doc_pos=chunk.doc_pos,
                    section_type=chunk.section_type,
                )
            )

        ranked_chunks.sort(key=lambda x: x.score, reverse=True)
        return ranked_chunks[:top_k] if top_k else ranked_chunks

    def rank_nacevo(
        self,
        query: str,
        nodes: List[NaceVo],
        top_k: Optional[int] = None,
    ) -> List[RankedNaceVo]:
        """Rerank ``NaceVo`` objects against *query* using the CrossEncoder.

        The node ``title`` is the passage text. Unlike :meth:`rank_chunkvo`
        there is no structural position boost — NACE entries have no
        document-depth semantics, so the model score is used directly.

        Args:
            query: Free-text product or activity description to rank against.
            nodes: ``NaceVo`` objects of any type to rerank.
            top_k: If given, return only the top *k* results.

        Returns:
            ``RankedNaceVo`` list sorted descending by score.
        """
        if not nodes:
            return []

        pairs = [(query, vo.title) for vo in nodes]
        scores = self._predict(pairs)

        ranked = [
            RankedNaceVo(vo=vo, score=float(raw_score), base_score=float(raw_score))
            for vo, raw_score in zip(nodes, scores)
        ]

        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked[:top_k] if top_k else ranked
