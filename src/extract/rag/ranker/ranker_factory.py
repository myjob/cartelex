"""Factory resolving a reranker backend from a named profile.

Every retrieval call site injects "a ranker" (the :class:`RankerProtocol`
structural interface) and, when none is injected, falls back to
:func:`create_ranker`, which builds the backend a :class:`RankerProfile`
names. Two profiles are configured, mirroring the LLM profiles in
``config.llm_factory`` so that repointing one consumer leaves the other alone:

* :func:`batch_ranker_profile` -- ``settings.RERANKER_BACKEND`` /
  ``settings.RERANKER_MODEL_ID`` (``"bge"`` by default). The implicit profile
  of every ingestion CLI and every ``ranker or create_ranker()`` fallback.
* :func:`ui_ranker_profile` -- ``settings.UI_RERANKER_BACKEND`` /
  ``settings.UI_RERANKER_MODEL_ID`` (``"flashrank"`` by default). Passed
  explicitly by the oi_dspy backend server, and by nothing else.

Concrete backends are imported lazily inside :func:`_build_ranker` so that
importing this module (e.g. just to reference :class:`RankerProtocol` in a type
hint) never pulls in ``flashrank``/``torch``. That laziness is also what lets an
image ship without a backend at all: the oi_dspy chat image installs
``requirements-ui.txt``, which has no ``torch`` and no ``sentence-transformers``,
so :class:`~extract.rag.ranker.bge_reranker.BgeReranker` is unimportable there
and asking for it raises :class:`RuntimeError` naming the rebuild required.

Instances are cached per ``(backend, model id)`` -- not per profile name -- so
that a process wiring several components (pipeline + keyword search + hybrid
search + CLI) loads the ~2.2 GB cross-encoder once instead of once per call
site, which otherwise exhausts VRAM on a GPU shared with Ollama. The cache holds
*weak* references: the weights live exactly as long as some caller still holds
its ranker, and a process that drops every reference gets the memory back.
Tests that patch a model boundary must call :func:`reset_ranker_cache` so a
mocked instance cannot be handed to a later test.
"""

import logging
import threading
import weakref
from typing import Any, List, Optional, Protocol, Tuple, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

from config.settings import settings
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from pipelines.nace.nace_tree import NaceVo, RankedNaceVo

logger = logging.getLogger(__name__)

BACKEND_FLASHRANK = "flashrank"
BACKEND_BGE = "bge"
SUPPORTED_BACKENDS = frozenset({BACKEND_FLASHRANK, BACKEND_BGE})

# Profile names, used in log lines and in the error raised for a bad backend so
# a misconfiguration says which knob to fix.
PROFILE_UI = "ui"
PROFILE_BATCH = "batch"

# (backend, model id) -> live ranker. WeakValueDictionary so an entry disappears
# once the last caller releases the ranker.
_ranker_cache: "weakref.WeakValueDictionary[Tuple[str, str], RankerProtocol]" = (
    weakref.WeakValueDictionary()
)
# Guards the check-then-build sequence: two threads asking for the same backend
# concurrently must not each load the model.
_ranker_cache_lock = threading.Lock()


@runtime_checkable
class RankerProtocol(Protocol):
    """Structural interface every reranker backend implements.

    Both :class:`~extract.rag.ranker.flash_ranker.FlashRanker` and
    :class:`~extract.rag.ranker.bge_reranker.BgeReranker` satisfy this, so any
    injection site can accept either backend.
    """

    def rank_chunkvo(
        self,
        query: str,
        chunks: List[ChunkVo],
        top_k: Optional[int] = None,
        apply_position_boost: bool = True,
    ) -> List[RankedChunkVo]:
        """Rerank *chunks* against *query*, sorted descending by score."""
        ...

    def rank_nacevo(
        self,
        query: str,
        nodes: List[NaceVo],
        top_k: Optional[int] = None,
    ) -> List[RankedNaceVo]:
        """Rerank NACE *nodes* against *query*, sorted descending by score."""
        ...


