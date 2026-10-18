"""
Integration tests for FactRepository.

Run with:
    PYTHONPATH=src .venv/bin/python -m pytest \
        src/pipelines/rag/pgvector/tests/test_fact_repository.py -v
"""
import json
import uuid
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock

from sqlalchemy import text

from pipelines.agent_dspy.dspy_protocols import FirstPageVO
from pipelines.rag.pgvector.fact_repository import FactRepository, FactTable
from pipelines.rag.pgvector.session import make_engine


class TestFactRepositoryProtocol(unittest.TestCase):
    """Verify FactRepository satisfies the SQLFactStore protocol surface."""

    def test_alias_fact_table_is_fact_repository(self):
        """FactTable is a simple alias — the class must be identical."""
        self.assertIs(FactTable, FactRepository)

    def test_has_required_protocol_methods(self):
        repo = FactRepository.__new__(FactRepository)
        for method in (
            "select_fact4celex",
            "select_all_facts_for_celex",
            "select_legacy_fact4celex",
            "select_case_ids_for_celex",
            "select_facts4celex",
            "select_available_fact_names",
            "exists",
        ):
            self.assertTrue(
                callable(getattr(repo, method, None)),
                f"Missing method: {method}",
            )


class TestFactRepositoryIntegration(unittest.TestCase):
    """Integration tests against the live celex_facts database."""

    CELEX_ID = "39530_2671_5"

    @classmethod
    def setUpClass(cls):
        cls.repo = FactRepository()
        # Use a dedicated engine for test setup/teardown raw SQL
        cls.engine = make_engine()

    # ------------------------------------------------------------------
    # select_facts4celex
    # ------------------------------------------------------------------
    def test_select_facts4celex_returns_dict(self):
        result = self.repo.select_facts4celex(self.CELEX_ID)
        self.assertIsInstance(result, dict)

    def test_select_facts4celex_law_article(self):
        """Mirrors the existing CelexDatabaseToolkit integration test."""
        result = self.repo.select_facts4celex(self.CELEX_ID)
        self.assertIn("law_article_all_relevant", result)
        expected = (
            "| law_article   |\n|:--------------|\n| Article 102   |\n| Article 82    |"
        )
        self.assertIn(expected, result["law_article_all_relevant"])

    def test_select_facts4celex_unknown_returns_empty_dict(self):
        result = self.repo.select_facts4celex("NON_EXISTENT_CELEX_99999")
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 0)

    # ------------------------------------------------------------------
    # select_available_fact_names
    # ------------------------------------------------------------------
    def test_select_available_fact_names_returns_sorted_list(self):
        names = self.repo.select_available_fact_names(self.CELEX_ID)
        self.assertIsInstance(names, list)
        self.assertGreater(len(names), 0)
        self.assertEqual(names, sorted(names))

    # ------------------------------------------------------------------
    # select_fact4celex (point read)
    # ------------------------------------------------------------------
    def test_select_fact4celex_known(self):
        rows = self.repo.select_fact4celex(self.CELEX_ID, "law_article_all_relevant")
        self.assertGreater(len(rows), 0)
        self.assertIsNotNone(rows[-1].value_md)

    def test_select_fact4celex_unknown_returns_empty_list(self):
        rows = self.repo.select_fact4celex("NON_EXISTENT", "NON_EXISTENT")
        self.assertEqual(rows, [])

    # ------------------------------------------------------------------
    # exists
    # ------------------------------------------------------------------
    def test_exists_true_for_known_fact(self):
        self.assertTrue(
            self.repo.exists(self.CELEX_ID, "law_article_all_relevant")
        )

    def test_exists_false_for_unknown(self):
        self.assertFalse(self.repo.exists("NON_EXISTENT", "NON_EXISTENT"))

    # ------------------------------------------------------------------
    # select_named_facts4celex
    # ------------------------------------------------------------------
    def test_select_named_facts4celex_returns_only_named_facts_in_order(self):
        """Unnamed facts (e.g. a retired name) are excluded; order follows ``names``."""
        unique_celex = f"TEST_NAMED_FACTS_{uuid.uuid4()}"
        wanted = ["law_article_all_relevant", "fines"]
        retired_fact = "company_country"

        with self.engine.begin() as conn:
            for name, value in [
                ("fines", "| company | amount |"),
                ("law_article_all_relevant", "| law_article |\n|:-----------|\n| Article 101 |"),
                (retired_fact, "DE"),
            ]:
                conn.execute(
                    text(
                        "INSERT INTO celex_facts.facts"
                        " (id, celex_id, name, value_md, value_json, created_at)"
                        " VALUES (:id, :celex_id, :name, :value_md, NULL, NOW())"
                    ),
                    {"id": str(uuid.uuid4()), "celex_id": unique_celex, "name": name, "value_md": value},
                )

        try:
            result = self.repo.select_named_facts4celex(
                unique_celex, wanted + ["not_stored_fact"]
            )
            self.assertEqual([row.name for row in result], wanted)
            self.assertTrue(all(row.created_at for row in result))
            self.assertEqual(self.repo.select_named_facts4celex(unique_celex, []), [])
        finally:
            with self.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM celex_facts.facts WHERE celex_id = :celex_id"),
                    {"celex_id": unique_celex},
                )

    # ------------------------------------------------------------------
    # Round-trip: insert → select → delete
    # ------------------------------------------------------------------
    def test_round_trip_via_select(self):
        """Insert a test fact via raw SQL, read via ORM, then clean up."""
        unique_celex = f"TEST_REPO_{uuid.uuid4()}"
        fact_name = "test_repo_fact"
        value_md = "**Refactored FactRepository works!**"
        value_json = json.dumps({"status": "ok"})

        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO celex_facts.facts"
                    " (id, celex_id, name, value_md, value_json, created_at)"
                    " VALUES (:id, :celex_id, :name, :value_md, :value_json, NOW())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "celex_id": unique_celex,
                    "name": fact_name,
                    "value_md": value_md,
                    "value_json": value_json,
                },
            )

        try:
            # ORM reads
            rows = self.repo.select_fact4celex(unique_celex, fact_name)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].value_md, value_md)

            bulk = self.repo.select_facts4celex(unique_celex)
            self.assertIn(fact_name, bulk)
            self.assertEqual(bulk[fact_name], value_md)

            names = self.repo.select_available_fact_names(unique_celex)
            self.assertIn(fact_name, names)

            self.assertTrue(self.repo.exists(unique_celex, fact_name))

        finally:
            with self.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM celex_facts.facts WHERE celex_id = :celex_id"),
                    {"celex_id": unique_celex},
                )


