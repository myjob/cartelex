"""Integration tests for the legal_provisions ontology (live database).

Requires ``migrate_legal_provisions.sql`` to be applied. Uses the configured
``DATABASE_URL`` per project convention for integration tests.
"""

import unittest

from pipelines.legal.infringement_concept import DEFAULT_PROVISION_MAP
from pipelines.rag.pgvector.provision_repository import (
    ProvisionRepository,
    load_provision_map,
)


class TestProvisionRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = ProvisionRepository()

    def test_runtime_rows_exclude_eea(self) -> None:
        rows = self.repo.select_provision_rows()
        articles = {row.article_number for row in rows}
        self.assertEqual(len(rows), 9)
        self.assertNotIn(53, articles)
        self.assertNotIn(54, articles)

    def test_eea_rows_are_seeded_as_data(self) -> None:
        rows = self.repo.select_provision_rows(treaties=("eea",))
        self.assertEqual({row.article_number for row in rows}, {53, 54})

    def test_db_map_matches_static_default(self) -> None:
        """Drift guard: the seed and _STATIC_PROVISION_ROWS must stay equal."""
        self.assertEqual(load_provision_map(), DEFAULT_PROVISION_MAP)

    def test_load_provision_map_is_cached(self) -> None:
        self.assertIs(load_provision_map(), load_provision_map())


if __name__ == "__main__":
    unittest.main()
