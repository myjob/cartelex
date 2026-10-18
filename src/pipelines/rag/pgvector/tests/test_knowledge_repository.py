"""
Unit + Integration tests for KnowledgeRepository.

Run with:
    PYTHONPATH=src .venv/bin/python -m pytest \
        src/pipelines/rag/pgvector/tests/test_knowledge_repository.py -v
"""
import uuid
import unittest
from unittest.mock import MagicMock

from sqlalchemy import text

from pipelines.rag.pgvector.knowledge_repository import KnowledgeRepository
from pipelines.rag.pgvector.models import (
    KnowledgeItem,
)
from pipelines.rag.pgvector.session import make_engine


# ---------------------------------------------------------------------------
# Unit tests — no database required
# ---------------------------------------------------------------------------


class TestKnowledgeRepositoryProtocol(unittest.TestCase):
    """Verify KnowledgeRepository exposes the expected public API."""

    def test_has_required_knowledge_item_methods(self):
        repo = KnowledgeRepository.__new__(KnowledgeRepository)
        for method in (
            "insert_knowledge_item",
            "select_knowledge_item",
            "list_knowledge_items",
            "update_knowledge_item",
            "delete_knowledge_item",
            "search_knowledge_fulltext",
            "exists",
        ):
            self.assertTrue(
                callable(getattr(repo, method, None)),
                f"Missing method: {method}",
            )

    def test_has_required_embedding_methods(self):
        repo = KnowledgeRepository.__new__(KnowledgeRepository)
        for method in (
            "insert_question_embedding",
            "list_question_embeddings",
            "delete_question_embeddings",
            "search_similar_questions",
        ):
            self.assertTrue(
                callable(getattr(repo, method, None)),
                f"Missing method: {method}",
            )

    def test_has_required_edge_methods(self):
        repo = KnowledgeRepository.__new__(KnowledgeRepository)
        for method in (
            "insert_edge",
            "list_edges_for_knowledge",
            "delete_edge",
            "list_edges_by_relation_type",
        ):
            self.assertTrue(
                callable(getattr(repo, method, None)),
                f"Missing method: {method}",
            )

    def test_insert_edge_rejects_zero_destinations(self):
        """ValueError when no destination is set."""
        with self.assertRaises(ValueError):
            KnowledgeRepository._validate_edge_destinations(None, None, None)

    def test_insert_edge_rejects_multiple_destinations(self):
        """ValueError when more than one destination is set."""
        with self.assertRaises(ValueError):
            KnowledgeRepository._validate_edge_destinations(
                uuid.uuid4(), uuid.uuid4(), None
            )

    def test_insert_edge_accepts_exactly_one_destination(self):
        """No error when exactly one destination is set."""
        # Should not raise — dst_source_id, dst_fact_id, dst_knowledge_id
        KnowledgeRepository._validate_edge_destinations(uuid.uuid4(), None, None)
        KnowledgeRepository._validate_edge_destinations(None, uuid.uuid4(), None)
        KnowledgeRepository._validate_edge_destinations(None, None, uuid.uuid4())


def _repo_with_rows(rows: list) -> KnowledgeRepository:
    """Build a repository whose session returns *rows* from ``execute``."""
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.fetchall.return_value = rows
    repo = KnowledgeRepository.__new__(KnowledgeRepository)
    repo.Session = MagicMock(return_value=session)
    return repo


class TestFindQueuedDuplicate(unittest.TestCase):
    """Producer dedup compares prompts through the injected normaliser."""

    def test_returns_row_with_same_normalised_prompt(self):
        row_id = uuid.uuid4()
        repo = _repo_with_rows(
            [(uuid.uuid4(), "something else"), (row_id, "List  NSK")]
        )
        found = repo.find_queued_duplicate(
            "answer", "list nsk", lambda p: " ".join(p.lower().split())
        )
        self.assertEqual(found, row_id)

    def test_none_when_no_prompt_matches(self):
        repo = _repo_with_rows([(uuid.uuid4(), "other question")])
        self.assertIsNone(repo.find_queued_duplicate("answer", "list nsk", str.lower))

    def test_none_when_answer_is_new(self):
        repo = _repo_with_rows([])
        self.assertIsNone(repo.find_queued_duplicate("answer", "list nsk", str.lower))


# ---------------------------------------------------------------------------
# Integration tests — requires a live celex_facts database
# ---------------------------------------------------------------------------


