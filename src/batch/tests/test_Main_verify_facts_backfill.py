"""
Unit tests for the fact-verification backfill driver.

The verifier module, its repositories, the DSPy LM configuration and the
database engine are all mocked at the module boundary, so no live database,
Ollama server or embedding model is required.

Covered:
    * ``_dry_run_verify`` -- the ``_persist`` monkeypatch actually suppresses
      the write, is restored afterwards (also on error), and targets an
      attribute that really exists on ``EvidenceVerifierModule``.
    * the ``None``-means-excluded contract of ``EvidenceVerifierModule``.
    * the counter arithmetic in ``run`` (strategies, statuses, excluded,
      errors) and the dry-run dispatch.
    * ``_fetch_facts`` SQL scoping for every combination of the ``--celex-id``
      and ``--fact-name`` filters, and the scope suffix they log.

Run with:
    PYTHONPATH=src .venv/bin/python -m unittest \
        src/batch/tests/test_Main_verify_facts_backfill.py -v
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from batch import Main_verify_facts_backfill as backfill
from pipelines.agent_dspy.evidence_verifier_module import (
    EvidenceVerifierModule,
    VerificationStatus,
    VerificationStrategy,
)

_MODULE = "batch.Main_verify_facts_backfill"


def _result(
    strategy: VerificationStrategy = VerificationStrategy.LLM_SUPPORT,
    status: VerificationStatus = VerificationStatus.VERIFIED_SUPPORTED,
) -> SimpleNamespace:
    """A stand-in for ``FactVerificationResult`` with the fields ``run`` reads."""
    return SimpleNamespace(strategy=strategy, status=status)


class _StubVerifier:
    """EvidenceVerifierModule stand-in that persists through ``_persist``."""

    def __init__(self, results: list[object] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, str]] = []
        self.persisted: list[object] = []

    def _persist(self, result: object) -> None:
        self.persisted.append(result)

    def __call__(self, celex_id: str, fact_name: str):
        self.calls.append((celex_id, fact_name))
        result = self.results.pop(0) if self.results else _result()
        # mirrors the real forward(): excluded facts return before persisting
        if result is not None:
            self._persist(result)
        return result


class TestDryRunVerify(unittest.TestCase):
    """_dry_run_verify neuters persistence for exactly one call."""

    def test_persist_target_exists_on_the_real_module(self) -> None:
        # guards the private-attribute monkeypatch against a silent rename:
        # if ``_persist`` is renamed, the stub would attach a dead attribute
        # and the dry run would start writing.
        self.assertTrue(callable(getattr(EvidenceVerifierModule, "_persist", None)))

    def test_nothing_is_persisted_during_the_dry_run(self) -> None:
        module = _StubVerifier([_result()])
        result = backfill._dry_run_verify(module, "CELEX-31982D0123", "fine_total")
        self.assertIsNotNone(result)
        self.assertEqual([("CELEX-31982D0123", "fine_total")], module.calls)
        self.assertEqual([], module.persisted)

    def test_persist_is_restored_after_the_call(self) -> None:
        module = _StubVerifier([_result(), _result()])
        original = module._persist
        backfill._dry_run_verify(module, "CELEX-31982D0123", "fine_total")
        self.assertEqual(original, module._persist)  # bound methods compare by (func, instance)
        module("CELEX-31982D0123", "fine_total")
        self.assertEqual(1, len(module.persisted))

    def test_persist_is_restored_when_verification_raises(self) -> None:
        class _FailingVerifier(_StubVerifier):
            def __call__(self, celex_id: str, fact_name: str):
                raise RuntimeError("retrieval failed")

        module = _FailingVerifier()
        original = module._persist
        with self.assertRaises(RuntimeError):
            backfill._dry_run_verify(module, "CELEX-X", "fine_total")
        self.assertEqual(original, module._persist)  # bound methods compare by (func, instance)

    def test_excluded_fact_returns_none_without_persisting(self) -> None:
        module = _StubVerifier([None])
        self.assertIsNone(backfill._dry_run_verify(module, "CELEX-X", "excluded_fact"))
        self.assertEqual([], module.persisted)


class _RunHarness(unittest.TestCase):
    """Shared patching of ``run``'s collaborators (DB, DSPy LM, repositories)."""

    def setUp(self) -> None:
        patches = [
            patch(f"{_MODULE}.make_engine", return_value=MagicMock()),
            patch(f"{_MODULE}._configure_dspy"),
            patch(f"{_MODULE}.build_thermal_gate", return_value=None),
            patch(f"{_MODULE}.FactEvidenceRepository"),
            patch(f"{_MODULE}.ChunkRepository"),
            patch(f"{_MODULE}.LineageRepository"),
            patch(f"{_MODULE}.FactRepository"),
            patch(f"{_MODULE}.QueryEmbeddingRepository"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _facts(*names: str) -> list[dict]:
        return [{"celex_id": "CELEX-31982D0123", "name": name} for name in names]

    def _run(self, facts: list[dict], verifier: object, **kwargs) -> tuple:
        """Run the backfill and return the final summary log's counters."""
        with patch(f"{_MODULE}._fetch_facts", return_value=facts), \
                patch(f"{_MODULE}.EvidenceVerifierModule", return_value=verifier):
            with self.assertLogs(backfill.logger, level="INFO") as captured:
                backfill.run(**kwargs)
        # final record: (dry suffix, strategies, statuses, excluded, errors)
        return captured.records[-1].args


class TestRunCounters(_RunHarness):
    """run() tallies strategies, statuses, exclusions and errors."""

    def test_strategy_and_status_counts(self) -> None:
        verifier = _StubVerifier([
            _result(VerificationStrategy.LLM_SUPPORT,
                    VerificationStatus.VERIFIED_SUPPORTED),
            _result(VerificationStrategy.LLM_SUPPORT,
                    VerificationStatus.WEAK_SUPPORT),
            _result(VerificationStrategy.DERIVED,
                    VerificationStatus.VERIFIED_SUPPORTED),
        ])
        _, strategies, statuses, excluded, errors = self._run(
            self._facts("a", "b", "c"), verifier
        )
        self.assertEqual({"llm_support": 2, "derived": 1}, strategies)
        self.assertEqual({"verified_supported": 2, "weak_support": 1}, statuses)
        self.assertEqual(0, excluded)
        self.assertEqual(0, errors)

    def test_none_result_counts_as_excluded_only(self) -> None:
        verifier = _StubVerifier([None, _result(), None])
        _, strategies, statuses, excluded, errors = self._run(
            self._facts("a", "b", "c"), verifier
        )
        self.assertEqual(2, excluded)
        self.assertEqual(1, sum(strategies.values()))
        self.assertEqual(1, sum(statuses.values()))
        self.assertEqual(0, errors)

    def test_exception_counts_as_error_and_does_not_abort(self) -> None:
        verifier = _StubVerifier([_result(), RuntimeError("boom"), _result()])

        original_call = _StubVerifier.__call__

        def _maybe_raise(self_, celex_id, fact_name):
            nxt = self_.results[0] if self_.results else None
            if isinstance(nxt, Exception):
                self_.results.pop(0)
                self_.calls.append((celex_id, fact_name))
                raise nxt
            return original_call(self_, celex_id, fact_name)

        with patch.object(_StubVerifier, "__call__", _maybe_raise):
            _, strategies, statuses, excluded, errors = self._run(
                self._facts("a", "b", "c"), verifier
            )
        self.assertEqual(1, errors)
        self.assertEqual(0, excluded)
        self.assertEqual(2, sum(strategies.values()))
        self.assertEqual(3, len(verifier.calls))  # the run continued past the error

    def test_empty_corpus_yields_zero_counters(self) -> None:
        _, strategies, statuses, excluded, errors = self._run([], _StubVerifier())
        self.assertEqual({}, strategies)
        self.assertEqual({}, statuses)
        self.assertEqual(0, excluded)
        self.assertEqual(0, errors)


class TestRunDryRunDispatch(_RunHarness):
    """A dry run counts verdicts but writes nothing."""

    def test_dry_run_suppresses_persistence(self) -> None:
        verifier = _StubVerifier([_result(), _result()])
        suffix, strategies, _, _, errors = self._run(
            self._facts("a", "b"), verifier, dry_run=True
        )
        self.assertEqual(" [DRY-RUN]", suffix)
        self.assertEqual(0, errors)
        self.assertEqual(2, sum(strategies.values()))
        self.assertEqual([], verifier.persisted)

    def test_wet_run_persists(self) -> None:
        verifier = _StubVerifier([_result(), _result()])
        suffix, *_ = self._run(self._facts("a", "b"), verifier)
        self.assertEqual("", suffix)
        self.assertEqual(2, len(verifier.persisted))


class TestFetchFacts(unittest.TestCase):
    """_fetch_facts scopes the query to one CELEX when asked."""

    @staticmethod
    def _conn(rows: list) -> MagicMock:
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = rows
        return conn

    def test_unfiltered_query_has_no_where_clause(self) -> None:
        conn = self._conn([SimpleNamespace(celex_id="CELEX-1", name="fine_total")])
        facts = backfill._fetch_facts(conn, None)
        self.assertEqual([{"celex_id": "CELEX-1", "name": "fine_total"}], facts)
        sql, params = conn.execute.call_args[0]
        self.assertNotIn("WHERE", str(sql))
        self.assertEqual({}, params)

    def test_celex_filter_binds_a_parameter(self) -> None:
        conn = self._conn([])
        backfill._fetch_facts(conn, "CELEX-31982D0123")
        sql, params = conn.execute.call_args[0]
        self.assertIn("WHERE celex_id = :celex_id", str(sql))
        self.assertEqual({"celex_id": "CELEX-31982D0123"}, params)

    def test_fact_name_filter_binds_a_parameter(self) -> None:
        conn = self._conn([])
        backfill._fetch_facts(conn, None, "fines")
        sql, params = conn.execute.call_args[0]
        self.assertIn("WHERE name = :fact_name", str(sql))
        self.assertNotIn("celex_id = :celex_id", str(sql))
        self.assertEqual({"fact_name": "fines"}, params)

    def test_both_filters_combine_conjunctively(self) -> None:
        conn = self._conn([])
        backfill._fetch_facts(conn, "CELEX-31982D0123", "fines")
        sql, params = conn.execute.call_args[0]
        self.assertIn("WHERE celex_id = :celex_id AND name = :fact_name", str(sql))
        self.assertEqual(
            {"celex_id": "CELEX-31982D0123", "fact_name": "fines"}, params
        )

    def test_ordering_survives_every_filter_combination(self) -> None:
        # the ORDER BY is what makes an interrupted pass restartable
        for celex, name in [
            (None, None), ("CELEX-1", None), (None, "fines"), ("CELEX-1", "fines"),
        ]:
            with self.subTest(celex=celex, name=name):
                conn = self._conn([])
                backfill._fetch_facts(conn, celex, name)
                sql, _ = conn.execute.call_args[0]
                self.assertTrue(str(sql).endswith("ORDER BY celex_id, name"))


class TestScopeSuffix(unittest.TestCase):
    """_scope_suffix renders only the filters that are actually set."""

    def test_no_filters_render_nothing(self) -> None:
        self.assertEqual("", backfill._scope_suffix(None, None))

    def test_each_filter_is_named(self) -> None:
        self.assertEqual(
            " (filter: celex=CELEX-1)", backfill._scope_suffix("CELEX-1", None)
        )
        self.assertEqual(" (filter: name=fines)", backfill._scope_suffix(None, "fines"))

    def test_both_filters_are_joined(self) -> None:
        self.assertEqual(
            " (filter: celex=CELEX-1, name=fines)",
            backfill._scope_suffix("CELEX-1", "fines"),
        )


class TestRunScoping(_RunHarness):
    """run() hands both scope filters to the fetch query."""

    def test_fact_name_filter_reaches_fetch_facts(self) -> None:
        verifier = _StubVerifier([_result()])
        with patch(f"{_MODULE}._fetch_facts", return_value=self._facts("fines")) \
                as fetch, \
                patch(f"{_MODULE}.EvidenceVerifierModule", return_value=verifier):
            with self.assertLogs(backfill.logger, level="INFO"):
                backfill.run(celex_filter="CELEX-1", fact_name_filter="fines")
        conn, celex, name = fetch.call_args[0]
        self.assertEqual("CELEX-1", celex)
        self.assertEqual("fines", name)
        self.assertEqual([("CELEX-31982D0123", "fines")], verifier.calls)


if __name__ == "__main__":
    unittest.main()
