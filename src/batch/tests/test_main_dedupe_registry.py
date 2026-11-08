"""Tests of the registry de-duplication batch (fakes only, no database)."""

import datetime as dt
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from sqlalchemy.exc import OperationalError

from batch import Main_dedupe_registry as mod
from pipelines.download import dedupe_plan
from pipelines.download.descriptor_store import MasterDescriptorStore
from pipelines.rag.pgvector.registry_dedupe_repository import (
    DecisionRow,
    DocumentRow,
    MergeCounts,
    RegistryDedupeRepository,
)

URL_A = "https://eur-lex.europa.eu/a.pdf"
URL_B = "https://eur-lex.europa.eu/b.pdf"
SHA1, SHA2, SHA3 = "1" * 64, "2" * 64, "3" * 64
KEY = "33585_2_4"
INCL = "CELEX-31992D0568"


def doc(url: str, sha: str | None = None, selected: bool = True,
        local: str | None = None) -> DocumentRow:
    """Build a PDF document row."""
    return DocumentRow(document_id=url, source_url=url, file_format="pdf",
                       is_selected=selected, is_source=selected,
                       local_file_pdf=local, sha256=sha)


def dec(key: str, docs: list[DocumentRow] | None = None, cases: list[str] | None = None,
        date: dt.date | None = None, celex: str | None = None,
        audit: list[str] | None = None) -> DecisionRow:
    """Build a registry decision."""
    return DecisionRow(decision_id=key, celex_number=celex, decision_date=date,
                       case_ids=cases or [], documents=docs or [],
                       audit_urls=audit or [])


def op_error() -> OperationalError:
    """Build a database error."""
    return OperationalError("x", {}, Exception("boom"))


class FakeRepo:
    """Records merges; stands in for the database."""

    def __init__(self, fail: set[str] | None = None, rows: list[DecisionRow] | None = None,
                 referenced: set[str] | None = None, check=None) -> None:
        self.deleted: list[str] = []
        self.merged: list[tuple[str, str]] = []
        self.fail = fail or set()
        self.rows = rows or []
        self.refs = referenced or set()
        self.check = check

    def load_decisions(self, extra_ids=()) -> list[DecisionRow]:
        """Return the canned rows."""
        if self.fail == {"load"}:
            raise op_error()
        return self.rows

    def referenced(self, rows) -> set[str]:
        """Return the canned references."""
        return {r.decision_id for r in rows} & self.refs

    def merge_decision(self, decision_id: str, key: str) -> MergeCounts:
        """Record a merge or fail."""
        if self.check:
            self.check(decision_id)
        if decision_id in self.fail:
            raise op_error()
        self.deleted.append(decision_id)
        self.merged.append((decision_id, key))
        return MergeCounts(documents_repointed=1, documents_dropped=0, case_links_moved=0)