class TestKnowledgeItemIntegration(unittest.TestCase):
    """Integration tests for KnowledgeItem CRUD against the live database."""

    @classmethod
    def setUpClass(cls):
        cls.repo = KnowledgeRepository()
        cls.engine = make_engine()
        cls._created_ids = []

    @classmethod
    def tearDownClass(cls):
        """Safety net: clean up any test items that survived individual test cleanup."""
        if cls._created_ids:
            with cls.engine.begin() as conn:
                for item_id in cls._created_ids:
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_edges WHERE src_knowledge_id = :id OR dst_knowledge_id = :id"),
                        {"id": item_id},
                    )
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_items WHERE id = :id"),
                        {"id": item_id},
                    )

    def _make_item(self, **overrides) -> KnowledgeItem:
        """Helper: insert a test knowledge item with a fixture edge.

        The deferred ``trg_knowledge_grounding`` trigger requires every item
        to have at least one outgoing edge at commit.  We satisfy this with a
        self-referencing ``test_fixture`` edge inserted in the same transaction.
        """
        defaults = dict(
            knowledge_text=f"Test knowledge {uuid.uuid4()}",
            knowledge_type="test_type",
            created_by="test_harness",
        )
        defaults.update(overrides)
        item_id = uuid.uuid4()
        scope = defaults.pop("scope", "global")
        jurisdiction = defaults.pop("jurisdiction", None)
        status = defaults.pop("status", "draft")
        confidence = defaults.pop("confidence", None)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_items "
                    "(id, knowledge_text, knowledge_type, scope, jurisdiction, status, confidence, created_by) "
                    "VALUES (:id, :kt, :ktype, :scope, :jur, :status, :conf, :cb)"
                ),
                {
                    "id": item_id,
                    "kt": defaults["knowledge_text"],
                    "ktype": defaults["knowledge_type"],
                    "scope": scope,
                    "jur": jurisdiction,
                    "status": status,
                    "conf": confidence,
                    "cb": defaults["created_by"],
                },
            )
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_edges "
                    "(src_knowledge_id, dst_knowledge_id, relation_type, created_by) "
                    "VALUES (:src, :dst, 'test_fixture', 'test_harness')"
                ),
                {"src": item_id, "dst": item_id},
            )
        # Read back the ORM object
        item = self.repo.select_knowledge_item(item_id)
        self._created_ids.append(item.id)
        return item

    def _cleanup_item(self, item_id):
        """Remove edges then the knowledge item — safe with grounding trigger."""
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM celex_facts.knowledge_edges WHERE src_knowledge_id = :id OR dst_knowledge_id = :id"),
                {"id": item_id},
            )
            conn.execute(
                text("DELETE FROM celex_facts.knowledge_items WHERE id = :id"),
                {"id": item_id},
            )

    # --- insert & select ---

    def test_insert_and_select_round_trip(self):
        item = self._make_item(
            knowledge_text="Article 101 prohibits anti-competitive agreements.",
            knowledge_type="legal_principle",
            scope="jurisdiction",
            jurisdiction="EU",
            confidence=0.95,
        )
        try:
            self.assertIsNotNone(item.id)
            self.assertEqual(item.knowledge_type, "legal_principle")
            self.assertEqual(item.scope, "jurisdiction")
            self.assertEqual(item.jurisdiction, "EU")
            self.assertAlmostEqual(item.confidence, 0.95, places=2)
            self.assertEqual(item.status, "draft")  # server default

            fetched = self.repo.select_knowledge_item(item.id)
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.knowledge_text, item.knowledge_text)
        finally:
            self._cleanup_item(item.id)
            self._created_ids.remove(item.id)

    def test_select_nonexistent_returns_none(self):
        self.assertIsNone(self.repo.select_knowledge_item(uuid.uuid4()))

    # --- exists ---

    def test_exists_true_for_known_item(self):
        item = self._make_item()
        try:
            self.assertTrue(self.repo.exists(item.id))
        finally:
            self._cleanup_item(item.id)
            self._created_ids.remove(item.id)

    def test_exists_false_for_unknown(self):
        self.assertFalse(self.repo.exists(uuid.uuid4()))

    # --- update ---

    def test_update_knowledge_item(self):
        item = self._make_item(status="draft", confidence=0.5)
        try:
            updated = self.repo.update_knowledge_item(
                item.id, status="validated", confidence=0.99
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, "validated")
            self.assertAlmostEqual(updated.confidence, 0.99, places=2)
            self.assertGreater(updated.updated_at, item.created_at)
        finally:
            self._cleanup_item(item.id)
            self._created_ids.remove(item.id)

    def test_update_nonexistent_returns_none(self):
        self.assertIsNone(
            self.repo.update_knowledge_item(uuid.uuid4(), status="validated")
        )

    def test_update_rejects_empty_fields(self):
        with self.assertRaises(ValueError):
            self.repo.update_knowledge_item(uuid.uuid4())

    def test_update_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            self.repo.update_knowledge_item(uuid.uuid4(), bogus_field="x")

    # --- delete ---

    def test_delete_knowledge_item(self):
        item = self._make_item()
        self._created_ids.remove(item.id)  # will be deleted in-test
        # Must remove edges first due to FK constraints
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM celex_facts.knowledge_edges WHERE src_knowledge_id = :id OR dst_knowledge_id = :id"),
                {"id": item.id},
            )
        self.assertTrue(self.repo.delete_knowledge_item(item.id))
        self.assertIsNone(self.repo.select_knowledge_item(item.id))

    def test_delete_nonexistent_returns_false(self):
        self.assertFalse(self.repo.delete_knowledge_item(uuid.uuid4()))

    # --- list with filters ---

    def test_list_knowledge_items_by_status(self):
        item1 = self._make_item(status="draft")
        item2 = self._make_item(status="validated")
        try:
            drafts = self.repo.list_knowledge_items(status="draft")
            draft_ids = {i.id for i in drafts}
            self.assertIn(item1.id, draft_ids)
            self.assertNotIn(item2.id, draft_ids)
        finally:
            self._cleanup_item(item1.id)
            self._cleanup_item(item2.id)
            self._created_ids.remove(item1.id)
            self._created_ids.remove(item2.id)

    def test_list_knowledge_items_by_scope_and_type(self):
        unique_type = f"test_type_{uuid.uuid4().hex[:8]}"
        item = self._make_item(scope="jurisdiction", knowledge_type=unique_type)
        try:
            results = self.repo.list_knowledge_items(
                scope="jurisdiction", knowledge_type=unique_type
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].id, item.id)
        finally:
            self._cleanup_item(item.id)
            self._created_ids.remove(item.id)

    # --- full-text search ---

    def test_search_knowledge_fulltext(self):
        unique_word = f"xyzzy{uuid.uuid4().hex[:8]}"
        item = self._make_item(
            knowledge_text=f"The {unique_word} principle applies to merger control."
        )
        try:
            results = self.repo.search_knowledge_fulltext(unique_word)
            result_ids = {r.id for r in results}
            self.assertIn(item.id, result_ids)
        finally:
            self._cleanup_item(item.id)
            self._created_ids.remove(item.id)

    def test_search_knowledge_fulltext_no_match(self):
        results = self.repo.search_knowledge_fulltext(f"nonexistent_{uuid.uuid4().hex}")
        self.assertEqual(results, [])


