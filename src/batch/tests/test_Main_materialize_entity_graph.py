"""Unit tests for the entity-graph materialization batch (DB mocked).

Follows the project's batch-test shape: ``EntityGraphRepository`` is stubbed at
the DB boundary and the assertions target the emitted edges and the stats
counters.  Focus is the company layer: it must come from
``companies_investigated`` alone, must survive that fact's two row keys, and
must report - not silently swallow - a document with no roster.
"""

import json
import unittest
from collections import defaultdict
from unittest.mock import MagicMock

from batch.Main_materialize_entity_graph import _materialize_celex
from pipelines.graph.entity_graph_mapping import (
    COMPANY_ROSTER_FAMILY,
    TEMPORAL_EDGE_FAMILY,
)
from pipelines.graph.entity_graph_models import EdgeRelation, EntityKind
from pipelines.rag.pgvector.entity_graph_repository import CompanyResolution


def _fact(name: str, value_json: str, fact_id: str | None = None) -> dict:
    """Build a facts-table row as ``fetch_facts_for_celex`` returns it."""
    return {
        "id": fact_id or f"fact-{name}",
        "name": name,
        "value_md": "",
        "value_json": value_json,
    }


class _StubRepository:
    """Records upserts instead of writing them, and resolves no LEIs."""

    def __init__(self, facts: list[dict]) -> None:
        self.facts = facts
        self.entities: list[tuple[EntityKind, str]] = []
        self.edges: list[tuple[str, EdgeRelation, str]] = []
        self.edge_rows: list[dict] = []
        self.deleted_celex: list[str] = []
        self.resolve_company_calls: list[str] = []
        self.alias_calls: list[str] = []
        self._ids: dict[str, str] = {}

    def delete_edges_for_celex(self, celex_id: str, session) -> int:
        self.deleted_celex.append(celex_id)
        return 0

    def fetch_facts_for_celex(self, celex_id: str, session=None) -> list[dict]:
        return self.facts

    def case_name(self, celex_id: str, session=None) -> str:
        return celex_id

    def resolve_company(self, name: str, session) -> CompanyResolution:
        self.resolve_company_calls.append(name)
        return CompanyResolution(
            natural_key=f"name:{name.casefold()}",
            canonical_name=name,
            lei_id=None,
            source="unresolved",
        )

    def canonicalize_alias(self, name: str, session) -> str | None:
        self.alias_calls.append(name)
        return None

    def upsert_entity(self, session, kind: EntityKind, natural_key: str, **kwargs) -> str:
        self.entities.append((kind, natural_key))
        entity_id = f"{kind.value}:{natural_key}"
        self._ids[natural_key] = entity_id
        return entity_id

    def upsert_edge(self, session, src_entity_id, dst_entity_id, relation, celex_id,
                    source_fact_id, value_json=None) -> None:
        self.edges.append((src_entity_id, relation, source_fact_id))
        self.edge_rows.append(
            {
                "src": src_entity_id,
                "dst": dst_entity_id,
                "relation": relation,
                "celex_id": celex_id,
                "source_fact_id": source_fact_id,
                "value_json": value_json,
            }
        )

    def company_edges(self) -> list[str]:
        """Company natural keys carrying a ``participated_in`` edge."""
        return [
            src.split(":", 1)[1]
            for src, relation, _ in self.edges
            if relation == EdgeRelation.PARTICIPATED_IN
        ]


