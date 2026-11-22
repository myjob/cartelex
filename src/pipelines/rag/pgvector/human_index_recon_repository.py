"""
Persistence layer for the human-index reconciliation.

Reads what :mod:`pipelines.legal.human_index_recon` compares -- the named facts
of every decision and each decision's stored content size -- and writes the
outcome to ``celex_facts.human_index_recon``, one row per (decision, target).

Re-runs upsert on ``(celex_id, target)``. The human-written ``verdict`` and
``verdict_note`` survive a re-run only while both compared values are
unchanged; a verdict on a value that has since changed is cleared.

Follows the **Provisioner pattern**: the engine may be injected to share a
connection pool with sibling repositories.

Exports:
    HumanIndexReconRepository -- reads and writes for the reconciliation.
    ReconRecord -- one row of ``celex_facts.human_index_recon``.
"""

import json
import logging
from collections import defaultdict
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from config.settings import settings
from pipelines.legal.human_index_recon import ReconResult, SizeBand
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

_TABLE = "celex_facts.human_index_recon"
_FACTS_TABLE = "celex_facts.facts"
_MIGRATION = "src/pipelines/rag/pgvector/migrate_human_index_recon.sql"

_UPSERT = text(
    f"INSERT INTO {_TABLE} "
    "(celex_id, target, sheet_column, fact_names, human_raw, human_norm, "
    " extracted_raw, extracted_norm, status, detail, title, decision_year, "
    " content_tokens, size_band, source_file, source_row) "
    "VALUES "
    "(:celex_id, :target, :sheet_column, :fact_names, :human_raw, :human_norm, "
    " :extracted_raw, :extracted_norm, :status, CAST(:detail AS jsonb), :title, "
    " :decision_year, :content_tokens, :size_band, :source_file, :source_row) "
    "ON CONFLICT (celex_id, target) DO UPDATE SET "
    "  sheet_column   = EXCLUDED.sheet_column, "
    "  fact_names     = EXCLUDED.fact_names, "
    "  human_norm     = EXCLUDED.human_norm, "
    "  extracted_norm = EXCLUDED.extracted_norm, "
    "  status         = EXCLUDED.status, "
    "  detail         = EXCLUDED.detail, "
    "  title          = EXCLUDED.title, "
    "  decision_year  = EXCLUDED.decision_year, "
    "  content_tokens = EXCLUDED.content_tokens, "
    "  size_band      = EXCLUDED.size_band, "
    "  source_file    = EXCLUDED.source_file, "
    "  source_row     = EXCLUDED.source_row, "
    "  reconciled_at  = now(), "
    # A verdict judges one pair of values; it does not carry over to another.
    f"  verdict = CASE WHEN {_TABLE}.human_raw IS NOT DISTINCT FROM EXCLUDED.human_raw "
    f"                  AND {_TABLE}.extracted_raw IS NOT DISTINCT FROM EXCLUDED.extracted_raw "
    f"                 THEN {_TABLE}.verdict END, "
    f"  verdict_note = CASE WHEN {_TABLE}.human_raw IS NOT DISTINCT FROM EXCLUDED.human_raw "
    f"                       AND {_TABLE}.extracted_raw IS NOT DISTINCT FROM EXCLUDED.extracted_raw "
    f"                      THEN {_TABLE}.verdict_note END, "
    "  human_raw      = EXCLUDED.human_raw, "
    "  extracted_raw  = EXCLUDED.extracted_raw"
)

_SELECT_FACTS = text(
    f"SELECT celex_id, name, value_md, value_json FROM {_FACTS_TABLE} WHERE name IN :names"
).bindparams(bindparam("names", expanding=True))

_SELECT_STATUS_COUNTS = text(
    f"SELECT target, status, count(*) FROM {_TABLE} GROUP BY target, status ORDER BY target, status"
)


