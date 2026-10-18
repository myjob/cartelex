"""Read-through embedding-cache behaviour of ``CelexDatabaseToolkit``.

The toolkit no longer owns the caching SQL — it delegates to
``QueryEmbeddingRepository.get_or_cache``. These tests pin that delegation and
the miss/hit semantics, mocking only the system boundaries (embedding provider,
vector store, repositories).
"""

import unittest
from unittest.mock import MagicMock, patch


class CelexToolkitEmbeddingCacheTest(unittest.TestCase):
    """Verify the toolkit embeds on a cache miss and not on a cache hit."""

    def setUp(self) -> None:
        """Patch every boundary the toolkit constructor touches."""
        patchers = [
            patch("tools.celex_tools.Qwen3OllamaEmbeddings"),
            patch("tools.celex_tools.CelexPGVectorPgQwen"),
            patch("tools.celex_tools.FactRepository"),
            patch("tools.celex_tools.ChunkRepository"),
            patch("tools.celex_tools.QueryEmbeddingRepository"),
        ]
        self.mock_embeddings_cls = patchers[0].start()
        self.mock_vector_store_cls = patchers[1].start()
        self.mock_fact_repo_cls = patchers[2].start()
        self.mock_chunk_repo_cls = patchers[3].start()
        self.mock_query_repo_cls = patchers[4].start()
        for patcher in patchers:
            self.addCleanup(patcher.stop)

        from tools.celex_tools import CelexDatabaseToolkit

        self.toolkit = CelexDatabaseToolkit()
        self.query_repo = self.mock_query_repo_cls.return_value
        self.embeddings = self.mock_embeddings_cls.return_value

    def test_cache_miss_computes_and_stores_embedding(self) -> None:
        """On a miss the repository computes via ``embed_query`` and caches it."""
        self.query_repo.get_cached.return_value = None
        self.embeddings.embed_query.return_value = [0.1, 0.2, 0.3]
        # Exercise the real read-through logic against the mocked DAO methods.
        self.query_repo.get_or_cache.side_effect = _real_get_or_cache(self.query_repo)

        result = self.toolkit._get_or_cache_embedding("test query")

        self.assertEqual([0.1, 0.2, 0.3], result)
        self.embeddings.embed_query.assert_called_once_with("test query")
        self.query_repo.cache.assert_called_once_with("test query", [0.1, 0.2, 0.3])

    def test_cache_hit_skips_the_embedding_provider(self) -> None:
        """On a hit neither the embedding provider nor the cache write is used."""
        self.query_repo.get_cached.return_value = [0.4, 0.5, 0.6]
        self.query_repo.get_or_cache.side_effect = _real_get_or_cache(self.query_repo)

        result = self.toolkit._get_or_cache_embedding("test query")

        self.assertEqual([0.4, 0.5, 0.6], result)
        self.embeddings.embed_query.assert_not_called()
        self.query_repo.cache.assert_not_called()

    def test_toolkit_delegates_to_the_repository(self) -> None:
        """The toolkit must not reimplement caching; it calls ``get_or_cache``."""
        self.query_repo.get_or_cache.return_value = [0.7]

        self.toolkit._get_or_cache_embedding("test query")

        self.query_repo.get_or_cache.assert_called_once()
        called_query, called_embed_fn = self.query_repo.get_or_cache.call_args[0]
        self.assertEqual("test query", called_query)
        self.assertIs(self.embeddings.embed_query, called_embed_fn)


def _real_get_or_cache(query_repo: MagicMock):
    """Return the real read-through algorithm bound to a mocked repository.

    Args:
        query_repo: Mock standing in for ``QueryEmbeddingRepository``.

    Returns:
        A ``(query, embed_fn) -> list`` callable mirroring
        ``QueryEmbeddingRepository.get_or_cache``.
    """

    def _get_or_cache(query: str, embed_fn) -> list:
        cached = query_repo.get_cached(query)
        if cached is not None:
            return cached
        embedding = embed_fn(query)
        query_repo.cache(query, embedding)
        return embedding

    return _get_or_cache


if __name__ == "__main__":
    unittest.main()
