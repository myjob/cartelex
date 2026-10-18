import unittest
import json
import uuid
from unittest.mock import MagicMock

from pydantic import ValidationError
from sqlalchemy import create_engine, text
from tools.PipelineFactIngestor import (
    FactIngestor,
    LineageSourceVO,
    _is_gate_predecessor,
)
from pipelines.rag.pgvector.vector_config import DATABASE_URL
from pipelines.agent_dspy.fact_extractor_param import FactExtractorParamVO

class TestFactIngestor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(DATABASE_URL)
        cls.test_celex_id = f"TEST_CELEX_{uuid.uuid4()}"
        cls.test_fact_name = "test_products"

    def test_save_fact_list_format(self):
        ingestor = FactIngestor(target_fact_name=self.test_fact_name)
        
        # Simulated LLM list output
        sample_output = ["Widget A", "Widget B", "Widget C"]
        
        # Save to database
        ingestor.save_fact(self.test_celex_id, sample_output)
        
        # Verify it was saved correctly
        with self.engine.connect() as conn:
            result = conn.execute(
                text("SELECT value_md, value_json FROM celex_facts.facts WHERE celex_id = :celex_id AND name = :name"),
                {"celex_id": self.test_celex_id, "name": self.test_fact_name}
            ).fetchone()
            
            self.assertIsNotNone(result, "Fact was not saved to database")
            
            value_md, value_json = result
            
            # Check Markdown conversion (bulleted list)
            self.assertIn("- Widget A", value_md)
            self.assertIn("- Widget B", value_md)
            self.assertIn("- Widget C", value_md)
            
            # Check JSON conversion
            parsed_json = json.loads(value_json)
            self.assertEqual(parsed_json, sample_output)

    def test_audit_run_success(self):
        ingestor = FactIngestor(target_fact_name=self.test_fact_name)
        params = FactExtractorParamVO(
            celex_id=self.test_celex_id,
            question="What are the products?",
            target_fact_name=self.test_fact_name,
            output_schema="product_name"
        )
        # Use real fact ID to satisfy foreign key constraint added in pg_celex-ddl.sql
        real_fact_id = ingestor.save_fact(self.test_celex_id, ["Dummy data"])
        
        # Call audit_run
        ingestor.audit_run(params, fact_id=real_fact_id, dspy_module='TestModule', success=True, duration_sec=5)
        
        # Verify it was saved correctly
        with self.engine.connect() as conn:
            result = conn.execute(
                text("SELECT id, fact_id, success, duration, errors FROM celex_facts.fact_ingestion_audit_trail WHERE celex_id = :celex_id AND target_fact_name = :name ORDER BY created_at DESC LIMIT 1"),
                {"celex_id": self.test_celex_id, "name": self.test_fact_name}
            ).fetchone()
            
            self.assertIsNotNone(result, "Audit trail was not saved to database")
            self.assertNotEqual(result.id, result.fact_id, "fact_id must be different from audit trail id")
            self.assertEqual(result.fact_id, real_fact_id)
            self.assertTrue(result.success)
            self.assertEqual(result.duration, 5)
            self.assertEqual(result.errors, "")
            
    def test_audit_run_failure(self):
        ingestor = FactIngestor(target_fact_name=self.test_fact_name)
        params = FactExtractorParamVO(
            celex_id=self.test_celex_id,
            question="What are the products?",
            target_fact_name=self.test_fact_name,
            output_schema="product_name"
        )
        
        # Call audit_run with error
        error_msg = "ValueError: Something went wrong"
        ingestor.audit_run(params, fact_id=None, dspy_module='TestModule', success=False, duration_sec=2, errors=error_msg)
        
        # Verify it was saved correctly
        with self.engine.connect() as conn:
            result = conn.execute(
                text("SELECT fact_id, success, duration, errors FROM celex_facts.fact_ingestion_audit_trail WHERE celex_id = :celex_id AND target_fact_name = :name ORDER BY created_at DESC LIMIT 1"),
                {"celex_id": self.test_celex_id, "name": self.test_fact_name}
            ).fetchone()
            
            self.assertIsNotNone(result, "Audit trail was not saved to database")
            
            fact_id, success, duration, errors = result
            self.assertIsNone(fact_id)
            self.assertFalse(success)
            self.assertEqual(duration, 2)
            self.assertEqual(errors, error_msg)

    def test_save_fact_updates_existing_no_doublet(self):
        """save_fact with force_override must UPDATE existing row, not INSERT a second one."""
        celex = f"TEST_DOUBLET_{uuid.uuid4()}"
        ingestor = FactIngestor(target_fact_name="court_list_raw")

        # First save — creates the row
        fact_id_1 = ingestor.save_fact(celex, [{"court": "Court A"}])
        self.assertIsNotNone(fact_id_1)

        # Second save — must update, not insert
        fact_id_2 = ingestor.save_fact(celex, [{"court": "Court B"}])
        self.assertEqual(fact_id_1, fact_id_2, "save_fact must reuse the existing row id")

        # Verify exactly 1 row in the DB
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, value_json FROM celex_facts.facts WHERE celex_id = :cid AND name = :n"),
                {"cid": celex, "n": "court_list_raw"},
            ).fetchall()
            self.assertEqual(len(rows), 1, f"Expected 1 row, got {len(rows)}")
            self.assertIn("Court B", rows[0].value_json)

        # cleanup
        with self.engine.connect() as conn:
            with conn.begin():
                conn.execute(text("DELETE FROM celex_facts.facts WHERE celex_id = :cid"), {"cid": celex})

    def test_save_fact_cleans_preexisting_duplicates(self):
        """If duplicates already exist, save_fact must purge extras and keep one."""
        celex = f"TEST_DUP_HEAL_{uuid.uuid4()}"
        fact_name = "court_list_raw"

        # Manually insert TWO rows to simulate pre-existing duplicates
        with self.engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text(
                        "INSERT INTO celex_facts.facts (id, celex_id, name, value_md, value_json, created_at) "
                        "VALUES (:id, :cid, :n, :md, :js, NOW() - interval '1 minute')"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "cid": celex,
                        "n": fact_name,
                        "md": "row-old",
                        "js": '[{"row": 0}]',
                    },
                )
                conn.execute(
                    text(
                        "INSERT INTO celex_facts.facts (id, celex_id, name, value_md, value_json, created_at) "
                        "VALUES (:id, :cid, :n, :md, :js, NOW())"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "cid": celex,
                        "n": fact_name,
                        "md": "row-new",
                        "js": '[{"row": 1}]',
                    },
                )

        # Verify 2 rows exist
        with self.engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM celex_facts.facts WHERE celex_id = :cid AND name = :n"),
                {"cid": celex, "n": fact_name},
            ).scalar()
            self.assertEqual(count, 2, "Pre-condition: expected 2 duplicate rows")

        # Now call save_fact — it should heal the duplicates
        ingestor = FactIngestor(target_fact_name=fact_name)
        ingestor.save_fact(celex, [{"court": "Healed Court"}])

        # Verify exactly 1 row remains
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, value_json FROM celex_facts.facts WHERE celex_id = :cid AND name = :n"),
                {"cid": celex, "n": fact_name},
            ).fetchall()
            self.assertEqual(len(rows), 1, f"Expected 1 row after healing, got {len(rows)}")
            self.assertIn("Healed Court", rows[0].value_json)

        # cleanup
        with self.engine.connect() as conn:
            with conn.begin():
                conn.execute(text("DELETE FROM celex_facts.facts WHERE celex_id = :cid"), {"cid": celex})

    @classmethod
    def tearDownClass(cls):
        # Clean up test facts in correct constraint order
        with cls.engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text("DELETE FROM celex_facts.fact_ingestion_audit_trail WHERE celex_id = :celex_id"),
                    {"celex_id": cls.test_celex_id}
                )
                conn.execute(
                    text("DELETE FROM celex_facts.facts WHERE celex_id = :celex_id"),
                    {"celex_id": cls.test_celex_id}
                )


