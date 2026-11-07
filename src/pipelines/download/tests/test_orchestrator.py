"""Tests for the download-and-register orchestrator (fakes only, no DB/HTTP)."""

import datetime as dt
import hashlib
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from pipelines.download.models import (
    CandidateDocument,
    DecisionRequest,
    FetchResult,
    Outcome,
    RowFailure,
)
from pipelines.download.orchestrator import DownloadOrchestrator, RunSummary
from pipelines.download.selector import CandidateSelector
from pipelines.rag.pgvector.document_registry_repository import (
    DocumentOwnershipConflict,
)

PDF_BYTES = b"%PDF-1.4 test body"
CELEX = "31988D0172"
EURLEX_EN = f"https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:{CELEX}"
EURLEX_DE = f"https://eur-lex.europa.eu/legal-content/DE/TXT/PDF/?uri=CELEX:{CELEX}"
EC_PDF_1 = "https://ec.europa.eu/competition/antitrust/cases/dec_docs/35388/35388_9_5.pdf"
EC_PDF_2 = "https://ec.europa.eu/competition/antitrust/cases/dec_docs/35388/35388_9_6.pdf"
EC_HTML = "https://ec.europa.eu/competition/antitrust/cases/35388.html"


class _Log:
    """Shared, ordered call log of all fakes."""

    def __init__(self) -> None:
        """Create an empty log."""
        self.calls: list[tuple[object, ...]] = []

    def names(self) -> list[object]:
        """Return the call names in order.

        Returns:
            First element of every logged call.
        """
        return [c[0] for c in self.calls]

    def of(self, name: str) -> list[tuple[object, ...]]:
        """Return the calls of one name.

        Args:
            name: Call name.

        Returns:
            Matching calls in order.
        """
        return [c for c in self.calls if c[0] == name]


def _body(url: str) -> bytes:
    """Return the default PDF bytes the fake serves for a URL (distinct per URL).

    Args:
        url: Source URL.

    Returns:
        ``PDF_BYTES`` followed by the URL.
    """
    return PDF_BYTES + b"\n%" + url.encode()


class FakeFetcher:
    """Fetcher writing a valid PDF unless the URL is configured to fail."""

    def __init__(self, log: _Log, fail: dict[str, str] | None = None) -> None:
        """Create the fake.

        Args:
            log: Shared call log.
            fail: URL -> failure reason for URLs that fail.
        """
        self.log = log
        self.fail = fail or {}
        self.bodies: dict[str, bytes] = {}

    def fetch(self, document_id: str, url: str, target: Path) -> FetchResult:
        """Record the call and simulate a download.

        Args:
            document_id: Document id.
            url: Source URL.
            target: Target path.

        Returns:
            Downloaded or failed result.
        """
        self.log.calls.append(("fetch", document_id, str(target)))
        if url in self.fail:
            return FetchResult(
                document_id=document_id, source_url=url, outcome=Outcome.FAILED,
                failure_reason=self.fail[url], http_status=404, duration_ms=1,
            )
        body = self.bodies.get(url, _body(url))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return FetchResult(
            document_id=document_id, source_url=url, outcome=Outcome.DOWNLOADED,
            local_path=str(target), sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body), http_status=200, duration_ms=1,
        )


class RaisingFetcher:
    """Fetcher that records every call and raises (the pool swallows raises)."""

    def __init__(self) -> None:
        """Create the fake with no calls."""
        self.calls: list[str] = []

    def fetch(self, document_id: str, url: str, target: Path) -> FetchResult:
        """Record the call and raise.

        Args:
            document_id: Document id.
            url: Source URL.
            target: Target path.

        Raises:
            AssertionError: Always.
        """
        self.calls.append(url)
        raise AssertionError(f"fetch called offline: {url}")


class FakeRegistry:
    """Registry recording writes; configurable failures.

    Models ``is_source`` like the database: a source upsert clears the
    decision's other source rows and sets its own, all-or-nothing (a conflict
    changes nothing); a non-source upsert keeps a stored flag. More than one
    source per decision raises, like the partial unique index.
    """

    def __init__(self, log: _Log) -> None:
        """Create the fake.

        Args:
            log: Shared call log.
        """
        self.log = log
        self.registered: set[str] = set()
        self.explode_on: set[str] = set()
        self.conflict_on: set[str] = set()
        self.paths: dict[str, str | None] = {}
        self.url_owner: dict[str, str] = {}
        self.sources: dict[str, bool] = {}
        self.doc_owner: dict[str, str] = {}
        self.content: dict[str, str] = {}
        self.doc_sha: dict[str, tuple[str, bool]] = {}
        self.aliases: dict[str, str] = {}
        self.alias_reads: list[int] = []

    def upsert_decision(self, decision: DecisionRequest, selection: object) -> None:
        """Record a decision upsert.

        Args:
            decision: Decision.
            selection: Selection.

        Raises:
            RuntimeError: For decision ids in ``explode_on``.
        """
        self.log.calls.append(
            ("upsert_decision", decision.decision_id, list(decision.review_reasons))
        )
        if decision.decision_id in self.explode_on:
            raise RuntimeError("boom")

    def upsert_cases_and_links(self, decision: DecisionRequest) -> None:
        """Record a links upsert.

        Args:
            decision: Decision.
        """
        self.log.calls.append(("upsert_links", decision.decision_id))

    def upsert_document(
        self,
        document: CandidateDocument,
        fetch: FetchResult | None,
        is_selected: bool,
        *,
        decision_id: str,
        is_source: bool = False,
    ) -> None:
        """Record a document upsert.

        Args:
            document: Candidate.
            fetch: Fetch result or None.
            is_selected: Selected flag.
            decision_id: Owning decision.
            is_source: Whether the document becomes the decision's source.

        Raises:
            DocumentOwnershipConflict: For ids in ``conflict_on``.
            AssertionError: If a decision would get a second source.
        """
        self.log.calls.append((
            "upsert_document", document.document_id,
            fetch.outcome if fetch else None, is_selected, decision_id, is_source,
        ))
        if document.document_id in self.conflict_on:
            raise DocumentOwnershipConflict("owned by someone else")
        owner = self.url_owner.setdefault(document.source_url, decision_id)
        if owner != decision_id:
            raise DocumentOwnershipConflict(f"url owned by {owner}")
        self.paths[document.document_id] = fetch.local_path if fetch else None
        self.doc_owner[document.document_id] = decision_id
        if fetch is not None and fetch.sha256:
            self.doc_sha[document.document_id] = (fetch.sha256, is_selected or is_source)
        if is_source:
            for doc_id, owner_id in self.doc_owner.items():
                if owner_id == decision_id and doc_id != document.document_id:
                    self.sources[doc_id] = False
        self.sources[document.document_id] = (
            self.sources.get(document.document_id, False) or is_source)
        flagged = [d for d, o in self.doc_owner.items()
                   if o == decision_id and self.sources.get(d)]
        if len(flagged) > 1:
            raise AssertionError(f"decision {decision_id} has two sources: {flagged}")

    def source_document_id(self, decision_id: str) -> str | None:
        """Return the decision's source document.

        Args:
            decision_id: Decision id.

        Returns:
            The document id flagged as source, or None.
        """
        return next((d for d, o in self.doc_owner.items()
                     if o == decision_id and self.sources.get(d)), None)

    def decision_of_content(
        self, sha256: str, decision_id: str, document_id: str
    ) -> str | None:
        """Return the holder of these bytes like the registry query.

        ``content`` presets sha -> other decision; upserted documents count
        as for the database (other decision's selected/source document, or
        the same decision's source under another id).

        Args:
            sha256: File hash.
            decision_id: Decision the file is for.
            document_id: Document the file is for.

        Returns:
            The holding decision id, or None.
        """
        self.log.calls.append(("decision_of_content", sha256, decision_id))
        preset = self.content.get(sha256)
        if preset is not None and preset != decision_id:
            return preset
        for doc, (sha, chosen) in self.doc_sha.items():
            owner = self.doc_owner[doc]
            if sha != sha256 or doc == document_id or not chosen:
                continue
            if owner != decision_id or self.sources.get(doc):
                return owner
        return None

    def pdf_urls_of(self, keys: object) -> list[tuple[str, str]]:
        """Return the configured alias URLs of the given keys.

        Args:
            keys: Master keys.

        Returns:
            (key, url) pairs of ``aliases`` whose key is given.
        """
        wanted = set(keys)  # type: ignore[call-overload]
        self.alias_reads.append(len(wanted))
        return [(k, u) for u, k in sorted(self.aliases.items()) if k in wanted]

    def is_registered(self, document_id: str) -> bool:
        """Report registration.

        Args:
            document_id: Document id.

        Returns:
            True if configured as registered.
        """
        return document_id in self.registered


