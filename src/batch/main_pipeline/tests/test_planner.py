"""Tests for the staleness planner of the main pipeline.

``build_plan`` is pure given its repository, so a fake repository drives every
trigger rule of the design; one live class checks the SQL against the real
schema and is skipped when the DB is unreachable.
"""

import unittest
from datetime import datetime

from batch.main_pipeline.models import Chapter
from batch.main_pipeline.planner import GRAPH_INPUT_FACTS, build_plan, fillable_gaps

_NOW = datetime(2026, 11, 8, 20, 0, 0)

_STRUCTS = [
    {"target_fact_name": "case_title", "question": "q", "output_schema": ""},
    {"target_fact_name": "companies_investigated", "question": "q", "output_schema": ""},
    {"target_fact_name": "products", "question": "q", "output_schema": ""},
    {"target_fact_name": "NACE_group_code", "question": "q", "output_schema": "", "predecessors": ["products"]},
    {"target_fact_name": "NACE_class_code", "question": "q", "output_schema": "", "predecessors": ["products", "NACE_group_code"]},
    {"target_fact_name": "OECD_sector", "question": "q", "output_schema": "", "predecessors": ["products"]},
    {
        "target_fact_name": "case_summary",
        "question": "q",
        "output_schema": "",
        "pipeline": "case_summary",
        "context_facts": ["case_title", "products", "NACE_class_code", "companies_investigated"],
    },
]


class FakeRepo:
    """In-memory stand-in for PlannerRepository."""

    def __init__(self, **kw: object) -> None:
        self.cases = kw.get("cases", {"C1", "C2", "C3"})
        self.stale = kw.get("stale", {})
        self.new_docs = kw.get("new_docs", set())
        self.changes = kw.get("changes", (set(), set()))
        self.chunk_age = kw.get("chunk_age", set())
        self.durations = kw.get("durations", {})

    def all_cases(self) -> set[str]:
        return set(self.cases)

    def stale_facts(self, fact_names: list[str], predecessors: dict[str, list[str]]) -> dict[str, set[str]]:
        return {f: set(c) for f, c in self.stale.items() if f in fact_names}

    def new_documents_since(self, ts: datetime | None) -> set[str]:
        return set(self.new_docs)

    def loader_changes(self, rows: dict[str, set[str]]) -> tuple[set[str], dict[str, str]]:
        added, changed = self.changes
        return set(added), dict(changed) if isinstance(changed, dict) else {i: i for i in changed}

    def chunks_newer_than_facts(self) -> set[str]:
        return set(self.chunk_age)

    def mean_duration_s(self, fact_names: list[str]) -> dict[str, float]:
        return {f: self.durations.get(f, 1.0) for f in fact_names}


def _plan(repo: FakeRepo, **kw: object):
    args = {
        "refresh_facts": set(),
        "completeness_gaps": {},
        "loader": {},
        "loader_file": "/x/filtered_final_manifest.xlsx",
        "last_done_at": None,
        "only": None,
        "now": _NOW,
        "chunk_age_trigger": False,
    }
    args.update(kw)
    return build_plan(repo, _STRUCTS, **args)


def _steps(plan, chapter: Chapter) -> list:
    return [s for s in plan.steps if s.chapter == chapter]