class ReconRecord(BaseModel):
    """One row of ``celex_facts.human_index_recon``: a result plus decision context."""

    model_config = ConfigDict(frozen=True)

    result: ReconResult = Field(description="The comparison outcome")
    title: str | None = Field(default=None, description="Case title from the human index")
    decision_year: int | None = Field(default=None, description="Decision year from the human index")
    content_tokens: int | None = Field(
        default=None, ge=0, description="Tokens stored for the decision in the chunk table"
    )
    size_band: SizeBand | None = Field(default=None, description="Content-size band")
    source_file: str = Field(description="File name of the workbook the row came from")
    source_row: int = Field(ge=2, description="1-based worksheet row")

    def as_params(self) -> dict[str, object]:
        """Return the bind parameters of the upsert statement for this row."""
        result = self.result
        return {
            "celex_id": result.celex_id,
            "target": result.target.value,
            "sheet_column": result.sheet_column,
            "fact_names": result.fact_names,
            "human_raw": result.human_raw,
            "human_norm": result.human_norm,
            "extracted_raw": result.extracted_raw,
            "extracted_norm": result.extracted_norm,
            "status": result.status.value,
            "detail": json.dumps(result.detail, ensure_ascii=False),
            "title": self.title,
            "decision_year": self.decision_year,
            "content_tokens": self.content_tokens,
            "size_band": self.size_band.value if self.size_band else None,
            "source_file": self.source_file,
            "source_row": self.source_row,
        }


class HumanIndexReconRepository:
    """DAO for the human-index reconciliation.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string. Ignored when ``engine`` is supplied.
    engine:
        Pre-built SQLAlchemy engine to share a connection pool (Provisioner
        pattern). When provided, ``db_url`` is unused.
    embedding_table:
        Chunk table the content size is summed from.
    """

    def __init__(
        self,
        db_url: str = DATABASE_URL,
        engine: Engine | None = None,
        embedding_table: str = settings.EMBEDDING_TABLE,
    ) -> None:
        if not embedding_table.isidentifier():
            raise ValueError(f"embedding_table is not a bare identifier: {embedding_table!r}")
        self.engine = engine if engine is not None else make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)
        self._select_token_totals = text(
            "SELECT celex_id, COALESCE(SUM(token_count), 0) "
            f"FROM celex_facts.{embedding_table} GROUP BY celex_id"
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def select_fact_values(
        self, names: Sequence[str]
    ) -> dict[str, dict[str, tuple[str | None, str | None]]]:
        """Return the named facts of every decision.

        Parameters
        ----------
        names:
            Fact names to fetch.

        Returns
        -------
        dict[str, dict[str, tuple[str | None, str | None]]]
            ``celex_id -> fact name -> (value_md, value_json)``. Decisions
            without any of the facts are absent.
        """
        by_celex: dict[str, dict[str, tuple[str | None, str | None]]] = defaultdict(dict)
        if not names:
            return {}
        with self.engine.connect() as conn:
            for celex_id, name, value_md, value_json in conn.execute(
                _SELECT_FACTS, {"names": list(names)}
            ):
                by_celex[celex_id][name] = (value_md, value_json)
        return dict(by_celex)

    def select_content_tokens(self) -> dict[str, int]:
        """Return the stored token total of every decision in the chunk table.

        Returns
        -------
        dict[str, int]
            ``celex_id -> summed token_count``; decisions without chunks are
            absent.
        """
        with self.engine.connect() as conn:
            return {celex_id: int(total) for celex_id, total in conn.execute(self._select_token_totals)}

    def select_status_counts(self) -> list[tuple[str, str, int]]:
        """Return the number of stored rows per (target, status).

        Returns
        -------
        list[tuple[str, str, int]]
            Rows ordered by target, then status.
        """
        with self.engine.connect() as conn:
            return [(t, s, int(n)) for t, s, n in conn.execute(_SELECT_STATUS_COUNTS)]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def probe_table(self) -> None:
        """Raise if ``celex_facts.human_index_recon`` is absent.

        Called before the first write so a missing migration surfaces at once
        rather than after the comparison has run.

        Raises
        ------
        RuntimeError
            When the table does not exist, naming the migration to apply.
        """
        try:
            with self.engine.connect() as conn:
                conn.execute(text(f"SELECT 1 FROM {_TABLE} LIMIT 0"))
        except Exception as exc:
            raise RuntimeError(f"Table {_TABLE} does not exist. Apply {_MIGRATION} first.") from exc

    def upsert_records(self, records: Sequence[ReconRecord]) -> int:
        """Insert or update reconciliation rows in one transaction.

        Parameters
        ----------
        records:
            Rows to store, keyed by ``(celex_id, target)``.

        Returns
        -------
        int
            Number of rows written.

        Raises
        ------
        sqlalchemy.exc.SQLAlchemyError
            On database errors; nothing is committed.
        """
        if not records:
            return 0
        with self.Session() as session:
            session.execute(_UPSERT, [record.as_params() for record in records])
            session.commit()
        return len(records)
