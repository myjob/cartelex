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

from sqlalchemy import select, text

from pipelines.agent_dspy.dspy_protocols import FirstPageVO
from pipelines.rag.pgvector import session as session_module
from pipelines.rag.pgvector.fact_repository import FactRepository, FactTable
from pipelines.rag.pgvector.models import Facts
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


# ---------------------------------------------------------------------------
# Lineage: upsert_fact(document_id=...) against fakes (no live DB)
# ---------------------------------------------------------------------------


class _FakeQuery:
    """Chainable ``session.query(Facts)`` stand-in returning canned rows."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def filter(self, *args: object) -> "_FakeQuery":
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
        return self

    def with_for_update(self) -> "_FakeQuery":
        return self

    def all(self) -> list:
        return self._rows


class _FakeSession:
    """ORM session stand-in recording adds, text statements and commits."""

    def __init__(self, rows: list, events: list[str] | None = None) -> None:
        self.rows = rows
        self.added: list = []
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0
        self.events = events if events is not None else []

    def __enter__(self) -> "_FakeSession":
        self.events.append("session")
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def query(self, *args: object) -> _FakeQuery:
        return _FakeQuery(self.rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def delete(self, obj: object) -> None:
        pass

    def flush(self) -> None:
        pass

    def execute(self, statement: object, params: dict | None = None) -> None:
        self.statements.append((str(statement), params or {}))

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


class _ProbeConn:
    """Connection answering the column probe with a canned row (or none)."""

    def __init__(self, engine: "_ProbeEngine") -> None:
        self._engine = engine

    def __enter__(self) -> "_ProbeConn":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, statement: object, params: dict | None = None) -> MagicMock:
        self._engine.probes.append((str(statement), params or {}))
        self._engine.events.append("probe")
        if self._engine.probe_error is not None:
            raise self._engine.probe_error
        result = MagicMock()
        result.first.return_value = (1,) if self._engine.column_present else None
        return result


class _ProbeEngine:
    """Engine stand-in for the one-off ``document_id`` column probe."""

    def __init__(self, column_present: bool, probe_error: Exception | None = None) -> None:
        self.column_present = column_present
        self.probe_error = probe_error
        self.probes: list[tuple[str, dict]] = []
        self.events: list[str] = []

    def connect(self) -> _ProbeConn:
        return _ProbeConn(self)


def _repo(
    rows: list, column_present: bool, probe_error: Exception | None = None
) -> tuple[FactRepository, _FakeSession, _ProbeEngine]:
    """Build a FactRepository wired to fakes; returns (repo, session, engine).

    The engine and the session share one ``events`` list, so tests can check
    that the column probe runs before the row-locking session opens.
    """
    engine = _ProbeEngine(column_present, probe_error)
    fake_session = _FakeSession(rows, engine.events)
    repo = FactRepository.__new__(FactRepository)
    repo.engine = engine
    repo.Session = lambda: fake_session
    repo._document_id_column = None
    return repo, fake_session, engine


def _lineage_writes(fake_session: _FakeSession) -> list[tuple[str, dict]]:
    """Statements that touch ``facts.document_id``."""
    return [(sql, p) for sql, p in fake_session.statements if "document_id" in sql]


class TestUpsertFactDocumentLineage(unittest.TestCase):
    """``upsert_fact`` records the source document only when the column exists."""

    def setUp(self) -> None:
        session_module._WARNED_ONCE.clear()

    def test_upsert_fact_insert_sets_document_id(self) -> None:
        repo, fake_session, _ = _repo(rows=[], column_present=True)
        fact_id = repo.upsert_fact("C1", "fines", "md", "[]", document_id="C1_EN")
        (sql, params), = _lineage_writes(fake_session)
        self.assertIn("UPDATE celex_facts.facts SET document_id", sql)
        self.assertEqual(params, {"document_id": "C1_EN", "fact_id": fact_id})
        self.assertEqual(fake_session.commits, 1)

    def test_upsert_fact_update_sets_document_id(self) -> None:
        survivor = MagicMock(id=uuid.uuid4())
        repo, fake_session, _ = _repo(rows=[survivor], column_present=True)
        repo.upsert_fact("C1", "fines", "md", "[]", document_id="C1_EN")
        (_, params), = _lineage_writes(fake_session)
        self.assertEqual(params, {"document_id": "C1_EN", "fact_id": survivor.id})

    def test_upsert_fact_update_keeps_document_id_when_none(self) -> None:
        survivor = MagicMock(id=uuid.uuid4())
        repo, fake_session, engine = _repo(rows=[survivor], column_present=True)
        repo.upsert_fact("C1", "fines", "md2", "[]", document_id=None)
        self.assertEqual(_lineage_writes(fake_session), [])
        self.assertEqual(engine.probes, [], "no probe without a document id")
        self.assertEqual(survivor.value_md, "md2")
        self.assertEqual(fake_session.commits, 1)

    def test_works_without_document_id_column(self) -> None:
        repo, fake_session, engine = _repo(rows=[], column_present=False)
        with self.assertLogs(
            "pipelines.rag.pgvector.fact_repository", "WARNING"
        ) as logs:
            repo.upsert_fact("C1", "fines", "md", "[]", document_id="C1_EN")
            repo.upsert_fact("C2", "fines", "md", "[]", document_id="C2_EN")
        self.assertEqual(len(logs.records), 1, "one warning per process")
        self.assertIn("migrate_document_lineage.sql", logs.output[0])
        self.assertEqual(len(engine.probes), 1, "probed once, then cached")
        self.assertEqual(_lineage_writes(fake_session), [])
        self.assertEqual(fake_session.commits, 2, "facts are still written")

    def test_probe_runs_before_the_row_locking_session(self) -> None:
        """The probe needs its own pooled connection; never while row locks are held."""
        survivor = MagicMock(id=uuid.uuid4())
        repo, _, engine = _repo(rows=[survivor], column_present=True)
        repo.upsert_fact("C1", "fines", "md", "[]", document_id="C1_EN")
        self.assertEqual(engine.events, ["probe", "session"])

    def test_probe_error_still_commits_and_warns_with_the_exception(self) -> None:
        repo, fake_session, engine = _repo(
            rows=[], column_present=True, probe_error=RuntimeError("pool timeout")
        )
        with self.assertLogs(
            "pipelines.rag.pgvector.fact_repository", "WARNING"
        ) as logs:
            repo.upsert_fact("C1", "fines", "md", "[]", document_id="C1_EN")
            repo.upsert_fact("C2", "fines", "md", "[]", document_id="C2_EN")
        self.assertEqual(len(logs.records), 1)
        self.assertIn("lineage probe failed, writing NULL", logs.output[0])
        self.assertIn("pool timeout", logs.output[0])
        self.assertNotIn("is missing", logs.output[0])
        self.assertEqual(len(engine.probes), 1, "failure cached for the process")
        self.assertEqual(_lineage_writes(fake_session), [])
        self.assertEqual(fake_session.commits, 2, "facts are still written")

    def test_orm_model_never_selects_or_inserts_document_id(self) -> None:
        """Pre-migration ORM reads and inserts of Facts must not name the column."""
        self.assertNotIn("document_id", str(select(Facts)))
        self.assertNotIn("document_id", Facts.__table__.columns)


if __name__ == "__main__":
    unittest.main()