class TestCompanyLayerSource(unittest.TestCase):
    """``participated_in`` comes from the roster fact, under either row key."""

    def _run(self, facts: list[dict]) -> tuple[_StubRepository, dict[str, int]]:
        repo = _StubRepository(facts)
        stats: dict[str, int] = defaultdict(int)
        _materialize_celex(repo, {}, "CELEX-31990D0645", MagicMock(), stats)
        return repo, stats

    def test_declared_row_key_yields_edges(self):
        repo, stats = self._run(
            [_fact(COMPANY_ROSTER_FAMILY, '[{"companies": "Solvay SA", "location": "BE"}]')]
        )
        self.assertEqual(repo.company_edges(), ["name:solvay sa"])
        self.assertEqual(stats["edge_participated_in"], 1)
        self.assertEqual(stats["roster_empty"], 0)
        self.assertEqual(stats["roster_missing"], 0)

    def test_variant_row_key_yields_edges_and_is_counted(self):
        # The 229-document "company"-keyed slice used to be skipped silently.
        repo, stats = self._run(
            [_fact(COMPANY_ROSTER_FAMILY, '[{"company": "Akzo NV"}, {"company": "BASF AG"}]')]
        )
        self.assertEqual(repo.company_edges(), ["name:akzo nv", "name:basf ag"])
        self.assertEqual(stats["edge_participated_in"], 2)
        self.assertEqual(stats[f"variant_key_{COMPANY_ROSTER_FAMILY}_company"], 2)

    def test_retired_company_fact_produces_no_edges(self):
        # A fact store still holding companies_canoncial must not re-seed the graph.
        repo, stats = self._run([_fact("companies_canoncial", '[{"companies": "Ghost AG"}]')])
        self.assertEqual(repo.edges, [])
        self.assertEqual(stats["edge_participated_in"], 0)
        self.assertEqual(stats["roster_missing"], 1)

    def test_missing_roster_is_reported(self):
        _, stats = self._run([_fact("fines", '[{"company": "Akzo NV", "amount": "ECU 5"}]')])
        self.assertEqual(stats["edge_fined"], 1)
        self.assertEqual(stats["roster_missing"], 1)
        self.assertEqual(stats["roster_empty"], 0)

    def test_empty_roster_is_reported(self):
        _, stats = self._run([_fact(COMPANY_ROSTER_FAMILY, "[]")])
        self.assertEqual(stats["roster_empty"], 1)
        self.assertEqual(stats["roster_missing"], 0)

    def test_unresolved_companies_counted(self):
        _, stats = self._run([_fact(COMPANY_ROSTER_FAMILY, '[{"companies": "Nowhere Ltd"}]')])
        self.assertEqual(stats["unresolved_companies"], 1)

    def test_lei_tier_is_counted_per_emission(self):
        """The run summary reports which tier supplied each company identity."""
        _, stats = self._run(
            [
                _fact(COMPANY_ROSTER_FAMILY, '[{"companies": "Nowhere Ltd"}]'),
                _fact("fines", '[{"company": "Nowhere Ltd", "amount": "ECU 5"}]'),
            ]
        )
        # Per-emission, not per company: the same firm in the roster and the
        # fines list counts twice, exactly like unresolved_companies.
        self.assertEqual(stats["lei_tier_unresolved"], 2)
        self.assertEqual(stats["unresolved_companies"], 2)


