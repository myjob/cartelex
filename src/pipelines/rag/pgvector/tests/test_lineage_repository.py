"""
Unit + integration tests for LineageRepository.

Unit tests (no DB required) verify the public API shape and input validation.
Integration tests require a live ``celex_facts`` database.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \
        src/pipelines/rag/pgvector/tests/test_lineage_repository.py -v
"""
import logging
import uuid
import unittest
from collections.abc import Iterable
from uuid import UUID

from pydantic import ValidationError as PydValidationError
from sqlalchemy import Engine, text
from sqlalchemy.dialects import postgresql

from pipelines.rag.pgvector.lineage_repository import (
    FactProvenanceRow,
    LineageRepository,
    _DEFAULT_TRUST_RANK,
    _TRUST_RANK_DEFAULTS,
)
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL
from tools.PipelineFactIngestor import LineageSourceVO


_LOGGER = logging.getLogger(__name__)

# Registry rows are shared: de-duplicated sources (a csv_human row keyed on
# celex_id + csv_row, a chunk keyed on celex_id) are reused across runs and may
# still support facts this test never created.  Deleting such a row outright
# would either violate the fact_source_link foreign key or strip provenance from
# unrelated facts, so the delete is guarded on the source being orphaned.
_DELETE_FACT_SQL = "DELETE FROM celex_facts.facts WHERE id = :id"
_DELETE_ORPHANED_SOURCE_SQL = (
    "DELETE FROM celex_facts.fact_source_registry r"
    " WHERE r.source_id = :id"
    "   AND NOT EXISTS ("
    "       SELECT 1 FROM celex_facts.fact_source_link l"
    "        WHERE l.source_id = r.source_id"
    "   )"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fact(conn, celex_id: str) -> UUID:
    """Insert a minimal facts row and return its id.

    Parameters
    ----------
    conn:
        An open SQLAlchemy connection inside a transaction.
    celex_id:
        Document scope written to the ``celex_id`` column.

    Returns
    -------
    UUID
        Primary key of the inserted ``celex_facts.facts`` row.
    """
    fact_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO celex_facts.facts (id, celex_id, name, value_md, value_json)"
            " VALUES (:id, :celex_id, :name, :md, :json)"
        ),
        {
            "id": str(fact_id),
            "celex_id": celex_id,
            "name": f"test_fact_{fact_id}",
            "md": "test",
            "json": '"test"',
        },
    )
    return fact_id


