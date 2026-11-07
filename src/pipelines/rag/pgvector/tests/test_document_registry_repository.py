"""Unit tests for the document registry and download audit repositories.

The engine boundary is faked: every statement is recorded with its bound
parameters, so the tests pin SQL shape (upsert targets, casts, filters) and
parameter serialisation without a live database. The migration file is read
as text only, never executed.

Run with:
    cd src && ../.venv/bin/python -m unittest \\
        pipelines.rag.pgvector.tests.test_document_registry_repository
"""

import datetime as dt
import json
import re
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    FetchResult,
    Outcome,
    RowFailure,
    SelectionStatus,
)
from pipelines.download.selector import Selection
from pipelines.rag.pgvector.document_registry_repository import (
    DocumentOwnershipConflict,
    DocumentRegistryRepository,
)
from pipelines.rag.pgvector import session as session_module
from pipelines.rag.pgvector.download_audit_repository import (
    DownloadAuditRepository,
)
from pipelines.rag.pgvector.source_document_resolver import SourceDocumentResolver

MIGRATION_SQL = Path(__file__).resolve().parents[1] / "migrate_document_registry.sql"
LINEAGE_SQL = Path(__file__).resolve().parents[1] / "migrate_document_lineage.sql"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConn:
    """Context-manager connection recording statements, returning canned rows."""

    def __init__(self, engine: "_FakeEngine", tx: int | None = None) -> None:
        self._engine = engine
        self._tx = tx

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, exc_type: object, *exc: object) -> bool:
        if self._tx is not None:
            self._engine.tx_ends[self._tx] = "rollback" if exc_type else "commit"
        return False

    def execute(self, statement: object, params: dict | None = None) -> SimpleNamespace:
        sql = str(statement)
        if self._engine.fail_probe and "LIMIT 0" in sql:
            raise RuntimeError("relation does not exist")
        self._engine.records.append((sql, params or {}))
        self._engine.tx_of.append(self._tx)
        responder = self._engine.responder
        rows = responder(sql, params or {}) if responder else self._engine.rows
        return SimpleNamespace(
            fetchall=lambda: rows,
            first=lambda: rows[0] if rows else None,
        )


class _FakeEngine:
    """Engine stand-in exposing ``connect`` and ``begin`` like SQLAlchemy."""

    def __init__(
        self,
        rows: list | None = None,
        fail_probe: bool = False,
        responder: Callable[[str, dict], list] | None = None,
    ) -> None:
        self.records: list[tuple[str, dict]] = []
        self.rows = rows or []
        self.fail_probe = fail_probe
        self.responder = responder
        self.begin_calls = 0
        self.tx_of: list[int | None] = []
        self.tx_ends: dict[int, str] = {}

    def connect(self) -> _FakeConn:
        return _FakeConn(self)

    def begin(self) -> _FakeConn:
        self.begin_calls += 1
        return _FakeConn(self, tx=self.begin_calls)

    def writes_with_tx(self) -> list[tuple[str, int | None]]:
        """Recorded non-probe statements with their transaction number."""
        return [(sql, tx) for (sql, _), tx in zip(self.records, self.tx_of, strict=True)
                if "LIMIT 0" not in sql]


def _decision(**overrides: object) -> DecisionRequest:
    """Build a decision with two case ids and loader meta."""
    values: dict = {
        "decision_id": "CELEX-31964D0233",
        "celex_number": "31964D0233",
        "case_ids": ["IV/223", "IV/A-00061"],
        "decision_date": dt.date(1967, 6, 27),
        "title": "Transocean Marine Paint",
        "parties": "Transocean",
        "document_class": "exemption",
        "meta": {"source": "manifest", "gemma_x": "y"},
        "review_reasons": ["two dates", "duplicate CELEX"],
        "loader_file": "filtered_final_manifest.xlsx",
        "loader_row": 12,
    }
    values.update(overrides)
    return DecisionRequest(**values)


def _candidate() -> CandidateDocument:
    return CandidateDocument(
        document_id="CELEX-31964D0233_EN",
        source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:31964D0233",
        language="EN",
        origin="eur-lex",
    )


def _selection() -> Selection:
    return Selection(
        status=SelectionStatus.SELECTED, selected=[_candidate()], language_init="en"
    )


