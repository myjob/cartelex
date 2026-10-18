"""
Unit + Integration tests for KnowledgeIngestModule and supporting components.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \
        src/pipelines/agent_dspy/tests/test_knowledge_ingest_module.py -v

Tests are organised in three groups:
  1. **Unit tests** — verify signature fields, Pydantic models, and module
     structure without touching the database or LLM.
  2. **Integration tests** — require a live celex_facts database to verify
     persistence, the deferred grounding trigger, and audit trail writes.
  3. **Module integration** — end-to-end forward() with a mock DSPy LM.
"""

import uuid
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from config.settings import settings
from pipelines.agent_dspy.knowledge_graph_evidence import GraphContradiction
from pipelines.agent_dspy.knowledge_signatures import (
    KnowHowExtractionResult,
    KnowHowExtractionSignature,
    KnowHowItem,
    QuestionVariantResult,
    QuestionVariantSignature,
)
from pipelines.agent_dspy.knowledge_ingest_module import KnowledgeIngestModule
from pipelines.graph.entity_graph_models import EdgeRelation, ValidatedRelation
from pipelines.rag.pgvector.knowledge_ingestor import (
    KnowledgeGraphContradiction,
    KnowledgeIngestor,
)
from pipelines.rag.pgvector.knowledge_repository import KnowledgeRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository
from pipelines.rag.pgvector.models import (
    KnowledgeExtractionQueue,
    KnowledgeIngestAuditTrail,
    KnowledgeItem,
)
from pipelines.rag.pgvector.session import Session, make_engine


# ---------------------------------------------------------------------------
# Unit tests — no database or LLM required
# ---------------------------------------------------------------------------


class TestKnowHowExtractionSignature(unittest.TestCase):
    """Verify the KnowHowExtractionSignature declares the expected fields."""

    def test_input_fields(self):
        fields = KnowHowExtractionSignature.input_fields
        for name in (
            "user_prompt",
            "agent_answer",
            "supporting_facts",
            "numbered_facts",
        ):
            self.assertIn(name, fields, f"Missing input field: {name}")

    def test_output_field(self):
        fields = KnowHowExtractionSignature.output_fields
        self.assertIn("output", fields)

    def test_know_how_item_model(self):
        item = KnowHowItem(
            knowledge_text="Article 101 prohibits cartels.",
            knowledge_type="legal_principle",
            confidence=0.9,
        )
        self.assertEqual(item.knowledge_type, "legal_principle")
        self.assertAlmostEqual(item.confidence, 0.9)
        # Attribution defaults to empty (no facts cited).
        self.assertEqual(item.source_fact_refs, [])

    def test_know_how_item_carries_fact_refs(self):
        item = KnowHowItem(
            knowledge_text="Leniency was granted to the first applicant.",
            knowledge_type="domain_fact",
            confidence=0.8,
            source_fact_refs=[0, 3],
        )
        self.assertEqual(item.source_fact_refs, [0, 3])

    def test_know_how_extraction_result_empty(self):
        result = KnowHowExtractionResult(items=[])
        self.assertEqual(len(result.items), 0)


class TestQuestionVariantSignature(unittest.TestCase):
    """Verify the QuestionVariantSignature declares the expected fields."""

    def test_input_fields(self):
        fields = QuestionVariantSignature.input_fields
        for name in ("knowledge_text", "seed_question"):
            self.assertIn(name, fields, f"Missing input field: {name}")

    def test_output_field(self):
        fields = QuestionVariantSignature.output_fields
        self.assertIn("output", fields)

    def test_question_variant_result(self):
        result = QuestionVariantResult(
            variants=["What does Art. 101 say?", "How does Article 101 apply?"]
        )
        self.assertEqual(len(result.variants), 2)


class TestKnowledgeIngestModuleStructure(unittest.TestCase):
    """Verify KnowledgeIngestModule has expected attributes without DB access."""

    def test_module_has_extractor_and_variant_gen(self):
        mock_ingestor = MagicMock(spec=KnowledgeIngestor)
        module = KnowledgeIngestModule(
            ingestor=mock_ingestor,
            fact_repo=MagicMock(),
            entity_graph_repo=MagicMock(),
        )
        self.assertTrue(hasattr(module, "extractor"))
        self.assertTrue(hasattr(module, "variant_gen"))
        self.assertTrue(hasattr(module, "ingestor"))
        self.assertTrue(callable(getattr(module, "forward", None)))


