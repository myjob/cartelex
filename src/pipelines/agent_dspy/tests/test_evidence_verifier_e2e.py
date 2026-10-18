"""End-to-end test for :class:`EvidenceVerifierModule` against the live DB.

Unlike :mod:`test_evidence_verifier_module` (fully mocked), this exercises the
real ``ChunkRepository`` and ``FactRepository`` against ``celex_facts`` so the
deterministic entity path is verified on actual data. It writes nothing: the
evidence repository and the LLM judge are mocked, so the test only reads.

Scenario — CELEX-31982D0123 (a two-party book-trade agreement). The verifier
must conclude the ``companies_investigated`` fact deterministically (no LLM) and select a
single chunk in which BOTH undertakings co-occur:
  - Vereeniging ter Bevordering van de Belangen des Boekhandels
  - Vereeniging ter Bevordering van het Vlaamsche Boekwezen

Skipped automatically when the database is unreachable or the fixture fact is
absent, so it is safe to run in environments without the corpus.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \\
        src/pipelines/agent_dspy/tests/test_evidence_verifier_e2e.py -v
"""

import unittest
from unittest.mock import MagicMock

from pipelines.agent_dspy.evidence_verifier_module import (
    EvidenceVerifierModule,
    VerificationStatus,
    VerificationStrategy,
)
from pipelines.rag.pgvector.chunk_repository import ChunkRepository
from pipelines.rag.pgvector.fact_evidence_repository import FactEvidenceRepository
from pipelines.rag.pgvector.fact_repository import FactRepository
from pipelines.rag.pgvector.lineage_repository import LineageRepository

CELEX_ID = "CELEX-31982D0123"
FACT_NAME = "companies_investigated"
COMPANY_A = "Vereeniging ter Bevordering van de Belangen des Boekhandels"
COMPANY_B = "Vereeniging ter Bevordering van het Vlaamsche Boekwezen"


class TestEvidenceVerifierEndToEnd(unittest.TestCase):
    """Deterministic entity verification on real CELEX-31982D0123 data."""

    @classmethod
    def setUpClass(cls):
        """Connect to the corpus; skip the whole class if it is unavailable."""
        try:
            cls.fact_repo = FactRepository()
            cls.chunk_repo = ChunkRepository()
            facts = cls.fact_repo.select_fact4celex(CELEX_ID, FACT_NAME)
        except Exception as exc:  # DB down / schema missing
            raise unittest.SkipTest(f"celex_facts DB unavailable: {exc}")
        if not facts:
            raise unittest.SkipTest(
                f"fixture fact {CELEX_ID}/{FACT_NAME} not present in corpus"
            )

    def _run(self):
        """Run the verifier read-only: real reads, writes stubbed, no LLM.

        Routing reads real provenance via the live ``FactEvidenceRepository``;
        only its write methods are neutered so the test never mutates the DB.
        """
        evidence_repo = FactEvidenceRepository()
        evidence_repo.upsert_evidence = MagicMock()
        evidence_repo.set_verification_status = MagicMock()
        judge = MagicMock()
        module = EvidenceVerifierModule(
            chunk_repo=self.chunk_repo,
            lineage_repo=LineageRepository(),
            fact_repo=self.fact_repo,
            evidence_repo=evidence_repo,
            support_judge=judge,
        )
        result = module(celex_id=CELEX_ID, fact_name=FACT_NAME)
        return result, evidence_repo, judge

    def test_companies_verified_via_chunk_with_both_undertakings(self):
        result, evidence_repo, judge = self._run()

        # Deterministic entity path — the LLM judge must never be consulted.
        self.assertEqual(result.strategy, VerificationStrategy.DETERMINISTIC_ENTITY)
        judge.assert_not_called()

        # A supporting chunk was chosen and recorded in the ledger.
        self.assertIsNotNone(result.best_chunk_id)
        self.assertEqual(result.status, VerificationStatus.VERIFIED_SUPPORTED)
        evidence_repo.upsert_evidence.assert_called_once()
        self.assertEqual(
            evidence_repo.upsert_evidence.call_args.kwargs["chunk_id"],
            result.best_chunk_id,
        )

        # The delivered chunk contains BOTH undertakings.
        chunk = self.chunk_repo.get_chunk_by_id(result.best_chunk_id)
        self.assertIsNotNone(chunk)
        document = chunk["document"]
        self.assertIn(COMPANY_A, document)
        self.assertIn(COMPANY_B, document)


if __name__ == "__main__":
    unittest.main()