# ---------------------------------------------------------------------------
# Question Embedding integration tests
# ---------------------------------------------------------------------------


class TestQuestionEmbeddingIntegration(unittest.TestCase):
    """Integration tests for KnowledgeQuestionEmbedding CRUD."""

    EMBEDDING_DIM = 16  # small dummy dimension for tests

    @classmethod
    def setUpClass(cls):
        cls.repo = KnowledgeRepository()
        cls.engine = make_engine()
        # Create a parent knowledge item with a fixture edge to satisfy
        # the deferred grounding trigger.
        cls._parent_id = uuid.uuid4()
        with cls.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_items "
                    "(id, knowledge_text, knowledge_type, created_by) "
                    "VALUES (:id, :kt, 'test_type', 'test_harness')"
                ),
                {"id": cls._parent_id, "kt": f"Embedding test parent {uuid.uuid4()}"},
            )
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_edges "
                    "(src_knowledge_id, dst_knowledge_id, relation_type, created_by) "
                    "VALUES (:src, :dst, 'test_fixture', 'test_harness')"
                ),
                {"src": cls._parent_id, "dst": cls._parent_id},
            )
        cls.parent_item = cls.repo.select_knowledge_item(cls._parent_id)

    @classmethod
    def tearDownClass(cls):
        # Clean up edges first, then the item (cascade covers embeddings).
        with cls.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM celex_facts.knowledge_edges WHERE src_knowledge_id = :id OR dst_knowledge_id = :id"),
                {"id": cls._parent_id},
            )
            conn.execute(
                text("DELETE FROM celex_facts.knowledge_items WHERE id = :id"),
                {"id": cls._parent_id},
            )

    def _dummy_embedding(self, seed: float = 0.1) -> list:
        """Return a deterministic dummy vector."""
        return [seed + i * 0.01 for i in range(self.EMBEDDING_DIM)]

    def test_insert_and_list_question_embedding(self):
        emb = self._dummy_embedding(0.1)
        result = self.repo.insert_question_embedding(
            knowledge_id=self.parent_item.id,
            question_variant="What does Article 101 prohibit?",
            embedding=emb,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.question_variant, "What does Article 101 prohibit?")

        embeddings = self.repo.list_question_embeddings(self.parent_item.id)
        self.assertGreaterEqual(len(embeddings), 1)
        qv_texts = {e.question_variant for e in embeddings}
        self.assertIn("What does Article 101 prohibit?", qv_texts)

    def test_delete_question_embeddings(self):
        # Insert two embeddings
        self.repo.insert_question_embedding(
            self.parent_item.id, "Q1 for delete test", self._dummy_embedding(0.2)
        )
        self.repo.insert_question_embedding(
            self.parent_item.id, "Q2 for delete test", self._dummy_embedding(0.3)
        )
        
        count = self.repo.delete_question_embeddings(self.parent_item.id)
        self.assertGreaterEqual(count, 2)
        after = self.repo.list_question_embeddings(self.parent_item.id)
        self.assertEqual(len(after), 0)

    def test_search_similar_questions(self):
        # Insert an embedding, then search with the same vector.
        # Scope to knowledge_id to avoid dimension-mismatch errors when the
        # table contains production rows with a different vector size.
        emb = self._dummy_embedding(0.5)
        self.repo.insert_question_embedding(
            self.parent_item.id, "Similarity search test", emb
        )
        try:
            results = self.repo.search_similar_questions(
                emb, limit=5, knowledge_id=self.parent_item.id
            )
            self.assertGreaterEqual(len(results), 1)
            top = results[0]
            self.assertIn("distance", top)
            # Distance to self should be very small (≈ 0.0).
            self.assertLess(top["distance"], 0.01)
        finally:
            self.repo.delete_question_embeddings(self.parent_item.id)