class TestFactAttribution(unittest.TestCase):
    """Unit: per-nugget fact attribution helpers — no DB or LLM required."""

    def _module(self, fact_map, relations=()):
        fact_repo = MagicMock()
        fact_repo.select_facts_by_ids.return_value = fact_map
        graph_repo = MagicMock()
        graph_repo.relations_for_facts.return_value = tuple(relations)
        return KnowledgeIngestModule(
            ingestor=MagicMock(spec=KnowledgeIngestor),
            fact_repo=fact_repo,
            entity_graph_repo=graph_repo,
        )

    def test_catalog_indices_align_with_input_order(self):
        ids = [uuid.uuid4(), uuid.uuid4()]
        fact_map = {
            str(ids[0]): ("fines", "EUR 10m"),
            str(ids[1]): ("leniency", "first applicant"),
        }
        catalog = self._module(fact_map)._build_fact_catalog(ids)
        self.assertEqual(
            catalog.splitlines(),
            ["[0] fines: EUR 10m", "[1] leniency: first applicant"],
        )

    def test_catalog_placeholder_for_missing_fact(self):
        ids = [uuid.uuid4()]
        catalog = self._module({})._build_fact_catalog(ids)
        self.assertEqual(catalog, "[0] <unknown>: ")

    def test_resolve_refs_maps_indices_to_uuids(self):
        ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
        resolved = KnowledgeIngestModule._resolve_fact_refs([2, 0], ids)
        self.assertEqual(resolved, [ids[2], ids[0]])

    def test_resolve_refs_drops_out_of_range_and_duplicates(self):
        ids = [uuid.uuid4(), uuid.uuid4()]
        resolved = KnowledgeIngestModule._resolve_fact_refs([0, 0, 5, -1], ids)
        self.assertEqual(resolved, [ids[0]])

    def test_resolve_refs_empty_when_no_citation(self):
        ids = [uuid.uuid4()]
        self.assertEqual(KnowledgeIngestModule._resolve_fact_refs([], ids), [])

    def test_forward_skips_ungrounded_nugget_without_ingesting(self):
        ids = [uuid.uuid4()]
        module = self._module({str(ids[0]): ("fines", "EUR 10m")})

        nugget = KnowHowItem(
            knowledge_text="Speculative statement with no cited fact.",
            knowledge_type="legal_principle",
            confidence=0.5,
            source_fact_refs=[],
        )
        extract_pred = MagicMock()
        extract_pred.output = KnowHowExtractionResult(items=[nugget])
        module.extractor = MagicMock(return_value=extract_pred)
        module.variant_gen = MagicMock()

        result = module.forward(
            user_prompt="q",
            agent_answer="a",
            supporting_facts="evidence",
            supporting_fact_ids=ids,
            supporting_chunk_ids=[],
        )

        self.assertEqual(result.ingested_items, [])
        module.ingestor.ingest.assert_not_called()
        # The ungrounded nugget is audited as a (non-fatal) failure.
        module.ingestor.audit_run.assert_called_once()
        self.assertFalse(module.ingestor.audit_run.call_args.kwargs["success"])


