"""Tests for EntityGraphRepository.

The unit tests need no database.  The integration tests require a live
``celex_facts`` database with the ``migrate_entity_graph.sql`` migration applied;
they self-skip when the ``entity_edges`` table is absent so the suite stays green
before the migration is run.
"""
import unittest
import uuid
from pathlib import Path

from sqlalchemy import text

from pipelines.graph.entity_graph_models import EdgeRelation, EntityKind
from pipelines.graph.heideltime_actor_resolution import (
    _INSTITUTION_MARKERS,
    _PLACEHOLDER_ACTORS,
)
from pipelines.rag.pgvector.entity_graph_repository import (
    EntityGraphRepository,
    _sanitize_stem,
)
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL


def _entity_graph_tables_exist(engine) -> bool:
    """True when the entity-graph tables have been migrated into the database."""
    with engine.connect() as conn:
        return (
            conn.execute(text("SELECT to_regclass('celex_facts.entity_edges')")).scalar()
            is not None
        )


class TestEntityGraphRepositoryProtocol(unittest.TestCase):
    """Public API and pure helpers, without touching the database."""

    def setUp(self):
        self.repo = EntityGraphRepository.__new__(EntityGraphRepository)

    def test_has_expected_methods(self):
        for method in (
            "resolve_company", "upsert_entity", "upsert_edge",
            "delete_edges_for_celex", "companies_in_multiple_cartels",
            "fines_for_company", "co_participation_network",
            "co_participants_by_name",
            "distinct_celex_ids", "fetch_facts_for_celex", "case_name",
        ):
            self.assertTrue(
                callable(getattr(self.repo, method, None)), f"Missing method: {method}"
            )

    def test_normalize_name_collapses_and_casefolds(self):
        self.assertEqual(
            EntityGraphRepository._normalize_name("  Akzo   Chemicals  BV "),
            "akzo chemicals bv",
        )


class TestEntityGraphRepositoryIntegration(unittest.TestCase):
    """Recursive-CTE and cross-case queries against a live database."""

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine(DATABASE_URL)
        if not _entity_graph_tables_exist(cls.engine):
            raise unittest.SkipTest(
                "entity_edges table absent — apply migrate_entity_graph.sql first"
            )
        cls.repo = EntityGraphRepository(engine=cls.engine)

    def test_multiple_cartels_and_co_participation(self):
        session = self.repo.Session()
        try:
            fact_id = uuid.uuid4()
            session.execute(
                text(
                    "INSERT INTO celex_facts.facts (id, celex_id, name, value_md, value_json)"
                    " VALUES (:id, :celex, :name, :md, :json)"
                ),
                {
                    "id": str(fact_id), "celex": "EG_TEST_A",
                    "name": f"eg_test_{fact_id}", "md": "x", "json": "[]",
                },
            )

            case_a = self.repo.upsert_entity(
                session, EntityKind.CASE, "EG_TEST_A", "Case A", celex_id="EG_TEST_A"
            )
            case_b = self.repo.upsert_entity(
                session, EntityKind.CASE, "EG_TEST_B", "Case B", celex_id="EG_TEST_B"
            )
            co1 = self.repo.upsert_entity(
                session, EntityKind.COMPANY, "name:eg_test_co1", "EG Test Co1"
            )
            co2 = self.repo.upsert_entity(
                session, EntityKind.COMPANY, "name:eg_test_co2", "EG Test Co2"
            )

            # co1 participates in both cases; co2 only in case A.
            self.repo.upsert_edge(
                session, co1, case_a, EdgeRelation.PARTICIPATED_IN, "EG_TEST_A", fact_id
            )
            self.repo.upsert_edge(
                session, co1, case_b, EdgeRelation.PARTICIPATED_IN, "EG_TEST_B", fact_id
            )
            self.repo.upsert_edge(
                session, co2, case_a, EdgeRelation.PARTICIPATED_IN, "EG_TEST_A", fact_id
            )

            multi_ids = {
                row["entity_id"]
                for row in self.repo.companies_in_multiple_cartels(session=session)
            }
            self.assertIn(co1, multi_ids)
            self.assertNotIn(co2, multi_ids)

            # From co2, co1 is reachable via the shared case A.
            network_ids = {
                row["entity_id"]
                for row in self.repo.co_participation_network(co2, session=session)
            }
            self.assertIn(co1, network_ids)
            self.assertNotIn(co2, network_ids)  # depth 0 excluded
        finally:
            session.rollback()  # never persist test fixtures
            session.close()

    def test_idempotent_edge_upsert_refreshes_payload(self):
        session = self.repo.Session()
        try:
            fact_id = uuid.uuid4()
            session.execute(
                text(
                    "INSERT INTO celex_facts.facts (id, celex_id, name, value_md, value_json)"
                    " VALUES (:id, :celex, :name, :md, :json)"
                ),
                {
                    "id": str(fact_id), "celex": "EG_TEST_C",
                    "name": f"eg_test_{fact_id}", "md": "x", "json": "[]",
                },
            )
            case_c = self.repo.upsert_entity(
                session, EntityKind.CASE, "EG_TEST_C", "Case C", celex_id="EG_TEST_C"
            )
            company = self.repo.upsert_entity(
                session, EntityKind.COMPANY, "name:eg_test_co3", "EG Test Co3"
            )
            self.repo.upsert_edge(
                session, company, case_c, EdgeRelation.FINED, "EG_TEST_C", fact_id,
                value_json='{"amount": "ECU 100"}',
            )
            self.repo.upsert_edge(
                session, company, case_c, EdgeRelation.FINED, "EG_TEST_C", fact_id,
                value_json='{"amount": "ECU 200"}',
            )
            count = session.execute(
                text(
                    "SELECT count(*), max(value_json) FROM celex_facts.entity_edges "
                    "WHERE src_entity_id = :s AND dst_entity_id = :d AND relation = 'fined'"
                ),
                {"s": company, "d": case_c},
            ).fetchone()
            self.assertEqual(count[0], 1)  # upsert, not duplicate
            self.assertEqual(count[1], '{"amount": "ECU 200"}')  # payload refreshed
        finally:
            session.rollback()
            session.close()