class FakeStore:
    """Master store recording upserts and flushes."""

    def __init__(self, log: _Log) -> None:
        """Create the fake.

        Args:
            log: Shared call log.
        """
        self.log = log
        self.keys: set[str] = set()
        self.urls: dict[str, str] = {}
        self.local: dict[str, str] = {}
        self.languages: dict[str, str] = {}
        self.reasons: list[str] = []
        self.flush_error: Exception | None = None
        self.web: dict[str, dict[str, str]] = {}
        self.cases: dict[str, list[str]] = {}

    def has_key(self, key: str) -> bool:
        """Report a master key.

        Args:
            key: Decision id.

        Returns:
            True if configured.
        """
        return key in self.keys

    def local_file_pdf(self, key: str) -> str | None:
        """Return the configured stored path.

        Args:
            key: Decision id.

        Returns:
            Stored path or None.
        """
        return self.local.get(key)

    def language_init(self, key: str) -> str | None:
        """Return the configured stored language.

        Args:
            key: Decision id.

        Returns:
            Stored language or None.
        """
        return self.languages.get(key)

    def url_pdf(self, key: str) -> str | None:
        """Return the stored URL of a key (reverse of ``urls``).

        Args:
            key: Decision id.

        Returns:
            The first URL mapped to the key, or None.
        """
        return next((u for u, k in self.urls.items() if k == key), None)

    def entries(self) -> list[tuple[str, dict[str, object]]]:
        """Return entries built from ``keys``, ``urls`` and ``web``.

        Returns:
            One (key, entry) pair per configured URL and per key, with the
            ``webDocDescriptor`` of ``web`` and the ``case_ids`` of ``cases``.
        """
        pairs = [(k, {"webDocDescriptor": {**self.web.get(k, {}), "url_pdf": u},
                      "case_ids": self.cases.get(k, [])})
                 for u, k in self.urls.items()]
        pairs += [(k, {"webDocDescriptor": dict(self.web.get(k, {})),
                       "case_ids": self.cases.get(k, [])})
                  for k in sorted(self.keys | set(self.web))]
        return pairs

    def upsert(
        self,
        decision: DecisionRequest,
        document: CandidateDocument,
        local_file_pdf: Path,
        language_init: str,
    ) -> list[str]:
        """Record an upsert.

        Args:
            decision: Decision.
            document: Document.
            local_file_pdf: Path.
            language_init: Language.

        Returns:
            The configured review reasons.
        """
        self.log.calls.append((
            "store_upsert", decision.decision_id, document.document_id,
            str(local_file_pdf), language_init,
        ))
        return list(self.reasons)

    def flush(self) -> None:
        """Record a flush.

        Raises:
            Exception: The configured ``flush_error``.
        """
        self.log.calls.append(("flush",))
        if self.flush_error is not None:
            raise self.flush_error


class FakeAudit:
    """Audit recording rows."""

    def __init__(self, log: _Log) -> None:
        """Create the fake.

        Args:
            log: Shared call log.
        """
        self.log = log

    def record(
        self,
        run_id: uuid.UUID,
        decision_id: str | None,
        result: FetchResult | RowFailure,
        loader_file: str,
        loader_row: int | None,
    ) -> None:
        """Record an audit row.

        Args:
            run_id: Run id.
            decision_id: Decision id or None.
            result: Result.
            loader_file: Loader file.
            loader_row: Loader row.
        """
        if isinstance(result, RowFailure):
            self.log.calls.append(
                ("audit", decision_id, "row_failure", None, result.reason)
            )
        else:
            self.log.calls.append((
                "audit", decision_id, result.outcome, result.document_id,
                result.failure_reason,
            ))


class _ForbiddenWriter:
    """Wraps a fake so any writer method raises."""

    _WRITERS = frozenset({
        "upsert_decision", "upsert_cases_and_links", "upsert_document",
        "upsert", "flush", "record", "fetch",
    })

    def __init__(self, inner: object, log: _Log) -> None:
        """Wrap a fake.

        Args:
            inner: Wrapped fake.
            log: Shared call log.
        """
        self._inner = inner
        self._log = log

    def __getattr__(self, name: str) -> object:
        """Return the attribute or a raising stub for writers.

        Args:
            name: Attribute name.

        Returns:
            The attribute, or a stub that logs and raises.
        """
        if name in self._WRITERS:
            def _forbidden(*args: object, **kwargs: object) -> None:
                self._log.calls.append(("FORBIDDEN", name))
                raise AssertionError(f"writer {name} called in dry run")
            return _forbidden
        return getattr(self._inner, name)


def _candidate(url: str, language: str | None = None, origin: str = "ec.europa.eu",
               file_format: str = "pdf", document_id: str = "provisional") -> CandidateDocument:
    """Build a candidate.

    Args:
        url: URL.
        language: Language or None.
        origin: Origin.
        file_format: Format.
        document_id: Provisional id.

    Returns:
        A CandidateDocument.
    """
    return CandidateDocument(
        document_id=document_id, source_url=url, language=language,
        origin=origin, file_format=file_format,
    )


def _decision(decision_id: str, candidates: list[CandidateDocument], *,
              celex_number: str | None = None, case: str = "AT.35388",
              date: dt.date | None = dt.date(1995, 5, 16), row: int = 1) -> DecisionRequest:
    """Build a decision.

    Args:
        decision_id: Provisional id.
        candidates: Candidates.
        celex_number: CELEX or None.
        case: First case id.
        date: Decision date.
        row: Loader row.

    Returns:
        A DecisionRequest.
    """
    return DecisionRequest(
        decision_id=decision_id, celex_number=celex_number, case_ids=[case],
        decision_date=date, title="T", candidates=candidates,
        loader_file="loader.xlsx", loader_row=row,
    )


def _celex_decision(row: int = 1) -> DecisionRequest:
    """Build a CELEX decision with an EN and a DE eur-lex PDF.

    Args:
        row: Loader row.

    Returns:
        A DecisionRequest.
    """
    return _decision(
        f"CELEX-{CELEX}",
        [_candidate(EURLEX_EN, "EN", "eur-lex"), _candidate(EURLEX_DE, "DE", "eur-lex")],
        celex_number=CELEX, row=row,
    )


DEC_ID = "DEC-AT35388-19950516"
SELECTED_ID = f"CELEX-{CELEX}"
SELECTED_DOC = f"CELEX-{CELEX}_EN"