def _fetch(outcome: Outcome = Outcome.DOWNLOADED, **overrides: object) -> FetchResult:
    values: dict = {
        "document_id": "CELEX-31964D0233_EN",
        "source_url": _candidate().source_url,
        "outcome": outcome,
        "local_path": "/docs/CELEX-31964D0233/CELEX-31964D0233.pdf",
        "sha256": "a" * 64,
        "size_bytes": 1234,
        "content_type": "application/pdf",
        "http_status": 200,
        "duration_ms": 87,
    }
    values.update(overrides)
    return FetchResult(**values)


def _writes(engine: _FakeEngine) -> list[tuple[str, dict]]:
    """Recorded statements excluding the construction-time probes."""
    return [r for r in engine.records if "LIMIT 0" not in r[0]]


def _registry(
    rows: list | None = None,
    responder: Callable[[str, dict], list] | None = None,
) -> tuple[DocumentRegistryRepository, _FakeEngine]:
    engine = _FakeEngine(rows=rows, responder=responder)
    return DocumentRegistryRepository(engine), engine  # type: ignore[arg-type]


def _owned_registry() -> tuple[DocumentRegistryRepository, _FakeEngine]:
    """Registry whose guarded document upsert affects one row (same owner)."""
    return _registry(rows=[("CELEX-31964D0233_EN",)])


def _audit(rows: list | None = None) -> tuple[DownloadAuditRepository, _FakeEngine]:
    engine = _FakeEngine(rows=rows)
    return DownloadAuditRepository(engine), engine  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DocumentRegistryRepository
# ---------------------------------------------------------------------------