class TestBuildPlan(unittest.TestCase):
    """Trigger rules of the design, one per test."""

    def test_empty_plan_is_empty(self) -> None:
        plan = _plan(FakeRepo())
        self.assertTrue(plan.is_empty)
        self.assertEqual(plan.run_id, "20261108200000")

    def test_products_refresh_plans_successors_and_summary(self) -> None:
        plan = _plan(FakeRepo(), refresh_facts={"products"})
        (facts,) = _steps(plan, Chapter.FACTS)
        self.assertEqual(set(facts.force_facts), {"products", "NACE_group_code", "NACE_class_code", "OECD_sector"})
        self.assertNotIn("case_summary", facts.force_facts)
        self.assertEqual(sorted(facts.case_ids), ["C1", "C2", "C3"])
        (summary,) = _steps(plan, Chapter.SUMMARIES)
        self.assertEqual(summary.extra_args, ["--filter-fact", "case_summary"])
        self.assertEqual(summary.force_facts, [])
        self.assertTrue(_steps(plan, Chapter.REPORT))
        self.assertEqual(_steps(plan, Chapter.ENTITY_GRAPH), [])

    def test_stale_fact_groups_cases_by_fact_set(self) -> None:
        repo = FakeRepo(stale={"OECD_sector": {"C1"}, "NACE_class_code": {"C1", "C2"}})
        steps = _steps(_plan(repo), Chapter.FACTS)
        by_cases = {tuple(s.case_ids): set(s.force_facts) for s in steps}
        self.assertEqual(by_cases, {("C1",): {"OECD_sector", "NACE_class_code"}, ("C2",): {"NACE_class_code"}})

    def test_new_document_runs_all_chapters(self) -> None:
        plan = _plan(FakeRepo(changes=({"D9"}, set())))
        chapters = {s.chapter for s in plan.steps}
        self.assertEqual(chapters, set(Chapter) - {Chapter.PREREQUISITES})
        (download,) = _steps(plan, Chapter.DOWNLOAD)
        self.assertEqual(download.case_ids, ["D9"])
        self.assertEqual(download.extra_args[:2], ["--loader-file", "/x/filtered_final_manifest.xlsx"])
        prep = [s.name for s in _steps(plan, Chapter.DOCUMENT_PREPARATION)]
        self.assertEqual(prep, ["convert", "decide", "translate", "embed", "classify", "enrich"])
        (facts,) = _steps(plan, Chapter.FACTS)
        self.assertEqual((facts.case_ids, facts.force_facts), (["D9"], []))
        self.assertEqual(set(facts.select_facts), {s["target_fact_name"] for s in _STRUCTS} - {"case_summary"})

    def test_download_step_uses_loader_ids_later_chapters_use_keys(self) -> None:
        plan = _plan(FakeRepo(changes=(set(), {"DEC-AT32450-20040430": "32450_35_6"})))
        (download,) = _steps(plan, Chapter.DOWNLOAD)
        self.assertEqual(download.case_ids, ["DEC-AT32450-20040430"])
        convert = [s for s in _steps(plan, Chapter.DOCUMENT_PREPARATION) if s.name == "convert"]
        self.assertEqual(convert[0].case_ids, ["32450_35_6"])

    def test_added_row_uses_the_downloaders_new_key(self) -> None:
        from batch.main_pipeline.planner import LoaderRow

        loader = {"DEC-X-20260101": LoaderRow(urls={"http://x/a.pdf"}, new_key="CELEX-32026D0001")}
        plan = _plan(FakeRepo(changes=({"DEC-X-20260101"}, {})), loader=loader)
        self.assertEqual(_steps(plan, Chapter.DOWNLOAD)[0].case_ids, ["DEC-X-20260101"])
        self.assertEqual(_steps(plan, Chapter.DOCUMENT_PREPARATION)[0].case_ids, ["CELEX-32026D0001"])

    def test_url_change_adds_download_with_force(self) -> None:
        plan = _plan(FakeRepo(changes=(set(), {"C2"})))
        (download,) = _steps(plan, Chapter.DOWNLOAD)
        self.assertIn("--force", download.extra_args)
        self.assertEqual(download.case_ids, ["C2"])

    def test_url_change_forces_document_preparation(self) -> None:
        plan = _plan(FakeRepo(changes=({"D9"}, {"C2"})))
        prep = _steps(plan, Chapter.DOCUMENT_PREPARATION)
        forced = {s.name: s.case_ids for s in prep if "--mode" in s.extra_args}
        self.assertEqual(forced, {"convert": ["C2"], "translate": ["C2"], "embed": ["C2"]})
        lazy_convert = next(s for s in prep if s.name == "convert" and "--mode" not in s.extra_args)
        self.assertEqual(lazy_convert.case_ids, ["D9"])
        self.assertTrue(all(s.extra_args[-2:] == ["--mode", "force"] for s in prep if "--mode" in s.extra_args))

    def test_url_change_forces_all_facts_but_summary(self) -> None:
        plan = _plan(FakeRepo(changes=(set(), {"C2"})))
        (facts,) = _steps(plan, Chapter.FACTS)
        self.assertEqual(facts.case_ids, ["C2"])
        self.assertEqual(len(facts.force_facts), len(_STRUCTS) - 1)
        self.assertEqual(_steps(plan, Chapter.SUMMARIES)[0].case_ids, ["C2"])

    def test_document_downloaded_outside_runs_preparation(self) -> None:
        plan = _plan(FakeRepo(new_docs={"D7"}))
        self.assertEqual(_steps(plan, Chapter.DOWNLOAD), [])
        self.assertEqual(_steps(plan, Chapter.DOCUMENT_PREPARATION)[0].case_ids, ["D7"])

    def test_completeness_gap_runs_unforced(self) -> None:
        (facts,) = _steps(_plan(FakeRepo(), completeness_gaps={"OECD_sector": {"C3"}}), Chapter.FACTS)
        self.assertEqual((facts.case_ids, facts.force_facts, facts.select_facts), (["C3"], [], ["OECD_sector"]))

    def test_plain_cases_grouped_by_selection(self) -> None:
        gaps = {"OECD_sector": {"C1", "C2"}, "products": {"C2"}}
        steps = _steps(_plan(FakeRepo(), completeness_gaps=gaps), Chapter.FACTS)
        by_cases = {tuple(s.case_ids): s.select_facts for s in steps}
        self.assertEqual(by_cases, {("C1",): ["OECD_sector"], ("C2",): ["OECD_sector", "products"]})

    def test_plan_records_only(self) -> None:
        self.assertEqual(_plan(FakeRepo(), only=Chapter.REPORT).only, Chapter.REPORT)

    def test_entity_graph_only_for_graph_facts(self) -> None:
        self.assertIn("companies_investigated", GRAPH_INPUT_FACTS)
        plan = _plan(FakeRepo(stale={"companies_investigated": {"C1"}, "OECD_sector": {"C2"}}))
        (graph,) = _steps(plan, Chapter.ENTITY_GRAPH)
        self.assertEqual(graph.case_ids, ["C1"])

    def test_chunk_age_trigger_off_by_default(self) -> None:
        self.assertTrue(_plan(FakeRepo(chunk_age={"C1"})).is_empty)

    def test_chunk_age_trigger_forces_facts_except_summary(self) -> None:
        (facts,) = _steps(_plan(FakeRepo(chunk_age={"C1"}), chunk_age_trigger=True), Chapter.FACTS)
        self.assertEqual(len(facts.force_facts), len(_STRUCTS) - 1)
        self.assertNotIn("case_summary", facts.force_facts)

    def test_only_report(self) -> None:
        plan = _plan(FakeRepo(stale={"OECD_sector": {"C1"}}), only=Chapter.REPORT)
        self.assertEqual({s.chapter for s in plan.steps}, {Chapter.REPORT})
        self.assertEqual(
            [s.name for s in plan.steps],
            ["report_celex_facts", "report_eddy_celex_facts", "retrieve_case_summary_md2disk", "zip_case_summary"],
        )

    def test_only_facts_drops_other_chapters(self) -> None:
        plan = _plan(FakeRepo(changes=({"D9"}, set())), only=Chapter.FACTS)
        self.assertEqual({s.chapter for s in plan.steps}, {Chapter.FACTS})

    def test_estimate_uses_mean_durations(self) -> None:
        repo = FakeRepo(stale={"OECD_sector": {"C1", "C2"}}, durations={"OECD_sector": 30.0, "case_summary": 50.0})
        plan = _plan(repo)
        (facts,) = _steps(plan, Chapter.FACTS)
        self.assertEqual(facts.estimate_s, 60.0)
        (summary,) = _steps(plan, Chapter.SUMMARIES)
        self.assertEqual(summary.estimate_s, 100.0)

    def test_triggers_explain_the_plan(self) -> None:
        plan = _plan(FakeRepo(stale={"OECD_sector": {"C1"}}), refresh_facts={"products"})
        text = " ".join(plan.triggers)
        self.assertIn("products", text)
        self.assertIn("OECD_sector", text)

    def test_gpu_needs(self) -> None:
        plan = _plan(FakeRepo(changes=({"D9"}, set())))
        gpus = {s.name: s.gpu for s in plan.steps}
        self.assertEqual(gpus["convert"], "mineru")
        self.assertEqual(gpus["facts"], "bge")