class TestGraphQaViewsIntegration(unittest.TestCase):
    """The flat views the Q&A planners query via kb_sql.

    Self-skips until ``migrate_graph_qa_views.sql`` has been applied, matching
    the convention above.
    """

    _VIEWS = (
        "celex_facts.company_case_relations_vw",
        "celex_facts.case_provision_relations_vw",
    )

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine(DATABASE_URL)
        with cls.engine.connect() as conn:
            for view in cls._VIEWS:
                if conn.execute(
                    text("SELECT to_regclass(:view)"), {"view": view}
                ).scalar() is None:
                    raise unittest.SkipTest(
                        f"{view} absent - apply migrate_graph_qa_views.sql first"
                    )

    def test_company_view_exposes_the_planner_columns(self):
        expected = {
            "company_name", "lei_id", "company_country", "legal_jurisdiction",
            "relation", "source_fact_name", "celex_id", "case_name", "amount",
            "source_fact_id",
        }
        with self.engine.connect() as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'celex_facts' "
                        "AND table_name = 'company_case_relations_vw'"
                    )
                )
            }
        self.assertEqual(expected, columns)

    def test_company_view_returns_fined_rows_with_a_lineage_anchor(self):
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT company_name, source_fact_id, source_fact_name "
                    "FROM celex_facts.company_case_relations_vw "
                    "WHERE relation = 'fined' LIMIT 1"
                )
            ).fetchone()
        self.assertIsNotNone(row, "no fined edges - rematerialize the graph")
        self.assertTrue(row[0])
        self.assertIsNotNone(row[1])
        self.assertEqual(row[2], "fines")

    def test_provision_view_holds_only_violated_edges(self):
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT canonical_concept, source_fact_name, source_fact_id "
                    "FROM celex_facts.case_provision_relations_vw LIMIT 5"
                )
            ).fetchall()
        self.assertTrue(rows, "no violated edges - rematerialize the graph")
        for concept, fact_name, fact_id in rows:
            self.assertTrue(concept)
            self.assertIsNotNone(fact_id)
            self.assertIn(
                fact_name, ("infringement_concept", "law_article_all_relevant")
            )

    _ALLOWED_RELATIONS = (
        "participated_in", "fined", "leniency_applied",
        "leniency_granted", "leniency_rejected",
    )

    def test_company_view_row_count_matches_its_company_case_edges(self):
        """The view must neither drop edges nor fan them out on the facts join.

        The edge count is restricted to the view's allow-list: ``dated_in`` is
        also a company -> case relation, and comparing against every such edge
        would make this test fail for the wrong reason once the temporal layer
        is materialized.
        """
        with self.engine.connect() as conn:
            view_rows = conn.execute(
                text("SELECT count(*) FROM celex_facts.company_case_relations_vw")
            ).scalar()
            edge_rows = conn.execute(
                text(
                    "SELECT count(*) FROM celex_facts.entity_edges ed "
                    "JOIN celex_facts.entities c ON c.entity_id = ed.src_entity_id "
                    "AND c.kind = 'company' "
                    "JOIN celex_facts.entities k ON k.entity_id = ed.dst_entity_id "
                    "AND k.kind = 'case' "
                    "WHERE ed.relation::text = ANY(:allowed)"
                ),
                {"allowed": list(self._ALLOWED_RELATIONS)},
            ).scalar()
        self.assertEqual(view_rows, edge_rows)

    def test_company_view_exposes_only_the_five_allowed_relations(self):
        """The allow-list is the contract every planner prompt depends on.

        Without it the temporal ``dated_in`` relation joins in silently and
        inflates a view that reports participation, not activity.
        """
        with self.engine.connect() as conn:
            relations = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT DISTINCT relation "
                        "FROM celex_facts.company_case_relations_vw"
                    )
                )
            }
        self.assertTrue(relations, "view is empty - rematerialize the graph")
        self.assertTrue(
            relations <= set(self._ALLOWED_RELATIONS),
            f"unexpected relations leaked into the view: "
            f"{sorted(relations - set(self._ALLOWED_RELATIONS))}",
        )