def _execute_isolated(engine: Engine, statement: str, params: dict) -> None:
    """Run one DELETE in its own transaction, logging rather than raising.

    Teardown must never abort partway: a single statement that fails, for
    instance because an unrelated row still holds a foreign key, would roll back
    every other delete if all of them shared one transaction and would leave the
    whole batch of scaffolding rows behind.

    Parameters
    ----------
    engine:
        Engine used to open a fresh transaction for this statement.
    statement:
        SQL text to execute.
    params:
        Bind parameters for ``statement``.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(statement), params)
    except Exception:  # pragma: no cover - teardown must stay non-fatal
        _LOGGER.warning("Teardown failed for %s: %s", statement, params, exc_info=True)


def _purge_lineage_rows(
    engine: Engine,
    fact_ids: Iterable[UUID] = (),
    source_ids: Iterable[UUID] = (),
) -> None:
    """Delete the facts and lineage sources a test created.

    Deleting a ``facts`` row cascades to ``fact_source_link.fact_id`` and to any
    fact-typed ``fact_source_registry`` row keyed on ``source_fact_id`` (see
    ``migrate_lineage_tables.sql``), so links never need an explicit delete.
    Only standalone registry rows -- chunk, csv_human, lei_corporate -- remain,
    and those are removed just when no link still references them.

    Parameters
    ----------
    engine:
        Engine pointed at the ``celex_facts`` database.
    fact_ids:
        Ids of ``celex_facts.facts`` rows to remove.
    source_ids:
        Ids of ``celex_facts.fact_source_registry`` rows to remove if orphaned.
    """
    for fact_id in fact_ids:
        _execute_isolated(engine, _DELETE_FACT_SQL, {"id": str(fact_id)})
    for source_id in source_ids:
        _execute_isolated(engine, _DELETE_ORPHANED_SOURCE_SQL, {"id": str(source_id)})


# ---------------------------------------------------------------------------
# Unit tests — no database required
# ---------------------------------------------------------------------------


class TestLineageRepositoryProtocol(unittest.TestCase):
    """Verify LineageRepository exposes the expected public API."""

    def setUp(self):
        self.repo = LineageRepository.__new__(LineageRepository)

    def test_has_source_registration_methods(self):
        for method in (
            "register_chunk_source",
            "register_book_source",
            "register_heideltime_source",
            "register_csv_source",
            "register_lei_source",
            "register_fact_source",
        ):
            self.assertTrue(
                callable(getattr(self.repo, method, None)),
                f"Missing method: {method}",
            )

    def test_has_link_and_read_methods(self):
        for method in (
            "link_fact_to_source",
            "flag_conflict",
            "get_sources_for_fact",
            "get_facts_for_source",
            "get_source",
            "get_fact_provenance",
        ):
            self.assertTrue(
                callable(getattr(self.repo, method, None)),
                f"Missing method: {method}",
            )


class TestTrustRankDefaults(unittest.TestCase):
    """Default trust ranks are defined for all supported source types."""

    def test_all_source_types_have_defaults(self):
        for source_type in (
            "chunk", "csv_human", "book_case", "lei_corporate", "heideltime", "fact",
        ):
            self.assertIn(source_type, _TRUST_RANK_DEFAULTS)

    def test_csv_human_highest_rank(self):
        self.assertEqual(_TRUST_RANK_DEFAULTS["csv_human"], 9)

    def test_chunk_rank(self):
        self.assertEqual(_TRUST_RANK_DEFAULTS["chunk"], 5)

    def test_fact_lowest_rank(self):
        """Derived facts are trusted just below a raw chunk."""
        self.assertEqual(_TRUST_RANK_DEFAULTS["fact"], 4)

    def test_ranking_order(self):
        order = ["csv_human", "book_case", "lei_corporate", "heideltime", "chunk", "fact"]
        ranks = [_TRUST_RANK_DEFAULTS[t] for t in order]
        self.assertEqual(ranks, sorted(ranks, reverse=True))


class TestLinkFactToSourceValidation(unittest.TestCase):
    """link_fact_to_source raises ValueError for invalid trust_rank."""

    def test_trust_rank_zero_rejected(self):
        repo = LineageRepository.__new__(LineageRepository)
        with self.assertRaises(ValueError):
            repo.link_fact_to_source(uuid.uuid4(), uuid.uuid4(), "test", trust_rank=0)

    def test_trust_rank_eleven_rejected(self):
        repo = LineageRepository.__new__(LineageRepository)
        with self.assertRaises(ValueError):
            repo.link_fact_to_source(uuid.uuid4(), uuid.uuid4(), "test", trust_rank=11)

    def test_trust_rank_none_passes_validation(self):
        """trust_rank=None skips validation (DB lookup needed for default)."""
        repo = LineageRepository.__new__(LineageRepository)
        # Should raise AttributeError (no DB), not ValueError
        with self.assertRaises(AttributeError):
            repo.link_fact_to_source(uuid.uuid4(), uuid.uuid4(), "test", trust_rank=None)


class TestResolveTrustRank(unittest.TestCase):
    """_resolve_trust_rank uses source_type without touching the database."""

    def test_known_source_type_resolved_in_python(self):
        repo = LineageRepository.__new__(LineageRepository)
        # session=None would raise if the DB path were taken; source_type wins.
        self.assertEqual(
            repo._resolve_trust_rank(uuid.uuid4(), "csv_human", session=None), 9
        )
        self.assertEqual(
            repo._resolve_trust_rank(uuid.uuid4(), "chunk", session=None), 5
        )

    def test_unknown_source_type_falls_back_to_default(self):
        repo = LineageRepository.__new__(LineageRepository)
        self.assertEqual(
            repo._resolve_trust_rank(uuid.uuid4(), "not_a_type", session=None),
            _DEFAULT_TRUST_RANK,
        )


class _FakeRow:
    """Minimal stand-in for a SQLAlchemy result row with named attributes."""

    def __init__(self, ancestry_path_names, chunk_id, depth):
        self.ancestry_path_names = ancestry_path_names
        self.chunk_id = chunk_id
        self.depth = depth


class _FakeSession:
    """Context-manager session whose ``execute`` returns canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.captured_params = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _stmt, params):
        self.captured_params = params
        return self

    def all(self):
        return self._rows


