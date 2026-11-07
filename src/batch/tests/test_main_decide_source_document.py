"""Tests for Main_decide_source_document (repository faked, real files and master store)."""

import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from batch import Main_decide_source_document as cli
from pipelines.decide.models import UndecidedDecision
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.rag.pgvector.source_decision_repository import CandidateRow, ExistingRow

FILLER = "\n\n" + "The Commission found that the undertakings infringed the competition rules. " * 80 + "\n\n"


def _decision_md(case: str, date: str, subject: str) -> str:
    return (f"# COMMISSION DECISION\nof {date}\nrelating to a proceeding (AT.{case} - {subject})\n"
            f"(Only the English text is authentic)\n{FILLER}HAS ADOPTED THIS DECISION:\nDone at Brussels,\n")


class FakeRepository:
    """In-memory registry: undecided decisions vanish once applied."""

    def __init__(self, decisions, existing=(), fail_on: str | None = None) -> None:
        self.decisions = list(decisions)
        self.existing = list(existing)
        self.applied: list = []
        self.fail_on = fail_on

    def load_undecided(self, ids):
        return [d for d in self.decisions if ids is None or d[0].decision_id in ids]

    def existing_sources(self):
        return self.existing

    def taken_decision_ids(self):
        return {d[0].decision_id for d in self.decisions}

    def apply(self, plan, rows, before_commit) -> None:
        before_commit()
        if plan.decision_id == self.fail_on:
            raise RuntimeError("commit lost")
        self.applied.append(plan)
        self.decisions = [d for d in self.decisions if d[0].decision_id != plan.decision_id]


class DecideCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.master_path = self.tmp / "master.json"
        self.master_path.write_text(json.dumps({"documents": {}}), encoding="utf-8")
        detect = patch.object(cli, "detect_markdown_language", return_value=("en", 0.9))
        detect.start()
        self.addCleanup(detect.stop)

    def _candidate(self, decision_id: str, doc: str, md: str) -> CandidateRow:
        path = self.tmp / decision_id / "candidates" / "mineru" / f"{doc}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        return CandidateRow(document_id=doc, decision_id=decision_id, origin="ec.europa.eu", sha256=f"sha-{doc}",
                            source_url=f"https://ec.europa.eu/{doc}.pdf", file_format="pdf",
                            local_file_pdf=str(path.parent.parent / f"{doc}.pdf"), local_file_md=str(path),
                            language=None)

    def _decisions(self):
        d1 = UndecidedDecision(decision_id="DEC-AT40135-20190516", decision_date=dt.date(2019, 5, 16),
                               case_ids=["AT.40135"], title="FOREX")
        d2 = UndecidedDecision(decision_id="DEC-AT35808-19951206", decision_date=dt.date(1995, 12, 6),
                               case_ids=["IV/35808"], title="MEDSAP")
        c1 = self._candidate(d1.decision_id, "fx-a", _decision_md("40135", "16.5.2019", "FOREX (Banana Split)"))
        c2 = self._candidate(d2.decision_id, "letter", "Dear Sirs," + FILLER + "Yours sincerely\nX\n")
        return [(d1, [c1]), (d2, [c2])]

    def _master(self) -> MasterDescriptorStore:
        return MasterDescriptorStore(self.master_path)

    def test_parse_defaults(self) -> None:
        args = cli.parse_args([])
        self.assertEqual((args.apply, args.ids), (False, None))

    def test_dry_run_writes_report_only(self) -> None:
        repo = FakeRepository(self._decisions())
        plans = cli.run(repo, self._master(), apply=False, ids=None)
        self.assertEqual(repo.applied, [])
        self.assertEqual({p.decision_id: p.verdict.value for p, _ in plans},
                         {"DEC-AT40135-20190516": "promote", "DEC-AT35808-19951206": "review"})
        report = cli.write_report(plans, self.tmp, "stamp")
        rows = list(csv.DictReader(report.open(encoding="utf-8")))
        self.assertEqual({r["document_id"]: r["verdict"] for r in rows},
                         {"fx-a": "promote", "letter": "review: letters only"})
        self.assertEqual(json.loads(self.master_path.read_text())["documents"], {})

    def test_apply_creates_master_entry_per_promotion(self) -> None:
        repo = FakeRepository(self._decisions())
        cli.run(repo, self._master(), apply=True, ids=None)
        entry = json.loads(self.master_path.read_text())["documents"]["DEC-AT40135-20190516"]
        self.assertTrue(entry["txt_md_file"].endswith("candidates/mineru/fx-a.md"))
        self.assertTrue(entry["local_file_pdf"].endswith("candidates/fx-a.pdf"))
        self.assertEqual(entry["language_init"], "en")
        self.assertIs(entry["mineru_flag"], True)
        self.assertNotIn("DEC-AT35808-19951206", json.loads(self.master_path.read_text())["documents"])

    def test_master_flush_failure_rolls_back_one_decision(self) -> None:
        decisions = self._decisions()
        d3 = UndecidedDecision(decision_id="DEC-AT39999-20200101", decision_date=dt.date(2020, 1, 1),
                               case_ids=["AT.39999"], title="Other")
        decisions.append((d3, [self._candidate(d3.decision_id, "other", _decision_md("39999", "1.1.2020", "Other"))]))
        repo = FakeRepository(decisions, fail_on="DEC-AT40135-20190516")
        master = self._master()
        with self.assertLogs("batch.Main_decide_source_document", "ERROR"):
            failures = cli.apply_plans(repo, master, cli.run(repo, master, apply=False, ids=None))
        self.assertEqual(failures, 1)
        self.assertEqual([p.decision_id for p in repo.applied], ["DEC-AT35808-19951206", "DEC-AT39999-20200101"])
        docs = json.loads(self.master_path.read_text())["documents"]
        self.assertNotIn("DEC-AT40135-20190516", docs)
        self.assertIn("DEC-AT39999-20200101", docs)

    def test_second_run_is_noop(self) -> None:
        repo = FakeRepository(self._decisions())
        cli.run(repo, self._master(), apply=True, ids=None)
        self.assertEqual(cli.run(repo, self._master(), apply=True, ids=None), [])

    def test_report_shows_registry_date_and_logs_correction(self) -> None:
        d = UndecidedDecision(decision_id="DEC-AT34174-20120611", decision_date=dt.date(2012, 6, 11),
                              case_ids=["IV/34174"], title="Sealink")
        c = self._candidate(d.decision_id, "34174_2_2", _decision_md("34174", "11.6.1992", "Sealink"))
        with self.assertLogs("batch.Main_decide_source_document", "WARNING") as logs:
            plans = cli.run(FakeRepository([(d, [c])]), self._master(), apply=False, ids=None)
        self.assertTrue(any("2012-06-11 -> 1992-06-11" in m for m in logs.output))
        rows = list(csv.DictReader(cli.write_report(plans, self.tmp, "s").open(encoding="utf-8")))
        self.assertEqual((rows[0]["registry_date"], rows[0]["doc_date"]), ("2012-06-11", "1992-06-11"))

    def test_existing_vocabulary_read_from_original(self) -> None:
        """A translated existing source is compared by its original, not its _en file."""
        original = self.tmp / "orig.md"
        original.write_text(_decision_md("40135", "16.5.2019", "FOREX (Banana Split)"), encoding="utf-8")
        english = self.tmp / "orig_en.md"
        english.write_text("translated text only", encoding="utf-8")
        existing = [ExistingRow(decision_id="AT_40135_X", document_id="AT_40135_X", sha256="other",
                                local_file_md=str(english), original_file_md=str(original), language="EN",
                                case_ids=["AT.40135"])]
        plans = cli.run(FakeRepository(self._decisions()[:1], existing), self._master(), apply=False, ids=None)
        self.assertEqual((plans[0][0].verdict.value, plans[0][0].merge_into), ("merge", "AT_40135_X"))

    def test_better_copy_switches_master_source(self) -> None:
        self.master_path.write_text(json.dumps({"documents": {"OWN": {
            "txt_md_file": "/old_en.md", "original_file_md": "/old.md", "local_file_pdf": "/old.pdf",
            "language_init": "de", "language_translated": "en"}}}), encoding="utf-8")
        own_md = self.tmp / "own.md"
        own_md.write_text(_decision_md("40135", "16.5.2019", "FOREX (Banana Split)"), encoding="utf-8")
        existing = [ExistingRow(decision_id="OWN", document_id="own", sha256="x", local_file_md=str(own_md),
                                language="DE", case_ids=["AT.40135"])]
        repo = FakeRepository(self._decisions()[:1], existing)
        cli.run(repo, self._master(), apply=True, ids=None)
        self.assertTrue(repo.applied[0].repoints[0].make_source)
        entry = json.loads(self.master_path.read_text())["documents"]["OWN"]
        self.assertTrue(entry["txt_md_file"].endswith("fx-a.md"))
        self.assertEqual((entry["language_init"], entry["language_translated"]), ("en", "NA"))
        self.assertNotIn("original_file_md", entry)

    def test_existing_source_by_sha_merges(self) -> None:
        decisions = self._decisions()[:1]
        owner_md = self.tmp / "own.md"
        owner_md.write_text(_decision_md("40135", "16.5.2019", "FOREX (Banana Split)"), encoding="utf-8")
        existing = [ExistingRow(decision_id="AT_40135_X", document_id="AT_40135_X", sha256="sha-fx-a",
                                local_file_md=str(owner_md), case_ids=["AT.40135"])]
        plans = cli.run(FakeRepository(decisions, existing), self._master(), apply=False, ids=None)
        self.assertEqual((plans[0][0].verdict.value, plans[0][0].merge_into), ("merge", "AT_40135_X"))


if __name__ == "__main__":
    unittest.main()