class TestCaseTimelineViewIntegration(unittest.TestCase):
    """The HeidelTime temporal read surface.

    Self-skips until ``migrate_graph_qa_views.sql`` has been re-applied with the
    timeline view, matching the convention of the other integration classes.
    """

    _VIEW = "celex_facts.case_timeline_vw"

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine(DATABASE_URL)
        with cls.engine.connect() as conn:
            if conn.execute(
                text("SELECT to_regclass(:view)"), {"view": cls._VIEW}
            ).scalar() is None:
                raise unittest.SkipTest(
                    f"{cls._VIEW} absent - re-apply migrate_graph_qa_views.sql"
                )

    def test_view_exposes_the_planner_columns(self):
        expected = {
            "celex_id", "case_name", "event_seq", "event_date", "event_date_raw",
            "actor", "actor_class", "company_name", "company_entity_id", "lei_id",
            "predicate_object", "celex_event_count", "source_fact_name",
            "source_fact_id",
        }
        with self.engine.connect() as conn:
            columns = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'celex_facts' "
                        "AND table_name = 'case_timeline_vw'"
                    )
                )
            }
        self.assertEqual(expected, columns)

    def test_a_full_scan_does_not_raise_on_real_data(self):
        """The date and JSON guards must hold across the whole corpus.

        A blind ``::date`` cast aborts the entire query rather than the offending
        row, and the store holds at least one correctly shaped but invalid date
        (``2011-00-27``), so this scan is the assertion that matters most.
        """
        with self.engine.connect() as conn:
            total = conn.execute(
                text("SELECT count(*) FROM celex_facts.case_timeline_vw")
            ).scalar()
        self.assertGreater(total, 50_000)

    def test_partial_precision_dates_stay_out_of_the_typed_column(self):
        with self.engine.connect() as conn:
            leaked = conn.execute(
                text(
                    "SELECT count(*) FROM celex_facts.case_timeline_vw "
                    "WHERE event_date IS NOT NULL "
                    "AND event_date_raw !~ '^\\d{4}-\\d{2}-\\d{2}$'"
                )
            ).scalar()
        self.assertEqual(leaked, 0)

    def test_every_row_carries_a_heideltime_lineage_anchor(self):
        with self.engine.connect() as conn:
            unanchored = conn.execute(
                text(
                    "SELECT count(*) FROM celex_facts.case_timeline_vw t "
                    "WHERE NOT EXISTS (SELECT 1 FROM celex_facts.facts f "
                    "WHERE f.id = t.source_fact_id "
                    "AND f.name = 'heideltime_event')"
                )
            ).scalar()
        self.assertEqual(unanchored, 0)

    def test_company_rows_always_carry_a_node_id(self):
        with self.engine.connect() as conn:
            inconsistent = conn.execute(
                text(
                    "SELECT count(*) FROM celex_facts.case_timeline_vw "
                    "WHERE actor_class = 'company' AND company_entity_id IS NULL"
                )
            ).scalar()
        self.assertEqual(inconsistent, 0)

    def test_actor_class_uses_only_the_documented_vocabulary(self):
        with self.engine.connect() as conn:
            classes = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT DISTINCT actor_class FROM celex_facts.case_timeline_vw"
                    )
                )
            }
        self.assertTrue(
            classes <= {"company", "institution", "placeholder", "unresolved"},
            f"undocumented actor_class values: {sorted(classes)}",
        )