# ---------------------------------------------------------------------------
# Knowledge Edge integration tests
# ---------------------------------------------------------------------------


class TestKnowledgeEdgeIntegration(unittest.TestCase):
    """Integration tests for KnowledgeEdge CRUD."""

    @classmethod
    def setUpClass(cls):
        cls.repo = KnowledgeRepository()
        cls.engine = make_engine()
        # Create two knowledge items with fixture edges to satisfy
        # the deferred grounding trigger.
        cls._src_id = uuid.uuid4()
        cls._dst_id = uuid.uuid4()
        with cls.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_items "
                    "(id, knowledge_text, knowledge_type, created_by) "
                    "VALUES (:id, :kt, 'test_type', 'test_harness')"
                ),
                {"id": cls._src_id, "kt": f"Edge test source {uuid.uuid4()}"},
            )
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_edges "
                    "(src_knowledge_id, dst_knowledge_id, relation_type, created_by) "
                    "VALUES (:src, :dst, 'test_fixture', 'test_harness')"
                ),
                {"src": cls._src_id, "dst": cls._src_id},
            )
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_items "
                    "(id, knowledge_text, knowledge_type, created_by) "
                    "VALUES (:id, :kt, 'test_type', 'test_harness')"
                ),
                {"id": cls._dst_id, "kt": f"Edge test destination {uuid.uuid4()}"},
            )
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_edges "
                    "(src_knowledge_id, dst_knowledge_id, relation_type, created_by) "
                    "VALUES (:src, :dst, 'test_fixture', 'test_harness')"
                ),
                {"src": cls._dst_id, "dst": cls._dst_id},
            )
        cls.src_item = cls.repo.select_knowledge_item(cls._src_id)
        cls.dst_item = cls.repo.select_knowledge_item(cls._dst_id)

    @classmethod
    def tearDownClass(cls):
        with cls.engine.begin() as conn:
            # Edges referencing these items are cleaned up by FK cascade or
            # explicit deletes in tests.  Delete items last.
            for item in (cls.src_item, cls.dst_item):
                conn.execute(
                    text(
                        "DELETE FROM celex_facts.knowledge_edges "
                        "WHERE src_knowledge_id = :id OR dst_knowledge_id = :id"
                    ),
                    {"id": item.id},
                )
                conn.execute(
                    text("DELETE FROM celex_facts.knowledge_items WHERE id = :id"),
                    {"id": item.id},
                )

    def test_insert_edge_knowledge_to_knowledge(self):
        edge = self.repo.insert_edge(
            src_knowledge_id=self.src_item.id,
            relation_type="supports",
            dst_knowledge_id=self.dst_item.id,
            confidence=0.85,
            created_by="test_harness",
        )
        try:
            self.assertIsNotNone(edge.id)
            self.assertEqual(edge.relation_type, "supports")
            self.assertEqual(edge.dst_knowledge_id, self.dst_item.id)
            self.assertIsNone(edge.dst_source_id)
            self.assertIsNone(edge.dst_fact_id)
        finally:
            self.repo.delete_edge(edge.id)

    def test_insert_edge_validates_destinations(self):
        """insert_edge must reject zero and multiple destinations."""
        with self.assertRaises(ValueError):
            self.repo.insert_edge(
                src_knowledge_id=self.src_item.id,
                relation_type="supports",
                # No destination set
            )
        with self.assertRaises(ValueError):
            self.repo.insert_edge(
                src_knowledge_id=self.src_item.id,
                relation_type="supports",
                dst_knowledge_id=self.dst_item.id,
                dst_fact_id=uuid.uuid4(),  # two destinations
            )

    def test_list_edges_for_knowledge(self):
        edge1 = self.repo.insert_edge(
            src_knowledge_id=self.src_item.id,
            relation_type="supports",
            dst_knowledge_id=self.dst_item.id,
        )
        edge2 = self.repo.insert_edge(
            src_knowledge_id=self.src_item.id,
            relation_type="contradicts",
            dst_knowledge_id=self.dst_item.id,
        )
        try:
            edges = self.repo.list_edges_for_knowledge(self.src_item.id)
            edge_ids = {e.id for e in edges}
            self.assertIn(edge1.id, edge_ids)
            self.assertIn(edge2.id, edge_ids)
        finally:
            self.repo.delete_edge(edge1.id)
            self.repo.delete_edge(edge2.id)

    def test_list_edges_by_relation_type(self):
        unique_rel = f"test_rel_{uuid.uuid4().hex[:8]}"
        edge = self.repo.insert_edge(
            src_knowledge_id=self.src_item.id,
            relation_type=unique_rel,
            dst_knowledge_id=self.dst_item.id,
        )
        try:
            edges = self.repo.list_edges_by_relation_type(unique_rel)
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0].id, edge.id)
        finally:
            self.repo.delete_edge(edge.id)

    def test_delete_edge(self):
        edge = self.repo.insert_edge(
            src_knowledge_id=self.src_item.id,
            relation_type="derived_from",
            dst_knowledge_id=self.dst_item.id,
        )
        self.assertTrue(self.repo.delete_edge(edge.id))
        # Second delete returns False
        self.assertFalse(self.repo.delete_edge(edge.id))

    def test_delete_edge_nonexistent(self):
        self.assertFalse(self.repo.delete_edge(uuid.uuid4()))


