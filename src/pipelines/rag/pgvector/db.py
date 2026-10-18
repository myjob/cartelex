"""
Backward-compatibility shim for ``pipelines.rag.pgvector.db``.

The canonical implementation has moved to ``fact_repository.py``.  All public
names are re-exported here so that existing imports continue to work without
modification:

    from pipelines.rag.pgvector.db import FactTable       # → FactRepository
"""

# Re-export everything from fact_repository — single source of truth.
from pipelines.rag.pgvector.fact_repository import (  # noqa: F401
    FactRepository,
    FactTable,
)

__all__ = ["FactRepository", "FactTable"]