class DedupeTest(unittest.TestCase):
    """Planner, apply step and report."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.master_path = self.root / "master.json"
        self.write_master({
            KEY: {"local_file_pdf": str(self.docs / KEY / "k.pdf"),
                  "case_ids": ["IV/33585"],
                  "webDocDescriptor": {"url_pdf": URL_A, "decision_date": "1992-01-02"}},
            "OTHER_1_1": {"case_ids": ["IV/1"], "webDocDescriptor": {"url_pdf": URL_B}},
        })

    def write_master(self, documents: dict) -> None:
        """Write the master JSON."""
        self.master_path.write_text(json.dumps({"documents": documents}))

    def add_downloader_key(self) -> None:
        """Add the downloader-created master entry sharing K's URL."""
        documents = json.loads(self.master_path.read_text())["documents"]
        documents[INCL] = {"case_ids": ["IV/33585"], "webDocDescriptor": {"url_pdf": URL_A}}
        self.write_master(documents)

    def store(self) -> MasterDescriptorStore:
        """Load the master store."""
        return MasterDescriptorStore(self.master_path)

    def master_decisions(self) -> list[DecisionRow]:
        """Registry rows of the master keys."""
        return [dec(KEY, [doc(URL_A, SHA1)]), dec("OTHER_1_1", [doc(URL_B, SHA2)])]

    def plan(self, decisions, include=None, referenced=lambda _r: set()):
        """Build a plan over the master decisions plus the given ones."""
        return dedupe_plan.build_plan(self.master_decisions() + decisions, self.store(),
                                      self.docs, include or [], referenced)

    def make_folder(self, key: str, name: str = "x.pdf") -> Path:
        """Create a decision folder with one file."""
        folder = self.docs / key
        folder.mkdir()
        (folder / name).write_text("data")
        return folder

    def quarantined(self, key: str, run: str = "T") -> Path:
        """Path of a quarantined decision folder."""
        return self.docs / mod.QUARANTINE_DIR / run / key

    def apply(self, plan, repo, run: str = "T"):
        """Apply a plan with a fresh store."""
        return mod.apply_plan(plan, repo, self.store(), self.docs, run)

    def test_dry_run_plans_without_side_effects(self) -> None:
        folder = self.make_folder("DEC-1")
        before = self.master_path.read_text()
        plan = self.plan([dec("DEC-1", [doc("https://x/other.pdf", SHA1)])])
        self.assertEqual([(i.decision_id, i.duplicate_of, i.signal, i.action, i.files_moved,
                           i.outcome) for i in plan],
                         [("DEC-1", KEY, "sha", "delete", 1, "planned")])
        report = mod.write_report(plan, self.root / "rep", "T")
        self.assertIn("outcome", report.read_text().splitlines()[0])
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.master_path.read_text(), before)
        self.assertEqual(list(self.root.glob("*.bak*")), [])

    def test_apply_order_master_then_folder_then_db(self) -> None:
        self.add_downloader_key()
        self.make_folder(INCL)
        seen: list[tuple[bool, bool]] = []

        def check(_key: str) -> None:
            gone = INCL not in json.loads(self.master_path.read_text())["documents"]
            seen.append((gone, not (self.docs / INCL).exists()))

        plan = self.plan([dec(INCL, [doc(URL_A)], celex="31992D0568")], [INCL])
        repo = FakeRepo(check=check)
        done = self.apply(plan, repo)
        self.assertEqual(seen, [(True, True)])
        self.assertEqual([i.outcome for i in done], ["done"])
        self.assertTrue((self.quarantined(INCL) / "x.pdf").is_file())
        self.assertEqual(len(list(self.root.glob("master.json.bak-*"))), 1)

    def test_db_failure_is_rerunnable(self) -> None:
        self.add_downloader_key()
        self.make_folder(INCL)
        rows = [dec(INCL, [doc(URL_A)], celex="31992D0568")]
        done = self.apply(self.plan(rows, [INCL]), FakeRepo({INCL}))
        self.assertEqual([i.outcome for i in done], ["db_failed"])
        replan = self.plan(rows, [INCL])
        self.assertEqual([(i.action, i.master_entry_removed) for i in replan],
                         [("delete", False)])
        repo = FakeRepo()
        self.assertEqual([i.outcome for i in self.apply(replan, repo)], ["done"])
        self.assertEqual(repo.deleted, [INCL])

    def test_move_failure_keeps_rows_and_rerun_resumes(self) -> None:
        self.make_folder("DEC-1")
        self.quarantined("DEC-1").mkdir(parents=True)
        plan = self.plan([dec("DEC-1", [doc("https://x/o.pdf", SHA1)])])
        repo = FakeRepo()
        self.assertEqual([i.outcome for i in self.apply(plan, repo)], ["move_failed"])
        self.assertEqual(repo.deleted, [])
        self.assertEqual([i.outcome for i in self.apply(plan, repo, "T2")], ["done"])
        self.assertEqual(repo.deleted, ["DEC-1"])

    def test_master_key_excluded_from_own_index(self) -> None:
        self.add_downloader_key()
        d = dec(INCL, [doc("https://x/own.pdf")], cases=["IV/33585"], celex="31992D0568")
        self.assertEqual(self.plan([d]), [])
        d = dec(INCL, [doc("https://x/own.pdf")], audit=[URL_A])
        plan = self.plan([d], [INCL])
        self.assertEqual([(i.duplicate_of, i.signal, i.action) for i in plan],
                         [(KEY, "identity:url", "delete")])
        self.assertTrue(plan[0].master_entry_removed)

    def test_included_key_without_registry_row_removed(self) -> None:
        self.add_downloader_key()
        self.make_folder(INCL)
        plan = self.plan([dec(INCL, audit=[URL_A])], [INCL])
        self.apply(plan, FakeRepo())
        self.assertNotIn(INCL, json.loads(self.master_path.read_text())["documents"])
        self.assertTrue(self.quarantined(INCL).is_dir())

    def test_sha_of_own_selected_document(self) -> None:
        plan = self.plan([dec("D1", [doc("https://x/1.pdf", SHA2)]),
                          dec("D2", [doc("https://x/2.pdf", SHA1)], audit=[URL_A])])
        self.assertEqual([(i.decision_id, i.duplicate_of, i.signal) for i in plan],
                         [("D1", "OTHER_1_1", "sha"), ("D2", KEY, "sha+identity:url")])

    def test_sha_via_copy_only_is_review(self) -> None:
        d = dec("D40049", [doc("https://x/own.pdf", SHA3),
                           doc("https://db-comp.eu/c.pdf", SHA1, selected=False)])
        plan = self.plan([d])
        self.assertEqual([(i.action, i.signal, i.duplicate_of) for i in plan],
                         [("review", "sha-copy", KEY)])

    def test_case_date_only_is_review(self) -> None:
        d = dec("D1", [doc("https://x/z.pdf", SHA3)], cases=["IV/33585"],
                date=dt.date(1992, 1, 2))
        plan = self.plan([d])
        self.assertEqual([(i.action, i.signal, i.duplicate_of) for i in plan],
                         [("review", "identity:case+date", KEY)])
        repo = FakeRepo()
        self.apply(plan, repo)
        self.assertEqual(repo.deleted, [])

    def test_conflicting_signals_no_action(self) -> None:
        plan = self.plan([dec("D1", [doc(URL_A, SHA2)])])
        self.assertEqual([(i.action, i.duplicate_of) for i in plan],
                         [("conflict", f"{KEY}|OTHER_1_1")])
        repo = FakeRepo()
        self.apply(plan, repo)
        self.assertEqual(repo.deleted, [])

    def test_ambiguous_no_action(self) -> None:
        plan = self.plan([dec("D1", [doc(URL_A), doc(URL_B)])])
        self.assertEqual([i.action for i in plan], ["ambiguous"])
        repo = FakeRepo()
        self.apply(plan, repo)
        self.assertEqual(repo.deleted, [])

    def test_refuses_folder_holding_master_file(self) -> None:
        folder = self.docs / "DEC-1"
        folder.mkdir()
        (folder / "k.pdf").write_text("x")
        k_doc = doc(URL_A, SHA1, local=str(folder / "k.pdf"))
        decisions = [dec(KEY, [k_doc]), dec("DEC-1", [doc("https://x/o.pdf", SHA1)])]
        plan = dedupe_plan.build_plan(decisions, self.store(), self.docs, [])
        self.assertEqual([(i.action, i.outcome) for i in plan], [("refused", "refused")])
        repo = FakeRepo()
        self.apply(plan, repo)
        self.assertEqual(repo.deleted, [])
        self.assertTrue(folder.is_dir())

    def test_refuses_referenced_and_unsafe_ids(self) -> None:
        plan = self.plan([dec("DEC-1", [doc("https://x/o.pdf", SHA1)]),
                          dec("../evil", [doc("https://x/e.pdf", SHA1)])],
                         referenced=lambda rows: {r.decision_id for r in rows})
        self.assertEqual({i.decision_id: i.action for i in plan},
                         {"DEC-1": "refused", "../evil": "refused"})

    def test_non_duplicates_untouched_and_apply_noop(self) -> None:
        before = self.master_path.read_text()
        plan = self.plan([dec("D9", [doc("https://x/new.pdf", SHA3)], cases=["IV/9"])])
        self.assertEqual(plan, [])
        repo = FakeRepo()
        self.assertEqual(self.apply(plan, repo), [])
        self.assertEqual(self.master_path.read_text(), before)
        self.assertEqual(list(self.root.glob("*.bak*")), [])

    def test_url_match_with_differing_date_is_review(self) -> None:
        d = dec("D1", [doc(URL_A, SHA3)], date=dt.date(2001, 5, 5))
        plan = self.plan([d])
        self.assertEqual([(i.action, i.signal, i.duplicate_of) for i in plan],
                         [("review", "url-date", KEY)])
        repo = FakeRepo()
        self.apply(plan, repo)
        self.assertEqual(repo.deleted, [])

    def test_sha_with_case_date_is_combined_signal(self) -> None:
        d = dec("D1", [doc("https://x/z.pdf", SHA1)], cases=["IV/33585"],
                date=dt.date(1992, 1, 2))
        self.assertEqual([(i.action, i.signal) for i in self.plan([d])],
                         [("delete", "sha+identity:case+date")])

    def test_plan_counts_repointed_documents_and_new_links(self) -> None:
        d = dec("D1", [doc("https://x/o.pdf", SHA1), doc("https://x/p.pdf", selected=False)],
                cases=["IV/33585", "AT.35473"])
        plan = self.plan([d])
        self.assertEqual([(i.action, i.documents_repointed, i.case_links_moved)
                          for i in plan], [("delete", 2, 2)])
        done = self.apply(plan, FakeRepo())
        self.assertEqual(done[0].outcome, "done")
        report = mod.write_report(done, self.root / "rep", "T").read_text()
        self.assertIn("documents_repointed,case_links_moved", report.splitlines()[0])

    def test_refused_when_key_has_no_registry_row(self) -> None:
        decisions = [dec("OTHER_1_1", [doc(URL_B, SHA2)]),
                     dec("D1", [doc("https://x/o.pdf")], audit=[URL_A])]
        plan = dedupe_plan.build_plan(decisions, self.store(), self.docs, [])
        self.assertEqual([(i.action, i.duplicate_of) for i in plan], [("refused", KEY)])

    def test_apply_merges_into_key(self) -> None:
        plan = self.plan([dec("DEC-1", [doc("https://x/o.pdf", SHA1)])])
        repo = FakeRepo()
        self.apply(plan, repo)
        self.assertEqual(repo.merged, [("DEC-1", KEY)])