class TestGetFactProvenance(unittest.TestCase):
    """get_fact_provenance maps the top-supporting row to a FactProvenanceRow VO."""

    def _repo_with_rows(self, rows):
        repo = LineageRepository.__new__(LineageRepository)
        session = _FakeSession(rows)
        repo.Session = lambda: session
        return repo, session

    def test_maps_top_row_to_value_object(self):
        # SQL applies LIMIT 1, so the repo receives at most the top row.
        rows = [_FakeRow("fines → fine → [doc, pos=28, score=1]", "chunk-abc", 1)]
        repo, session = self._repo_with_rows(rows)

        result = repo.get_fact_provenance("39952_1517_5", "fines")

        self.assertEqual(len(result), 1)
        self.assertTrue(all(isinstance(r, FactProvenanceRow) for r in result))
        self.assertEqual(
            result[0].ancestry_path_names,
            "fines → fine → [doc, pos=28, score=1]",
        )
        self.assertEqual(result[0].chunk_id, "chunk-abc")
        self.assertEqual(result[0].depth, 1)

    def test_binds_celex_and_fact_name_params(self):
        repo, session = self._repo_with_rows([])
        repo.get_fact_provenance("CELEX-32002D0180", "court_list")
        self.assertEqual(
            session.captured_params,
            {"celex_id": "CELEX-32002D0180", "fact_name": "court_list"},
        )

    def test_empty_result_returns_empty_list(self):
        repo, _ = self._repo_with_rows([])
        self.assertEqual(repo.get_fact_provenance("CELEX-x", "fines"), [])

    def test_null_chunk_id_preserved_as_none(self):
        repo, _ = self._repo_with_rows([_FakeRow("fines", None, 0)])
        result = repo.get_fact_provenance("CELEX-x", "fines")
        self.assertIsNone(result[0].chunk_id)


class _FakeDeleteSession:
    """Session stand-in capturing the executed statement and a fixed rowcount."""

    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.statement = None
        self.committed = False

    def execute(self, stmt):
        self.statement = stmt
        return self

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


class TestRetractFactLinks(unittest.TestCase):
    """retract_fact_links builds a producer-scoped DELETE and reports the count."""

    def _run(self, keep_source_id=None, rowcount=2):
        repo = LineageRepository.__new__(LineageRepository)
        session = _FakeDeleteSession(rowcount)
        repo.Session = lambda: session
        fact_id = uuid.uuid4()
        removed = repo.retract_fact_links(
            fact_id=fact_id,
            relation_type="corroborated_by",
            extraction_method="book_cross_check",
            source_type="book_case",
            keep_source_id=keep_source_id,
        )
        compiled = session.statement.compile(dialect=postgresql.dialect())
        return removed, session, str(compiled), compiled.params, fact_id

    def test_returns_rowcount_and_commits_own_session(self):
        removed, session, _, _, _ = self._run(rowcount=3)
        self.assertEqual(removed, 3)
        self.assertTrue(session.committed)

    def test_scopes_delete_to_producer_edge_and_source_type(self):
        _, _, sql, params, fact_id = self._run()
        self.assertTrue(sql.startswith("DELETE FROM celex_facts.fact_source_link"))
        self.assertIn(fact_id, params.values())
        self.assertIn("corroborated_by", params.values())
        self.assertIn("book_cross_check", params.values())
        self.assertIn("book_case", params.values())

    def test_keep_source_excluded_only_when_given(self):
        keep = uuid.uuid4()
        _, _, sql_keep, params_keep, _ = self._run(keep_source_id=keep)
        _, _, sql_all, _, _ = self._run()
        self.assertIn("!=", sql_keep)
        self.assertIn(keep, params_keep.values())
        self.assertNotIn("!=", sql_all)