class RankerProfile(BaseModel):
    """One consumer's reranker selection.

    Frozen because ``(backend, model_id)`` is the factory cache key: a profile
    that could change after construction could alias a cached instance.

    Args:
        name: Profile name, used in logs and errors (e.g. ``batch``).
        backend: ``flashrank`` or ``bge``.
        model_id: CrossEncoder model id for ``bge``. Always blank for
            ``flashrank``, which pins its own bundled model.

    Raises:
        ValueError: If the backend is not supported, or a ``bge`` profile
            carries no model id.
    """

    model_config = {"frozen": True}

    name: str = Field(description="Profile name used in logs and errors.")
    backend: str = Field(description="Reranker backend: 'flashrank' or 'bge'.")
    model_id: str = Field(
        default="",
        description="CrossEncoder model id for 'bge'; always blank for 'flashrank'.",
    )

    @model_validator(mode="before")
    @classmethod
    def blank_flashrank_model_id(cls, data: Any) -> Any:
        """Drop the model id when the backend is FlashRank.

        FlashRank ignores the id, and keeping a stray value (say, the batch
        profile's ``RERANKER_MODEL_ID``) would key a second cache entry for
        the same bundled model. Runs before field validation because the model
        is frozen and an after-validator could not reassign the field.

        Args:
            data: The raw constructor input.

        Returns:
            The input, with ``model_id`` blanked for a FlashRank backend.
        """
        if isinstance(data, dict):
            backend = str(data.get("backend", "")).strip().lower()
            if backend == BACKEND_FLASHRANK:
                return {**data, "model_id": ""}
        return data

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, value: str) -> str:
        """Return a supported backend name.

        Args:
            value: Configured backend.

        Returns:
            The trimmed, lowercased backend.

        Raises:
            ValueError: If the backend is not one the factory can build.
        """
        backend = value.strip().lower()
        if backend not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"Unsupported reranker backend {value!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_BACKENDS))}."
            )
        return backend

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        """Return the trimmed model id.

        Args:
            value: Configured model id.

        Returns:
            The trimmed model id (possibly empty; the after-validator decides
            whether empty is acceptable for this backend).
        """
        return value.strip()

    @model_validator(mode="after")
    def require_model_id_for_bge(self) -> "RankerProfile":
        """Reject a CrossEncoder profile that names no model.

        Returns:
            The validated profile.

        Raises:
            ValueError: If the backend is ``bge`` and the model id is blank. A
                blank setting is resolved to ``RERANKER_MODEL_ID`` before the
                profile is built, so an empty value here means the default is
                empty too.
        """
        if self.backend == BACKEND_BGE and not self.model_id:
            raise ValueError(
                f"Reranker profile {self.name!r}: backend 'bge' needs a model id."
            )
        return self


def _resolve_model_id(backend: str, configured_model_id: str) -> str:
    """Return the model id for a backend, falling back to its shared default.

    Args:
        backend: Already-normalised backend name.
        configured_model_id: The profile's configured id; blank means "use the
            backend's shared default".

    Returns:
        The model id to run with: blank for FlashRank, which pins its own
        model; the configured id or ``settings.RERANKER_MODEL_ID`` for bge.
    """
    if backend == BACKEND_FLASHRANK:
        return ""
    if configured_model_id.strip():
        return configured_model_id.strip()
    return settings.RERANKER_MODEL_ID


def _profile_from_settings(
    name: str, backend: str, configured_model_id: str
) -> RankerProfile:
    """Build a validated profile from its configured settings.

    Args:
        name: Profile name for logs and errors.
        backend: Configured backend.
        configured_model_id: Configured model id, possibly blank.

    Returns:
        The validated profile.

    Raises:
        ValueError: If the backend is unsupported or no model id resolves.
    """
    normalized_backend = backend.strip().lower()
    return RankerProfile(
        name=name,
        backend=normalized_backend,
        model_id=_resolve_model_id(normalized_backend, configured_model_id),
    )


def ui_ranker_profile() -> RankerProfile:
    """Return the profile backing the oi_dspy backend server.

    Returns:
        The profile from ``settings.UI_RERANKER_BACKEND`` /
        ``settings.UI_RERANKER_MODEL_ID``.

    Raises:
        ValueError: If the configured backend or model is unusable.
    """
    return _profile_from_settings(
        PROFILE_UI, settings.UI_RERANKER_BACKEND, settings.UI_RERANKER_MODEL_ID
    )


