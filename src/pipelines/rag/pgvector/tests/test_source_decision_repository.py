"""Unit tests for SourceDecisionRepository with a faked engine boundary.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest pipelines.rag.pgvector.tests.test_source_decision_repository
"""

import datetime as dt
import unittest
from types import SimpleNamespace

from pipelines.decide.models import (
    CandidateEvidence,
    DecisionPlan,
    DocumentKind,
    Promotion,
    Repoint,
    TitleKey,
    Verdict,
)
from pipelines.rag.pgvector.registry_dedupe_repository import MergeStep
from pipelines.rag.pgvector.source_decision_repository import CandidateRow, SourceDecisionRepository

DATE = dt.date(2019, 5, 16)


class _Conn:
    def __init__(self, engine: "_Engine", tx: bool) -> None:
        self.engine = engine
        self.tx = tx

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, exc_type: object, *exc: object) -> bool:
        if self.tx:
            self.engine.outcomes.append("rollback" if exc_type else "commit")
        return False

    def execute(self, statement: object, params: dict | None = None) -> SimpleNamespace:
        self.engine.records.append((" ".join(str(statement).split()), params or {}))
        return SimpleNamespace(fetchall=lambda: self.engine.rows, first=lambda: None,
                               scalar=lambda: self.engine.scalar, rowcount=1)


class _Engine:
    def __init__(self, rows: list | None = None, scalar: object = "loader.xlsx") -> None:
        self.records: list[tuple[str, dict]] = []
        self.outcomes: list[str] = []
        self.rows = rows or []
        self.scalar = scalar

    def connect(self) -> _Conn:
        return _Conn(self, False)

    def begin(self) -> _Conn:
        return _Conn(self, True)

    def sql(self, needle: str) -> list[tuple[str, dict]]:
        return [r for r in self.records if needle in r[0]]


def _winner(doc: str, lang: str = "EN") -> CandidateEvidence:
    return CandidateEvidence(
        document_id=doc, decision_id="D", origin="ec.europa.eu", sha256="s", local_file_pdf=f"/p/{doc}.pdf",
        local_file_md=f"/p/{doc}.md", language=lang, kind=DocumentKind.DECISION,
        title_key=TitleKey(case_numbers=("40135",), subject="forex essex express", doc_date=DATE),
        operative=True, md_chars=10)


def _row(doc: str) -> CandidateRow:
    return CandidateRow(document_id=doc, decision_id="D", origin="ec.europa.eu", sha256="s",
                        source_url=f"https://ec.europa.eu/{doc}.pdf", file_format="pdf",
                        local_file_pdf=f"/p/{doc}.pdf", local_file_md=f"/p/{doc}.md", language=None)


ROWS = {d: _row(d) for d in ("w", "m", "x", "k")}