class TestLineageSourceVOValidation(unittest.TestCase):
    """LineageSourceVO enforces the natural-key fields per source type."""

    def test_chunk_requires_chunk_id(self):
        with self.assertRaises(PydValidationError):
            LineageSourceVO(source_type="chunk", celex_id="X", extraction_method="m")

    def test_heideltime_requires_event_id(self):
        with self.assertRaises(PydValidationError):
            LineageSourceVO(source_type="heideltime", celex_id="X", extraction_method="m")

    def test_csv_requires_row(self):
        with self.assertRaises(PydValidationError):
            LineageSourceVO(source_type="csv_human", celex_id="X", extraction_method="m")

    def test_valid_lei_source(self):
        vo = LineageSourceVO(source_type="lei_corporate", lei_id="LEI1", extraction_method="m")
        self.assertEqual(vo.source_type, "lei_corporate")

    def test_valid_chunk_source(self):
        vo = LineageSourceVO(
            source_type="chunk",
            celex_id="X",
            chunk_id=str(uuid.uuid4()),
            extraction_method="m",
        )
        self.assertEqual(vo.source_type, "chunk")

    def test_pipeline_and_module_default_to_none(self):
        vo = LineageSourceVO(source_type="lei_corporate", lei_id="LEI1", extraction_method="m")
        self.assertIsNone(vo.pipeline)
        self.assertIsNone(vo.dspy_module)

    def test_pipeline_and_module_round_trip(self):
        vo = LineageSourceVO(
            source_type="lei_corporate",
            lei_id="LEI1",
            extraction_method="m",
            pipeline="universal",
            dspy_module="UniversalFactExtractionModule",
        )
        self.assertEqual(vo.pipeline, "universal")
        self.assertEqual(vo.dspy_module, "UniversalFactExtractionModule")


# ---------------------------------------------------------------------------
# Integration tests — requires a live celex_facts database
# ---------------------------------------------------------------------------


class TestRetractFactLinksIntegration(unittest.TestCase):
    """retract_fact_links removes only the scoped links on a live database."""

    @classmethod
    def setUpClass(cls):
        cls.repo = LineageRepository(DATABASE_URL)
        cls.engine = make_engine(DATABASE_URL)

    def setUp(self):
        """Create one fact linked to three book sources by two producers."""
        with self.engine.begin() as conn:
            self.fact_id = _make_fact(conn, "32021R9999")
        self.sources = [
            self.repo.register_book_source(celex_id="32021R9999", chunk_id=str(uuid.uuid4()))
            for _ in range(3)
        ]
        keep, drifted, foreign = self.sources
        for source_id in (keep, drifted):
            self.repo.link_fact_to_source(
                fact_id=self.fact_id, source_id=source_id,
                extraction_method="book_cross_check",
                relation_type="corroborated_by", source_type="book_case",
            )
        # Same source type and edge, different producer: must survive.
        self.repo.link_fact_to_source(
            fact_id=self.fact_id, source_id=foreign,
            extraction_method="universal",
            relation_type="corroborated_by", source_type="book_case",
        )

    def tearDown(self):
        """Delete the fact (cascading its links) and the orphaned sources."""
        _purge_lineage_rows(self.engine, fact_ids=[self.fact_id], source_ids=self.sources)

    def _linked_sources(self) -> set[UUID]:
        return {link.source_id for link in self.repo.get_sources_for_fact(self.fact_id)}

    def test_keep_source_preserves_anchor_and_foreign_producer(self):
        keep, drifted, foreign = self.sources
        removed = self.repo.retract_fact_links(
            fact_id=self.fact_id, relation_type="corroborated_by",
            extraction_method="book_cross_check", source_type="book_case",
            keep_source_id=keep,
        )
        self.assertEqual(removed, 1)
        self.assertEqual(self._linked_sources(), {keep, foreign})

    def test_without_keep_removes_all_scoped_links(self):
        _, _, foreign = self.sources
        removed = self.repo.retract_fact_links(
            fact_id=self.fact_id, relation_type="corroborated_by",
            extraction_method="book_cross_check", source_type="book_case",
        )
        self.assertEqual(removed, 2)
        self.assertEqual(self._linked_sources(), {foreign})


