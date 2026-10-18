"""Real, no-mock benchmark for the Master Planner verify stage.

Reproduces the "Verifying contributing facts" stall reported in the UI for the
query "List all cases where Bayer AG was part of a cartel" and proves the
fact-name scoping fix cuts the verify fan-out (and the LLM-judge calls inside
it) by more than an order of magnitude.

This wires the REAL ``FactRepository``, ``ChunkRepository`` and
``EvidenceVerifierModule`` against the live ``celex_facts`` corpus and the real
DSPy LM. It is read-only: the evidence repository's two write methods are
stubbed so nothing is persisted. The DSPy planner predictors are not involved -
the stage methods are driven with the same structured ``QueryAnalysis`` /
``MasterPlan`` the planner would emit for this query.

Because it needs the corpus, the LLM host, and is slow, it is opt-in:

    MASTER_PLANNER_PERF=1 PYTHONPATH=src .venv/bin/python -m unittest \\
        pipelines.agent_dspy.tests.test_master_planner_verify_perf -v

It is skipped automatically when ``MASTER_PLANNER_PERF`` is unset or the corpus
is unavailable, so it never blocks the normal unit run.
"""

import os
import time
import unittest

from config.llm_thinking import thinking_kwargs
from config.settings import settings
from pipelines.agent_dspy.master_planner_module import MasterPlannerModule
from pipelines.agent_dspy.master_planner_signature import (
    MasterPlan,
    QueryAnalysis,
    ResolvedEntity,
    SubTask,
)

# Entity facts the "company" concept resolves to - all deterministic-entity
# strategy (no LLM judge). The scoped verify set must stay within this set so
# the per-candidate LLM-judge storm never fires for a membership case list.
_ENTITY_FACTS = frozenset(
    {
        "companies_investigated",
        "companies_country",
        "companies_address",
        "companies_industry",
    }
)