class LoadTests(unittest.TestCase):
    def test_load_undecided_scope_sql(self) -> None:
        engine = _Engine()
        SourceDecisionRepository(engine).load_undecided(["D"])
        sql = engine.records[0][0]
        self.assertIn("selection_status = 'undecided'", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("d.decision_id = ANY(:ids)", sql)
        cand_sql = engine.records[1][0]
        self.assertIn("is_selected AND NOT r.is_source AND r.local_file_pdf IS NOT NULL", cand_sql)


class ApplyTests(unittest.TestCase):
    def test_apply_promote_sets_source_and_decision(self) -> None:
        engine = _Engine()
        plan = DecisionPlan(decision_id="D", verdict=Verdict.PROMOTE, promotions=[Promotion(
            decision_id="D", is_new=False, winner=_winner("w"), members=["w", "m"],
            decision_date=dt.date(1992, 6, 11), language_init="en")])
        hook = []
        SourceDecisionRepository(engine).apply(plan, ROWS, before_commit=lambda: hook.append(1))
        src = engine.sql("SET is_source = true")
        self.assertEqual(src[0][1]["document_id"], "w")
        self.assertEqual(src[0][1]["language"], "EN")
        dec = engine.sql("selection_status = 'selected'")
        self.assertEqual(dec[0][1], {"decision_id": "D", "language_init": "en",
                                     "decision_date": dt.date(1992, 6, 11)})
        self.assertEqual(hook, [1])
        self.assertEqual(engine.outcomes, ["commit"])

    def test_apply_split_inserts_decision_and_moves_members(self) -> None:
        engine = _Engine()
        plan = DecisionPlan(decision_id="D", verdict=Verdict.SPLIT, promotions=[
            Promotion(decision_id="D", is_new=False, winner=_winner("w"), members=["w"],
                      decision_date=DATE, language_init="en"),
            Promotion(decision_id="D_2", is_new=True, winner=_winner("x"), members=["x", "m"],
                      decision_date=DATE, language_init="en")])
        SourceDecisionRepository(engine).apply(plan, ROWS, before_commit=lambda: None)
        ins = engine.sql("INSERT INTO celex_facts.decision_registry")
        self.assertEqual(ins[0][1]["decision_id"], "D_2")
        self.assertEqual(ins[0][1]["split_from"], "D")
        self.assertEqual(ins[0][1]["decision_date"], DATE)
        self.assertTrue(engine.sql("SELECT case_id, :new"))
        moved = engine.sql("SET decision_id = :new")
        self.assertEqual(moved[0][1], {"new": "D_2", "members": ["x", "m"], "d": "D"})

    def test_apply_merge_moves_links_and_deletes(self) -> None:
        engine = _Engine()
        plan = DecisionPlan(decision_id="D", verdict=Verdict.MERGE, merge_into="K",
                            repoints=[Repoint(document_id="k", owner_decision_id="K")])
        SourceDecisionRepository(engine).apply(plan, ROWS, before_commit=lambda: None)
        texts = [r[0] for r in engine.records]
        repoint = next(i for i, t in enumerate(texts) if "SET document_id = :new, decision_id = :k" in t)
        links = next(i for i, t in enumerate(texts) if "INSERT INTO celex_facts.competition_case_decision" in t)
        delete = next(i for i, t in enumerate(texts) if t.startswith("DELETE FROM celex_facts.decision_registry"))
        self.assertLess(repoint, links)
        self.assertLess(links, delete)
        self.assertEqual(engine.records[links][1], {"k": "K", "d": "D"})

    def test_merge_moves_every_remaining_document_of_d(self) -> None:
        """Non-selected and HTML rows of D also go to the owner before D is deleted."""
        engine = _Engine(rows=[("D_EN_html", "https://eur-lex/x.html", "html")])
        plan = DecisionPlan(decision_id="D", verdict=Verdict.MERGE, merge_into="K",
                            repoints=[Repoint(document_id="k", owner_decision_id="K")])
        SourceDecisionRepository(engine).apply(plan, ROWS, before_commit=lambda: None)
        moved = [r[1]["old"] for r in engine.sql("SET document_id = :new, decision_id = :k")]
        self.assertEqual(moved, ["k", "D_EN_html"])
        texts = [r[0] for r in engine.records]
        own = next(i for i, t in enumerate(texts) if "FOR UPDATE" in t)
        delete = next(i for i, t in enumerate(texts) if t.startswith("DELETE FROM celex_facts.decision_registry"))
        self.assertLess(own, delete)

    def test_merge_records_lineage_after_the_delete(self) -> None:
        engine = _Engine()
        plan = DecisionPlan(decision_id="D", verdict=Verdict.MERGE, merge_into="K",
                            repoints=[Repoint(document_id="k", owner_decision_id="K")])
        SourceDecisionRepository(engine).apply(plan, ROWS, before_commit=lambda: None)
        texts = [r[0] for r in engine.records]
        delete = next(i for i, t in enumerate(texts) if t.startswith("DELETE FROM celex_facts.decision_registry"))
        ledger = next(i for i, t in enumerate(texts) if t.startswith("INSERT INTO celex_facts.decision_merge"))
        self.assertLess(delete, ledger)
        self.assertEqual(engine.records[ledger][1], {"d": "D", "k": "K", "by": "decide_source_document"})
        self.assertEqual(engine.outcomes, ["commit"])

    def test_merge_records_the_injected_step(self) -> None:
        engine = _Engine()
        plan = DecisionPlan(decision_id="D", verdict=Verdict.MERGE, merge_into="K")
        SourceDecisionRepository(engine, merge_step=MergeStep.REPAIR_DECIDED_DUPLICATES).apply(
            plan, ROWS, before_commit=lambda: None)
        self.assertEqual(engine.sql("decision_merge")[0][1]["by"], "repair_decided_duplicates")

    def test_better_copy_switches_owner_source(self) -> None:
        engine = _Engine()
        plan = DecisionPlan(decision_id="D", verdict=Verdict.MERGE, merge_into="K",
                            repoints=[Repoint(document_id="k", owner_decision_id="K", make_source=True)])
        SourceDecisionRepository(engine).apply(plan, ROWS, before_commit=lambda: None)
        texts = [r[0] for r in engine.records]
        demote = next(i for i, t in enumerate(texts) if "SET is_source = false" in t)
        move = next(i for i, t in enumerate(texts) if "SET decision_id = :owner, is_source = true" in t)
        self.assertLess(demote, move)
        self.assertEqual(engine.records[demote][1], {"owner": "K"})
        self.assertEqual(engine.records[move][1]["document_id"], "k")
        self.assertFalse(engine.sql("SET document_id = :new, decision_id = :k"))
        lang = engine.sql("language_translated = 'NA'")
        self.assertEqual(lang[0][1]["decision_id"], "K")

    def test_document_rows_of_a_decision(self) -> None:
        engine = _Engine(rows=[SimpleNamespace(_mapping=_row("w").model_dump())])
        rows = SourceDecisionRepository(engine).document_rows("D")
        self.assertEqual(list(rows), ["w"])
        self.assertIn("decision_id = :d", engine.records[0][0])

    def test_apply_review_appends_reason_once_and_repoints(self) -> None:
        engine = _Engine()
        plan = DecisionPlan(decision_id="D", verdict=Verdict.REVIEW, review_reason="letters only",
                            repoints=[Repoint(document_id="k", owner_decision_id="K")])
        SourceDecisionRepository(engine).apply(plan, ROWS, before_commit=lambda: None)
        rev = engine.sql("array_append(review_reasons")
        self.assertIn("NOT (:reason = ANY(review_reasons))", rev[0][0])
        self.assertEqual(rev[0][1], {"reason": "letters only", "decision_id": "D"})
        self.assertTrue(engine.sql("SET document_id = :new, decision_id = :k"))
        self.assertFalse(engine.sql("DELETE FROM celex_facts.decision_registry"))
        self.assertFalse(engine.sql("decision_merge"))

    def test_hook_error_rolls_back(self) -> None:
        engine = _Engine()
        plan = DecisionPlan(decision_id="D", verdict=Verdict.REVIEW, review_reason="x")

        def boom() -> None:
            raise OSError("disk full")

        with self.assertRaises(OSError):
            SourceDecisionRepository(engine).apply(plan, ROWS, before_commit=boom)
        self.assertEqual(engine.outcomes, ["rollback"])


if __name__ == "__main__":
    unittest.main()