class TestGatePredecessorDetection(unittest.TestCase):
    """_is_gate_predecessor distinguishes boolean gates from data sources."""

    def test_boolean_equality_is_gate(self):
        self.assertTrue(
            _is_gate_predecessor("is_court_involved", "is_court_involved == true")
        )

    def test_boolean_gate_among_data_predecessors(self):
        condition = 'exists("leniency_case") and leniency_case == true'
        self.assertTrue(_is_gate_predecessor("leniency_case", condition))
        self.assertFalse(_is_gate_predecessor("companies_investigated", condition))

    def test_exists_call_is_gate(self):
        self.assertTrue(
            _is_gate_predecessor("case_is_in_book", 'exists("case_is_in_book")')
        )

    def test_membership_value_use_is_not_gate(self):
        # the predecessor's value is consumed, so it is data lineage
        self.assertFalse(
            _is_gate_predecessor("court_list_raw", "'court' in court_list_raw")
        )

    def test_no_condition_is_not_gate(self):
        self.assertFalse(_is_gate_predecessor("companies_investigated", None))


class TestDerivePredecessorLineage(unittest.TestCase):
    """_derive_predecessor_lineage builds 'fact' sources from YAML predecessors.

    The FactRepository is mocked at the DB boundary; no live database is used.
    """

    def setUp(self):
        self.celex_id = "CELEX-32002D0180"
        self.repo = MagicMock()
        self.ingestor = FactIngestor(
            target_fact_name="court_list", fact_table=self.repo
        )

    def _predecessor_row(self, fact_id: uuid.UUID) -> MagicMock:
        """A stand-in for one celex_facts.facts ORM row."""
        row = MagicMock()
        row.id = fact_id
        return row

    def test_no_predecessors_makes_no_db_call(self):
        self.assertEqual(
            self.ingestor._derive_predecessor_lineage(self.celex_id, None), []
        )
        self.assertEqual(
            self.ingestor._derive_predecessor_lineage(self.celex_id, []), []
        )
        self.repo.select_fact4celex.assert_not_called()

    def test_data_predecessor_becomes_derived_from(self):
        predecessor_id = uuid.uuid4()
        self.repo.select_fact4celex.return_value = [
            self._predecessor_row(predecessor_id)
        ]

        sources = self.ingestor._derive_predecessor_lineage(
            self.celex_id,
            ["court_list_raw"],
            condition="'court' in court_list_raw",
            pipeline="universal",
            dspy_module="UniversalFactModule",
        )

        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source.source_type, "fact")
        self.assertEqual(source.relation_type, "derived_from")
        self.assertEqual(source.source_fact_id, predecessor_id)
        self.assertEqual(source.celex_id, self.celex_id)
        self.assertEqual(source.description, "fact:court_list_raw")
        self.assertEqual(source.extraction_method, "court_list")
        self.assertEqual(source.pipeline, "universal")
        self.assertEqual(source.dspy_module, "UniversalFactModule")

    def test_boolean_gate_predecessor_becomes_gated_by(self):
        self.repo.select_fact4celex.return_value = [
            self._predecessor_row(uuid.uuid4())
        ]

        sources = self.ingestor._derive_predecessor_lineage(
            self.celex_id,
            ["is_court_involved"],
            condition="is_court_involved == true",
        )

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].relation_type, "gated_by")

    def test_mixed_predecessors_classified_individually(self):
        self.repo.select_fact4celex.side_effect = lambda celex, name: [
            self._predecessor_row(uuid.uuid4())
        ]

        sources = self.ingestor._derive_predecessor_lineage(
            self.celex_id,
            ["companies_investigated", "leniency_case"],
            condition='exists("leniency_case") and leniency_case == true',
        )

        by_description = {s.description: s.relation_type for s in sources}
        self.assertEqual(
            by_description,
            {
                "fact:companies_investigated": "derived_from",
                "fact:leniency_case": "gated_by",
            },
        )

    def test_missing_predecessor_is_skipped_not_raised(self):
        self.repo.select_fact4celex.return_value = []

        sources = self.ingestor._derive_predecessor_lineage(
            self.celex_id, ["court_list_raw"]
        )

        self.assertEqual(sources, [])

    def test_last_row_wins_for_duplicate_predecessors(self):
        # select_fact4celex returns every matching row; the newest is the one
        # the orchestrator's context also uses (existing[-1]).
        first, last = uuid.uuid4(), uuid.uuid4()
        self.repo.select_fact4celex.return_value = [
            self._predecessor_row(first),
            self._predecessor_row(last),
        ]

        sources = self.ingestor._derive_predecessor_lineage(
            self.celex_id, ["court_list_raw"]
        )

        self.assertEqual(sources[0].source_fact_id, last)


