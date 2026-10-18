"""
Persistence layer for the fact-vs-book check ledger.

Writes ``celex_facts.fact2book_checks`` rows produced by
:class:`pipelines.agent_dspy.book_cross_check_module.BookCrossCheckModule`
for every fact it judges against the book. The ``action`` column records the
routing decision (``corroborated`` / ``mismatch`` / ``skipped``), so the table
is a complete per-fact check ledger rather than a contradiction-only one.

Each ``(fact_id, celex_id)`` pair maps to exactly one row (one verdict per
fact per case); re-runs upsert (``ON CONFLICT DO UPDATE``) rather than
duplicate, so the pipeline is idempotent and the citation anchor follows the
latest run.

The same repository keeps the case-level run ledger
``celex_facts.fact2book_case_runs``: one row per ``(celex_id, case_id)``
recording the outcome of the driver's last pass over that case (in book or
why not, reference type, verdict counts, run id, timestamps). The driver's
``--resume`` reads it to skip cases finished under the same run id.

Follows the **Provisioner pattern**: the engine may be injected to share a
connection pool with sibling repositories.

Exports:
    Fact2BookCheckRepository — canonical persistence service for fact2book_checks
    and fact2book_case_runs.
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

_TABLE = "celex_facts.fact2book_checks"
_CASE_RUNS_TABLE = "celex_facts.fact2book_case_runs"


class Fact2BookCheckRepository:
    """DAO for ``celex_facts.fact2book_checks``.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string.  Ignored when ``engine`` is supplied.
    engine:
        Pre-built SQLAlchemy engine to share a connection pool (Provisioner
        pattern).  When provided, ``db_url`` is unused.
    """

    def __init__(self, db_url: str = DATABASE_URL, engine: Engine | None = None) -> None:
        self.engine = engine if engine is not None else make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)
        self._probe_table()

    def _probe_table(self) -> None:
        """Raise immediately if the target table is absent.

        Runs a zero-row SELECT at construction time so callers discover a
        missing ``celex_facts.fact2book_checks`` before the first write
        attempt rather than at an unpredictable point during pipeline execution.

        Raises
        ------
        RuntimeError
            When the table does not exist, with a DDL hint.
        """
        try:
            with self.engine.connect() as conn:
                conn.execute(text(f"SELECT 1 FROM {_TABLE} LIMIT 0"))
        except Exception as exc:
            raise RuntimeError(
                f"Table {_TABLE} does not exist. "
                f"Create it with:\n\n"
                f"  CREATE TABLE {_TABLE} (\n"
                f"      id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),\n"
                f"      fact_id       uuid        NOT NULL REFERENCES celex_facts.facts(id),\n"
                f"      celex_id      text        NOT NULL,\n"
                f"      case_id       text        NULL,\n"
                f"      book_chunk_id varchar     NOT NULL,\n"
                f"      book_path     text        NULL,\n"
                f"      explanation   text        NOT NULL,\n"
                f"      support_label text        NULL,\n"
                f"      final_score   numeric     NULL,\n"
                f"      action        varchar     NOT NULL,\n"
                f"      reference_type varchar    NULL,\n"
                f"      created_at    timestamptz NOT NULL DEFAULT now(),\n"
                f"      CONSTRAINT fact2book_checks_uq UNIQUE (fact_id, celex_id)\n"
                f"  );"
            ) from exc

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_check(
        self,
        fact_id: UUID,
        celex_id: str,
        book_chunk_id: str,
        explanation: str,
        action: str,
        case_id: str | None = None,
        book_path: str | None = None,
        support_label: str | None = None,
        final_score: float | None = None,
        reference_type: str | None = None,
    ) -> None:
        """Insert or update one check row.

        Upserts on the unique key ``(fact_id, celex_id)`` so re-running the
        cross-check pipeline overwrites stale verdicts and scores rather than
        creating duplicates. ``book_chunk_id`` is deliberately *not* part of
        the key: the citation anchor is re-selected per run by the reranker, so
        keying on it would insert a second row whenever the anchor drifted.

        Parameters
        ----------
        fact_id:
            UUID of the ``celex_facts.facts`` row.
        celex_id:
            CELEX document identifier — denormalized for cheap celex-scoped
            queries.
        book_chunk_id:
            Stable ``pg_embedding_book_qwen.id`` (varchar) of the keyword-hit
            chunk whose chapter context was judged against the fact.
        explanation:
            Judge reason or supporting/contradicting quote (non-empty).
        action:
            Routing decision recorded for the fact: ``"corroborated"``,
            ``"mismatch"`` or ``"skipped"``. Required — the column is
            ``NOT NULL`` and CHECK-constrained to that value set.
        case_id:
            COMP/IV/AT case identifier used to locate the book chapter,
            e.g. ``"COMP/35.587"``.
        book_path:
            Human-readable chapter path from ``BookChapterVo.path``,
            e.g. ``"Infringements -> 2 Horizontal restrictions -> A Introduction"``.
        support_label:
            Judge support label (``"supports"`` / ``"contradicts"`` / ...).
        final_score:
            Judge confidence score (0.0–1.0).
        reference_type:
            How the book references the case (``"landmark"`` or ``"simple"``);
            the provenance that justified the verdict, used by the
            Art.102-vs-Art.101 audit to filter landmark-only checks.
        """
        with self.Session() as session:
            session.execute(
                text(
                    f"INSERT INTO {_TABLE} "
                    "(fact_id, celex_id, case_id, book_chunk_id, book_path, "
                    " explanation, support_label, final_score, action, "
                    " reference_type) "
                    "VALUES "
                    "(:fact_id, :celex_id, :case_id, :book_chunk_id, :book_path, "
                    " :explanation, :support_label, :final_score, :action, "
                    " :reference_type) "
                    "ON CONFLICT (fact_id, celex_id) DO UPDATE SET "
                    "  case_id        = EXCLUDED.case_id, "
                    "  book_chunk_id  = EXCLUDED.book_chunk_id, "
                    "  book_path      = EXCLUDED.book_path, "
                    "  explanation    = EXCLUDED.explanation, "
                    "  support_label  = EXCLUDED.support_label, "
                    "  final_score    = EXCLUDED.final_score, "
                    "  action         = EXCLUDED.action, "
                    "  reference_type = EXCLUDED.reference_type, "
                    "  created_at     = now()"
                ),
                {
                    "fact_id": str(fact_id),
                    "celex_id": celex_id,
                    "case_id": case_id,
                    "book_chunk_id": book_chunk_id,
                    "book_path": book_path,
                    "explanation": explanation,
                    "support_label": support_label,
                    "final_score": final_score,
                    "action": action,
                    "reference_type": reference_type,
                },
            )
            session.commit()
            logger.debug(
                "Upserted check fact=%s book_chunk=%s action=%s",
                fact_id, book_chunk_id, action,
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def select_checked_fact_ids(self, celex_id: str) -> set[str]:
        """Return the ``fact_id``s that already have a check row for *celex_id*.

        Used by the cross-check module's skip mode to avoid re-judging facts
        already present in the ledger.

        Parameters
        ----------
        celex_id:
            CELEX document identifier.

        Returns
        -------
        set[str]
            Distinct ``fact_id`` values (as strings) with at least one row.
        """
        sql = text(
            f"SELECT DISTINCT fact_id FROM {_TABLE} WHERE celex_id = :celex_id"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"celex_id": celex_id}).fetchall()
        return {str(row[0]) for row in rows}

    def list_for_fact(self, fact_id: UUID) -> list[dict[str, Any]]:
        """Return all check rows for one fact, ordered by ``final_score`` desc.

        Parameters
        ----------
        fact_id:
            UUID of the ``celex_facts.facts`` row.

        Returns
        -------
        list[dict]
            One dict per row with keys matching the table columns.
        """
        sql = text(
            f"SELECT id, fact_id, celex_id, case_id, book_chunk_id, book_path, "
            f"       explanation, support_label, final_score, action, "
            f"       reference_type, created_at "
            f"FROM {_TABLE} "
            f"WHERE fact_id = :fact_id "
            f"ORDER BY final_score DESC NULLS LAST"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"fact_id": str(fact_id)}).fetchall()
        return [dict(row._mapping) for row in rows]

    def list_for_celex(self, celex_id: str) -> list[dict[str, Any]]:
        """Return all check rows for one CELEX document.

        Parameters
        ----------
        celex_id:
            CELEX document identifier.

        Returns
        -------
        list[dict]
            One dict per row ordered by ``fact_id``, then ``final_score`` desc.
        """
        sql = text(
            f"SELECT id, fact_id, celex_id, case_id, book_chunk_id, book_path, "
            f"       explanation, support_label, final_score, action, "
            f"       reference_type, created_at "
            f"FROM {_TABLE} "
            f"WHERE celex_id = :celex_id "
            f"ORDER BY fact_id, final_score DESC NULLS LAST"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"celex_id": celex_id}).fetchall()
        return [dict(row._mapping) for row in rows]

    def list_mismatches(self, fact_name_pattern: str) -> list[dict[str, Any]]:
        """Return every ``mismatch`` row whose fact name matches a LIKE pattern.

        Parameters
        ----------
        fact_name_pattern:
            SQL ``LIKE`` pattern on ``facts.name`` (e.g. ``"%infringement%"``).

        Returns
        -------
        list[dict]
            One dict per row with keys ``celex_id``, ``case_id``, ``name``,
            ``value`` (``value_md``, else ``value_json`` as text), ``book_path``,
            ``reference_type`` and ``explanation``; ordered by ``celex_id``,
            then fact name.
        """
        sql = text(
            f"SELECT c.celex_id, c.case_id, f.name, "
            f"       coalesce(f.value_md, f.value_json::text) AS value, "
            f"       c.book_path, c.reference_type, c.explanation "
            f"FROM {_TABLE} c "
            f"JOIN celex_facts.facts f ON f.id = c.fact_id "
            f"WHERE c.action = 'mismatch' AND f.name LIKE :pattern "
            f"ORDER BY c.celex_id, f.name"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"pattern": fact_name_pattern}).fetchall()
        return [dict(row._mapping) for row in rows]

    # ------------------------------------------------------------------
    # Case-level run ledger
    # ------------------------------------------------------------------

    def upsert_case_run(
        self,
        celex_id: str,
        case_id: str,
        run_id: str,
        in_book: bool,
        absence_reason: str,
        reference_type: str | None,
        corroborated: int,
        mismatch: int,
        skipped: int,
        already_checked: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        """Record the outcome of one driver pass over a case (idempotent).

        Upserts on ``(celex_id, case_id)`` so the ledger always holds the latest
        pass; a resumed run overwrites the row of an earlier, crashed pass.

        Parameters
        ----------
        celex_id, case_id:
            The case processed.
        run_id:
            Operator-chosen run label; ``--resume`` skips keys finished under it.
        in_book:
            Whether the case was found in the book.
        absence_reason:
            ``none`` / ``no_surface`` / ``no_anchor`` (``BookAbsence`` value).
        reference_type:
            ``landmark`` / ``simple`` / ``index``, or ``None`` when absent.
        corroborated, mismatch, skipped, already_checked:
            Per-fact counters of the pass.
        started_at, finished_at:
            Wall-clock bounds of the pass.
        """
        sql = text(
            f"""
            INSERT INTO {_CASE_RUNS_TABLE}
                (celex_id, case_id, run_id, in_book, absence_reason,
                 reference_type, corroborated, mismatch, skipped,
                 already_checked, started_at, finished_at)
            VALUES
                (:celex_id, :case_id, :run_id, :in_book, :absence_reason,
                 :reference_type, :corroborated, :mismatch, :skipped,
                 :already_checked, :started_at, :finished_at)
            ON CONFLICT (celex_id, case_id) DO UPDATE SET
                run_id          = EXCLUDED.run_id,
                in_book         = EXCLUDED.in_book,
                absence_reason  = EXCLUDED.absence_reason,
                reference_type  = EXCLUDED.reference_type,
                corroborated    = EXCLUDED.corroborated,
                mismatch        = EXCLUDED.mismatch,
                skipped         = EXCLUDED.skipped,
                already_checked = EXCLUDED.already_checked,
                started_at      = EXCLUDED.started_at,
                finished_at     = EXCLUDED.finished_at
            """
        )
        with self.Session() as session:
            session.execute(
                sql,
                {
                    "celex_id": celex_id,
                    "case_id": case_id,
                    "run_id": run_id,
                    "in_book": in_book,
                    "absence_reason": absence_reason,
                    "reference_type": reference_type,
                    "corroborated": corroborated,
                    "mismatch": mismatch,
                    "skipped": skipped,
                    "already_checked": already_checked,
                    "started_at": started_at,
                    "finished_at": finished_at,
                },
            )
            session.commit()

    def select_finished_case_keys(self, run_id: str) -> set[tuple[str, str]]:
        """Return the ``(celex_id, case_id)`` keys finished under *run_id*.

        Parameters
        ----------
        run_id:
            The run label to resume.

        Returns
        -------
        set[tuple[str, str]]
            Keys whose ledger row carries *run_id* and a ``finished_at``.
        """
        sql = text(
            f"SELECT celex_id, case_id FROM {_CASE_RUNS_TABLE} "
            f"WHERE run_id = :run_id AND finished_at IS NOT NULL"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"run_id": run_id}).fetchall()
        return {(str(row[0]), str(row[1])) for row in rows}