class TestTemporalLayer(unittest.TestCase):
    """``dated_in`` edges come from heideltime_event, matched case-scoped."""

    ROSTER = _fact(
        COMPANY_ROSTER_FAMILY,
        '[{"companies": "Teva Pharmaceutical Industries Ltd"},'
        ' {"companies": "Lundbeck A/S"}]',
    )

    @staticmethod
    def _events(*triples: tuple[str, str, str]) -> dict:
        return _fact(
            TEMPORAL_EDGE_FAMILY,
            json.dumps(
                [
                    {"event_date": d, "actor": a, "predicate_object": p}
                    for d, a, p in triples
                ]
            ),
        )

    def _run(self, facts: list[dict]) -> tuple[_StubRepository, dict[str, int]]:
        repo = _StubRepository(facts)
        stats: dict[str, int] = defaultdict(int)
        _materialize_celex(repo, {}, "CELEX-31990D0645", MagicMock(), stats)
        return repo, stats

    def _dated_in(self, repo: _StubRepository) -> list[dict]:
        return [r for r in repo.edge_rows if r["relation"] == EdgeRelation.DATED_IN]

    def test_one_aggregated_edge_per_resolved_company(self):
        repo, stats = self._run(
            [
                self.ROSTER,
                self._events(
                    ("1998-05-13", "Teva", "attended a meeting"),
                    ("2002-11-07", "Teva", "signed an agreement"),
                    ("1999-03-01", "Lundbeck", "sent an email"),
                ),
            ]
        )
        edges = self._dated_in(repo)
        self.assertEqual(len(edges), 2)
        self.assertEqual(stats["edge_dated_in"], 2)
        teva = next(e for e in edges if "teva" in e["src"])
        payload = json.loads(teva["value_json"])
        self.assertEqual(payload["first_date"], "1998-05-13")
        self.assertEqual(payload["last_date"], "2002-11-07")
        self.assertEqual(payload["event_count"], 2)
        self.assertEqual(payload["actors"], ["Teva"])

    def test_temporal_edges_are_emitted_after_the_company_layer(self):
        # The candidate set must be complete before matching, so the temporal
        # pass has to run last; fetch_facts_for_celex returns facts unordered.
        repo, _ = self._run(
            [self._events(("1998-05-13", "Teva", "met")), self.ROSTER]
        )
        relations = [row["relation"] for row in repo.edge_rows]
        self.assertEqual(relations[-1], EdgeRelation.DATED_IN)
        self.assertIn(EdgeRelation.PARTICIPATED_IN, relations[:-1])

    def test_no_company_node_is_ever_invented_from_an_actor(self):
        # Without a roster there are no candidates; the events must produce no
        # edges and no entities rather than creating nodes from actor strings.
        repo, stats = self._run([self._events(("1998-05-13", "Teva", "met"))])
        self.assertEqual(self._dated_in(repo), [])
        self.assertEqual(
            [kind for kind, _ in repo.entities], [EntityKind.CASE]
        )
        self.assertEqual(stats["heideltime_actor_unresolved"], 1)
        self.assertEqual(stats["heideltime_no_actor_resolved"], 1)

    def test_resolve_company_is_never_called_for_an_actor(self):
        # The regression guard for the design's central constraint: actors must
        # never reach the global pg_trgm LEI probe.
        repo, _ = self._run(
            [
                self.ROSTER,
                self._events(
                    ("1998-05-13", "Teva", "met"),
                    ("1998-05-14", "Some Unknown Party Ltd", "met"),
                ),
            ]
        )
        self.assertEqual(
            repo.resolve_company_calls,
            ["Teva Pharmaceutical Industries Ltd", "Lundbeck A/S"],
        )

    def test_placeholder_and_institution_actors_are_counted_not_matched(self):
        _, stats = self._run(
            [
                self.ROSTER,
                self._events(
                    ("1998-05-13", "redacted", "met"),
                    ("1998-05-14", "European Commission", "carried out inspections"),
                    ("1998-05-15", "Teva", "met"),
                ),
            ]
        )
        self.assertEqual(stats["heideltime_actor_placeholder"], 1)
        self.assertEqual(stats["heideltime_actor_institution"], 1)
        self.assertEqual(stats["heideltime_events_attached"], 1)
        self.assertEqual(stats["heideltime_events_dropped"], 2)

    def test_ambiguous_actor_yields_no_edge(self):
        roster = _fact(
            COMPANY_ROSTER_FAMILY,
            '[{"companies": "Jungbunzlauer AG"}, {"companies": "Jungbunzlauer Holding AG"}]',
        )
        repo, stats = self._run(
            [roster, self._events(("1998-05-13", "Jungbunzlauer", "met"))]
        )
        self.assertEqual(self._dated_in(repo), [])
        self.assertEqual(stats["heideltime_actor_ambiguous"], 1)

    def test_missing_and_empty_temporal_facts_are_counted(self):
        _, missing = self._run([self.ROSTER])
        self.assertEqual(missing["heideltime_fact_missing"], 1)
        self.assertEqual(missing["heideltime_fact_empty"], 0)

        _, empty = self._run([self.ROSTER, _fact(TEMPORAL_EDGE_FAMILY, "[]")])
        self.assertEqual(empty["heideltime_fact_missing"], 0)
        self.assertEqual(empty["heideltime_fact_empty"], 1)

    def test_temporal_edge_cites_the_heideltime_fact_as_its_source(self):
        repo, _ = self._run(
            [self.ROSTER, self._events(("1998-05-13", "Teva", "met"))]
        )
        edge = self._dated_in(repo)[0]
        self.assertEqual(edge["source_fact_id"], f"fact-{TEMPORAL_EDGE_FAMILY}")
        self.assertEqual(edge["celex_id"], "CELEX-31990D0645")


class TestPerDocumentRebuild(unittest.TestCase):
    """Each document is a delete-then-insert rebuild anchored on its case node."""

    def test_edges_deleted_before_insert_and_case_node_upserted(self):
        repo = _StubRepository([_fact(COMPANY_ROSTER_FAMILY, '[{"companies": "Solvay SA"}]')])
        _materialize_celex(repo, {}, "CELEX-31990D0645", MagicMock(), defaultdict(int))
        self.assertEqual(repo.deleted_celex, ["CELEX-31990D0645"])
        self.assertEqual(repo.entities[0], (EntityKind.CASE, "CELEX-31990D0645"))
        self.assertIn((EntityKind.COMPANY, "name:solvay sa"), repo.entities)


if __name__ == "__main__":
    unittest.main()
