"""
Per-run cache of each decision's source document for the lineage writers.

Chunk ingestion and fact extraction stamp ``document_id`` on the rows they
write. :class:`SourceDocumentResolver` looks the decision's ``is_source``
document up once per ``celex_id`` via
:meth:`DocumentRegistryRepository.source_document_id` and never raises: a
missing registry or a failed lookup degrades to ``None`` (NULL lineage).

Exports:
    SourceDocumentResolver -- per-run cache over ``source_document_id``.
"""

import logging
from collections.abc import Callable

from sqlalchemy.engine import Engine

from pipelines.rag.pgvector.document_registry_repository import (
    REGISTRY_MIGRATION,
    DocumentRegistryRepository,
)
from pipelines.rag.pgvector.session import warn_once

logger = logging.getLogger(__name__)


class SourceDocumentResolver:
    """Resolve a decision's source document once per run, never failing.

    Wraps an injected lookup (normally
    :meth:`DocumentRegistryRepository.source_document_id`) for the lineage
    writers. Each ``celex_id`` (== ``decision_id``) is looked up once and
    cached, ``None`` results included. A lookup error -- e.g. the registry
    migration is not applied -- logs one warning per process and resolves to
    ``None``, so ingestion and fact extraction write NULL instead of failing.

    Args:
        lookup: ``decision_id -> document_id | None``; ``None`` resolves
            every decision to ``None``.
    """

    def __init__(self, lookup: Callable[[str], str | None] | None) -> None:
        self._lookup = lookup
        self._cache: dict[str, str | None] = {}

    @classmethod
    def from_engine(cls, engine: Engine) -> "SourceDocumentResolver":
        """Build a resolver over the registry, degrading to NULL if it is absent.

        Args:
            engine: SQLAlchemy engine shared with the caller's repositories.

        Returns:
            A resolver backed by ``source_document_id``, or one resolving to
            ``None`` (after one warning) when the registry tables are missing.
        """
        try:
            repo = DocumentRegistryRepository(engine)
        except Exception as exc:
            warn_once(
                logger, "source_document_registry",
                "Document registry unavailable (%s); lineage document_id is "
                "written as NULL. Apply %s.", exc, REGISTRY_MIGRATION,
            )
            return cls(None)
        return cls(repo.source_document_id)

    def resolve(self, celex_id: str) -> str | None:
        """Return the source ``document_id`` for ``celex_id``, or None.

        Args:
            celex_id: Decision id (the project-wide ``celex_id``).

        Returns:
            The cached or freshly looked-up ``document_id``; None when the
            decision has no source document or the lookup failed.
        """
        if celex_id in self._cache:
            return self._cache[celex_id]
        document_id: str | None = None
        if self._lookup is not None:
            try:
                document_id = self._lookup(celex_id)
            except Exception as exc:
                warn_once(
                    logger, "source_document_lookup",
                    "Source-document lookup failed (%s); lineage document_id "
                    "is written as NULL for this run.", exc,
                )
        self._cache[celex_id] = document_id
        return document_id