class TestEntityGraphEvidenceWiring(unittest.TestCase):
    """The validated entity graph reaches the extractor and each nugget."""

    def _relation(self, fact_id, relation=EdgeRelation.FINED):
        """A validated edge anchored on *fact_id*."""
        return ValidatedRelation(
            src_name="NSK Deutschland GmbH",
            relation=relation,
            dst_name="Bearings",
            celex_id="39922_2067_2",
            source_fact_id=str(fact_id),
            amount="EUR 62 406 000",
        )

    def _module(self, ids, relations):
        """Module with mocked boundaries and a stubbed graph lookup."""
        fact_repo = MagicMock()
        fact_repo.select_facts_by_ids.return_value = {
            str(fid): ("fines", "EUR 62m") for fid in ids
        }
        graph_repo = MagicMock()
        graph_repo.relations_for_facts.return_value = tuple(relations)
        return KnowledgeIngestModule(
            ingestor=MagicMock(spec=KnowledgeIngestor),
            fact_repo=fact_repo,
            entity_graph_repo=graph_repo,
        )

    def _run(self, module, ids, refs):
        """Drive forward() with one nugget citing *refs*."""
        nugget = KnowHowItem(
            knowledge_text="A fined undertaking remains liable after a merger.",
            knowledge_type="legal_principle",
            confidence=0.8,
            source_fact_refs=refs,
        )
        extract_pred = MagicMock()
        extract_pred.output = KnowHowExtractionResult(items=[nugget])
        module.extractor = MagicMock(return_value=extract_pred)
        variant_pred = MagicMock()
        variant_pred.output = QuestionVariantResult(variants=["v1"])
        module.variant_gen = MagicMock(return_value=variant_pred)
        return module.forward(
            user_prompt="q",
            agent_answer="a",
            supporting_facts="evidence",
            supporting_fact_ids=ids,
            supporting_chunk_ids=[],
        )

    def test_relations_rendered_into_the_extractor(self):
        """The extractor is told the graph relations behind the catalog."""
        ids = [uuid.uuid4()]
        module = self._module(ids, [self._relation(ids[0])])

        self._run(module, ids, [0])

        block = module.extractor.call_args.kwargs["validated_relations"]
        self.assertIn("--fined--> Bearings [39922_2067_2]", block)

    def test_each_nugget_gets_only_its_cited_relations(self):
        """A relation behind an uncited fact must not reach the nugget."""
        ids = [uuid.uuid4(), uuid.uuid4()]
        relations = [self._relation(ids[0]), self._relation(ids[1])]
        module = self._module(ids, relations)

        self._run(module, ids, [1])

        passed = module.ingestor.ingest.call_args.kwargs["validated_relations"]
        self.assertEqual([r.source_fact_id for r in passed], [str(ids[1])])

    def test_graph_failure_degrades_to_no_relations(self):
        """A graph outage must weaken the gate, never fail the run."""
        ids = [uuid.uuid4()]
        module = self._module(ids, [])
        module.entity_graph_repo.relations_for_facts.side_effect = RuntimeError(
            "connection refused"
        )

        result = self._run(module, ids, [0])

        self.assertEqual(module.extractor.call_args.kwargs["validated_relations"], "")
        self.assertEqual(result.errors, [])
        module.ingestor.ingest.assert_called_once()

    def test_contradiction_is_audited_and_counted(self):
        """A refuted nugget is audited distinctly and not counted as an error."""
        ids = [uuid.uuid4()]
        module = self._module(ids, [self._relation(ids[0])])
        contradiction = GraphContradiction(
            relation=self._relation(ids[0]),
            trigger="negated_relation",
            sentence="NSK was not fined.",
        )
        module.ingestor.ingest.side_effect = KnowledgeGraphContradiction(contradiction)

        result = self._run(module, ids, [0])

        self.assertEqual(result.ingested_items, [])
        self.assertEqual(result.contradicted_count, 1)
        self.assertEqual(result.rejected_count, 0)
        self.assertEqual(result.errors, [])
        audit = module.ingestor.audit_run.call_args.kwargs
        self.assertFalse(audit["success"])
        self.assertIn("graph_contradiction", audit["errors"])
        self.assertEqual(
            audit["predecessors"]["graph_contradiction"]["trigger"],
            "negated_relation",
        )

    def test_disabled_by_settings_builds_no_graph_repo(self):
        """The A/B toggle leaves the module without a graph repository."""
        with patch.object(settings, "KNOWLEDGE_GRAPH_EVIDENCE_ENABLED", False):
            module = KnowledgeIngestModule(
                ingestor=MagicMock(spec=KnowledgeIngestor), fact_repo=MagicMock()
            )
        self.assertIsNone(module.entity_graph_repo)
        self.assertEqual(module._resolve_validated_relations([uuid.uuid4()]), ())


# ---------------------------------------------------------------------------
# Integration tests — require a live celex_facts database
# ---------------------------------------------------------------------------