class TestActorVocabularyDrift(unittest.TestCase):
    """The placeholder / institution lists are duplicated in Python and SQL.

    ``case_timeline_vw`` classifies actors that resolved to no company node, so
    it must repeat the vocabulary that ``heideltime_actor_resolution`` applies
    at write time.  This is the only Python-to-SQL duplication in the temporal
    layer; without this test the two drift apart silently and the view starts
    labelling placeholders as unresolved.  Needs no database.
    """

    @staticmethod
    def _view_sql() -> str:
        path = (
            Path(__file__).resolve().parents[1] / "migrate_graph_qa_views.sql"
        )
        return path.read_text(encoding="utf-8").casefold()

    def test_every_placeholder_actor_appears_in_the_view(self):
        sql = self._view_sql()
        for actor in _PLACEHOLDER_ACTORS:
            with self.subTest(actor=actor):
                self.assertIn(actor.casefold(), sql)

    def test_every_institution_marker_appears_in_the_view(self):
        sql = self._view_sql()
        for marker in _INSTITUTION_MARKERS:
            with self.subTest(marker=marker):
                self.assertIn(marker.casefold(), sql)




class TestSanitizeStem(unittest.TestCase):
    """The seed stem is spliced into a ``~*`` pattern, so it must be inert."""

    def test_metacharacters_are_stripped(self):
        self.assertEqual(_sanitize_stem("bay.*er"), "bay er")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(_sanitize_stem("  Koninklijke   Philips "), "Koninklijke Philips")

    def test_only_metacharacters_yields_empty(self):
        self.assertEqual(_sanitize_stem("(.*)"), "")

    def test_none_like_input_yields_empty(self):
        self.assertEqual(_sanitize_stem(""), "")


class TestCoParticipantsByNameIntegration(unittest.TestCase):
    """Stem-seeded co-participation traversal against a live graph."""

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine(DATABASE_URL)
        if not _entity_graph_tables_exist(cls.engine):
            raise unittest.SkipTest("entity_graph migration not applied")
        cls.repo = EntityGraphRepository(engine=cls.engine)
        cls.result = cls.repo.co_participants_by_name("bayer", max_hops=1)
        if not cls.result.seeds:
            raise unittest.SkipTest("no 'bayer' company node in this database")

    def test_stem_seeds_several_alias_nodes(self):
        """Aliases split across nodes, so one stem must seed more than one."""
        self.assertGreater(len(self.result.seeds), 1)
        for seed in self.result.seeds:
            self.assertGreater(seed.edge_count, 0)

    def test_word_boundary_excludes_mere_prefix_matches(self):
        """'bayer' must not drag in the unrelated Bavarian-prefixed companies.

        Measured: a substring match also returns 'Bayerische Hypo- und
        Vereinsbank AG', 'Bayerischer Rundfunk' and 'Bayerische
        Versicherungskammer' - a bank, a broadcaster and an insurer.
        """
        names = [seed.canonical_name.casefold() for seed in self.result.seeds]
        self.assertTrue(names)
        for name in names:
            self.assertNotIn("bayerische", name)
            self.assertNotIn("bayerischer", name)

    def test_no_peer_is_a_seed(self):
        """The regression: a sibling alias must never be its own peer.

        The seed array, not the current node, is the exclusion set - otherwise
        'Bayer FRG' is returned as a peer of 'Bayer'.
        """
        seed_ids = {seed.entity_id for seed in self.result.seeds}
        peer_ids = {peer.entity_id for peer in self.result.peers}
        self.assertEqual(seed_ids & peer_ids, set())

    def test_every_peer_carries_its_evidence(self):
        """A peer with no shared case or no lineage anchor cannot be cited."""
        if not self.result.peers:
            self.skipTest("no peers in this database")
        for peer in self.result.peers:
            self.assertTrue(peer.shared_celex_ids, peer.canonical_name)
            self.assertTrue(peer.source_fact_ids, peer.canonical_name)

    def test_hops_never_exceed_the_bound(self):
        for peer in self.result.peers:
            self.assertLessEqual(peer.hops, 1)
            self.assertGreaterEqual(peer.hops, 1)

    def test_peer_cap_is_honoured_and_declared(self):
        capped = self.repo.co_participants_by_name("bayer", max_hops=1, max_peers=3)
        self.assertLessEqual(len(capped.peers), 3)
        if len(self.result.peers) > 3:
            self.assertTrue(capped.truncated)

    def test_unmatched_stem_returns_empty_not_error(self):
        empty = self.repo.co_participants_by_name("zzzznotacompanyname")
        self.assertEqual(empty.seeds, ())
        self.assertEqual(empty.peers, ())

    def test_blank_stem_short_circuits_without_a_query(self):
        empty = self.repo.co_participants_by_name("   ")
        self.assertEqual(empty.seeds, ())

    def test_relation_scopes_the_traversal(self):
        """A different relation defines a different, smaller cohort."""
        fined = self.repo.co_participants_by_name(
            "bayer", relation=EdgeRelation.FINED, max_hops=1
        )
        self.assertEqual(fined.relation, EdgeRelation.FINED)
        fined_seed_ids = {seed.entity_id for seed in fined.seeds}
        fined_peer_ids = {peer.entity_id for peer in fined.peers}
        self.assertEqual(fined_seed_ids & fined_peer_ids, set())