class TestSaveFactLineageAssembly(unittest.TestCase):
    """save_fact combines chunk and predecessor lineage rather than choosing."""

    def setUp(self):
        self.celex_id = "CELEX-32002D0180"
        self.fact_id = uuid.uuid4()
        self.repo = MagicMock()
        self.repo.upsert_fact.return_value = self.fact_id
        self.lineage_repo = MagicMock()
        self.ingestor = FactIngestor(
            target_fact_name="court_list",
            fact_table=self.repo,
            lineage_repo=self.lineage_repo,
            chunk_repo=MagicMock(),
        )
        # Silence the real lineage write; assert on the assembled source list.
        self.persisted: list = []
        self.ingestor._persist_lineage = lambda fact_id, sources: self.persisted.extend(
            sources
        )

    def test_predecessor_sources_are_appended_to_chunk_sources(self):
        chunk_source = LineageSourceVO(
            source_type="chunk",
            celex_id=self.celex_id,
            chunk_id=str(uuid.uuid4()),
            extraction_method="court_list",
        )
        self.ingestor._derive_chunk_lineage = lambda *args, **kwargs: [chunk_source]
        self.repo.select_fact4celex.return_value = [MagicMock(id=uuid.uuid4())]

        self.ingestor.save_fact(
            self.celex_id,
            [{"court": "Amsterdam District Court"}],
            references_doc=[3],
            predecessors=["court_list_raw"],
            condition="'court' in court_list_raw",
        )

        source_types = [s.source_type for s in self.persisted]
        self.assertEqual(source_types, ["chunk", "fact"])

    def test_register_source_forwards_description_on_every_branch(self):
        """Each source type passes its label to the registry, not just 'fact'.

        The description is the only human-readable handle on a shared registry
        row; dropping it leaves provenance readers with a bare UUID.
        """
        repo = MagicMock()
        cases = {
            "chunk": ("register_chunk_source", {"chunk_id": "c-1"}),
            "book_case": ("register_book_source", {"chunk_id": "c-1"}),
            "heideltime": (
                "register_heideltime_source",
                {"heideltime_event_id": uuid.uuid4()},
            ),
            "csv_human": ("register_csv_source", {"csv_row": 3}),
            "lei_corporate": ("register_lei_source", {"lei_id": "LEI-1"}),
        }
        for source_type, (method_name, extra) in cases.items():
            with self.subTest(source_type=source_type):
                repo.reset_mock()
                src = LineageSourceVO(
                    source_type=source_type,
                    celex_id="CELEX-31964D0599",
                    description="30 June 2013",
                    extraction_method="unit-test",
                    **extra,
                )
                FactIngestor._register_source(repo, src, session=MagicMock())
                called = getattr(repo, method_name)
                called.assert_called_once()
                self.assertEqual(
                    called.call_args.kwargs.get("description"),
                    "30 June 2013",
                    f"{source_type} dropped its description",
                )

    def test_explicit_lineage_sources_also_keep_predecessors(self):
        explicit = LineageSourceVO(
            source_type="book_case",
            celex_id=self.celex_id,
            chunk_id=str(uuid.uuid4()),
            extraction_method="court_list",
        )
        self.repo.select_fact4celex.return_value = [MagicMock(id=uuid.uuid4())]

        self.ingestor.save_fact(
            self.celex_id,
            [{"court": "Amsterdam District Court"}],
            lineage_sources=[explicit],
            predecessors=["court_list_raw"],
        )

        source_types = [s.source_type for s in self.persisted]
        self.assertEqual(source_types, ["book_case", "fact"])

    def test_without_predecessors_behaviour_is_unchanged(self):
        chunk_source = LineageSourceVO(
            source_type="chunk",
            celex_id=self.celex_id,
            chunk_id=str(uuid.uuid4()),
            extraction_method="court_list",
        )
        self.ingestor._derive_chunk_lineage = lambda *args, **kwargs: [chunk_source]

        self.ingestor.save_fact(
            self.celex_id, [{"court": "Amsterdam District Court"}], references_doc=[3]
        )

        self.assertEqual([s.source_type for s in self.persisted], ["chunk"])
        self.repo.select_fact4celex.assert_not_called()


class TestLineageSourceVOFactType(unittest.TestCase):
    """The 'fact' source type is guarded by the same natural-key validator."""

    def test_fact_source_requires_source_fact_id(self):
        with self.assertRaises(ValidationError):
            LineageSourceVO(
                source_type="fact",
                celex_id="CELEX-32002D0180",
                extraction_method="court_list",
            )

    def test_fact_source_requires_celex_id(self):
        with self.assertRaises(ValidationError):
            LineageSourceVO(
                source_type="fact",
                source_fact_id=uuid.uuid4(),
                extraction_method="court_list",
            )

    def test_valid_fact_source(self):
        source_fact_id = uuid.uuid4()
        vo = LineageSourceVO(
            source_type="fact",
            celex_id="CELEX-32002D0180",
            source_fact_id=source_fact_id,
            extraction_method="court_list",
            relation_type="derived_from",
        )
        self.assertEqual(vo.source_fact_id, source_fact_id)
        self.assertEqual(vo.relation_type, "derived_from")


if __name__ == '__main__':
    unittest.main()