@unittest.skipUnless(
    os.getenv("MASTER_PLANNER_PERF") == "1",
    "perf benchmark; set MASTER_PLANNER_PERF=1 to run (needs corpus + LLM)",
)
class TestMasterPlannerVerifyPerf(unittest.TestCase):
    """Verify-stage fan-out on real Bayer cartel data, scoped vs unscoped."""

    @classmethod
    def setUpClass(cls) -> None:
        """Wire the real repositories + LLM; skip the class if unavailable."""
        try:
            import dspy

            from pipelines.rag.pgvector.chunk_repository import ChunkRepository
            from pipelines.rag.pgvector.fact_evidence_repository import (
                FactEvidenceRepository,
            )
            from pipelines.rag.pgvector.fact_repository import FactRepository
            from pipelines.rag.pgvector.lineage_repository import LineageRepository
            from pipelines.agent_dspy.evidence_verifier_module import (
                EvidenceVerifierModule,
            )
            from sqlalchemy import text

            model_name = f"openai/{settings.LLM_MODEL_ID}"
            dspy.configure(
                lm=dspy.LM(
                    model_name,
                    api_base=f"{settings.OLLAMA_HOST}/v1",
                    api_key="ollama",
                    cache=False,
                    **thinking_kwargs(model_name),
                )
            )
            cls.fact_repo = FactRepository()
            chunk_repo = ChunkRepository()
            evidence_repo = FactEvidenceRepository(engine=cls.fact_repo.engine)
            lineage_repo = LineageRepository(engine=cls.fact_repo.engine)
            # Read-only: never mutate the corpus from a benchmark.
            evidence_repo.upsert_evidence = lambda **kwargs: None
            evidence_repo.set_verification_status = lambda *args, **kwargs: None

            verifier = EvidenceVerifierModule(
                chunk_repo=chunk_repo,
                lineage_repo=lineage_repo,
                fact_repo=cls.fact_repo,
                evidence_repo=evidence_repo,
            )
            cls.module = MasterPlannerModule(
                sql_store=cls.fact_repo, dispatcher=object(), verifier=verifier
            )

            with cls.fact_repo.engine.connect() as conn:
                cls.cases = [
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT DISTINCT celex_id FROM celex_facts.facts "
                            "WHERE name = 'companies_investigated' "
                            "AND value_md ILIKE '%Bayer%' LIMIT :n"
                        ),
                        {"n": settings.MASTER_PLANNER_MAX_CASES},
                    ).fetchall()
                ]
        except Exception as exc:  # DB / LLM / corpus unavailable
            raise unittest.SkipTest(f"corpus or LLM unavailable: {exc}")
        if len(cls.cases) < 3:
            raise unittest.SkipTest("too few Bayer cases in corpus to benchmark")

    @staticmethod
    def _bayer_plan() -> tuple[QueryAnalysis, MasterPlan, list[SubTask]]:
        """The analysis / plan the planner emits for the Bayer membership query."""
        analysis = QueryAnalysis(
            entities=[
                ResolvedEntity(
                    surface="Bayer AG",
                    entity_type="company",
                    available_in=["fact", "chunk"],
                    candidate_fact_names=["companies_investigated"],
                )
            ],
            target_type="case_list",
            aggregation="none",
            requires_statistics=False,
            rationale="entity-only case list",
        )
        subtasks = [
            SubTask(
                task_id=1,
                kind="retrieve",
                tool="doc_search",
                query="Bayer AG cartel",
                rationale="candidate cases",
            )
        ]
        plan = MasterPlan(supported=True, target_type="case_list", subtasks=subtasks)
        return analysis, plan, subtasks

    def test_scoping_collapses_verify_fanout(self) -> None:
        """Scoped verify set is a small, membership-only fraction of unscoped."""
        analysis, _plan, subtasks = self._bayer_plan()
        cases_df = self.module._build_case_table(self.cases)
        self.assertFalse(cases_df.is_empty(), "no facts loaded for Bayer cases")

        unscoped = cases_df.select(["celex_id", "fact_name"]).unique().height
        scoped_df = self.module._scope_to_relevant_facts(cases_df, analysis, subtasks)
        scoped = scoped_df.select(["celex_id", "fact_name"]).unique().height

        print(
            f"\n[verify fan-out] cases={len(self.cases)} "
            f"unscoped_rows={unscoped} scoped_rows={scoped}"
        )

        # The unscoped path is the pathology: ~60 facts per case, far past the
        # runaway cap, hundreds of them concept facts that hit the LLM judge.
        self.assertGreater(unscoped, 5 * len(self.cases))
        self.assertGreater(unscoped, settings.MASTER_PLANNER_MAX_VERIFY_ROWS)
        # Scoping collapses it by more than 4x, to entity-only facts (no LLM).
        self.assertLess(scoped, unscoped / 4)
        self.assertTrue(
            set(scoped_df["fact_name"].unique().to_list()).issubset(_ENTITY_FACTS),
            f"scoped facts must be entity-only, got "
            f"{scoped_df['fact_name'].unique().to_list()}",
        )
        # And the scoped set stays within the runaway cap, so the LLM-judge
        # storm that stalled the stage for minutes can no longer fire.
        self.assertLessEqual(scoped, settings.MASTER_PLANNER_MAX_VERIFY_ROWS)

    def test_scoped_verify_runs_fast_and_llm_free(self) -> None:
        """Verifying the scoped membership rows completes quickly (deterministic)."""
        analysis, _plan, subtasks = self._bayer_plan()
        cases_df = self.module._build_case_table(self.cases)
        scoped_df = self.module._scope_to_relevant_facts(cases_df, analysis, subtasks)

        start = time.perf_counter()
        kept = self.module._verify_rows(scoped_df)
        elapsed = time.perf_counter() - start

        rows = scoped_df.select(["celex_id", "fact_name"]).unique().height
        print(
            f"\n[scoped verify] {rows} membership rows verified in {elapsed:.1f}s "
            f"({kept.height} kept)"
        )
        # Membership facts route to the deterministic entity strategy (no LLM);
        # the whole scoped set must finish well under the multi-minute stall.
        self.assertLess(elapsed, 120.0)


if __name__ == "__main__":
    unittest.main()