class TestKnowledgeExtractionQueueORM(unittest.TestCase):
    """Integration: round-trip a KnowledgeExtractionQueue row."""

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()

    def test_insert_and_read_queue_row(self):
        row_id = None
        try:
            with Session() as session:
                row = KnowledgeExtractionQueue(
                    user_prompt="What are the fines for CELEX-31982D0367?",
                    agent_answer="The fines are listed in the case summary.",
                    supporting_facts="fact_1, fact_2",
                    fact_ids=["uuid-1", "uuid-2"],
                    chunk_ids=["chunk-a"],
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                row_id = row.id
                self.assertIsNotNone(row.id)
                self.assertEqual(row.status, "pending")
                self.assertIsNone(row.processed_at)

            # Read back
            with Session() as session:
                fetched = session.get(KnowledgeExtractionQueue, row_id)
                self.assertIsNotNone(fetched)
                self.assertEqual(fetched.user_prompt, "What are the fines for CELEX-31982D0367?")
                self.assertEqual(fetched.fact_ids, ["uuid-1", "uuid-2"])
        finally:
            if row_id:
                with self.engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_extraction_queue WHERE id = :id"),
                        {"id": row_id},
                    )


class TestKnowledgeIngestorAudit(unittest.TestCase):
    """Integration: verify audit_run writes to knowledge_ingest_audit_trail."""

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()
        cls.repo = KnowledgeRepository()
        cls.ingestor = KnowledgeIngestor(
            knowledge_repo=cls.repo,
            embed_fn=lambda text: [0.1] * 16,  # dummy embed
        )

    def test_audit_run_writes_row(self):
        audit_id = None
        try:
            self.ingestor.audit_run(
                queue_item_id=None,
                knowledge_id=None,
                user_prompt="test question",
                agent_answer="test answer",
                dspy_module="TestModule",
                success=True,
                duration_sec=5,
                errors="",
                predecessors={"fact_ids": [], "chunk_ids": []},
            )

            # Read back the latest audit row
            with Session() as session:
                audit = (
                    session.query(KnowledgeIngestAuditTrail)
                    .filter(KnowledgeIngestAuditTrail.dspy_module == "TestModule")
                    .order_by(KnowledgeIngestAuditTrail.created_at.desc())
                    .first()
                )
                self.assertIsNotNone(audit)
                self.assertTrue(audit.success)
                self.assertEqual(audit.user_prompt, "test question")
                audit_id = audit.id
        finally:
            if audit_id:
                with self.engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_ingest_audit_trail WHERE id = :id"),
                        {"id": audit_id},
                    )


class TestKnowledgeIngestorTransaction(unittest.TestCase):
    """Integration: verify ingest() writes item + embeddings + edges atomically."""

    EMBEDDING_DIM = 16

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()
        cls.repo = KnowledgeRepository()
        cls.ingestor = KnowledgeIngestor(
            knowledge_repo=cls.repo,
            embed_fn=lambda text: [0.1] * cls.EMBEDDING_DIM,  # dummy embed
        )

    def test_ingest_single_transaction(self):
        """Item + embeddings + edges all land in one commit."""
        # We need a real fact_id to FK into — create a dummy fact
        fact_id = uuid.uuid4()
        item = None
        try:
            # Insert a dummy fact to satisfy the FK constraint
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO celex_facts.facts (id, celex_id, name, value_md) "
                        "VALUES (:id, :celex, :name, :val)"
                    ),
                    {
                        "id": str(fact_id),
                        "celex": "TEST-INGEST",
                        "name": "test_fact",
                        "val": "test value",
                    },
                )

            item = self.ingestor.ingest(
                knowledge_text=f"Test knowledge {uuid.uuid4()}",
                knowledge_type="test_type",
                confidence=0.85,
                question_variants=[
                    "What is this test?",
                    "How does this test work?",
                ],
                supporting_fact_ids=[fact_id],
                created_by="test_harness",
            )

            self.assertIsNotNone(item.id)
            self.assertEqual(item.knowledge_type, "test_type")

            # Verify embeddings exist
            embeddings = self.repo.list_question_embeddings(item.id)
            self.assertEqual(len(embeddings), 2)

            # The fact has no fact_source_link provenance, so grounding rests
            # solely on the derived_from edge (the no-verified-sources case).
            edges = self.repo.list_edges_for_knowledge(item.id)
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0].relation_type, "derived_from")
            self.assertEqual(edges[0].dst_fact_id, fact_id)

        finally:
            # Cleanup
            if item:
                with self.engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_edges WHERE src_knowledge_id = :id"),
                        {"id": item.id},
                    )
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_question_embeddings WHERE knowledge_id = :id"),
                        {"id": item.id},
                    )
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_items WHERE id = :id"),
                        {"id": item.id},
                    )
            with self.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM celex_facts.facts WHERE id = :id"),
                    {"id": str(fact_id)},
                )