class TestFillableGaps(unittest.TestCase):
    """Gaps behind an empty predecessor cannot be filled and stay out of the plan."""

    def _frame(self, rows: list[tuple[str, str, bool, bool, bool]]):
        import polars as pl

        return pl.DataFrame(
            [{"celex_id": c, "fact": f, "present": p, "empty_value": e, "conditional": k} for c, f, p, e, k in rows],
        )

    def test_gap_with_present_predecessors_is_fillable(self) -> None:
        frame = self._frame([("C1", "products", True, False, False), ("C1", "OECD_sector", False, False, False)])
        self.assertEqual(fillable_gaps(frame, _STRUCTS), {"OECD_sector": {"C1"}})

    def test_gap_behind_na_predecessor_is_skipped(self) -> None:
        frame = self._frame([("C1", "products", True, True, False), ("C1", "OECD_sector", False, False, False)])
        self.assertEqual(fillable_gaps(frame, _STRUCTS), {})

    def test_gap_behind_missing_predecessor_is_skipped_but_predecessor_filled(self) -> None:
        frame = self._frame([("C1", "products", False, False, False), ("C1", "OECD_sector", False, False, False)])
        self.assertEqual(fillable_gaps(frame, _STRUCTS), {"products": {"C1"}})

    def test_conditional_gap_is_skipped(self) -> None:
        frame = self._frame([("C1", "case_title", False, False, True)])
        self.assertEqual(fillable_gaps(frame, _STRUCTS), {})

    def test_conditional_gap_after_failed_attempt_is_fillable(self) -> None:
        import polars as pl

        rows = [
            {"celex_id": "C1", "fact": "case_title", "present": False, "empty_value": False, "conditional": True, "last_audit_ok": False},
            {"celex_id": "C2", "fact": "case_title", "present": False, "empty_value": False, "conditional": True, "last_audit_ok": True},
            {"celex_id": "C3", "fact": "case_title", "present": False, "empty_value": False, "conditional": True, "last_audit_ok": None},
        ]
        self.assertEqual(fillable_gaps(pl.DataFrame(rows), _STRUCTS), {"case_title": {"C1"}})