class TestSourceRegistrationIntegration(unittest.TestCase):
    """Integration tests for source registration methods."""

    @classmethod
    def setUpClass(cls):
        cls.repo = LineageRepository(DATABASE_URL)
        cls.engine = make_engine(DATABASE_URL)

    def setUp(self):
        """Start each test with an empty ledger of rows to clean up."""
        self._source_ids: list[UUID] = []

    def tearDown(self):
        """Delete the registry rows this test registered."""
        _purge_lineage_rows(self.engine, source_ids=self._source_ids)

    def _track(self, source_id: UUID) -> UUID:
        """Record a registered source so tearDown can remove it."""
        self._source_ids.append(source_id)
        return source_id

    def test_register_chunk_source_returns_uuid(self):
        sid = self._track(
            self.repo.register_chunk_source(
                celex_id="32021R9999",
                chunk_id=str(uuid.uuid4()),
                chunk_seq=[3, 4, 5],
                description="test chunk",
            )
        )
        self.assertIsInstance(sid, UUID)

    def test_register_chunk_source_persisted(self):
        chunk_id = str(uuid.uuid4())
        sid = self._track(
            self.repo.register_chunk_source(
                celex_id="32021R9999",
                chunk_id=chunk_id,
                chunk_seq=[1],
            )
        )
        row = self.repo.get_source(sid)
        self.assertIsNotNone(row)
        self.assertEqual(row.source_type, "chunk")
        self.assertEqual(row.celex_id, "32021R9999")
        self.assertEqual(row.chunk_id, chunk_id)

    def test_register_book_source(self):
        sid = self._track(
            self.repo.register_book_source(
                celex_id="BOOK-001",
                chunk_seq=[10],
            )
        )
        row = self.repo.get_source(sid)
        self.assertEqual(row.source_type, "book_case")

    def test_register_heideltime_source(self):
        event_id = uuid.uuid4()
        sid = self._track(
            self.repo.register_heideltime_source(
                celex_id="32021R9999",
                heideltime_event_id=event_id,
            )
        )
        row = self.repo.get_source(sid)
        self.assertEqual(row.source_type, "heideltime")
        self.assertEqual(row.heideltime_event_id, event_id)

    def test_register_csv_source(self):
        sid = self._track(
            self.repo.register_csv_source(celex_id="32021R9999", csv_row=42)
        )
        row = self.repo.get_source(sid)
        self.assertEqual(row.source_type, "csv_human")
        self.assertEqual(row.csv_row, 42)

    def test_register_lei_source(self):
        sid = self._track(
            self.repo.register_lei_source(lei_id="549300XYZTEST00001")
        )
        row = self.repo.get_source(sid)
        self.assertEqual(row.source_type, "lei_corporate")
        self.assertEqual(row.lei_id, "549300XYZTEST00001")

    def test_register_same_chunk_is_normalized(self):
        """Registering the same chunk twice returns the same source_id."""
        chunk_id = str(uuid.uuid4())
        first = self._track(
            self.repo.register_chunk_source(celex_id="32021R9999", chunk_id=chunk_id)
        )
        second = self.repo.register_chunk_source(celex_id="32021R9999", chunk_id=chunk_id)
        self.assertEqual(first, second)

    def test_register_description_refreshes_on_reuse(self):
        """Re-registering a source with a new description refreshes the label,
        while a ``None`` description never clears an existing one."""
        chunk_id = str(uuid.uuid4())
        sid = self._track(
            self.repo.register_chunk_source(
                celex_id="32021R9999",
                chunk_id=chunk_id,
                description="derived_from court_list_raw",
            )
        )
        # Same source, new non-None description -> refreshed in place.
        again = self.repo.register_chunk_source(
            celex_id="32021R9999", chunk_id=chunk_id, description="fact:court_list_raw"
        )
        self.assertEqual(sid, again)
        self.assertEqual(self.repo.get_source(sid).description, "fact:court_list_raw")

        # A None description must not clear the stored label.
        self.repo.register_chunk_source(celex_id="32021R9999", chunk_id=chunk_id)
        self.assertEqual(self.repo.get_source(sid).description, "fact:court_list_raw")

    def test_register_same_heideltime_is_normalized(self):
        """Registering the same heideltime extraction twice is de-duplicated."""
        event_id = uuid.uuid4()
        first = self._track(
            self.repo.register_heideltime_source(
                celex_id="32021R9999", heideltime_event_id=event_id
            )
        )
        second = self.repo.register_heideltime_source(
            celex_id="32021R9999", heideltime_event_id=event_id
        )
        self.assertEqual(first, second)

    def test_register_each_type_produces_unique_ids(self):
        ids = [
            self._track(self.repo.register_chunk_source(celex_id="32021R0001")),
            self._track(self.repo.register_csv_source(celex_id="32021R0001", csv_row=1)),
            self._track(self.repo.register_lei_source(lei_id="TEST-LEI")),
        ]
        self.assertEqual(len(set(ids)), 3)