class OrchestratorTest(unittest.TestCase):
    """Behaviour of DownloadOrchestrator.run with fake collaborators."""

    def setUp(self) -> None:
        """Create temp docs dir and fakes."""
        self._tmp = tempfile.TemporaryDirectory()
        self.docs = Path(self._tmp.name)
        self.log = _Log()
        self.fetcher = FakeFetcher(self.log)
        self.registry = FakeRegistry(self.log)
        self.store = FakeStore(self.log)
        self.audit = FakeAudit(self.log)
        self.run_id = uuid.uuid4()

    def tearDown(self) -> None:
        """Remove the temp dir."""
        self._tmp.cleanup()

    def _orchestrator(self) -> DownloadOrchestrator:
        """Build the orchestrator under test.

        Returns:
            The orchestrator wired to the fakes.
        """
        return DownloadOrchestrator(
            CandidateSelector(), self.fetcher, self.store, self.registry,
            self.audit, self.docs,
        )

    def _run(self, decisions: list[DecisionRequest], *, force: bool = False,
             dry_run: bool = False, failures: list[RowFailure] | None = None,
             offline: bool = False) -> RunSummary:
        """Run the orchestrator.

        Args:
            decisions: Decisions.
            force: Force flag.
            dry_run: Dry-run flag.
            failures: Mapper row failures.
            offline: Offline flag.

        Returns:
            The summary.
        """
        return self._orchestrator().run(
            decisions, run_id=self.run_id, force=force, dry_run=dry_run,
            max_workers=2, failures=failures or [], offline=offline,
        )

    def _selected_path(self, decision_id: str = SELECTED_ID) -> Path:
        """Return the selected-document target.

        Args:
            decision_id: Decision id.

        Returns:
            ``<docs>/<id>/<id>.pdf``.
        """
        return self.docs / decision_id / f"{decision_id}.pdf"

    def _audit_outcomes(self) -> list[tuple[object, object]]:
        """Return (document_id, outcome) of all audit rows.

        Returns:
            Pairs in order.
        """
        return [(c[3], c[2]) for c in self.log.of("audit")]

    def test_existing_valid_pdf_registered_is_skipped_exists(self) -> None:
        """Valid file + registry row + master key -> skipped_exists, no writes."""
        path = self._selected_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(PDF_BYTES)
        self.registry.registered.add(SELECTED_DOC)
        self.store.keys.add(SELECTED_ID)
        summary = self._run([_celex_decision()])
        self.assertEqual(self.log.of("fetch"), [])
        self.assertEqual(self.log.of("store_upsert"), [])
        selected_docs = [c for c in self.log.of("upsert_document") if c[3]]
        self.assertEqual(selected_docs, [])
        self.assertIn((SELECTED_DOC, Outcome.SKIPPED_EXISTS), self._audit_outcomes())
        self.assertEqual(summary.outcomes[Outcome.SKIPPED_EXISTS], 1)

    def test_existing_valid_pdf_unregistered_is_registered_existing_without_http(self) -> None:
        """Valid file without registration -> registered_existing, no fetch."""
        path = self._selected_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(PDF_BYTES)
        summary = self._run([_celex_decision()])
        self.assertEqual(self.log.of("fetch"), [])
        self.assertIn(
            ("upsert_document", SELECTED_DOC, Outcome.REGISTERED_EXISTING, True,
             SELECTED_ID, True),
            self.log.calls,
        )
        self.assertEqual(len(self.log.of("store_upsert")), 1)
        self.assertIn(
            (SELECTED_DOC, Outcome.REGISTERED_EXISTING), self._audit_outcomes()
        )
        self.assertEqual(summary.outcomes[Outcome.REGISTERED_EXISTING], 1)

    def test_invalid_existing_file_is_redownloaded(self) -> None:
        """A file without PDF magic is re-downloaded."""
        path = self._selected_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"<html>error</html>")
        self.registry.registered.add(SELECTED_DOC)
        self.store.keys.add(SELECTED_ID)
        summary = self._run([_celex_decision()])
        self.assertEqual(
            self.log.of("fetch"), [("fetch", SELECTED_DOC, str(path))]
        )
        self.assertTrue(path.read_bytes().startswith(PDF_BYTES))
        self.assertEqual(summary.outcomes[Outcome.DOWNLOADED], 1)

    def test_force_redownloads(self) -> None:
        """--force fetches even a valid registered file."""
        path = self._selected_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(PDF_BYTES)
        self.registry.registered.add(SELECTED_DOC)
        self.store.keys.add(SELECTED_ID)
        summary = self._run([_celex_decision()], force=True)
        self.assertEqual(len(self.log.of("fetch")), 1)
        self.assertEqual(summary.outcomes[Outcome.DOWNLOADED], 1)
        self.assertEqual(summary.outcomes[Outcome.SKIPPED_EXISTS], 0)

    def test_undecided_goes_to_candidates_dir_and_gets_no_master_entry(self) -> None:
        """Unmarked PDFs go to candidates/ and never into the master JSON."""
        decision = _decision(DEC_ID, [_candidate(EC_PDF_1), _candidate(EC_PDF_2)])
        summary = self._run([decision])
        targets = sorted(c[2] for c in self.log.of("fetch"))
        cand_dir = self.docs / DEC_ID / "candidates"
        self.assertEqual(
            targets,
            [str(cand_dir / "35388_9_5.pdf"), str(cand_dir / "35388_9_6.pdf")],
        )
        self.assertEqual(self.log.of("store_upsert"), [])
        docs = {c[1]: c[3] for c in self.log.of("upsert_document")}
        self.assertEqual(docs, {"35388_9_5": True, "35388_9_6": True})
        self.assertEqual(summary.outcomes[Outcome.DOWNLOADED], 2)

    def test_failure_registers_nothing_and_is_audited(self) -> None:
        """A failed fetch is audited and registers neither document nor master."""
        self.fetcher.fail[EURLEX_EN] = "http_404"
        summary = self._run([_celex_decision()])
        selected_docs = [c for c in self.log.of("upsert_document") if c[1] == SELECTED_DOC]
        self.assertEqual(selected_docs, [])
        self.assertEqual(self.log.of("store_upsert"), [])
        self.assertIn(
            ("audit", SELECTED_ID, Outcome.FAILED, SELECTED_DOC, "http_404"),
            self.log.calls,
        )
        self.assertEqual(summary.failure_reasons, {"http_404": 1})
        self.assertEqual(summary.exit_code, 1)

    def test_one_bad_decision_does_not_abort_run(self) -> None:
        """An unexpected error in one decision is counted; the next proceeds."""
        self.registry.explode_on.add(SELECTED_ID)
        good = _decision(DEC_ID, [_candidate(EC_PDF_1)], row=2)
        summary = self._run([_celex_decision(), good])
        self.assertEqual(summary.decisions_errored, 1)
        self.assertEqual(summary.decisions_processed, 1)
        self.assertEqual(
            [c[1] for c in self.log.of("fetch")], ["35388_9_5"]
        )
        self.assertTrue(any(
            c[1] == SELECTED_ID and c[2] == "row_failure" for c in self.log.of("audit")
        ))
        self.assertEqual(summary.exit_code, 1)

    def test_decision_without_pdf_registered_and_audited_no_pdf_source(self) -> None:
        """HTML-only decision: registered, no_pdf_source + not_selected audited."""
        decision = _decision(DEC_ID, [_candidate(EC_HTML, file_format="html")])
        summary = self._run([decision])
        self.assertEqual(self.log.of("fetch"), [])
        self.assertEqual(self.log.of("upsert_decision")[0][1], DEC_ID)
        outcomes = [c[2] for c in self.log.of("audit")]
        self.assertIn(Outcome.NO_PDF_SOURCE, outcomes)
        self.assertIn(Outcome.NOT_SELECTED, outcomes)
        self.assertEqual(summary.outcomes[Outcome.NO_PDF_SOURCE], 1)
        self.assertEqual(summary.exit_code, 0)

    def test_same_decision_id_collision_gets_suffix(self) -> None:
        """Second decision with the same id gets _2 and a review reason."""
        first = _decision(DEC_ID, [_candidate(EC_PDF_1)], row=1)
        second = _decision(DEC_ID, [_candidate(EC_PDF_2)], row=2)
        self._run([first, second])
        ids = [c[1] for c in self.log.of("upsert_decision")]
        self.assertEqual(ids, [DEC_ID, f"{DEC_ID}_2"])
        reasons = self.log.of("upsert_decision")[1][2]
        self.assertTrue(any("collision" in r for r in reasons))

    def test_dry_run_calls_no_writer(self) -> None:
        """Dry run reads only: no writer, no file, same counters."""
        self.fetcher = _ForbiddenWriter(self.fetcher, self.log)
        self.registry = _ForbiddenWriter(self.registry, self.log)
        self.store = _ForbiddenWriter(self.store, self.log)
        self.audit = _ForbiddenWriter(self.audit, self.log)
        decisions = [
            _celex_decision(),
            _decision(DEC_ID, [_candidate(EC_PDF_1)], row=2),
            _decision("bad/id", [_candidate(EC_PDF_2)], case="", date=None, row=3),
        ]
        summary = self._run(decisions, dry_run=True)
        self.assertEqual(self.log.calls, [])
        self.assertEqual(list(self.docs.iterdir()), [])
        self.assertTrue(summary.dry_run)
        self.assertEqual(summary.decisions_errored, 0)
        self.assertEqual(summary.outcomes[Outcome.DOWNLOADED], 2)
        self.assertEqual(summary.outcomes[Outcome.NOT_SELECTED], 1)
        self.assertEqual(summary.row_failures, 1)

    def test_candidate_document_ids_rederived_from_final_decision_id(self) -> None:
        """Two DEC- decisions with the same id get distinct document ids."""
        first = _decision(DEC_ID, [_candidate(EURLEX_EN, "EN", "eur-lex")], row=1)
        second = _decision(
            DEC_ID, [_candidate(f"{EURLEX_EN}&from=EN", "EN", "eur-lex")], row=2)
        self._run([first, second])
        fetched = sorted(c[1] for c in self.log.of("fetch"))
        self.assertEqual(fetched, sorted([f"{DEC_ID}_EN", f"{DEC_ID}_2_EN"]))
        second_id = f"{DEC_ID}_2"
        self.assertIn(
            ("fetch", f"{second_id}_EN", str(self._selected_path(second_id))),
            self.log.calls,
        )

    def test_duplicate_document_id_in_decision_is_audited(self) -> None:
        """Two URLs with the same file stem: the second is a failed candidate."""
        dup = "https://db-comp.eu/other/35388_9_5.pdf"
        summary = self._run([_decision(DEC_ID, [_candidate(EC_PDF_1), _candidate(dup)])])
        self.assertEqual([c[1] for c in self.log.of("fetch")], ["35388_9_5"])
        self.assertEqual(summary.failure_reasons, {"duplicate_document_id": 1})
        self.assertEqual(summary.outcomes[Outcome.DOWNLOADED], 1)

    def test_unsafe_decision_id_is_row_failure_not_exception(self) -> None:
        """An unsafe id is audited as a row failure with decision_id None."""
        bad = _decision("../evil", [_candidate(EC_PDF_1)], case="", date=None)
        summary = self._run([bad, _decision(DEC_ID, [_candidate(EC_PDF_2)], row=2)])
        row_audits = [c for c in self.log.of("audit") if c[2] == "row_failure"]
        self.assertEqual(len(row_audits), 1)
        self.assertIsNone(row_audits[0][1])
        self.assertEqual(summary.row_failures, 1)
        self.assertEqual(summary.decisions_skipped, 1)
        self.assertEqual(summary.decisions_processed, 1)
        self.assertEqual(summary.decisions_errored, 0)

    def test_ownership_conflict_is_audited_and_run_continues(self) -> None:
        """A DocumentOwnershipConflict becomes a failed audit row and a reason."""
        self.registry.conflict_on.add("35388_9_5")
        first = _decision(DEC_ID, [_candidate(EC_PDF_1)], row=1)
        other = _decision("DEC-AT1-19950516", [_candidate(EC_PDF_2)], case="AT.1", row=2)
        summary = self._run([first, other])
        self.assertIn(
            ("audit", DEC_ID, Outcome.FAILED, "35388_9_5", "document_owned_by_other_decision"),
            [c[:4] + (str(c[4]).split(":")[0],) for c in self.log.of("audit")],
        )
        self.assertEqual(summary.failure_reasons["document_owned_by_other_decision"], 1)
        last_reasons = [c[2] for c in self.log.of("upsert_decision") if c[1] == DEC_ID][-1]
        self.assertTrue(any("35388_9_5" in r for r in last_reasons))
        self.assertIn("35388_9_6", [c[1] for c in self.log.of("upsert_document")])
        self.assertEqual(summary.outcomes[Outcome.FAILED], 1)

    def test_master_reasons_flow_into_decision_review_reasons(self) -> None:
        """Master upsert review reasons are stored on the decision row."""
        self.store.reasons = ["url_pdf conflict: stored a supplied b"]
        summary = self._run([_celex_decision()])
        last = self.log.of("upsert_decision")[-1]
        self.assertIn("url_pdf conflict: stored a supplied b", last[2])
        self.assertEqual(summary.review_reasons, 1)

    def test_registry_called_before_fetch(self) -> None:
        """Order: decision, links, fetch, document, master, flush."""
        self._run([_celex_decision()])
        names = self.log.names()
        first_fetch = names.index("fetch")
        self.assertLess(names.index("upsert_decision"), first_fetch)
        self.assertLess(names.index("upsert_links"), first_fetch)
        selected_doc = [
            i for i, c in enumerate(self.log.calls)
            if c[0] == "upsert_document" and c[1] == SELECTED_DOC
        ]
        self.assertGreater(selected_doc[0], first_fetch)
        self.assertGreater(names.index("store_upsert"), selected_doc[0])
        self.assertEqual(names[-1], "flush")
        self.assertEqual(names.count("flush"), 1)

    def test_existing_master_key_is_reused(self) -> None:
        """A master entry matched by URL donates its key as decision id."""
        self.store.urls[EC_PDF_1] = "32450_35_6"
        self._run([_decision(DEC_ID, [_candidate(EC_PDF_1, "EN")])])
        self.assertEqual(self.log.of("upsert_decision")[0][1], "32450_35_6")
        self.assertEqual(
            self.log.of("fetch")[0][2], str(self._selected_path("32450_35_6"))
        )
        self.assertEqual(self.log.of("store_upsert")[0][1], "32450_35_6")

    def test_summary_exit_code(self) -> None:
        """exit_code: 0 clean, 0 for row failures only, 1 for a failed candidate."""
        clean = self._run([_decision(DEC_ID, [_candidate(EC_PDF_1)])])
        self.assertEqual(clean.exit_code, 0)
        mapped = self._run(
            [], failures=[RowFailure(loader_file="l.xlsx", loader_row=3, reason="x")]
        )
        self.assertEqual(mapped.exit_code, 0)
        self.assertEqual(mapped.row_failures, 1)
        self.fetcher.fail[EC_PDF_2] = "timeout"
        failed = self._run([_decision("DEC-AT9-19950516", [_candidate(EC_PDF_2)],
                                      case="AT.9")])
        self.assertEqual(failed.exit_code, 1)
        self.assertIn(("audit", None, "row_failure", None, "x"), self.log.calls)


    def test_shared_url_owned_by_first_decision_regardless_of_completion(self) -> None:
        """First loader row owns a shared URL; the second never fetches it."""
        first_started = threading.Event()
        release = threading.Event()
        inner = self.fetcher

        class _SlowFirst:
            """Delays the first decision's fetch until the others are done."""

            def fetch(self, document_id: str, url: str, target: Path) -> FetchResult:
                """Fetch, delaying the first decision.

                Args:
                    document_id: Document id.
                    url: URL.
                    target: Target.

                Returns:
                    The inner result.
                """
                if DEC_ID + "/" in str(target):
                    first_started.set()
                    release.wait(timeout=2)
                else:
                    release.set()
                return inner.fetch(document_id, url, target)

        self.fetcher = _SlowFirst()
        first = _decision(DEC_ID, [_candidate(EC_PDF_1)], row=1)
        second = _decision("DEC-AT2-19950516", [_candidate(EC_PDF_1), _candidate(EC_PDF_2)],
                           case="AT.2", row=2)
        summary = self._run([first, second])
        fetched_urls = [c[2] for c in self.log.of("fetch")]
        self.assertEqual(sum("35388_9_5" in t for t in fetched_urls), 1)
        self.assertEqual(self.registry.url_owner[EC_PDF_1], DEC_ID)
        self.assertIn(
            ("audit", "DEC-AT2-19950516", Outcome.NOT_SELECTED, "35388_9_5",
             f"url_claimed_by_{DEC_ID}"),
            self.log.calls,
        )
        reasons = self.log.of("upsert_decision")[-1][2]
        self.assertTrue(any("claimed by" in r for r in reasons))
        self.assertEqual(summary.outcomes[Outcome.FAILED], 0)
        self.assertEqual(summary.exit_code, 0)

    def _stored_entry(self) -> tuple[str, Path, DecisionRequest]:
        """Configure a master entry with a non-standard stored PDF path.

        Returns:
            Key, stored path and a decision matching the entry.
        """
        key = "40013_1084_2"
        url = "https://ec.europa.eu/competition/antitrust/cases/dec_docs/40013/AT_40013_5708398_1084_5.pdf"
        stored = self.docs / key / "AT_40013_5708398_1084_5.pdf"
        self.store.keys.add(key)
        self.store.local[key] = str(stored)
        return key, stored, _decision(key, [_candidate(url, "EN")], case="AT.40013")

    def test_stored_local_file_pdf_is_used(self) -> None:
        """Existing master path: no download, DB row names the stored path."""
        key, stored, decision = self._stored_entry()
        stored.parent.mkdir(parents=True)
        stored.write_bytes(PDF_BYTES)
        summary = self._run([decision])
        self.assertEqual(self.log.of("fetch"), [])
        self.assertEqual(summary.outcomes[Outcome.REGISTERED_EXISTING], 1)
        self.assertEqual(self.registry.paths["AT_40013_5708398_1084_5"], str(stored))
        self.assertEqual(self.log.of("store_upsert")[0][3], str(stored))
        self.assertFalse(self._selected_path(key).exists())
        self.registry.registered.add("AT_40013_5708398_1084_5")
        again = self._run([decision])
        self.assertEqual(again.outcomes[Outcome.SKIPPED_EXISTS], 1)
        self.assertEqual(self.log.of("fetch"), [])

    def test_stored_local_file_pdf_missing_is_downloaded_there(self) -> None:
        """A missing stored file is downloaded to the stored path."""
        _key, stored, decision = self._stored_entry()
        self._run([decision])
        self.assertEqual(self.log.of("fetch")[0][2], str(stored))
        self.assertEqual(self.registry.paths["AT_40013_5708398_1084_5"], str(stored))

    def test_exception_from_future_is_failed_candidate(self) -> None:
        """A fetcher exception fails that candidate; the run continues."""
        inner = self.fetcher

        class _Raising:
            """Raises for one URL."""

            def fetch(self, document_id: str, url: str, target: Path) -> FetchResult:
                """Fetch or raise.

                Args:
                    document_id: Document id.
                    url: URL.
                    target: Target.

                Returns:
                    The inner result.

                Raises:
                    RuntimeError: For EC_PDF_1.
                """
                if url == EC_PDF_1:
                    raise RuntimeError("socket gone")
                return inner.fetch(document_id, url, target)

        self.fetcher = _Raising()
        summary = self._run([_decision(DEC_ID, [_candidate(EC_PDF_1), _candidate(EC_PDF_2)])])
        self.assertEqual(summary.failure_reasons, {"unexpected_error": 1})
        self.assertEqual(summary.outcomes[Outcome.DOWNLOADED], 1)
        self.assertEqual(summary.exit_code, 1)

    def test_file_invalid_after_download_is_failed_with_reason(self) -> None:
        """A downloaded result without a valid file is failed, not registered."""

        class _Garbage:
            """Claims success but writes HTML."""

            def fetch(self, document_id: str, url: str, target: Path) -> FetchResult:
                """Write garbage.

                Args:
                    document_id: Document id.
                    url: URL.
                    target: Target.

                Returns:
                    A downloaded result.
                """
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"<html>")
                return FetchResult(document_id=document_id, source_url=url,
                                   outcome=Outcome.DOWNLOADED, local_path=str(target),
                                   duration_ms=1)

        self.fetcher = _Garbage()
        summary = self._run([_decision(DEC_ID, [_candidate(EC_PDF_1)])])
        self.assertEqual(summary.failure_reasons, {"file_invalid_after_download": 1})
        self.assertEqual(self.log.of("upsert_document"), [])
        reasons = self.log.of("upsert_decision")[-1][2]
        self.assertTrue(any("file_invalid_after_download" in r for r in reasons))

    def test_flush_called_when_phase_two_raises(self) -> None:
        """A BaseException escaping the pool still flushes the master store."""

        class _Interrupt:
            """Raises KeyboardInterrupt."""

            def fetch(self, document_id: str, url: str, target: Path) -> FetchResult:
                """Interrupt.

                Args:
                    document_id: Document id.
                    url: URL.
                    target: Target.

                Raises:
                    KeyboardInterrupt: Always.
                """
                raise KeyboardInterrupt

        self.fetcher = _Interrupt()
        self.store.flush_error = OSError("disk full")
        with self.assertRaises(KeyboardInterrupt):
            self._run([_celex_decision()])
        self.assertEqual(self.log.of("flush"), [("flush",)])

    def test_flush_error_raised_after_clean_run(self) -> None:
        """A flush OSError propagates when nothing else went wrong."""
        self.store.flush_error = OSError("disk full")
        with self.assertRaises(OSError):
            self._run([_celex_decision()])


    def test_not_selected_url_is_not_claimed(self) -> None:
        """A's not-selected DE URL stays available for B, which selects it."""
        b = _decision("DEC-AT3-19950516", [_candidate(EURLEX_DE, "DE", "eur-lex")],
                      case="AT.3", row=2)
        summary = self._run([_celex_decision(row=1), b])
        b_fetch = [c for c in self.log.of("fetch") if "DEC-AT3-19950516" in c[1]]
        self.assertEqual(len(b_fetch), 1)
        self.assertEqual(self.registry.url_owner[EURLEX_DE], "DEC-AT3-19950516")
        self.assertIn(
            ("audit", SELECTED_ID, Outcome.NOT_SELECTED, f"{SELECTED_ID}_DE",
             "url_claimed_by_DEC-AT3-19950516"),
            self.log.calls,
        )
        self.assertEqual(summary.outcomes[Outcome.FAILED], 0)
        self.assertEqual(summary.exit_code, 0)

    def test_same_dec_id_same_url_second_is_no_pdf_source(self) -> None:
        """DEC- collision with the same URL: second is no_pdf_source, exit 0."""
        summary = self._run([_decision(DEC_ID, [_candidate(EC_PDF_1)], row=1),
                             _decision(DEC_ID, [_candidate(EC_PDF_1)], row=2)])
        second = f"{DEC_ID}_2"
        self.assertEqual([c[1] for c in self.log.of("fetch")], ["35388_9_5"])
        self.assertIn(
            ("audit", second, Outcome.NOT_SELECTED, "35388_9_5",
             f"url_claimed_by_{DEC_ID}"),
            self.log.calls,
        )
        audits = [c[2] for c in self.log.of("audit") if c[1] == second]
        self.assertIn(Outcome.NO_PDF_SOURCE, audits)
        upserts = [c for c in self.log.of("upsert_decision") if c[1] == second]
        self.assertTrue(upserts)
        self.assertTrue(any("claimed by" in r for r in upserts[-1][2]))
        self.assertEqual(summary.outcomes[Outcome.FAILED], 0)
        self.assertEqual(summary.exit_code, 0)

    def _en_and_de_rows(self, *, en_first: bool) -> list[DecisionRequest]:
        """Build two rows of one CELEX: an EN-only and a DE-only row.

        Args:
            en_first: True puts the EN row first.

        Returns:
            The two decisions in loader order (rows 272 and 274).
        """
        en = _decision(f"CELEX-{CELEX}", [_candidate(EURLEX_EN, "EN", "eur-lex")],
                       celex_number=CELEX, case="IV/31.017", row=272)
        de = _decision(f"CELEX-{CELEX}", [_candidate(EURLEX_DE, "DE", "eur-lex")],
                       celex_number=CELEX, case="IV/31.018", row=274)
        en_row, de_row = (en, de) if en_first else (de.model_copy(update={"loader_row": 272}),
                                                   en.model_copy(update={"loader_row": 274}))
        return [en_row, de_row]

    def test_same_celex_rows_are_merged_and_en_selected(self) -> None:
        """EN row + DE-only row of one CELEX: one decision, one master entry."""
        summary = self._run(self._en_and_de_rows(en_first=True))
        self.assertEqual({c[1] for c in self.log.of("upsert_decision")}, {SELECTED_ID})
        self.assertEqual(len(self.log.of("upsert_links")), 1)
        self.assertEqual([c[1] for c in self.log.of("fetch")], [SELECTED_DOC])
        upserts = self.log.of("store_upsert")
        self.assertEqual(len(upserts), 1)
        self.assertEqual(upserts[0][1:3], (SELECTED_ID, SELECTED_DOC))
        self.assertIn(
            ("audit", SELECTED_ID, Outcome.NOT_SELECTED, f"{SELECTED_ID}_DE", None),
            self.log.calls,
        )
        reasons = self.log.of("upsert_decision")[0][2]
        self.assertIn(f"merged duplicate row 274 of loader.xlsx ({SELECTED_ID})", reasons)
        self.assertFalse(any("collision" in r for r in reasons))
        self.assertEqual(summary.decisions_processed, 1)
        self.assertEqual(summary.exit_code, 0)

    def test_merged_decision_unites_case_ids_and_prefers_en_of_later_row(self) -> None:
        """DE-only row first, EN row later: EN still wins; case ids united."""
        seen: list[DecisionRequest] = []
        inner = self.registry.upsert_decision

        def _capture(decision: DecisionRequest, selection: object) -> None:
            """Capture the decision and delegate.

            Args:
                decision: Decision.
                selection: Selection.
            """
            seen.append(decision.model_copy(deep=True))
            inner(decision, selection)

        self.registry.upsert_decision = _capture  # type: ignore[method-assign]
        self._run(self._en_and_de_rows(en_first=False))
        self.assertEqual([c[1] for c in self.log.of("fetch")], [SELECTED_DOC])
        self.assertEqual(len(self.log.of("store_upsert")), 1)
        self.assertEqual(seen[0].case_ids, ["IV/31.018", "IV/31.017"])
        self.assertEqual(seen[0].loader_row, 272)
        self.assertEqual(len(seen[0].candidates), 2)

    def test_celex_differing_from_master_key_reuses_master_key(self) -> None:
        """The selected URL owned by master key K wins over an absent CELEX key."""
        url = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32002D0758(01)"
        self.store.urls[url] = "CELEX-32002D0758_01"
        self.store.keys.add("CELEX-32002D0758_01")
        decision = _decision("CELEX-32002D0758", [_candidate(url, "EN", "eur-lex")],
                             celex_number="CELEX-32002D0758")
        self._run([decision])
        first = self.log.of("upsert_decision")[0]
        self.assertEqual(first[1], "CELEX-32002D0758_01")
        self.assertIn(
            "CELEX CELEX-32002D0758 differs from master key CELEX-32002D0758_01",
            first[2])
        self.assertEqual(self.log.of("store_upsert")[0][1], "CELEX-32002D0758_01")

    def test_celex_key_in_master_is_kept(self) -> None:
        """A CELEX key that is itself a master key is not replaced."""
        self.store.urls[EURLEX_EN] = "OTHER-KEY"
        self.store.keys.update({SELECTED_ID, "OTHER-KEY"})
        self._run([_celex_decision()])
        first = self.log.of("upsert_decision")[0]
        self.assertEqual(first[1], SELECTED_ID)
        self.assertFalse(any("differs from master key" in r for r in first[2]))

    def _de_stored_entry(self, language: str) -> tuple[Path, Path]:
        """Configure the CELEX master entry with a stored file of a language.

        Args:
            language: Stored ``language_init``.

        Returns:
            The stored path and the staging path of the EN document.
        """
        stored = self.docs / SELECTED_ID / f"{SELECTED_ID}.pdf"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(PDF_BYTES)
        self.store.keys.add(SELECTED_ID)
        self.store.local[SELECTED_ID] = str(stored)
        self.store.languages[SELECTED_ID] = language
        return stored, self.docs / SELECTED_ID / "candidates" / f"{SELECTED_DOC}.pdf"

    def test_stored_file_of_other_language_is_not_reused(self) -> None:
        """DE file stored, EN selected: EN staged in candidates, master untouched."""
        stored, staged = self._de_stored_entry("de")
        summary = self._run([_celex_decision()])
        self.assertEqual(self.log.of("fetch"),
                         [("fetch", SELECTED_DOC, str(staged))])
        self.assertEqual(self.log.of("store_upsert"), [])
        self.assertEqual(stored.read_bytes(), PDF_BYTES)
        self.assertEqual(self.registry.paths[SELECTED_DOC], str(staged))
        reasons = self.log.of("upsert_decision")[0][2]
        self.assertIn(f"master entry {SELECTED_ID} holds de file; selected en "
                      "staged in candidates", reasons)
        self.assertEqual(summary.exit_code, 0)

    def test_stored_file_of_same_language_is_reused(self) -> None:
        """EN file stored, EN selected: reused, no download."""
        stored, _staged = self._de_stored_entry("en")
        summary = self._run([_celex_decision()])
        self.assertEqual(self.log.of("fetch"), [])
        self.assertEqual(summary.outcomes[Outcome.REGISTERED_EXISTING], 1)
        self.assertEqual(self.registry.paths[SELECTED_DOC], str(stored))
        self.assertEqual(self.log.of("store_upsert")[0][3], str(stored))

    def test_stored_file_under_other_url_is_not_made_source(self) -> None:
        """Same language, other stored url_pdf: file reused, source row kept."""
        stored, _staged = self._de_stored_entry("en")
        self.store.urls["https://ec.europa.eu/old/original.pdf"] = SELECTED_ID
        self._run([_celex_decision()])
        self.assertEqual(self.log.of("fetch"), [])
        self.assertEqual(self.registry.paths[SELECTED_DOC], str(stored))
        self.assertIn(("upsert_document", SELECTED_DOC, Outcome.REGISTERED_EXISTING,
                       True, SELECTED_ID, False), self.log.calls)
        self.assertEqual(self.log.of("store_upsert")[0][3], str(stored))

    def test_stored_file_with_matching_url_is_reused(self) -> None:
        """A stored url_pdf equal to the selected URL (http/from variant) reuses it."""
        stored, _staged = self._de_stored_entry("de")
        self.store.urls[EURLEX_EN.replace("https", "http", 1) + "&from=DE"] = SELECTED_ID
        self._run([_celex_decision()])
        self.assertEqual(self.log.of("fetch"), [])
        self.assertEqual(self.registry.paths[SELECTED_DOC], str(stored))

    def _source_flags(self) -> dict[object, object]:
        """Return document id -> is_source of the logged document upserts.

        Returns:
            The last logged flag per document.
        """
        return {c[1]: c[5] for c in self.log.of("upsert_document")}

    def test_master_file_document_is_source(self) -> None:
        """registered_existing and downloaded master documents become the source."""
        for outcome in (Outcome.REGISTERED_EXISTING, Outcome.DOWNLOADED):
            with self.subTest(outcome=outcome.value):
                self.setUp()
                if outcome is Outcome.REGISTERED_EXISTING:
                    path = self._selected_path()
                    path.parent.mkdir(parents=True)
                    path.write_bytes(PDF_BYTES)
                summary = self._run([_celex_decision()])
                self.assertEqual(summary.outcomes[outcome], 1)
                self.assertIn(("upsert_document", SELECTED_DOC, outcome, True,
                               SELECTED_ID, True), self.log.calls)
                self.assertEqual(self.registry.source_document_id(SELECTED_ID),
                                 SELECTED_DOC)
                self.assertFalse(self._source_flags()[f"CELEX-{CELEX}_DE"])
                self.tearDown()

    def test_staged_and_undecided_are_not_source(self) -> None:
        """Language-guard staged files and undecided candidates are no source."""
        self._de_stored_entry("de")
        self._run([_celex_decision()])
        self.assertFalse(self._source_flags()[SELECTED_DOC])
        self.assertIsNone(self.registry.source_document_id(SELECTED_ID))
        self.log.calls.clear()
        self._run([_decision(DEC_ID, [_candidate(EC_PDF_1), _candidate(EC_PDF_2)])])
        self.assertEqual(self._source_flags(),
                         {"35388_9_5": False, "35388_9_6": False})
        self.assertIsNone(self.registry.source_document_id(DEC_ID))

    def test_new_source_clears_previous_source(self) -> None:
        """A later run's new master document replaces the earlier source."""
        self.registry.doc_owner[f"CELEX-{CELEX}_DE"] = SELECTED_ID
        self.registry.sources[f"CELEX-{CELEX}_DE"] = True
        self._run([_celex_decision()])
        self.assertEqual(self.registry.source_document_id(SELECTED_ID), SELECTED_DOC)
        self.assertFalse(self.registry.sources[f"CELEX-{CELEX}_DE"])

    def test_ownership_conflict_keeps_previous_source(self) -> None:
        """A rejected new source leaves the earlier source in place."""
        old = f"CELEX-{CELEX}_DE"
        self.registry.doc_owner[old] = SELECTED_ID
        self.registry.sources[old] = True
        self.registry.conflict_on.add(SELECTED_DOC)
        summary = self._run([_celex_decision()])
        self.assertEqual(summary.failure_reasons["document_owned_by_other_decision"], 1)
        self.assertEqual(self.registry.source_document_id(SELECTED_ID), old)

    def _two_decisions(self) -> list[DecisionRequest]:
        """Build a decision with a file on disk and one without.

        Returns:
            The decisions (file present for the first one only).
        """
        path = self._selected_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(PDF_BYTES)
        other = _decision("35388_9_5", [_candidate(EC_PDF_1, "EN")], row=2)
        return [_celex_decision(), other]

    def test_offline_never_fetches(self) -> None:
        """Even a missing file and --force do not reach the fetcher."""
        fetcher = self.fetcher = RaisingFetcher()
        decisions = self._two_decisions()
        self._run(decisions, offline=True)
        self._run(decisions, offline=True, force=True)
        self.assertEqual(fetcher.calls, [])

    def test_offline_registers_existing_file(self) -> None:
        """A valid file becomes registered_existing, or skipped_exists if known."""
        decisions = self._two_decisions()
        summary = self._run(decisions[:1], offline=True)
        self.assertEqual(summary.outcomes[Outcome.REGISTERED_EXISTING], 1)
        self.assertIn(
            ("upsert_document", SELECTED_DOC, Outcome.REGISTERED_EXISTING, True,
             SELECTED_ID, True), self.log.calls)
        self.registry.registered.add(SELECTED_DOC)
        self.store.keys.add(SELECTED_ID)
        summary = self._run(decisions[:1], offline=True)
        self.assertEqual(summary.outcomes[Outcome.SKIPPED_EXISTS], 1)

    def test_offline_missing_file_is_failed_file_missing_offline(self) -> None:
        """A missing file is audited as failed; the run continues."""
        summary = self._run(self._two_decisions(), offline=True)
        self.assertEqual(summary.outcomes[Outcome.REGISTERED_EXISTING], 1)
        self.assertEqual(summary.outcomes[Outcome.FAILED], 1)
        self.assertEqual(summary.failure_reasons, {"file_missing_offline": 1})
        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(summary.decisions_processed, 2)
        failed = [c for c in self.log.of("audit") if c[2] is Outcome.FAILED]
        self.assertEqual(len(failed), 1)
        self.assertTrue(str(failed[0][4]).startswith("file_missing_offline"))
        self.assertEqual(self.log.of("fetch"), [])

    def test_offline_invalid_file_is_failed(self) -> None:
        """A file without PDF magic counts as missing offline."""
        path = self._selected_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"<html>error</html>")
        summary = self._run([_celex_decision()], offline=True)
        self.assertEqual(summary.failure_reasons, {"file_missing_offline": 1})

    def test_offline_dry_run_counts_without_writes(self) -> None:
        """Offline plus dry run counts the same outcomes and writes nothing."""
        log = _Log()
        self.registry = _ForbiddenWriter(FakeRegistry(log), log)
        self.audit = _ForbiddenWriter(FakeAudit(log), log)
        self.store = _ForbiddenWriter(FakeStore(log), log)
        self.fetcher = RaisingFetcher()
        summary = self._run(self._two_decisions(), offline=True, dry_run=True)
        self.assertEqual(summary.outcomes[Outcome.REGISTERED_EXISTING], 1)
        self.assertEqual(summary.outcomes[Outcome.FAILED], 1)
        self.assertEqual(log.of("FORBIDDEN"), [])

    def test_undecided_resolved_by_master_entry_uses_stored_file(self) -> None:
        """An unmarked URL equal to the master url_pdf is the decision's source."""
        stored = self.docs / "35388_9_5" / "35388_9_5.pdf"
        stored.parent.mkdir(parents=True)
        stored.write_bytes(PDF_BYTES)
        self.store.keys.add("35388_9_5")
        self.store.urls[EC_PDF_1] = "35388_9_5"
        self.store.local["35388_9_5"] = str(stored)
        other = _candidate(EC_PDF_2)
        decision = _decision("35388_9_5", [_candidate(EC_PDF_1), other])
        self.fetcher = RaisingFetcher()
        summary = self._run([decision], offline=True)
        self.assertEqual(summary.outcomes[Outcome.REGISTERED_EXISTING], 1)
        self.assertEqual(summary.outcomes[Outcome.FAILED], 0)
        docs = [c for c in self.log.of("upsert_document") if c[3]]
        self.assertEqual(len(docs), 1)
        self.assertTrue(docs[0][5])
        self.assertEqual(self.log.of("store_upsert")[0][3], str(stored))
        reasons = self.log.of("upsert_decision")[0][2]
        self.assertIn("undecided resolved by master entry", reasons)

    def test_undecided_without_master_url_match_stays_staged(self) -> None:
        """Unmarked URLs not equal to the master url_pdf are still staged."""
        self.store.keys.add("35388_9_5")
        self.store.urls[EC_PDF_2] = "35388_9_5"
        decision = _decision("35388_9_5", [_candidate(EC_PDF_1)])
        self._run([decision])
        path = self.docs / "35388_9_5" / "candidates"
        self.assertTrue(any(path.iterdir()))
        self.assertEqual(self.log.of("store_upsert"), [])