class TestPlannerRepositoryLive(unittest.TestCase):
    """The SQL runs against the real schema (skipped without a DB)."""

    @classmethod
    def setUpClass(cls) -> None:
        from sqlalchemy import text

        from batch.main_pipeline.planner import PlannerRepository
        from pipelines.rag.pgvector.session import make_engine

        try:
            engine = make_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"database unreachable: {exc}") from exc
        cls.engine = engine
        cls.repo = PlannerRepository(engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_queries_return_declared_types(self) -> None:
        cases = self.repo.all_cases()
        self.assertIsInstance(cases, set)
        self.assertGreater(len(cases), 100)
        stale = self.repo.stale_facts(["NACE_class_code"], {"NACE_class_code": ["products", "NACE_group_code"]})
        self.assertIsInstance(stale, dict)
        self.assertIsInstance(self.repo.new_documents_since(datetime(2026, 11, 1)), set)
        added, changed = self.repo.loader_changes({"NO-SUCH-DECISION": {"http://example.invalid/x.pdf"}})
        self.assertEqual((added, changed), ({"NO-SUCH-DECISION"}, {}))
        self.assertIsInstance(self.repo.chunks_newer_than_facts(), set)
        durations = self.repo.mean_duration_s(["products"])
        self.assertGreater(durations["products"], 0.0)

    def test_loader_rows_already_downloaded_are_not_added(self) -> None:
        """20261108: 187 loader rows looked "added" because their mapped id
        (``DEC-AT32450-20040430``) differs from the id the downloader audited them
        under (``32450_35_6``); the audit URLs link them."""
        from pathlib import Path

        from batch.main_pipeline.planner import loader_rows
        from config.settings import settings

        for required in (settings.PIPELINE_LOADER_FILE, settings.DOWNLOAD_FILE_MANIFEST_PATH):
            if not Path(required).is_file():
                self.skipTest(f"loader input missing: {required}")
        rows = loader_rows(Path(settings.PIPELINE_LOADER_FILE))
        self.assertIn("DEC-AT32450-20040430", rows)
        added, changed = self.repo.loader_changes({i: r.urls for i, r in rows.items()})
        self.assertEqual(added, set())
        self.assertNotIn("DEC-AT32450-20040430", changed)

class _TxEngine:
    """Engine stand-in handing out one connection whose transaction is rolled back."""

    def __init__(self, conn: object) -> None:
        self._conn = conn

    def connect(self):
        from contextlib import nullcontext

        return nullcontext(self._conn)


class TestPlannerSqlLive(unittest.TestCase):
    """Staleness SQL on rows inserted inside a rolled-back transaction."""

    def setUp(self) -> None:
        from sqlalchemy import text

        from batch.main_pipeline.planner import PlannerRepository
        from pipelines.rag.pgvector.session import make_engine

        try:
            self.engine = make_engine()
            self.conn = self.engine.connect()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"database unreachable: {exc}") from exc
        self.tx = self.conn.begin()
        self.repo = PlannerRepository(_TxEngine(self.conn))
        self.text = text

    def tearDown(self) -> None:
        self.tx.rollback()
        self.conn.close()
        self.engine.dispose()

    def _fact(self, celex: str, name: str, at: datetime, md: str = "value", js: str = '[{"value": "value"}]') -> None:
        self.conn.execute(
            self.text("""INSERT INTO celex_facts.facts (id, celex_id, name, value_md, value_json, created_at)
                         VALUES (gen_random_uuid(), :c, :n, :md, :js, :at)"""),
            {"c": celex, "n": name, "md": md, "js": js, "at": at},
        )

    def _attempt(self, celex: str, name: str, at: datetime, success: bool) -> None:
        self.conn.execute(
            self.text("""INSERT INTO celex_facts.fact_ingestion_audit_trail (celex_id, target_fact_name, success, created_at)
                         VALUES (:c, :n, :s, :at)"""),
            {"c": celex, "n": name, "s": success, "at": at},
        )

    @staticmethod
    def _has(stale: dict[str, set[str]], fact: str) -> bool:
        return "TEST-PLAN-1" in stale.get(fact, set())

    def test_failed_attempt_does_not_count_as_checked(self) -> None:
        self._fact("TEST-PLAN-1", "fines", datetime(2026, 1, 1))
        self._fact("TEST-PLAN-1", "companies_investigated", datetime(2026, 2, 1))
        self._attempt("TEST-PLAN-1", "fines", datetime(2026, 3, 1), success=False)
        stale = self.repo.stale_facts(["fines"], {"fines": ["companies_investigated"]})
        self.assertTrue(self._has(stale, "fines"))

    def test_successful_attempt_counts_as_checked(self) -> None:
        self._fact("TEST-PLAN-1", "fines", datetime(2026, 1, 1))
        self._fact("TEST-PLAN-1", "companies_investigated", datetime(2026, 2, 1))
        self._attempt("TEST-PLAN-1", "fines", datetime(2026, 3, 1), success=True)
        self.assertFalse(self._has(self.repo.stale_facts(["fines"], {"fines": ["companies_investigated"]}), "fines"))

    def test_stale_behind_na_predecessor_is_skipped(self) -> None:
        self._fact("TEST-PLAN-1", "OECD_sector", datetime(2026, 1, 1))
        self._fact("TEST-PLAN-1", "products", datetime(2026, 2, 1), md="NA", js="[]")
        self.assertFalse(self._has(self.repo.stale_facts(["OECD_sector"], {"OECD_sector": ["products"]}), "OECD_sector"))

    def test_stale_behind_missing_predecessor_is_skipped(self) -> None:
        self._fact("TEST-PLAN-1", "NACE_class_code", datetime(2026, 1, 1))
        self._fact("TEST-PLAN-1", "products", datetime(2026, 2, 1))
        stale = self.repo.stale_facts(["NACE_class_code"], {"NACE_class_code": ["products", "NACE_group_code"]})
        self.assertFalse(self._has(stale, "NACE_class_code"))

    def test_summary_inputs_never_gate(self) -> None:
        self._fact("TEST-PLAN-1", "case_summary", datetime(2026, 1, 1))
        self._fact("TEST-PLAN-1", "products", datetime(2026, 2, 1))
        stale = self.repo.stale_facts(["case_summary"], {"case_summary": ["products", "leniency_granted"]})
        self.assertTrue(self._has(stale, "case_summary"))

    def test_new_documents_since_naive_local_baseline(self) -> None:
        from datetime import timedelta

        self.conn.execute(self.text(
            """INSERT INTO celex_facts.document_download_audit (run_id, decision_id, outcome, loader_file, created_at)
               VALUES (gen_random_uuid(), 'TEST-PLAN-DL', 'downloaded', 't.xlsx', now())"""
        ))
        baseline = datetime.now() - timedelta(minutes=30)  # naive local, as stored plans carry it
        self.assertIn("TEST-PLAN-DL", self.repo.new_documents_since(baseline))

    def test_url_change_follows_decision_merge(self) -> None:
        self.conn.execute(self.text(
            """INSERT INTO celex_facts.document_download_audit (run_id, decision_id, source_url, outcome, loader_file)
               VALUES (gen_random_uuid(), 'TEST-PLAN-OLD', 'http://test.invalid/known.pdf', 'downloaded', 't.xlsx')"""
        ))
        self.conn.execute(self.text(
            """INSERT INTO celex_facts.decision_merge (merged_decision_id, merged_into, merged_by)
               VALUES ('TEST-PLAN-OLD', 'TEST-PLAN-NEW', 'test')"""
        ))
        rows = {
            "DEC-TEST-1": {"http://test.invalid/known.pdf", "http://test.invalid/new.pdf"},
            "DEC-TEST-2": {"http://test.invalid/known.pdf"},
            "DEC-TEST-3": {"http://test.invalid/never.pdf"},
        }
        added, changed = self.repo.loader_changes(rows)
        self.assertEqual(added, {"DEC-TEST-3"})
        self.assertEqual(changed, {"DEC-TEST-1": "TEST-PLAN-NEW"})

    def test_chunks_newer_than_facts_across_timezones(self) -> None:
        from datetime import timedelta

        from config.settings import settings

        self._attempt("TEST-PLAN-CH", "products", datetime.now() - timedelta(minutes=30), success=True)
        self.conn.execute(self.text(
            f"""INSERT INTO celex_facts.{settings.EMBEDDING_TABLE} (id, celex_id, ingestion_timestamp)
                VALUES ('test-plan-chunk', 'TEST-PLAN-CH', now())"""
        ))
        self.assertIn("TEST-PLAN-CH", self.repo.chunks_newer_than_facts())


class TestLocalTimezone(unittest.TestCase):
    """Naive local timestamps are converted with the machine's IANA zone."""

    def test_local_tz_name_is_iana(self) -> None:
        from batch.main_pipeline.planner import local_tz_name

        name = local_tz_name()
        self.assertTrue(name == "UTC" or "/" in name, name)
