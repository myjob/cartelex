import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from pipelines.rag.pgvector.celex_identity_repository import CelexIdentityRepository


# ---------------------------------------------------------------------------
# Unit tests for CelexIdentityRepository.select_distinct_book_cases
# ---------------------------------------------------------------------------


def _repo_with_rows(rows: list) -> CelexIdentityRepository:
    """Build a CelexIdentityRepository with a faked engine.connect."""

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, _stmt):
            return SimpleNamespace(fetchall=lambda: rows)

    repo = CelexIdentityRepository.__new__(CelexIdentityRepository)
    repo.engine = SimpleNamespace(connect=lambda: _FakeConn())
    repo.Session = MagicMock()
    return repo


class TestSelectDistinctBookCases(unittest.TestCase):
    """Unit tests for CelexIdentityRepository.select_distinct_book_cases."""

    def test_returns_list_of_dicts(self):
        """Each row is projected to a dict with the three expected keys."""
        rows = [("CELEX-31990D0645", "Polypropylene", "IV/31.149")]
        repo = _repo_with_rows(rows)
        result = repo.select_distinct_book_cases()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["celex_id"], "CELEX-31990D0645")
        self.assertEqual(result[0]["case_x"], "Polypropylene")
        self.assertEqual(result[0]["case_id"], "IV/31.149")

    def test_returns_empty_list_when_no_rows(self):
        repo = _repo_with_rows([])
        self.assertEqual(repo.select_distinct_book_cases(), [])

    def test_returns_multiple_rows(self):
        rows = [
            ("CELEX-31990D0645", "Polypropylene", "IV/31.149"),
            ("CELEX-31964D0344", "Dyestuffs", "IV/26.267"),
        ]
        repo = _repo_with_rows(rows)
        result = repo.select_distinct_book_cases()
        self.assertEqual(len(result), 2)
        celex_ids = {r["celex_id"] for r in result}
        self.assertIn("CELEX-31990D0645", celex_ids)
        self.assertIn("CELEX-31964D0344", celex_ids)

    def test_nullable_columns_allowed(self):
        """case_x and case_id may be NULL in the table."""
        rows = [("CELEX-31990D0645", None, None)]
        repo = _repo_with_rows(rows)
        result = repo.select_distinct_book_cases()
        self.assertIsNone(result[0]["case_x"])
        self.assertIsNone(result[0]["case_id"])

    def test_dict_has_exactly_three_keys(self):
        rows = [("CELEX-31990D0645", "Polypropylene", "IV/31.149")]
        repo = _repo_with_rows(rows)
        result = repo.select_distinct_book_cases()
        self.assertEqual(set(result[0].keys()), {"celex_id", "case_x", "case_id"})


if __name__ == '__main__':
    unittest.main()