class TestIngestSourceEdgesFromVerifiedSources(unittest.TestCase):
    """Integration: source edges reuse and differentiate the facts' provenance.

    Seeds a supporting fact linked to two verified sources — a chunk source
    and a predecessor-fact source — then verifies ``ingest()`` emits a
    ``supports`` edge for the chunk source and a ``derived_from`` edge for the
    fact source (in addition to the direct ``derived_from`` edge to the fact).
    """

    EMBEDDING_DIM = 16

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()
        cls.repo = KnowledgeRepository()
        cls.lineage = LineageRepository(engine=cls.engine)
        cls.ingestor = KnowledgeIngestor(
            knowledge_repo=cls.repo,
            embed_fn=lambda text: [0.1] * cls.EMBEDDING_DIM,
        )

    def test_source_edges_differentiated_by_type(self):
        fact_id = uuid.uuid4()
        predecessor_fact_id = uuid.uuid4()
        item = None
        chunk_source_id = None
        fact_source_id = None
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO celex_facts.facts (id, celex_id, name, value_md) "
                        "VALUES (:id, :celex, :name, :val), "
                        "       (:pid, :celex, :pname, :pval)"
                    ),
                    {
                        "id": str(fact_id),
                        "celex": "TEST-GROUND",
                        "name": "test_fact",
                        "val": "test value",
                        "pid": str(predecessor_fact_id),
                        "pname": "predecessor_fact",
                        "pval": "predecessor value",
                    },
                )

            # Register the fact's verified provenance: a chunk source (→
            # supports) and a predecessor-fact source (→ derived_from).
            chunk_source_id = self.lineage.register_chunk_source(
                celex_id="TEST-GROUND",
                chunk_id=str(uuid.uuid4()),
            )
            fact_source_id = self.lineage.register_fact_source(
                celex_id="TEST-GROUND",
                source_fact_id=predecessor_fact_id,
            )
            self.lineage.link_fact_to_source(
                fact_id=fact_id,
                source_id=chunk_source_id,
                extraction_method="test_harness",
            )
            self.lineage.link_fact_to_source(
                fact_id=fact_id,
                source_id=fact_source_id,
                extraction_method="test_harness",
                relation_type="derived_from",
            )

            item = self.ingestor.ingest(
                knowledge_text=f"Test knowledge {uuid.uuid4()}",
                knowledge_type="test_type",
                confidence=0.85,
                question_variants=["What is this test?"],
                supporting_fact_ids=[fact_id],
                created_by="test_harness",
            )

            edges = self.repo.list_edges_for_knowledge(item.id)
            self.assertEqual(len(edges), 3)

            supports = [e for e in edges if e.relation_type == "supports"]
            derived = [e for e in edges if e.relation_type == "derived_from"]

            self.assertEqual([e.dst_source_id for e in supports], [chunk_source_id])

            # Two derived_from edges: the direct fact edge and the fact-source edge.
            self.assertEqual({e.dst_fact_id for e in derived}, {fact_id, None})
            self.assertEqual(
                {e.dst_source_id for e in derived}, {None, fact_source_id}
            )

        finally:
            with self.engine.begin() as conn:
                if item:
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_edges WHERE src_knowledge_id = :id"),
                        {"id": item.id},
                    )
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_question_embeddings WHERE knowledge_id = :id"),
                        {"id": item.id},
                    )
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_items WHERE id = :id"),
                        {"id": item.id},
                    )
                conn.execute(
                    text("DELETE FROM celex_facts.fact_source_link WHERE fact_id = :id"),
                    {"id": str(fact_id)},
                )
                for src_id in (chunk_source_id, fact_source_id):
                    if src_id:
                        conn.execute(
                            text("DELETE FROM celex_facts.fact_source_registry WHERE source_id = :id"),
                            {"id": str(src_id)},
                        )
                conn.execute(
                    text("DELETE FROM celex_facts.facts WHERE id = ANY(:ids)"),
                    {"ids": [str(fact_id), str(predecessor_fact_id)]},
                )