class TestSelectAllFactsForCelex(unittest.TestCase):
    """Unit tests for FactRepository.select_all_facts_for_celex (DB session mocked)."""

    def _make_fact(self, fact_id, name, value_md="v"):
        """Return a minimal Facts-like object."""
        import types
        f = types.SimpleNamespace(id=fact_id, name=name, value_md=value_md)
        return f

    def _repo_with_query_returning(self, facts):
        """Build a FactRepository whose Session.query returns *facts*."""
        repo = FactRepository.__new__(FactRepository)

        query_mock = MagicMock()
        query_mock.filter.return_value = query_mock
        query_mock.order_by.return_value = query_mock
        query_mock.all.return_value = facts

        session_mock = MagicMock()
        session_mock.query.return_value = query_mock

        @contextmanager
        def _session_cm():
            yield session_mock

        repo.Session = _session_cm
        return repo, session_mock

    def test_returns_all_fact_objects(self):
        facts = [
            self._make_fact(uuid.uuid4(), "fines"),
            self._make_fact(uuid.uuid4(), "parties"),
        ]
        repo, _ = self._repo_with_query_returning(facts)
        result = repo.select_all_facts_for_celex("CELEX-31990D0645")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, "fines")

    def test_filters_by_celex_id(self):
        repo, session_mock = self._repo_with_query_returning([])
        repo.select_all_facts_for_celex("CELEX-31990D0645")
        # query(Facts) was called
        session_mock.query.assert_called_once()

    def test_returns_empty_list_when_no_facts(self):
        repo, _ = self._repo_with_query_returning([])
        result = repo.select_all_facts_for_celex("NONEXISTENT")
        self.assertEqual(result, [])

    def test_orders_by_name(self):
        repo, session_mock = self._repo_with_query_returning([])
        repo.select_all_facts_for_celex("CELEX-31990D0645")
        query_chain = session_mock.query.return_value
        query_chain.order_by.assert_called_once()


class TestSelectFirstPage4Celex(unittest.TestCase):
    """Unit tests for the lineage-aware first-page retrieval (DB session mocked)."""

    def _repo_with_session_returning(self, row):
        """Build a FactRepository whose Session yields a query result of *row*."""
        repo = FactRepository.__new__(FactRepository)
        session = MagicMock()
        session.execute.return_value.fetchone.return_value = row

        @contextmanager
        def _session_cm():
            yield session

        repo.Session = _session_cm
        return repo

    def test_returns_first_page_vo_with_chunk_identity(self):
        chunk_id = uuid.uuid4()
        repo = self._repo_with_session_returning((chunk_id, 0, "First page text"))

        result = repo.select_first_page4celex("CELEX-TEST-001")

        self.assertIsInstance(result, FirstPageVO)
        self.assertEqual(result.chunk_id, str(chunk_id))
        self.assertEqual(result.doc_pos, 0)
        self.assertEqual(result.document, "First page text")

    def test_returns_none_when_no_row(self):
        repo = self._repo_with_session_returning(None)
        self.assertIsNone(repo.select_first_page4celex("CELEX-TEST-001"))


if __name__ == "__main__":
    unittest.main()