class _FakeSession:
    """Session stub that answers each SQL shape from a scripted result.

    Mocks only the DB boundary: ``resolve_company`` is pure control flow above
    three queries, so the tiers can be exercised without a live database.
    Records which shapes were reached, which is how "the trigram never ran" is
    asserted rather than assumed.
    """

    def __init__(
        self,
        alias: str | None = None,
        registry_lei: str | None = None,
        trigram: tuple[str, float] | None = None,
    ) -> None:
        self.alias = alias
        self.registry_lei = registry_lei
        self.trigram = trigram
        self.shapes: list[str] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        if "company_canonical2alias" in sql:
            self.shapes.append("alias")
            return _FakeResult((self.alias,) if self.alias else None)
        if "company_registry_master_vw" in sql:
            self.shapes.append("registry")
            return _FakeResult((self.registry_lei,) if self.registry_lei else None)
        if "corp_lei" in sql:
            self.shapes.append("trigram")
            return _FakeResult(self.trigram)
        raise AssertionError(f"unexpected query: {sql}")


class _FakeResult:
    """Minimal stand-in for a SQLAlchemy result carrying one optional row."""

    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class TestResolveCompanyLadder(unittest.TestCase):
    """The identity ladder: alias -> length floor -> registry view -> trigram."""

    LONG = "Pirelli Cavi e Sistemi S.p.A."  # comfortably over the floor

    def _repo(self) -> EntityGraphRepository:
        """Build a repository with no engine, so only the ladder is exercised."""
        return EntityGraphRepository(
            engine=object(), lei_match_threshold=0.6, lei_min_name_length=12
        )

    def test_registry_view_wins_and_skips_the_trigram(self):
        repo = self._repo()
        session = _FakeSession(registry_lei="5493001KJTIIGC8Y1R12", trigram=("WRONG", 0.99))
        resolution = repo.resolve_company(self.LONG, session)
        self.assertEqual(resolution.lei_id, "5493001KJTIIGC8Y1R12")
        self.assertEqual(resolution.natural_key, "5493001KJTIIGC8Y1R12")
        self.assertEqual(resolution.source, "registry")
        self.assertNotIn("trigram", session.shapes)

    def test_trigram_is_the_fallback_for_off_roster_mentions(self):
        repo = self._repo()
        session = _FakeSession(registry_lei=None, trigram=("5493001KJTIIGC8Y1R12", 0.82))
        resolution = repo.resolve_company(self.LONG, session)
        self.assertEqual(resolution.lei_id, "5493001KJTIIGC8Y1R12")
        self.assertEqual(resolution.source, "trigram")
        self.assertEqual(session.shapes, ["alias", "registry", "trigram"])

    def test_trigram_below_threshold_is_refused(self):
        repo = self._repo()
        session = _FakeSession(registry_lei=None, trigram=("5493001KJTIIGC8Y1R12", 0.41))
        resolution = repo.resolve_company(self.LONG, session)
        self.assertIsNone(resolution.lei_id)
        self.assertEqual(resolution.source, "unresolved")
        self.assertTrue(resolution.natural_key.startswith("name:"))

    def test_short_mention_is_never_keyed_on_an_lei(self):
        """The floor outranks both sources: neither query may even run."""
        repo = self._repo()
        session = _FakeSession(
            registry_lei="5493001KJTIIGC8Y1R12", trigram=("OTHERLEI", 0.99)
        )
        resolution = repo.resolve_company("Gant", session)
        self.assertIsNone(resolution.lei_id)
        self.assertEqual(resolution.source, "below_floor")
        self.assertEqual(resolution.natural_key, "name:gant")
        self.assertEqual(session.shapes, ["alias"])

    def test_alias_can_lift_a_short_mention_over_the_floor(self):
        """The alias tier is a keyed lookup, so its output clears the floor."""
        repo = self._repo()
        session = _FakeSession(
            alias="AstraZeneca Pharmaceuticals PLC",
            registry_lei="5493001KJTIIGC8Y1R12",
        )
        resolution = repo.resolve_company("AZ", session)
        self.assertEqual(resolution.canonical_name, "AstraZeneca Pharmaceuticals PLC")
        self.assertEqual(resolution.source, "registry")

    def test_nfkc_folding_matches_the_roster_spelling(self):
        """A compatibility character keys onto the roster's NFKC-folded name."""
        repo = self._repo()
        # U+FF33 FULLWIDTH LATIN CAPITAL LETTER S folds to a plain "S".
        resolution = repo.resolve_company(
            "Ｓolvay Chemicals International", _FakeSession()
        )
        self.assertEqual(resolution.canonical_name, "Solvay Chemicals International")
        self.assertEqual(
            resolution.natural_key, "name:solvay chemicals international"
        )

    def test_resolution_is_cached_per_mention(self):
        repo = self._repo()
        session = _FakeSession(registry_lei="5493001KJTIIGC8Y1R12")
        first = repo.resolve_company(self.LONG, session)
        second = repo.resolve_company(self.LONG, session)
        self.assertIs(first, second)
        self.assertEqual(session.shapes, ["alias", "registry"])