class TestRegistryProbe(unittest.TestCase):
    """Construction-time table probe."""

    def test_probe_failure_raises_at_construction(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            DocumentRegistryRepository(_FakeEngine(fail_probe=True))  # type: ignore[arg-type]
        self.assertIn("migrate_document_registry.sql", str(ctx.exception))

    def test_probe_covers_every_registry_table(self) -> None:
        _, engine = _registry()
        probed = " ".join(sql for sql, _ in engine.records)
        for table in (
            "celex_facts.competition_case ",
            "celex_facts.decision_registry ",
            "celex_facts.competition_case_decision ",
            "celex_facts.document_registry ",
        ):
            self.assertIn(table, probed)


class TestUpsertDecision(unittest.TestCase):
    """upsert_decision SQL shape and parameters."""

    def test_upsert_decision_is_on_conflict_do_update(self) -> None:
        repo, engine = _registry()
        repo.upsert_decision(_decision(), _selection())
        (sql, params), = _writes(engine)
        self.assertIn("INSERT INTO celex_facts.decision_registry", sql)
        self.assertIn("ON CONFLICT (decision_id) DO UPDATE SET", sql)
        self.assertEqual(params["decision_id"], "CELEX-31964D0233")
        self.assertEqual(params["celex_number"], "CELEX-31964D0233")
        self.assertEqual(params["selection_status"], "selected")
        self.assertEqual(params["language_init"], "en")
        self.assertEqual(params["decision_date"], dt.date(1967, 6, 27))
        self.assertEqual(params["loader_file"], "filtered_final_manifest.xlsx")

    def test_review_reasons_and_meta_are_cast(self) -> None:
        repo, engine = _registry()
        repo.upsert_decision(_decision(), _selection())
        (sql, params), = _writes(engine)
        self.assertIn("CAST(:review_reasons AS text[])", sql)
        self.assertIn("CAST(:meta AS jsonb)", sql)
        self.assertEqual(params["review_reasons"], ["two dates", "duplicate CELEX"])
        self.assertEqual(json.loads(params["meta"]), {"source": "manifest", "gemma_x": "y"})

    def test_conflict_keeps_language_translated(self) -> None:
        """The translate service owns language_translated; re-runs never reset it."""
        repo, engine = _registry()
        repo.upsert_decision(_decision(), _selection())
        (sql, _), = _writes(engine)
        update_clause = sql.split("DO UPDATE SET", 1)[1]
        self.assertNotIn("language_translated", update_clause)
        self.assertIn("updated_at", update_clause)

    def test_upsert_keeps_decided_fields(self) -> None:
        """A re-run that maps a decided decision as undecided keeps the decision (ISSUES 20261027)."""
        repo, engine = _registry()
        repo.upsert_decision(_decision(), _selection())
        (sql, _), = _writes(engine)
        update_clause = " ".join(sql.split("DO UPDATE SET", 1)[1].split())
        guard = ("CASE WHEN decision_registry.selection_status = 'selected' "
                 "AND EXCLUDED.selection_status = 'undecided' THEN decision_registry.{col} "
                 "ELSE EXCLUDED.{col} END")
        for col in ("selection_status", "language_init", "decision_date"):
            self.assertIn(f"{col} = " + guard.format(col=col), update_clause)
        self.assertIn("title = EXCLUDED.title", update_clause)

    def test_null_celex_and_date_are_passed_as_none(self) -> None:
        repo, engine = _registry()
        decision = _decision(decision_id="DEC-AT35388-19950516", celex_number=None, decision_date=None)
        selection = Selection(status=SelectionStatus.NO_PDF_SOURCE, language_init="NA")
        repo.upsert_decision(decision, selection)
        (_, params), = _writes(engine)
        self.assertIsNone(params["celex_number"])
        self.assertIsNone(params["decision_date"])
        self.assertEqual(params["selection_status"], "no_pdf_source")


class TestUpsertCasesAndLinks(unittest.TestCase):
    """upsert_cases_and_links SQL shape and transaction scope."""

    def test_links_use_composite_key(self) -> None:
        repo, engine = _registry()
        repo.upsert_cases_and_links(_decision())
        writes = _writes(engine)
        links = [w for w in writes if "competition_case_decision" in w[0]]
        cases = [w for w in writes if "INSERT INTO celex_facts.competition_case " in w[0]]
        self.assertEqual(len(links), 2)
        self.assertEqual(len(cases), 2)
        for sql, _ in links:
            self.assertIn("ON CONFLICT (case_id, decision_id) DO UPDATE", sql)
        for sql, _ in cases:
            self.assertIn("ON CONFLICT (case_id) DO UPDATE", sql)
        self.assertEqual(
            [p["case_id"] for _, p in links], ["IV/223", "IV/A-00061"]
        )
        self.assertTrue(all(p["decision_id"] == "CELEX-31964D0233" for _, p in links))
        self.assertEqual(engine.begin_calls, 1, "cases and links share one transaction")

    def test_no_case_ids_writes_nothing(self) -> None:
        repo, engine = _registry()
        repo.upsert_cases_and_links(_decision(case_ids=[]))
        self.assertEqual(_writes(engine), [])
        self.assertEqual(engine.begin_calls, 0)


class TestUpsertDocument(unittest.TestCase):
    """upsert_document SQL shape and validation."""

    def test_downloaded_document_carries_file_evidence(self) -> None:
        repo, engine = _owned_registry()
        repo.upsert_document(
            _candidate(), _fetch(), True, decision_id="CELEX-31964D0233"
        )
        (sql, params), = _writes(engine)
        self.assertIn("INSERT INTO celex_facts.document_registry", sql)
        self.assertIn("ON CONFLICT (document_id) DO UPDATE SET", sql)
        self.assertIn("WHERE doc.decision_id = EXCLUDED.decision_id", sql)
        self.assertIn("RETURNING document_id", sql)
        self.assertEqual(params["decision_id"], "CELEX-31964D0233")
        self.assertEqual(params["local_file_pdf"], "/docs/CELEX-31964D0233/CELEX-31964D0233.pdf")
        self.assertEqual(params["sha256"], "a" * 64)
        self.assertEqual(params["size_bytes"], 1234)
        self.assertTrue(params["is_selected"])
        self.assertIsInstance(params["downloaded_at"], dt.datetime)
        self.assertIsNotNone(params["downloaded_at"].tzinfo)

    def test_registered_existing_has_no_downloaded_at(self) -> None:
        repo, engine = _owned_registry()
        repo.upsert_document(
            _candidate(), _fetch(Outcome.REGISTERED_EXISTING, http_status=None),
            True, decision_id="CELEX-31964D0233",
        )
        (_, params), = _writes(engine)
        self.assertIsNone(params["downloaded_at"])
        self.assertEqual(params["sha256"], "a" * 64)

    def test_without_fetch_registers_metadata_only(self) -> None:
        repo, engine = _owned_registry()
        repo.upsert_document(_candidate(), None, False, decision_id="CELEX-31964D0233")
        (sql, params), = _writes(engine)
        for key in ("local_file_pdf", "sha256", "size_bytes", "content_type", "downloaded_at"):
            self.assertIsNone(params[key], key)
        self.assertFalse(params["is_selected"])
        # A metadata-only upsert must not erase evidence of an earlier download.
        self.assertIn(
            "COALESCE(EXCLUDED.sha256, doc.sha256)", sql
        )

    def test_failed_fetch_is_rejected(self) -> None:
        repo, engine = _registry()
        failed = _fetch(Outcome.FAILED, failure_reason="not_a_pdf", local_path=None, sha256=None)
        with self.assertRaises(ValueError):
            repo.upsert_document(_candidate(), failed, True, decision_id="CELEX-31964D0233")
        self.assertEqual(_writes(engine), [])

    def test_mismatched_fetch_document_id_is_rejected(self) -> None:
        repo, _ = _registry()
        with self.assertRaises(ValueError):
            repo.upsert_document(
                _candidate(), _fetch(document_id="other"), True,
                decision_id="CELEX-31964D0233",
            )


class TestLocalFileMd(unittest.TestCase):
    """The markdown path is spec'd from the PDF path at registration."""

    def test_upsert_document_sets_local_file_md_from_pdf(self) -> None:
        repo, engine = _owned_registry()
        repo.upsert_document(
            _candidate(),
            _fetch(local_path="/d/X/X.pdf"),
            True,
            decision_id="CELEX-31964D0233",
        )
        (sql, params), = _writes(engine)
        self.assertEqual(params["local_file_md"], "/d/X/mineru/X.md")
        flat = " ".join(sql.split())
        self.assertIn(
            "local_file_md = CASE WHEN EXCLUDED.local_file_pdf IS NOT NULL "
            "AND EXCLUDED.local_file_pdf IS DISTINCT FROM doc.local_file_pdf "
            "THEN EXCLUDED.local_file_md "
            "ELSE COALESCE(doc.local_file_md, EXCLUDED.local_file_md) END", flat)

    def test_upsert_document_without_fetch_binds_no_markdown(self) -> None:
        repo, engine = _owned_registry()
        repo.upsert_document(_candidate(), None, False, decision_id="CELEX-31964D0233")
        (_, params), = _writes(engine)
        self.assertIsNone(params["local_file_md"])


class TestDocumentOwnership(unittest.TestCase):
    """A document never silently moves to another decision."""

    def test_other_owner_by_document_id_raises(self) -> None:
        def responder(sql: str, params: dict) -> list:
            if "INSERT INTO" in sql:
                return []  # guarded DO UPDATE skipped: stored owner differs
            return [("CELEX-31988D0172",)]

        repo, engine = _registry(responder=responder)
        with self.assertRaises(DocumentOwnershipConflict) as ctx:
            repo.upsert_document(_candidate(), _fetch(), True, decision_id="CELEX-31964D0233")
        self.assertIsInstance(ctx.exception, ValueError)
        message = str(ctx.exception)
        for part in ("CELEX-31964D0233_EN", "CELEX-31988D0172", "CELEX-31964D0233"):
            self.assertIn(part, message)
        lookup = _writes(engine)[-1]
        self.assertEqual(lookup[1], {"document_id": "CELEX-31964D0233_EN"})

    def test_source_url_owned_elsewhere_raises(self) -> None:
        def responder(sql: str, params: dict) -> list:
            if "INSERT INTO" in sql:
                raise IntegrityError(
                    sql, params,
                    Exception('duplicate key value violates unique constraint '
                              '"uq_document_registry_source_url"'),
                )
            return [("dbcomp-1754", "DEC-AT35388-19950516")]

        repo, engine = _registry(responder=responder)
        with self.assertRaises(DocumentOwnershipConflict) as ctx:
            repo.upsert_document(_candidate(), None, False, decision_id="CELEX-31964D0233")
        message = str(ctx.exception)
        for part in ("CELEX-31964D0233_EN", "DEC-AT35388-19950516", "CELEX-31964D0233"):
            self.assertIn(part, message)
        lookup = _writes(engine)[-1]
        self.assertEqual(lookup[1], {"source_url": _candidate().source_url})

    def test_other_integrity_error_propagates(self) -> None:
        def responder(sql: str, params: dict) -> list:
            if "INSERT INTO" in sql:
                raise IntegrityError(sql, params, Exception("fk_violation decision_id"))
            return []

        repo, _ = _registry(responder=responder)
        with self.assertRaises(IntegrityError):
            repo.upsert_document(_candidate(), None, False, decision_id="CELEX-31964D0233")


def _is_clear(sql: str) -> bool:
    """Whether a statement is the clearing UPDATE of the decision's source."""
    return sql.lstrip().startswith("UPDATE celex_facts.document_registry")


class TestIsSource(unittest.TestCase):
    """is_source: one processing source per decision, cleared before set."""

    def test_new_source_clears_previous_source(self) -> None:
        repo, engine = _owned_registry()
        repo.upsert_document(_candidate(), _fetch(), True,
                             decision_id="CELEX-31964D0233", is_source=True)
        (clear_sql, clear_tx), (upsert_sql, upsert_tx) = engine.writes_with_tx()
        self.assertTrue(_is_clear(clear_sql), clear_sql)
        self.assertIn("SET is_source = false", clear_sql)
        self.assertIn("WHERE decision_id = :decision_id", clear_sql)
        self.assertIn("AND is_source", clear_sql)
        self.assertIn("document_id <> :document_id", clear_sql)
        self.assertIn("INSERT INTO celex_facts.document_registry", upsert_sql)
        self.assertEqual(clear_tx, upsert_tx, "clear and set share one transaction")
        self.assertIsNotNone(clear_tx)
        self.assertEqual(engine.tx_ends[clear_tx], "commit")
        clear_params = _writes(engine)[0][1]
        self.assertEqual(clear_params, {"decision_id": "CELEX-31964D0233",
                                        "document_id": "CELEX-31964D0233_EN"})
        self.assertTrue(_writes(engine)[1][1]["is_source"])

    def test_non_source_upsert_does_not_clear_and_keeps_stored_source(self) -> None:
        repo, engine = _owned_registry()
        repo.upsert_document(_candidate(), None, False, decision_id="CELEX-31964D0233")
        (sql, params), = _writes(engine)
        self.assertFalse(params["is_source"])
        update_clause = sql.split("DO UPDATE SET", 1)[1]
        self.assertIn("is_source      = doc.is_source OR EXCLUDED.is_source", update_clause)

    def test_ownership_conflict_keeps_previous_source(self) -> None:
        def responder(sql: str, params: dict) -> list:
            if "INSERT INTO" in sql:
                return []  # guarded DO UPDATE skipped: stored owner differs
            return [("CELEX-31988D0172",)]

        repo, engine = _registry(responder=responder)
        with self.assertRaises(DocumentOwnershipConflict):
            repo.upsert_document(_candidate(), _fetch(), True,
                                 decision_id="CELEX-31964D0233", is_source=True)
        writes = engine.writes_with_tx()
        self.assertTrue(_is_clear(writes[0][0]))
        clear_tx = writes[0][1]
        self.assertEqual(writes[1][1], clear_tx)
        self.assertEqual(engine.tx_ends[clear_tx], "rollback",
                         "the clear is rolled back with the rejected upsert")

    def test_source_url_conflict_rolls_back_the_clear(self) -> None:
        def responder(sql: str, params: dict) -> list:
            if "INSERT INTO" in sql:
                raise IntegrityError(sql, params, Exception(
                    'violates unique constraint "uq_document_registry_source_url"'))
            return [("dbcomp-1754", "DEC-AT35388-19950516")]

        repo, engine = _registry(responder=responder)
        with self.assertRaises(DocumentOwnershipConflict):
            repo.upsert_document(_candidate(), _fetch(), True,
                                 decision_id="CELEX-31964D0233", is_source=True)
        clear_tx = engine.writes_with_tx()[0][1]
        self.assertEqual(engine.tx_ends[clear_tx], "rollback")

    def test_source_document_id_returns_none_when_absent(self) -> None:
        repo, engine = _registry(rows=[])
        self.assertIsNone(repo.source_document_id("CELEX-31964D0233"))
        (sql, params), = _writes(engine)
        self.assertIn("SELECT document_id FROM celex_facts.document_registry", sql)
        self.assertIn("decision_id = :decision_id AND is_source", sql)
        self.assertEqual(params, {"decision_id": "CELEX-31964D0233"})
        self.assertEqual(engine.begin_calls, 0, "read-only")

    def test_source_document_id_returns_the_source(self) -> None:
        repo, _ = _registry(rows=[("CELEX-31964D0233_EN",)])
        self.assertEqual(repo.source_document_id("CELEX-31964D0233"),
                         "CELEX-31964D0233_EN")


class TestCelexIdNormalisation(unittest.TestCase):
    """decision_registry.celex_number stores the prefixed CELEX- key."""

    def _bound_celex(self, celex_number: str | None) -> object:
        repo, engine = _registry()
        repo.upsert_decision(_decision(celex_number=celex_number), _selection())
        (_, params), = _writes(engine)
        return params["celex_number"]

    def test_bare_is_prefixed(self) -> None:
        self.assertEqual(self._bound_celex("31964D0233"), "CELEX-31964D0233")

    def test_prefixed_is_not_doubled(self) -> None:
        self.assertEqual(self._bound_celex("CELEX-31964D0233"), "CELEX-31964D0233")

    def test_none_stays_none(self) -> None:
        self.assertIsNone(self._bound_celex(None))

    def test_parenthesised_suffix(self) -> None:
        self.assertEqual(self._bound_celex("32002D0758(01)"), "CELEX-32002D0758_01")


class TestIsRegistered(unittest.TestCase):
    """is_registered read."""

    def test_true_when_row_with_file_exists(self) -> None:
        repo, engine = _registry(rows=[(1,)])
        self.assertTrue(repo.is_registered("CELEX-31964D0233_EN"))
        (sql, params), = _writes(engine)
        self.assertIn("local_file_pdf IS NOT NULL", sql)
        self.assertEqual(params, {"document_id": "CELEX-31964D0233_EN"})

    def test_false_when_no_row(self) -> None:
        repo, _ = _registry(rows=[])
        self.assertFalse(repo.is_registered("missing"))


class TestDecisionOfContent(unittest.TestCase):
    """decision_of_content read (content safety net)."""

    def test_returns_holder_decision(self) -> None:
        repo, engine = _registry(rows=[("36571_222_1",)])
        self.assertEqual(
            repo.decision_of_content("a" * 64, "DEC-AT36571-20020611", "DOC_1"),
            "36571_222_1")
        (sql, params), = _writes(engine)
        self.assertIn("sha256 = :sha256 AND (is_source OR is_selected)", sql)
        self.assertIn("document_id <> :document_id", sql)
        self.assertIn("(decision_id <> :decision_id OR is_source)", sql)
        self.assertIn("ORDER BY decision_id = :decision_id DESC, is_source DESC", sql)
        self.assertEqual(params, {"sha256": "a" * 64,
                                  "decision_id": "DEC-AT36571-20020611",
                                  "document_id": "DOC_1"})
        self.assertEqual(engine.begin_calls, 0, "read-only")

    def test_none_when_no_row(self) -> None:
        repo, _ = _registry(rows=[])
        self.assertIsNone(repo.decision_of_content("b" * 64, "X", "D"))


class TestPdfUrlsOf(unittest.TestCase):
    """pdf_urls_of read (registry alias URLs of the master keys)."""

    def test_reads_pdf_rows_of_the_keys(self) -> None:
        repo, engine = _registry(rows=[("K1", "https://a/x.pdf")])
        self.assertEqual(repo.pdf_urls_of(["K2", "K1", "K1"]), [("K1", "https://a/x.pdf")])
        (sql, params), = _writes(engine)
        self.assertIn("decision_id = ANY(:keys) AND file_format = :fmt", sql)
        self.assertEqual(params, {"keys": ["K1", "K2"], "fmt": "pdf"})
        self.assertEqual(engine.begin_calls, 0, "read-only")

    def test_no_keys_no_query(self) -> None:
        repo, engine = _registry(rows=[])
        self.assertEqual(repo.pdf_urls_of([]), [])
        self.assertEqual(_writes(engine), [])


# ---------------------------------------------------------------------------
# DownloadAuditRepository
# ---------------------------------------------------------------------------


class TestAuditProbe(unittest.TestCase):
    """Construction-time probe of the audit table and failures view."""

    def test_probe_failure_raises_at_construction(self) -> None:
        with self.assertRaises(RuntimeError):
            DownloadAuditRepository(_FakeEngine(fail_probe=True))  # type: ignore[arg-type]

    def test_probe_covers_table_and_view(self) -> None:
        _, engine = _audit()
        probed = " ".join(sql for sql, _ in engine.records)
        self.assertIn("celex_facts.document_download_audit ", probed)
        self.assertIn("celex_facts.document_download_failures_vw ", probed)


class TestAuditRecord(unittest.TestCase):
    """record() parameter serialisation."""

    def test_audit_record_serializes_outcome_value(self) -> None:
        repo, engine = _audit()
        run_id = uuid.uuid4()
        repo.record(
            run_id, "CELEX-31964D0233",
            _fetch(Outcome.FAILED, failure_reason="http_404", http_status=404,
                   local_path=None, sha256=None, size_bytes=None),
            "filtered_final_manifest.xlsx", 12,
        )
        (sql, params), = _writes(engine)
        self.assertIn("INSERT INTO celex_facts.document_download_audit", sql)
        self.assertIn("CAST(:run_id AS uuid)", sql)
        self.assertEqual(params["outcome"], "failed")
        self.assertIs(type(params["outcome"]), str)
        self.assertEqual(params["run_id"], str(run_id))
        self.assertEqual(params["failure_reason"], "http_404")
        self.assertEqual(params["http_status"], 404)
        self.assertEqual(params["document_id"], "CELEX-31964D0233_EN")
        self.assertEqual(params["duration_ms"], 87)
        self.assertEqual(params["loader_row"], 12)

    def test_row_failure_has_null_decision_and_document(self) -> None:
        repo, engine = _audit()
        failure = RowFailure(loader_file="cases.csv", loader_row=7, reason="no case or date")
        repo.record(uuid.uuid4(), None, failure, "cases.csv", 7)
        (_, params), = _writes(engine)
        self.assertEqual(params["outcome"], "failed")
        self.assertEqual(params["failure_reason"], "no case or date")
        for key in ("decision_id", "document_id", "source_url", "http_status",
                    "duration_ms", "sha256", "size_bytes", "content_type"):
            self.assertIsNone(params[key], key)
        self.assertEqual(params["loader_file"], "cases.csv")
        self.assertEqual(params["loader_row"], 7)

    def test_null_loader_row_is_accepted(self) -> None:
        repo, engine = _audit()
        repo.record(uuid.uuid4(), "D1", _fetch(Outcome.NOT_SELECTED), "urls.txt", None)
        (_, params), = _writes(engine)
        self.assertIsNone(params["loader_row"])
        self.assertEqual(params["outcome"], "not_selected")


class TestAuditDecisionLevel(unittest.TestCase):
    """Decision-level rows carry NULL, not '', for document fields."""

    def test_empty_document_fields_become_null(self) -> None:
        repo, engine = _audit()
        result = FetchResult(
            document_id="", source_url="", outcome=Outcome.NO_PDF_SOURCE, duration_ms=0
        )
        repo.record(uuid.uuid4(), "DEC-AT35388-19950516", result, "m.xlsx", 3)
        (_, params), = _writes(engine)
        self.assertIsNone(params["document_id"])
        self.assertIsNone(params["source_url"])
        self.assertEqual(params["outcome"], "no_pdf_source")


class TestAuditFailures(unittest.TestCase):
    """failures() reads the view with an optional run filter."""

    def test_failures_filters_by_run_id(self) -> None:
        row = SimpleNamespace(_mapping={"decision_id": "D1", "outcome": "failed"})
        repo, engine = _audit(rows=[row])
        run_id = uuid.uuid4()
        result = repo.failures(run_id)
        (sql, params), = _writes(engine)
        self.assertIn("FROM celex_facts.document_download_failures_vw", sql)
        self.assertIn("ORDER BY created_at DESC", sql)
        self.assertEqual(params, {"run_id": str(run_id)})
        self.assertEqual(result, [{"decision_id": "D1", "outcome": "failed"}])

    def test_failures_without_run_id_passes_null(self) -> None:
        repo, engine = _audit(rows=[])
        self.assertEqual(repo.failures(None), [])
        (_, params), = _writes(engine)
        self.assertEqual(params, {"run_id": None})


# ---------------------------------------------------------------------------
# Migration file (read as text, never executed)
# ---------------------------------------------------------------------------


def _check_values(sql: str, constraint: str) -> set[str]:
    """Return the quoted values listed in a named CHECK (... IN (...))."""
    match = re.search(constraint + r"\s+CHECK\s*\([^)]*IN\s*\(([^)]*)\)", sql)
    if match is None:
        raise AssertionError(f"constraint {constraint} not found")
    return set(re.findall(r"'([^']+)'", match.group(1)))


class TestMigrationFile(unittest.TestCase):
    """The DDL pins the Python enums and stays idempotent."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION_SQL.read_text(encoding="utf-8")

    def test_outcome_check_matches_enum(self) -> None:
        self.assertEqual(
            _check_values(self.sql, "ck_document_download_audit_outcome"),
            {o.value for o in Outcome},
        )

    def test_selection_status_check_matches_enum(self) -> None:
        self.assertEqual(
            _check_values(self.sql, "ck_decision_registry_selection_status"),
            {s.value for s in SelectionStatus},
        )

    def test_ddl_is_guarded(self) -> None:
        self.assertNotRegex(self.sql, r"CREATE TABLE (?!IF NOT EXISTS)")
        self.assertNotRegex(self.sql, r"CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)")
        self.assertIn("CREATE OR REPLACE VIEW celex_facts.document_download_failures_vw", self.sql)

    def test_content_lookup_index_matches_query_predicate(self) -> None:
        """The sha256 index (lineage migration) covers the decision_of_content filter."""
        self.assertNotIn("ix_document_registry_sha256", self.sql)
        self.assertRegex(
            LINEAGE_SQL.read_text(encoding="utf-8"),
            r"CREATE INDEX IF NOT EXISTS ix_document_registry_sha256\s+"
            r"ON celex_facts\.document_registry \(sha256\)\s+"
            r"WHERE sha256 IS NOT NULL AND \(is_source OR is_selected\);")

    def test_legacy_link_tables_untouched(self) -> None:
        statements = re.sub(r"--[^\n]*", "", self.sql)
        self.assertNotRegex(statements, r"\b(celex_case_id|case_celex_id)\b")


class TestSourceDocumentResolver(unittest.TestCase):
    """Per-run cache over the injected source-document lookup; never raises."""

    def setUp(self) -> None:
        session_module._WARNED_ONCE.clear()

    def test_resolves_once_per_celex(self) -> None:
        calls: list[str] = []

        def lookup(decision_id: str) -> str | None:
            calls.append(decision_id)
            return f"{decision_id}_EN"

        resolver = SourceDocumentResolver(lookup)
        self.assertEqual(resolver.resolve("A"), "A_EN")
        self.assertEqual(resolver.resolve("A"), "A_EN")
        self.assertEqual(resolver.resolve("B"), "B_EN")
        self.assertEqual(calls, ["A", "B"])

    def test_unregistered_decision_caches_none(self) -> None:
        calls: list[str] = []

        def lookup(decision_id: str) -> str | None:
            calls.append(decision_id)
            return None

        resolver = SourceDocumentResolver(lookup)
        self.assertIsNone(resolver.resolve("A"))
        self.assertIsNone(resolver.resolve("A"))
        self.assertEqual(calls, ["A"])

    def test_lookup_error_warns_once_and_returns_none(self) -> None:
        def lookup(decision_id: str) -> str | None:
            raise RuntimeError("relation does not exist")

        resolver = SourceDocumentResolver(lookup)
        with self.assertLogs(
            "pipelines.rag.pgvector.source_document_resolver", "WARNING"
        ) as logs:
            self.assertIsNone(resolver.resolve("A"))
            self.assertIsNone(resolver.resolve("B"))
            self.assertIsNone(SourceDocumentResolver(lookup).resolve("C"))
        self.assertEqual(len(logs.records), 1, "one warning per process")

    def test_from_engine_without_registry_tables_resolves_null(self) -> None:
        engine = _FakeEngine(fail_probe=True)
        with self.assertLogs(
            "pipelines.rag.pgvector.source_document_resolver", "WARNING"
        ) as logs:
            resolver = SourceDocumentResolver.from_engine(engine)  # type: ignore[arg-type]
        self.assertIn("migrate_document_registry.sql", logs.output[0])
        self.assertIsNone(resolver.resolve("CELEX-31964D0233"))

    def test_from_engine_reads_the_source_flag(self) -> None:
        engine = _FakeEngine(rows=[("CELEX-31964D0233_EN",)])
        resolver = SourceDocumentResolver.from_engine(engine)  # type: ignore[arg-type]
        self.assertEqual(resolver.resolve("CELEX-31964D0233"), "CELEX-31964D0233_EN")

    def test_without_lookup_resolves_null(self) -> None:
        self.assertIsNone(SourceDocumentResolver(None).resolve("A"))


if __name__ == "__main__":
    unittest.main()