class StoreRemoveTest(unittest.TestCase):
    """MasterDescriptorStore.remove."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "m.json"
        self.write({"A": {"x": 1}, "B": {"x": 2}, "C": {"x": 3}})

    def write(self, docs: dict) -> None:
        """Write the master JSON."""
        self.path.write_text(json.dumps({"documents": docs}))

    def test_remove_flush_backs_up_once(self) -> None:
        store = MasterDescriptorStore(self.path)
        self.assertTrue(store.remove("A"))
        self.assertFalse(store.remove("A"))
        store.flush()
        self.assertEqual(sorted(json.loads(self.path.read_text())["documents"]), ["B", "C"])
        self.assertEqual(len(list(self.path.parent.glob("m.json.bak-*"))), 1)

    def test_remove_survives_stale_reload(self) -> None:
        store = MasterDescriptorStore(self.path)
        store.remove("A")
        self.write({"A": {"x": 1}, "B": {"x": 2}, "C": {"x": 3}, "D": {"x": 4}})
        store.flush()
        self.assertEqual(sorted(json.loads(self.path.read_text())["documents"]),
                         ["B", "C", "D"])


class MainTest(unittest.TestCase):
    """main() wiring and exit codes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "docs" / "DEC-1").mkdir(parents=True)
        self.master = self.root / "m.json"
        self.master.write_text(json.dumps({"documents": {
            KEY: {"webDocDescriptor": {"url_pdf": URL_A}}}}))
        self.rows = [dec(KEY, [doc(URL_A, SHA1)]),
                     dec("DEC-1", [doc("https://x/o.pdf", SHA1)])]

    def run_main(self, argv, repo, probe=None) -> int:
        """Run main with fakes at the boundaries."""
        engine = mock.Mock()
        patches = [
            mock.patch.object(mod, "make_engine", return_value=engine),
            mock.patch.object(mod, "DocumentRegistryRepository",
                              side_effect=probe, create=False),
            mock.patch.object(mod, "RegistryDedupeRepository", return_value=repo),
            mock.patch.object(mod.settings, "CELEX_DOCS_DIR", str(self.root / "docs")),
            mock.patch.object(mod.settings, "REPORT_PATH", str(self.root / "rep")),
            mock.patch.object(mod.settings, "MASTER_DOC_DESCRIPTORS_PATH", str(self.master)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return mod.main(argv)

    def test_dry_run_never_writes(self) -> None:
        repo = FakeRepo(rows=self.rows)
        before = self.master.read_text()
        self.assertEqual(self.run_main([], repo), 0)
        self.assertEqual(repo.deleted, [])
        self.assertTrue((self.root / "docs" / "DEC-1").is_dir())
        self.assertEqual(self.master.read_text(), before)
        self.assertEqual(len(list((self.root / "rep").glob("dedupe_registry_*.csv"))), 1)

    def test_apply_deletes(self) -> None:
        repo = FakeRepo(rows=self.rows)
        self.assertEqual(self.run_main(["--apply"], repo), 0)
        self.assertEqual(repo.deleted, ["DEC-1"])

    def test_apply_failure_exits_1(self) -> None:
        repo = FakeRepo({"DEC-1"}, rows=self.rows)
        self.assertEqual(self.run_main(["--apply"], repo), 1)

    def test_not_migrated_exits_2(self) -> None:
        self.assertEqual(self.run_main([], FakeRepo(), probe=RuntimeError("missing")), 2)

    def test_db_down_exits_1(self) -> None:
        def probe(_engine):
            raise RuntimeError("x") from op_error()

        self.assertEqual(self.run_main([], FakeRepo(), probe=probe), 1)
        self.assertEqual(self.run_main([], FakeRepo({"load"}), probe=None), 1)

    def test_usage_error_exits_2(self) -> None:
        self.assertEqual(self.run_main(["--nope"], FakeRepo()), 2)


class _Result:
    """Canned statement result."""

    def __init__(self, rows: list[tuple] | None = None, rowcount: int = 1) -> None:
        self.rows = rows or []
        self.rowcount = rowcount

    def first(self):
        """First row or None."""
        return self.rows[0] if self.rows else None

    def fetchall(self):
        """All rows."""
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _Conn:
    """Connection answering by statement prefix; records (sql, params)."""

    def __init__(self, log: list[tuple[str, dict]], answers) -> None:
        self.log = log
        self.answers = answers

    def execute(self, stmt, params=None):
        """Record the statement and answer it."""
        sql = " ".join(str(stmt).split())
        self.log.append((sql, params or {}))
        return self.answers(sql, params or {})

    def execution_options(self, **_kw):
        """Ignore options."""
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Engine:
    def __init__(self, answers=lambda _s, _p: _Result()) -> None:
        self.log: list[tuple[str, dict]] = []
        self.begins = 0
        self.answers = answers

    @contextmanager
    def begin(self):
        """Count transactions."""
        self.begins += 1
        yield _Conn(self.log, self.answers)

    def connect(self):
        """A read connection."""
        return _Conn(self.log, self.answers)


class RepositoryTest(unittest.TestCase):
    """The merge runs in one transaction; references check both columns."""

    def merge_answers(self, sql: str, params: dict) -> _Result:
        """Answer the merge statements: K exists, D has an eur-lex and an ec row."""
        if sql.startswith("SELECT 1 FROM celex_facts.decision_registry"):
            return _Result([(1,)])
        if sql.startswith("SELECT document_id, source_url, file_format"):
            return _Result([
                ("DEC-1_EN", "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/"
                             "?uri=CELEX:31992D0568", "pdf"),
                ("AT_1_2", "https://ec.europa.eu/x/AT_1_2.pdf", "pdf"),
                ("DEC-1_DE", "https://eur-lex.europa.eu/legal-content/DE/TXT/PDF/"
                             "?uri=CELEX:31992D0568", "pdf"),
            ])
        if sql.startswith("SELECT 1 FROM celex_facts.document_registry"):
            return _Result([(1,)] if params["i"] == f"{KEY}_DE" else [])
        if sql.startswith("INSERT INTO celex_facts.competition_case_decision"):
            return _Result(rowcount=2)
        return _Result()

    def test_merge_repoints_moves_links_then_deletes_in_one_transaction(self) -> None:
        engine = _Engine(self.merge_answers)
        counts = RegistryDedupeRepository(engine).merge_decision("DEC-1", KEY)
        self.assertEqual(engine.begins, 1)
        self.assertEqual((counts.documents_repointed, counts.documents_dropped,
                          counts.case_links_moved), (2, 1, 2))
        updates = [p for s, p in engine.log if s.startswith("UPDATE")]
        self.assertEqual([(p["old"], p["new"], p["k"]) for p in updates],
                         [("DEC-1_EN", f"{KEY}_EN", KEY), ("AT_1_2", "AT_1_2", KEY)])
        update_sql = next(s for s, _ in engine.log if s.startswith("UPDATE"))
        for part in ("is_selected = false", "is_source = false", "local_file_pdf = NULL"):
            self.assertIn(part, update_sql)
        dropped = [p for s, p in engine.log if s.startswith("DELETE FROM celex_facts.document")]
        self.assertEqual([p["old"] for p in dropped], ["DEC-1_DE"])
        verbs = [s.split()[0] + " " + s.split()[2].rsplit(".", 1)[-1]
                 for s, _ in engine.log if s.split()[0] in ("INSERT", "DELETE")]
        self.assertEqual(verbs[-4:], ["INSERT competition_case_decision",
                                      "DELETE competition_case_decision",
                                      "DELETE decision_registry",
                                      "INSERT decision_merge"])
        self.assertIn("ON CONFLICT (case_id, decision_id) DO NOTHING",
                      next(s for s, _ in engine.log if s.startswith("INSERT")))

    def test_merge_records_lineage_in_the_merge_transaction(self) -> None:
        engine = _Engine(self.merge_answers)
        RegistryDedupeRepository(engine).merge_decision("DEC-1", KEY)
        self.assertEqual(engine.begins, 1)
        ledger = [p for s, p in engine.log if s.startswith("INSERT INTO celex_facts.decision_merge")]
        self.assertEqual(ledger, [{"d": "DEC-1", "k": KEY, "by": "dedupe_registry"}])

    def test_merge_without_key_row_records_no_lineage(self) -> None:
        engine = _Engine(lambda _s, _p: _Result())
        with self.assertRaises(ValueError):
            RegistryDedupeRepository(engine).merge_decision("DEC-1", KEY)
        self.assertFalse(any("decision_merge" in s for s, _ in engine.log))

    def test_merge_refuses_missing_key_row(self) -> None:
        engine = _Engine(lambda _s, _p: _Result())
        with self.assertRaises(ValueError):
            RegistryDedupeRepository(engine).merge_decision("DEC-1", KEY)
        self.assertFalse(any(s.startswith(("UPDATE", "DELETE")) for s, _ in engine.log))

    def test_referenced_checks_celex_id_and_document_id(self) -> None:
        def answers(sql: str, params: dict) -> _Result:
            if "information_schema" in sql:
                return _Result([(1,)])
            if sql.startswith("SELECT DISTINCT celex_id FROM celex_facts.facts"):
                return _Result([("D1",)])
            if sql.startswith("SELECT DISTINCT document_id FROM celex_facts.pg_embedding"):
                return _Result([("d2doc",)])
            return _Result()

        rows = [dec("D1", [doc("d1doc")]), dec("D2", [doc("d2doc")]), dec("D3")]
        engine = _Engine(answers)
        self.assertEqual(RegistryDedupeRepository(engine).referenced(rows), {"D1", "D2"})
        sqls = [s for s, _ in engine.log]
        self.assertEqual(sum("celex_id = ANY" in s for s in sqls), 2)
        self.assertEqual(sum("document_id = ANY" in s for s in sqls), 2)


if __name__ == "__main__":
    unittest.main()