def batch_ranker_profile() -> RankerProfile:
    """Return the profile backing the ingestion CLIs and injection fallbacks.

    Returns:
        The profile from ``settings.RERANKER_BACKEND`` /
        ``settings.RERANKER_MODEL_ID``.

    Raises:
        ValueError: If the configured backend or model is unusable.
    """
    return _profile_from_settings(
        PROFILE_BATCH, settings.RERANKER_BACKEND, settings.RERANKER_MODEL_ID
    )


def create_ranker(
    profile: RankerProfile | None = None,
    *,
    use_cache: bool = True,
) -> RankerProtocol:
    """Return the shared reranker for *profile* (the batch profile by default).

    The first call for a given ``(backend, model id)`` loads the model; later
    calls return that same instance for as long as a caller keeps it alive, so
    the heavy weights are not duplicated across call sites or across profiles
    that happen to select the same backend and model.

    Args:
        profile: The profile to build a ranker for. ``None`` resolves
            :func:`batch_ranker_profile`, which is what every call site that
            does not name a profile gets.
        use_cache: When ``False``, always build a fresh instance and leave the
            cache untouched (for A/B runs that need two independent rankers of
            the same backend).

    Returns:
        A ranker satisfying :class:`RankerProtocol`.

    Raises:
        ValueError: If the resolved profile names an unsupported backend or
            no usable model id.
    """
    resolved_profile = profile or batch_ranker_profile()

    if not use_cache:
        return _build_ranker(resolved_profile)

    cache_key = (resolved_profile.backend, resolved_profile.model_id)
    with _ranker_cache_lock:
        cached_ranker = _ranker_cache.get(cache_key)
        if cached_ranker is not None:
            logger.debug(
                "create_ranker: profile %s reusing cached ranker %s",
                resolved_profile.name,
                cache_key,
            )
            return cached_ranker

        ranker = _build_ranker(resolved_profile)
        _ranker_cache[cache_key] = ranker
        return ranker


def _build_ranker(profile: RankerProfile) -> RankerProtocol:
    """Construct a backend instance, bypassing the cache.

    Args:
        profile: Validated profile naming the backend and model to load.

    Returns:
        A freshly loaded ranker satisfying :class:`RankerProtocol`.
    """
    logger.info(
        "Loading reranker for profile %s: backend=%s model=%s",
        profile.name,
        profile.backend,
        profile.model_id or "bundled",
    )
    if profile.backend == BACKEND_FLASHRANK:
        from extract.rag.ranker.flash_ranker import FlashRanker

        return FlashRanker()

    try:
        from extract.rag.ranker.bge_reranker import BgeReranker
    except ImportError as exc:
        # Not a bug to fix at the call site: the deployment simply has no bge
        # backend in it. The serving image installs requirements-ui.txt, whose
        # whole point is to omit torch and sentence-transformers (~1.8 GB), so
        # this is the expected outcome of asking a lean image for the
        # cross-encoder. Named explicitly because the bare ModuleNotFoundError
        # points at sentence_transformers and says nothing about which lock the
        # image was built from or how to change it.
        raise RuntimeError(
            f"Reranker profile '{profile.name}' asked for the '{BACKEND_BGE}' "
            "backend, but this environment has no sentence-transformers/torch "
            "installed, so BgeReranker cannot be imported. Either set the "
            "profile's backend to 'flashrank', or install a lock that carries "
            "the bge stack (requirements-cpu.txt, or requirements.txt for CUDA) "
            "-- in Docker that means rebuilding the image with "
            "REQUIREMENTS_FILE=requirements-cpu.txt, e.g. via "
            "docker-compose.cpu.yml."
        ) from exc

    return BgeReranker(model_name=profile.model_id)


def reset_ranker_cache() -> None:
    """Drop every cached ranker.

    Required in tests that patch a model boundary (``CrossEncoder``,
    FlashRank's ``Ranker``): without it a ranker built under the patch could be
    handed to a later test running without it.
    """
    with _ranker_cache_lock:
        _ranker_cache.clear()