# ---------------------------------------------------------------------------
# Cascade behaviour
# ---------------------------------------------------------------------------


class TestCascadeDelete(unittest.TestCase):
    """Verify that deleting a knowledge_item cascades to embeddings."""

    @classmethod
    def setUpClass(cls):
        cls.repo = KnowledgeRepository()
        cls.engine = make_engine()

    def test_cascade_deletes_embeddings_and_edges(self):
        """Deleting a knowledge item should cascade to its embeddings.

        Edge cascade depends on DB FK ON DELETE behaviour; edges with
        src_knowledge_id pointing to the deleted item use default
        (RESTRICT/NO ACTION), so we clean edges explicitly first.
        """
        # Insert item + fixture edge in one transaction to satisfy trigger
        item_id = uuid.uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_items "
                    "(id, knowledge_text, knowledge_type, created_by) "
                    "VALUES (:id, :kt, 'test_type', 'test_harness')"
                ),
                {"id": item_id, "kt": f"Cascade test {uuid.uuid4()}"},
            )
            conn.execute(
                text(
                    "INSERT INTO celex_facts.knowledge_edges "
                    "(src_knowledge_id, dst_knowledge_id, relation_type, created_by) "
                    "VALUES (:src, :dst, 'test_fixture', 'test_harness')"
                ),
                {"src": item_id, "dst": item_id},
            )

        # Insert an embedding (FK ON DELETE CASCADE).
        self.repo.insert_question_embedding(
            item_id,
            "Cascade question?",
            [0.1] * 16,
        )
        embeddings_before = self.repo.list_question_embeddings(item_id)
        self.assertGreaterEqual(len(embeddings_before), 1)

        # Clean edges first (no cascade on edges FK), then delete item.
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM celex_facts.knowledge_edges WHERE src_knowledge_id = :id OR dst_knowledge_id = :id"),
                {"id": item_id},
            )
        self.repo.delete_knowledge_item(item_id)

        # Verify embeddings are gone.
        with self.engine.begin() as conn:
            remaining = conn.execute(
                text(
                    "SELECT count(*) FROM celex_facts.knowledge_question_embeddings "
                    "WHERE knowledge_id = :kid"
                ),
                {"kid": item_id},
            ).scalar()
            self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