class TestDeferredTriggerRejectsUngrounded(unittest.TestCase):
    """Integration: inserting a knowledge item with no edges must fail at commit."""

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()
        cls.repo = KnowledgeRepository()

    def test_ungrounded_item_rejected(self):
        """Direct insert without edges should raise from the deferred trigger."""
        from sqlalchemy.exc import ProgrammingError

        with self.assertRaises(ProgrammingError):
            with self.repo.Session() as session:
                item = KnowledgeItem(
                    knowledge_text=f"Ungrounded test {uuid.uuid4()}",
                    knowledge_type="test_type",
                    created_by="test_harness",
                )
                session.add(item)
                # Commit triggers the deferred constraint — should raise
                session.commit()


class TestKnowledgeIngestModuleForward(unittest.TestCase):
    """Integration: end-to-end forward() with a mock LM.

    Uses DummyLM to avoid real Ollama calls while still exercising
    the full signature → ingestor → DB flow.
    """

    EMBEDDING_DIM = 16

    @classmethod
    def setUpClass(cls):
        cls.engine = make_engine()
        cls.repo = KnowledgeRepository()
        cls.ingestor = KnowledgeIngestor(
            knowledge_repo=cls.repo,
            embed_fn=lambda text: [0.1] * cls.EMBEDDING_DIM,
        )

    def test_forward_with_mock_lm(self):
        """Mock DSPy predictions to verify the full forward pipeline."""
        module = KnowledgeIngestModule(ingestor=self.ingestor)

        # Create a dummy fact to use as supporting evidence
        fact_id = uuid.uuid4()
        ingested_items = []
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO celex_facts.facts (id, celex_id, name, value_md) "
                        "VALUES (:id, :celex, :name, :val)"
                    ),
                    {
                        "id": str(fact_id),
                        "celex": "TEST-MODULE",
                        "name": "test_fact",
                        "val": "test evidence",
                    },
                )

            # Mock the DSPy predictors
            mock_extraction_result = KnowHowExtractionResult(
                items=[
                    KnowHowItem(
                        knowledge_text="Cartel fines are calculated based on turnover.",
                        knowledge_type="legal_principle",
                        confidence=0.9,
                        # Cite the single catalog fact so the nugget grounds.
                        source_fact_refs=[0],
                    )
                ]
            )
            mock_variant_result = QuestionVariantResult(
                variants=[
                    "How are cartel fines determined?",
                    "What is the basis for calculating cartel penalties?",
                ]
            )

            # Patch the DSPy Predict calls
            mock_extract_pred = MagicMock()
            mock_extract_pred.output = mock_extraction_result

            mock_variant_pred = MagicMock()
            mock_variant_pred.output = mock_variant_result

            module.extractor = MagicMock(return_value=mock_extract_pred)
            module.variant_gen = MagicMock(return_value=mock_variant_pred)

            result = module.forward(
                user_prompt="How are fines calculated?",
                agent_answer="Fines are based on company turnover.",
                supporting_facts="Evidence from case law.",
                supporting_fact_ids=[fact_id],
                supporting_chunk_ids=[],
            )

            self.assertEqual(len(result.ingested_items), 1)
            self.assertEqual(len(result.errors), 0)

            ingested_item = result.ingested_items[0]
            self.assertIsNotNone(ingested_item.id)
            self.assertEqual(ingested_item.knowledge_type, "legal_principle")

            ingested_items = result.ingested_items

            # Verify question variants were embedded (original + 2 generated = 3)
            embeddings = self.repo.list_question_embeddings(ingested_item.id)
            self.assertEqual(len(embeddings), 3)

            # Verify edges
            edges = self.repo.list_edges_for_knowledge(ingested_item.id)
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0].relation_type, "derived_from")

        finally:
            # Cleanup
            for item in ingested_items:
                with self.engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_ingest_audit_trail WHERE knowledge_id = :id"),
                        {"id": item.id},
                    )
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_edges WHERE src_knowledge_id = :id"),
                        {"id": item.id},
                    )
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_question_embeddings WHERE knowledge_id = :id"),
                        {"id": item.id},
                    )
                    conn.execute(
                        text("DELETE FROM celex_facts.knowledge_items WHERE id = :id"),
                        {"id": item.id},
                    )
            with self.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM celex_facts.facts WHERE id = :id"),
                    {"id": str(fact_id)},
                )


if __name__ == "__main__":
    unittest.main()