class ContentNetTest(unittest.TestCase):
    """Content safety net: a downloaded file another decision holds is dropped."""

    def setUp(self) -> None:
        """Create temp docs dir and fakes."""
        self._tmp = tempfile.TemporaryDirectory()
        self.docs = Path(self._tmp.name)
        self.log = _Log()
        self.fetcher = FakeFetcher(self.log)
        self.registry = FakeRegistry(self.log)
        self.store = FakeStore(self.log)
        self.audit = FakeAudit(self.log)

    def tearDown(self) -> None:
        """Remove the temp dir."""
        self._tmp.cleanup()

    def _run(self, decisions: list[DecisionRequest], *, force: bool = False) -> RunSummary:
        """Run a fresh orchestrator over the fakes.

        Args:
            decisions: Decisions.
            force: Force flag.

        Returns:
            The summary.
        """
        return DownloadOrchestrator(
            CandidateSelector(), self.fetcher, self.store, self.registry,
            self.audit, self.docs,
        ).run(decisions, run_id=uuid.uuid4(), force=force, dry_run=False,
              max_workers=2)

    def _registered_files(self) -> list[tuple[object, ...]]:
        """Return the document upserts that carried a file.

        Returns:
            The logged upserts with a fetch outcome.
        """
        return [c for c in self.log.of("upsert_document") if c[2] is not None]

    def test_duplicate_of_existing_source_is_not_registered(self) -> None:
        """Bytes of master 36571_222_1's source: audited, file and folder removed."""
        sha = hashlib.sha256(_body(EC_PDF_1)).hexdigest()
        self.registry.content[sha] = "36571_222_1"
        summary = self._run([_decision(DEC_ID, [_candidate(EC_PDF_1, "EN")])])
        target = Path(str(self.log.of("fetch")[0][2]))
        self.assertFalse(target.exists())
        self.assertFalse((self.docs / DEC_ID).exists())
        self.assertEqual(self._registered_files(), [])
        self.assertEqual(self.log.of("store_upsert"), [])
        audits = [c for c in self.log.of("audit") if c[2] is Outcome.NOT_SELECTED]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0][4], "duplicate_content_of_36571_222_1")
        reasons = self.log.of("upsert_decision")[-1][2]
        self.assertTrue(any("same content as decision 36571_222_1" in r
                            for r in reasons))
        self.assertEqual(summary.outcomes[Outcome.DOWNLOADED], 0)
        self.assertEqual(summary.outcomes[Outcome.NOT_SELECTED], 1)
        self.assertEqual(summary.exit_code, 0)

    def test_in_run_duplicate_between_two_new_decisions(self) -> None:
        """Two new decisions download the same bytes: the second is dropped."""
        self.registry.decision_of_content = (  # type: ignore[method-assign]
            lambda sha256, decision_id, document_id: None)
        other_url = "https://ec.europa.eu/competition/antitrust/cases/dec_docs/37275/x.pdf"
        self.fetcher.bodies[other_url] = _body(EC_PDF_1)
        first = _decision("DEC-AT36820-20051207", [_candidate(EC_PDF_1, "EN")],
                          case="AT.36820", date=dt.date(2005, 12, 7), row=1)
        second = _decision("DEC-AT37275-20051207", [_candidate(other_url, "EN")],
                           case="AT.37275", date=dt.date(2005, 12, 7), row=2)
        self._run([first, second])
        registered = self._registered_files()
        self.assertEqual(len(registered), 1)
        owner = registered[0][4]
        loser = ({"DEC-AT36820-20051207", "DEC-AT37275-20051207"} - {owner}).pop()
        self.assertIn(("audit", loser, Outcome.NOT_SELECTED,
                       self._doc_of(loser),
                       f"duplicate_content_of_{owner}"), self.log.calls)
        self.assertFalse((self.docs / loser).exists())
        self.assertTrue((self.docs / str(owner)).exists())

    def _doc_of(self, decision_id: str) -> object:
        """Return the document id fetched for a decision.

        Args:
            decision_id: Decision id.

        Returns:
            The fetched document id.
        """
        return next(c[1] for c in self.log.of("fetch")
                    if f"/{decision_id}/" in str(c[2]))

    def test_preexisting_target_is_never_removed(self) -> None:
        """--force over an existing file: duplicate not registered, file kept."""
        sha = hashlib.sha256(_body(EC_PDF_1)).hexdigest()
        self.registry.content[sha] = "36571_222_1"
        target = self.docs / DEC_ID / f"{DEC_ID}.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(PDF_BYTES)
        self._run([_decision(DEC_ID, [_candidate(EC_PDF_1, "EN")])], force=True)
        self.assertEqual(self.log.of("fetch")[0][2], str(target))
        self.assertTrue(target.exists())
        self.assertEqual(self._registered_files(), [])

    def test_existing_unregistered_duplicate_is_audited_and_kept(self) -> None:
        """M1: a pre-existing file of another decision's bytes: audit only."""
        target = self.docs / DEC_ID / f"{DEC_ID}.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(PDF_BYTES)
        self.registry.content[hashlib.sha256(PDF_BYTES).hexdigest()] = "36571_222_1"
        summary = self._run([_decision(DEC_ID, [_candidate(EC_PDF_1, "EN")])])
        self.assertEqual(self.log.of("fetch"), [])
        self.assertTrue(target.exists())
        self.assertEqual(self._registered_files(), [])
        self.assertIn(("audit", DEC_ID, Outcome.NOT_SELECTED, self._doc_of_target(target),
                       "duplicate_content_of_36571_222_1"), self.log.calls)
        self.assertEqual(summary.outcomes[Outcome.REGISTERED_EXISTING], 0)

    def _doc_of_target(self, target: Path) -> object:
        """Return the document id audited for a decision's stored file.

        Args:
            target: The stored file.

        Returns:
            The document id of the duplicate audit row.
        """
        return next(c[3] for c in self.log.of("audit")
                    if c[2] is Outcome.NOT_SELECTED and c[4])

    def test_bytes_of_own_source_are_a_duplicate(self) -> None:
        """M2: a new document with the bytes of the decision's own source."""
        sha = hashlib.sha256(_body(EURLEX_EN)).hexdigest()
        self.registry.doc_owner["OLD_SOURCE"] = SELECTED_ID
        self.registry.sources["OLD_SOURCE"] = True
        self.registry.doc_sha["OLD_SOURCE"] = (sha, True)
        self._run([_celex_decision()])
        self.assertEqual(self._registered_files(), [])
        audits = [c for c in self.log.of("audit") if c[4]
                  and str(c[4]).startswith("duplicate_content_of_")]
        self.assertEqual([c[4] for c in audits], [f"duplicate_content_of_{SELECTED_ID}"])
        self.assertTrue(self.registry.sources["OLD_SOURCE"])
        self.assertFalse(Path(str(self.log.of("fetch")[0][2])).exists())

    def test_case_and_date_hint_then_content_net_catches_identical_file(self) -> None:
        """36571 shape: own key kept by the planner; the identical bytes are caught."""
        new_url = "https://ec.europa.eu/competition/antitrust/cases1/202514/AT_36571_222.pdf"
        self.store.keys.add("36571_222_1")
        self.store.cases["36571_222_1"] = ["IV/36571"]
        self.store.web["36571_222_1"] = {"case": "36571",
                                         "decision_date": "2002-06-11 00:00:00"}
        self.registry.content[hashlib.sha256(_body(new_url)).hexdigest()] = "36571_222_1"
        decision = _decision("DEC-AT36571-20020611", [_candidate(new_url, "EN")],
                             case="AT.36571", date=dt.date(2002, 6, 11))
        self._run([decision])
        upserts = self.log.of("upsert_decision")
        self.assertEqual(upserts[0][1], "DEC-AT36571-20020611")
        self.assertIn("possible duplicate of master entry 36571_222_1 (case+date)",
                      upserts[0][2])
        self.assertEqual(self._registered_files(), [])
        self.assertTrue(any(c[4] == "duplicate_content_of_36571_222_1"
                            for c in self.log.of("audit")))
        self.assertFalse((self.docs / "DEC-AT36571-20020611").exists())

    def _repointed_alias(self, date: dt.date) -> tuple[str, str, DecisionRequest]:
        """Master 36571_222_1 with the rows the clean-up re-pointed from D.

        Args:
            date: Decision date of the manifest row.

        Returns:
            Key, alias URL and the manifest decision (alias EN PDF selected,
            alias DE PDF not selected).
        """
        key = "36571_222_1"
        alias = "https://ec.europa.eu/competition/antitrust/cases1/202514/AT_36571_222.pdf"
        alias_de = alias.replace(".pdf", "_de.pdf")
        self.store.urls["https://ec.europa.eu/competition/antitrust/cases/dec_docs/"
                        "36571/36571_222_1.pdf"] = key
        self.store.web[key] = {"case": "36571", "decision_date": "2002-06-11 00:00:00"}
        for url, doc in ((alias, "AT_36571_222"), (alias_de, "AT_36571_222_de")):
            self.registry.aliases[url] = key
            self.registry.url_owner[url] = key
            self.registry.doc_owner[doc] = key
        decision = _decision("DEC-AT36571-20020611",
                             [_candidate(alias, "EN"), _candidate(alias_de, "DE")],
                             case="AT.36571", date=date)
        return key, alias, decision

    def test_registry_alias_url_maps_to_master_key_without_conflict(self) -> None:
        """A URL the clean-up re-pointed to K maps the row to K; K owns its rows."""
        key, alias, decision = self._repointed_alias(dt.date(2002, 6, 11))
        summary = self._run([decision])
        self.assertEqual(self.registry.alias_reads, [1])
        upserts = self.log.of("upsert_decision")
        self.assertEqual(upserts[0][1], key)
        self.assertIn(f"matched master entry {key} by url", upserts[0][2])
        documents = self.log.of("upsert_document")
        self.assertEqual({(c[1], c[4]) for c in documents},
                         {("AT_36571_222", key), ("AT_36571_222_de", key)})
        self.assertFalse(any(c[4] and "document_owned_by_other_decision" in str(c[4])
                             for c in self.log.of("audit")))
        self.assertEqual(summary.exit_code, 0)
        self.assertFalse((self.docs / "DEC-AT36571-20020611").exists())

    def test_registry_alias_url_keeps_the_date_guard(self) -> None:
        """An alias URL with a differing date is not reused (own key, reason)."""
        key, _alias, decision = self._repointed_alias(dt.date(2003, 1, 1))
        self._run([decision])
        upserts = self.log.of("upsert_decision")
        self.assertEqual(upserts[0][1], "DEC-AT36571-20020611")
        self.assertNotIn(f"matched master entry {key} by url", upserts[0][2])
        self.assertIn(f"url matches master entry {key} but date differs "
                      "(2002-06-11 vs 2003-01-01)", upserts[0][2])

    def test_unique_content_is_registered(self) -> None:
        """No other decision holds the bytes: registered as before."""
        summary = self._run([_decision(DEC_ID, [_candidate(EC_PDF_1, "EN")])])
        self.assertEqual(len(self.log.of("decision_of_content")), 1)
        self.assertEqual(len(self._registered_files()), 1)
        self.assertEqual(summary.outcomes[Outcome.DOWNLOADED], 1)
        self.assertEqual(len(self.log.of("store_upsert")), 1)


if __name__ == "__main__":
    unittest.main()