class TestFactSourceLinkIntegration(unittest.TestCase):
    """Integration tests for fact-source linking and reads."""

    @classmethod
    def setUpClass(cls):
        cls.repo = LineageRepository(DATABASE_URL)
        cls.engine = make_engine(DATABASE_URL)

    def setUp(self):
        """Start each test with an empty ledger of rows to clean up."""
        self._fact_ids: list[UUID] = []
        self._source_ids: list[UUID] = []

    def tearDown(self):
        """Delete this test's scaffolding rows from celex_facts.

        Cleanup is per test rather than per class so an interrupted run strands
        at most one test's ``test_fact_<uuid>`` rows instead of the whole
        class's -- those rows otherwise pollute the live fact vocabulary that
        ``FactRepository.select_distinct_fact_names`` reads.
        """
        _purge_lineage_rows(self.engine, self._fact_ids, self._source_ids)

    def _make_fact(self) -> UUID:
        with self.engine.begin() as conn:
            fid = _make_fact(conn, "32021R9999")
        self._fact_ids.append(fid)
        return fid

    def _make_chunk_source(self) -> UUID:
        sid = self.repo.register_chunk_source(celex_id="32021R9999")
        self._source_ids.append(sid)
        return sid

    def test_link_fact_to_source_creates_row(self):
        fact_id = self._make_fact()
        source_id = self._make_chunk_source()
        self.repo.link_fact_to_source(
            fact_id=fact_id,
            source_id=source_id,
            extraction_method="universal",
        )
        links = self.repo.get_sources_for_fact(fact_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].source_id, source_id)
        self.assertEqual(links[0].extraction_method, "universal")

    def test_link_records_pipeline_and_module(self):
        """pipeline / dspy_module persist and survive an idempotent re-link."""
        fact_id = self._make_fact()
        source_id = self._make_chunk_source()
        self.repo.link_fact_to_source(
            fact_id=fact_id,
            source_id=source_id,
            extraction_method="universal",
            pipeline="universal",
            dspy_module="UniversalFactExtractionModule",
        )
        links = self.repo.get_sources_for_fact(fact_id)
        self.assertEqual(links[0].pipeline, "universal")
        self.assertEqual(links[0].dspy_module, "UniversalFactExtractionModule")

        # Re-link with a different pipeline refreshes the columns.
        self.repo.link_fact_to_source(
            fact_id=fact_id,
            source_id=source_id,
            extraction_method="hybrid_search",
            pipeline="hybrid_search",
            dspy_module="HybridSearchModule",
        )
        links = self.repo.get_sources_for_fact(fact_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].pipeline, "hybrid_search")
        self.assertEqual(links[0].dspy_module, "HybridSearchModule")

    def test_register_fact_source_and_derived_from_link(self):
        """A predecessor fact is catalogued as a 'fact' source and linked."""
        derived_fact = self._make_fact()
        predecessor_fact = self._make_fact()

        source_id = self.repo.register_fact_source(
            celex_id="32021R9999",
            source_fact_id=predecessor_fact,
            description="derived_from court_list_raw",
        )
        self._source_ids.append(source_id)

        row = self.repo.get_source(source_id)
        self.assertEqual(row.source_type, "fact")
        self.assertEqual(row.source_fact_id, predecessor_fact)

        # Idempotent on source_fact_id.
        again = self.repo.register_fact_source(
            celex_id="32021R9999", source_fact_id=predecessor_fact
        )
        self.assertEqual(source_id, again)

        self.repo.link_fact_to_source(
            fact_id=derived_fact,
            source_id=source_id,
            extraction_method="backfill",
            relation_type="derived_from",
            source_type="fact",
            pipeline="fact_context",
            dspy_module="FactContextModule",
        )
        links = self.repo.get_sources_for_fact(derived_fact)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].relation_type, "derived_from")
        self.assertEqual(links[0].trust_rank, _TRUST_RANK_DEFAULTS["fact"])
        self.assertEqual(links[0].pipeline, "fact_context")
        self.assertEqual(links[0].dspy_module, "FactContextModule")

    def test_link_defaults_to_extracted_from(self):
        fact_id = self._make_fact()
        source_id = self._make_chunk_source()
        self.repo.link_fact_to_source(fact_id, source_id, "universal")
        links = self.repo.get_sources_for_fact(fact_id)
        self.assertEqual(links[0].relation_type, "extracted_from")

    def test_link_with_explicit_trust_rank(self):
        fact_id = self._make_fact()
        source_id = self._make_chunk_source()
        self.repo.link_fact_to_source(fact_id, source_id, "universal", trust_rank=8)
        links = self.repo.get_sources_for_fact(fact_id)
        self.assertEqual(links[0].trust_rank, 8)

    def test_link_idempotent_on_duplicate(self):
        """Second call updates the row rather than inserting a duplicate."""
        fact_id = self._make_fact()
        source_id = self._make_chunk_source()
        self.repo.link_fact_to_source(fact_id, source_id, "pipeline_v1", trust_rank=5)
        self.repo.link_fact_to_source(fact_id, source_id, "pipeline_v2", trust_rank=7)
        links = self.repo.get_sources_for_fact(fact_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].extraction_method, "pipeline_v2")
        self.assertEqual(links[0].trust_rank, 7)

    def test_get_facts_for_source(self):
        fact_id = self._make_fact()
        source_id = self._make_chunk_source()
        self.repo.link_fact_to_source(fact_id, source_id, "universal")
        links = self.repo.get_facts_for_source(source_id)
        fact_ids_in_links = [lnk.fact_id for lnk in links]
        self.assertIn(fact_id, fact_ids_in_links)

    def test_flag_conflict_sets_flag_and_note(self):
        fact_id = self._make_fact()
        source_id = self._make_chunk_source()
        self.repo.link_fact_to_source(fact_id, source_id, "universal")
        self.repo.flag_conflict(fact_id, source_id, "Contradicts CSV row 42")
        links = self.repo.get_sources_for_fact(fact_id)
        self.assertTrue(links[0].conflict_flag)
        self.assertEqual(links[0].conflict_note, "Contradicts CSV row 42")

    def test_flag_conflict_missing_link_raises(self):
        with self.assertRaises(LookupError):
            self.repo.flag_conflict(uuid.uuid4(), uuid.uuid4(), "irrelevant")

    def test_option3_corroborated_by_relation(self):
        """Edge semantics: a CSV source can corroborate a chunk extraction."""
        fact_id = self._make_fact()
        chunk_src = self._make_chunk_source()
        csv_src_id = self.repo.register_csv_source(celex_id="32021R9999", csv_row=7)
        self._source_ids.append(csv_src_id)

        self.repo.link_fact_to_source(fact_id, chunk_src, "universal", relation_type="extracted_from")
        self.repo.link_fact_to_source(fact_id, csv_src_id, "csv_import", relation_type="corroborated_by", trust_rank=9)

        links = self.repo.get_sources_for_fact(fact_id)
        relation_types = {lnk.relation_type for lnk in links}
        self.assertIn("extracted_from", relation_types)
        self.assertIn("corroborated_by", relation_types)

    def test_sources_ordered_by_trust_rank_desc(self):
        """get_sources_for_fact returns highest trust first."""
        fact_id = self._make_fact()
        low_src = self._make_chunk_source()
        high_src = self.repo.register_csv_source(celex_id="32021R9999", csv_row=99)
        self._source_ids.append(high_src)

        self.repo.link_fact_to_source(fact_id, low_src, "pipeline", trust_rank=3)
        self.repo.link_fact_to_source(fact_id, high_src, "csv_import", trust_rank=9)

        links = self.repo.get_sources_for_fact(fact_id)
        self.assertGreaterEqual(links[0].trust_rank, links[-1].trust_rank)


if __name__ == "__main__":
    unittest.main()