class TestRegistryLeiLookupIntegration(unittest.TestCase):
    """The view lookup against the live database."""

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine(DATABASE_URL)
        with cls.engine.connect() as connection:
            exists = connection.execute(
                text("SELECT to_regclass('celex_facts.company_registry_master_vw')")
            ).scalar()
        if exists is None:
            raise unittest.SkipTest(
                "company_registry_master_vw absent — apply "
                "migrate_registry_master_view.sql first"
            )
        cls.repo = EntityGraphRepository(engine=cls.engine)

    def test_view_lookup_returns_a_well_formed_lei(self):
        session = self.repo.Session()
        try:
            name = session.execute(
                text(
                    "SELECT company_name FROM celex_facts.company_registry_master_vw"
                    " WHERE source = 'lei' AND length(company_name) >= 12 LIMIT 1"
                )
            ).scalar()
            if name is None:
                self.skipTest("no LEI-branch rows to probe")
            lei = self.repo._registry_lei(name, session)
            self.assertIsNotNone(lei)
            self.assertEqual(len(lei), 20)
        finally:
            session.close()

    def test_unknown_name_returns_none_rather_than_raising(self):
        session = self.repo.Session()
        try:
            self.assertIsNone(
                self.repo._registry_lei("Nowhere Holdings Limited XYZZY", session)
            )
        finally:
            session.close()

    def test_lei_branch_never_maps_one_name_to_two_leis(self):
        """The invariant that makes the name-keyed cache correct."""
        session = self.repo.Session()
        try:
            conflicts = session.execute(
                text(
                    "SELECT count(*) FROM ("
                    " SELECT company_name"
                    " FROM celex_facts.company_registry_master_vw"
                    " WHERE source = 'lei' GROUP BY 1"
                    " HAVING count(DISTINCT registry_id) > 1) AS x"
                )
            ).scalar()
            self.assertEqual(conflicts, 0)
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
