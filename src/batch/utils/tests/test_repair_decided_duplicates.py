"""Unit tests for the decided-duplicates repair (pure plan building).

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest batch.utils.tests.test_repair_decided_duplicates
"""

import unittest
from unittest import mock

from batch.utils import repair_decided_duplicates as repair
from batch.utils.repair_decided_duplicates import DUPLICATES, DuplicatePair, merge_plan
from pipelines.decide.models import Verdict
from pipelines.rag.pgvector.registry_dedupe_repository import MergeStep
from pipelines.rag.pgvector.source_decision_repository import CandidateRow


def _row(doc: str) -> CandidateRow:
    return CandidateRow(document_id=doc, decision_id="D", origin="db-comp", sha256="s",
                        source_url=f"https://x/{doc}", file_format="pdf", local_file_pdf=f"/p/{doc}.pdf",
                        local_file_md=f"/p/{doc}.md", language="EN")


class MergePlanTests(unittest.TestCase):
    def test_keep_existing_source(self) -> None:
        pair = DuplicatePair(decision_id="DEC-AT39401-20091016", owner="39401_1535_1", better_copy=None)
        plan = merge_plan(pair, {"39401_1536_3": _row("39401_1536_3"), "x": _row("x")})
        self.assertEqual((plan.verdict, plan.merge_into), (Verdict.MERGE, "39401_1535_1"))
        self.assertEqual(sorted(r.document_id for r in plan.repoints), ["39401_1536_3", "x"])
        self.assertFalse(any(r.make_source for r in plan.repoints))

    def test_better_copy_becomes_source(self) -> None:
        pair = DuplicatePair(decision_id="DEC-35470-20031016", owner="35470_14_4", better_copy="dbcomp-1414")
        plan = merge_plan(pair, {"dbcomp-1414": _row("dbcomp-1414")})
        self.assertTrue(plan.repoints[0].make_source)

    def test_missing_better_copy_raises(self) -> None:
        pair = DuplicatePair(decision_id="D", owner="O", better_copy="gone")
        with self.assertRaises(KeyError):
            merge_plan(pair, {"x": _row("x")})

    def test_repair_merges_are_recorded_as_repair_step(self) -> None:
        """main() hands the repair step to the repository, so its ledger rows name the repair."""
        engine = mock.MagicMock()
        engine.connect.return_value.__enter__.return_value.execute.return_value.first.return_value = None
        with mock.patch.object(repair, "make_engine", return_value=engine), \
                mock.patch.object(repair, "MasterDescriptorStore"), \
                mock.patch.object(repair, "SourceDecisionRepository") as repository:
            self.assertEqual(repair.main([]), 0)
        repository.assert_called_once_with(engine, merge_step=MergeStep.REPAIR_DECIDED_DUPLICATES)

    def test_verified_list(self) -> None:
        """The eight reviewer-confirmed pairs; better copies per the user's rule (text length / EN original)."""
        self.assertEqual(len(DUPLICATES), 8)
        switches = {p.decision_id: p.better_copy for p in DUPLICATES if p.better_copy}
        self.assertEqual(switches, {"DEC-35470-20031016": "dbcomp-1414", "DEC-38359-20031203": "dbcomp-1241"})


if __name__ == "__main__":
    unittest.main()
