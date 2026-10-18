"""Self-contained persistence layer for the OpenCorporates lookup cache.

This module owns its declarative ``Base`` and three ORM models mapped to the
``celex_facts`` schema. It does not reuse the pgvector repositories or their
engine: the OpenCorporates cache is an independent concern with its own tables
and lifecycle. The engine is built from :data:`settings.DATABASE_URL` and is
injectable so unit tests can supply a mock.
"""

import logging
import uuid
from datetime import date
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from config.settings import settings
from pipelines.corp_index.opencorp_models import (
    OpenCorpCompany,
    OpenCorpCompanyDetail,
    OpenCorpSearchResult,
)

logger = logging.getLogger(__name__)

Base = declarative_base()

_SCHEMA = settings.DB_SCHEMA
_DETAIL_UNIQUE_CONSTRAINT = "uq_opencorp_detail_co"

# Nested JSONB section columns on the company-detail table. Shared by the
# ORM model, the upsert set-clause, and the row -> model mapping so the list
# stays defined in exactly one place.
_DETAIL_SECTION_COLUMNS: tuple[str, ...] = (
    "previous_names",
    "alternative_names",
    "identifiers",
    "industry_codes",
    "officers",
    "filings",
    "financial_summary",
    "corporate_groupings",
    "ultimate_beneficial_owners",
    "registered_address",
    "source",
)

# Scalar (non-section, non-key) columns on the company-detail table.
_DETAIL_SCALAR_COLUMNS: tuple[str, ...] = (
    "name",
    "incorporation_date",
    "dissolution_date",
    "company_type",
    "registry_url",
    "inactive",
    "current_status",
    "opencorporates_url",
    "agent_name",
    "agent_address",
    "number_of_employees",
    "native_company_number",
    "registered_address_in_full",
)


class OpenCorpQuery(Base):
    """A cached company-name search request and its pagination metadata."""

    __tablename__ = "opencorp_index_query"
    __table_args__ = (
        UniqueConstraint(
            "normalized_query",
            "jurisdiction_code",
            name="uq_opencorp_query_norm_juris",
        ),
        {"schema": _SCHEMA},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    query_text = Column(Text, nullable=False)
    normalized_query = Column(Text, nullable=False)
    jurisdiction_code = Column(String(64), nullable=False, default="")
    fetched_pages = Column(Integer, nullable=False, default=0)
    total_pages = Column(Integer, nullable=True)
    total_count = Column(Integer, nullable=True)
    fetched_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    # Populated only by save_query_error(); NULL means the last attempt for
    # this (normalized_query, jurisdiction_code) key succeeded. search()
    # excludes rows with error_class set so a failed attempt is retried
    # instead of being served back as a false "no hits" cache hit.
    error_class = Column(String(255), nullable=True)
    error_status_code = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)

    companies = relationship(
        "OpenCorpCompanyRow",
        back_populates="query",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class OpenCorpCompanyRow(Base):
    """A single company hit belonging to a cached search request."""

    __tablename__ = "opencorp_index_companies"
    __table_args__ = (
        UniqueConstraint(
            "query_id",
            "jurisdiction_code",
            "company_number",
            name="uq_opencorp_company_query_co",
        ),
        Index("ix_opencorp_company_juris_number", "jurisdiction_code", "company_number"),
        Index("ix_opencorp_company_name", "name"),
        {"schema": _SCHEMA},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    query_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{_SCHEMA}.opencorp_index_query.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(Text, nullable=False)
    company_number = Column(String(255), nullable=False)
    jurisdiction_code = Column(String(64), nullable=False)
    incorporation_date = Column(Date, nullable=True)
    dissolution_date = Column(Date, nullable=True)
    company_type = Column(Text, nullable=True)
    registry_url = Column(Text, nullable=True)
    inactive = Column(Boolean, nullable=True)
    current_status = Column(Text, nullable=True)
    opencorporates_url = Column(Text, nullable=True)
    previous_names = Column(JSONB, nullable=True)
    # Fed by the resolution ladder's hit scoring. registered_address_locality is
    # stored alongside the formatted address because the structured object has a
    # locality for 'gb'/'ch' but not for 'de'/'us_de', so the scorer tokenizes
    # the full string and treats the locality as a bonus rather than the source.
    registered_address_in_full = Column(Text, nullable=True)
    registered_address_locality = Column(Text, nullable=True)
    industry_codes = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    query = relationship("OpenCorpQuery", back_populates="companies")


class OpenCorpCompanyDetailRow(Base):
    """A cached full company-detail record, keyed by jurisdiction and number."""

    __tablename__ = "opencorp_index_company_detail"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction_code",
            "company_number",
            name=_DETAIL_UNIQUE_CONSTRAINT,
        ),
        {"schema": _SCHEMA},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    jurisdiction_code = Column(String(64), nullable=False)
    company_number = Column(String(255), nullable=False)
    name = Column(Text, nullable=False)
    incorporation_date = Column(Date, nullable=True)
    dissolution_date = Column(Date, nullable=True)
    company_type = Column(Text, nullable=True)
    registry_url = Column(Text, nullable=True)
    inactive = Column(Boolean, nullable=True)
    current_status = Column(Text, nullable=True)
    opencorporates_url = Column(Text, nullable=True)
    agent_name = Column(Text, nullable=True)
    agent_address = Column(Text, nullable=True)
    number_of_employees = Column(String(255), nullable=True)
    native_company_number = Column(String(255), nullable=True)
    registered_address_in_full = Column(Text, nullable=True)
    previous_names = Column(JSONB, nullable=True)
    alternative_names = Column(JSONB, nullable=True)
    identifiers = Column(JSONB, nullable=True)
    industry_codes = Column(JSONB, nullable=True)
    officers = Column(JSONB, nullable=True)
    filings = Column(JSONB, nullable=True)
    financial_summary = Column(JSONB, nullable=True)
    corporate_groupings = Column(JSONB, nullable=True)
    ultimate_beneficial_owners = Column(JSONB, nullable=True)
    registered_address = Column(JSONB, nullable=True)
    source = Column(JSONB, nullable=True)
    fetched_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OpenCorpApiDailyCallRow(Base):
    """A durable per-day counter of OpenCorporates API calls (pages).

    One row per calendar day. ``call_count`` counts every HTTP page fetched
    against the daily budget; enrichment batches read and increment it so the
    quota survives process restarts and cross-day resumes.
    """

    __tablename__ = "opencorp_api_daily_calls"
    __table_args__ = ({"schema": _SCHEMA},)

    call_date = Column(Date, primary_key=True)
    call_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OpenCorpIndexRepository:
    """Data-access object for the OpenCorporates lookup cache tables."""

    def __init__(
        self, db_url: str = settings.DATABASE_URL, engine: Engine | None = None
    ) -> None:
        """Build the repository over an existing or freshly created engine.

        Args:
            db_url: PostgreSQL connection string used when ``engine`` is not
                supplied. Defaults to :data:`settings.DATABASE_URL`.
            engine: Pre-built SQLAlchemy engine (used by tests). When ``None``,
                an engine is created from ``db_url`` with the project search
                path applied so unqualified names resolve to ``celex_facts``.
        """
        self.engine: Engine = engine or create_engine(
            db_url,
            pool_pre_ping=True,
            connect_args={"options": f"-csearch_path={settings.DB_SEARCH_PATH}"},
        )
        self._session_factory = sessionmaker(bind=self.engine)

    def ensure_tables(self) -> None:
        """Create the OpenCorporates cache tables if they do not yet exist.

        Idempotent: delegates to ``Base.metadata.create_all`` which issues
        ``CREATE TABLE IF NOT EXISTS`` for each mapped table.
        """
        Base.metadata.create_all(self.engine)

    def search(
        self,
        normalized_query: str,
        jurisdiction_code: str | None,
        *,
        require_complete: bool = False,
    ) -> OpenCorpSearchResult | None:
        """Return a cached search result for the given key, or ``None``.

        Args:
            normalized_query: Casefolded, whitespace-collapsed cache key.
            jurisdiction_code: Jurisdiction filter, or ``None`` for an
                unfiltered search. ``None`` maps to the stored empty-string key.
            require_complete: When True, decline a row whose stored pages do not
                cover its own ``total_pages``. The resolution ladder abandons an
                over-cap rung after one page, and that truncated row shares a
                cache key with a full fetch of the same query -- without this
                guard it would silently answer a later caller that wanted every
                page.

        Returns:
            The cached :class:`OpenCorpSearchResult` with ``from_cache=True``,
            or ``None`` when no row matches the key.
        """
        jurisdiction_key = jurisdiction_code or ""
        session = self._session_factory()
        try:
            query_row = session.execute(
                select(OpenCorpQuery).where(
                    OpenCorpQuery.normalized_query == normalized_query,
                    OpenCorpQuery.jurisdiction_code == jurisdiction_key,
                    OpenCorpQuery.error_class.is_(None),
                )
            ).scalar_one_or_none()
            if query_row is None:
                return None
            if (
                require_complete
                and query_row.total_pages is not None
                and query_row.fetched_pages < query_row.total_pages
            ):
                logger.debug(
                    "Declining partial cache row for %r (%d of %d pages)",
                    normalized_query,
                    query_row.fetched_pages,
                    query_row.total_pages,
                )
                return None

            company_rows = session.execute(
                select(OpenCorpCompanyRow).where(
                    OpenCorpCompanyRow.query_id == query_row.id
                )
            ).scalars().all()
            companies = [self._company_from_row(row) for row in company_rows]
            return OpenCorpSearchResult(
                query_text=query_row.query_text,
                normalized_query=query_row.normalized_query,
                jurisdiction_code=query_row.jurisdiction_code or None,
                fetched_pages=query_row.fetched_pages,
                total_pages=query_row.total_pages,
                total_count=query_row.total_count,
                companies=companies,
                from_cache=True,
            )
        finally:
            session.close()

    def save_search(self, result: OpenCorpSearchResult) -> uuid.UUID:
        """Persist a fresh search result, replacing any cached row for its key.

        Runs in one transaction: the existing query row for the cache key is
        deleted (its company rows cascade away via the foreign key), then a new
        query row and its company rows are inserted.

        Args:
            result: The freshly fetched search result to cache.

        Returns:
            The UUID primary key of the inserted query row.
        """
        jurisdiction_key = result.jurisdiction_code or ""
        query_id = uuid4()
        session = self._session_factory()
        try:
            existing = session.execute(
                select(OpenCorpQuery).where(
                    OpenCorpQuery.normalized_query == result.normalized_query,
                    OpenCorpQuery.jurisdiction_code == jurisdiction_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                session.delete(existing)
                session.flush()

            session.add(
                OpenCorpQuery(
                    id=query_id,
                    query_text=result.query_text,
                    normalized_query=result.normalized_query,
                    jurisdiction_code=jurisdiction_key,
                    fetched_pages=result.fetched_pages,
                    total_pages=result.total_pages,
                    total_count=result.total_count,
                )
            )
            for company in result.companies:
                session.add(
                    OpenCorpCompanyRow(
                        id=uuid4(),
                        query_id=query_id,
                        **self._company_to_columns(company),
                    )
                )
            session.commit()
            return query_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def save_query_error(
        self,
        query_text: str,
        normalized_query: str,
        jurisdiction_code: str | None,
        error_class: str,
        error_status_code: int | None,
        error_message: str,
    ) -> uuid.UUID:
        """Record a failed search attempt, replacing any row for its key.

        Mirrors :meth:`save_search`'s delete-then-insert shape so a query row
        always has at most one entry per ``(normalized_query,
        jurisdiction_code)`` key: no company rows are attached, and
        ``fetched_pages`` stays 0. The failed name is left off the roster
        marker by the caller, so it is retried on the next run; when that
        retry succeeds, :meth:`save_search` replaces this row and the error
        columns disappear with it.

        Args:
            query_text: The raw company name that was searched.
            normalized_query: Casefolded, whitespace-collapsed cache key,
                e.g. from :meth:`OpenCorpClient.normalize_query`.
            jurisdiction_code: Jurisdiction filter, or ``None`` for an
                unfiltered search.
            error_class: The raised exception's class name (e.g.
                ``"OpenCorpRateLimitError"``).
            error_status_code: The HTTP status code that caused the failure,
                or ``None`` for a transport-level error with no response.
            error_message: The exception's message text.

        Returns:
            The UUID primary key of the inserted query row.
        """
        jurisdiction_key = jurisdiction_code or ""
        query_id = uuid4()
        session = self._session_factory()
        try:
            existing = session.execute(
                select(OpenCorpQuery).where(
                    OpenCorpQuery.normalized_query == normalized_query,
                    OpenCorpQuery.jurisdiction_code == jurisdiction_key,
                )
            ).scalar_one_or_none()
            if existing is not None:
                session.delete(existing)
                session.flush()

            session.add(
                OpenCorpQuery(
                    id=query_id,
                    query_text=query_text,
                    normalized_query=normalized_query,
                    jurisdiction_code=jurisdiction_key,
                    fetched_pages=0,
                    error_class=error_class,
                    error_status_code=error_status_code,
                    error_message=error_message,
                )
            )
            session.commit()
            return query_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def company_row_ids(
        self, normalized_query: str, jurisdiction_code: str | None
    ) -> list[uuid.UUID]:
        """Return the ``opencorp_index_companies`` row IDs for a cached search.

        Resolves the query row by its cache key ``(normalized_query,
        jurisdiction_code)`` -- ``None`` maps to the stored empty-string key,
        matching :meth:`search` -- and returns the primary keys of its company
        rows. These UUIDs are what a downstream N:M link table references;
        :class:`OpenCorpSearchResult` carries the company models but not their
        persisted row IDs, so this lookup fills that gap.

        Args:
            normalized_query: Casefolded, whitespace-collapsed cache key.
            jurisdiction_code: Jurisdiction filter, or ``None`` for an
                unfiltered search.

        Returns:
            The company row UUIDs for the matching query, or an empty list when
            no query row matches or it has no company rows.
        """
        jurisdiction_key = jurisdiction_code or ""
        session = self._session_factory()
        try:
            rows = session.execute(
                select(OpenCorpCompanyRow.id)
                .join(
                    OpenCorpQuery,
                    OpenCorpCompanyRow.query_id == OpenCorpQuery.id,
                )
                .where(
                    OpenCorpQuery.normalized_query == normalized_query,
                    OpenCorpQuery.jurisdiction_code == jurisdiction_key,
                )
            ).scalars().all()
            return list(rows)
        finally:
            session.close()

    def company_row_ids_by_key(
        self, normalized_query: str, jurisdiction_code: str | None
    ) -> dict[tuple[str, str], uuid.UUID]:
        """Return a cached search's company row IDs keyed by registry identity.

        The scored path of the resolution ladder decides which *models* to link
        and then needs their persisted row IDs. Pairing the two by list position
        would depend on :meth:`company_row_ids` and
        :attr:`OpenCorpSearchResult.companies` sharing an order, which nothing
        guarantees; keying on the registry identity the unique constraint is
        already built from makes the join explicit instead.

        Args:
            normalized_query: Casefolded, whitespace-collapsed cache key.
            jurisdiction_code: Jurisdiction filter, or ``None`` for an
                unfiltered search.

        Returns:
            ``(jurisdiction_code, company_number) -> row id`` for the matching
            query, empty when no query row matches or it has no company rows.
        """
        jurisdiction_key = jurisdiction_code or ""
        session = self._session_factory()
        try:
            rows = session.execute(
                select(
                    OpenCorpCompanyRow.jurisdiction_code,
                    OpenCorpCompanyRow.company_number,
                    OpenCorpCompanyRow.id,
                )
                .join(
                    OpenCorpQuery,
                    OpenCorpCompanyRow.query_id == OpenCorpQuery.id,
                )
                .where(
                    OpenCorpQuery.normalized_query == normalized_query,
                    OpenCorpQuery.jurisdiction_code == jurisdiction_key,
                )
            ).all()
            return {
                (row.jurisdiction_code, row.company_number): row.id for row in rows
            }
        finally:
            session.close()

    def today_call_count(self, call_date: date) -> int:
        """Return the recorded OpenCorporates page count for ``call_date``.

        Args:
            call_date: The calendar day to read.

        Returns:
            The stored ``call_count`` for the day, or ``0`` when no row exists.
        """
        session = self._session_factory()
        try:
            count = session.execute(
                select(OpenCorpApiDailyCallRow.call_count).where(
                    OpenCorpApiDailyCallRow.call_date == call_date
                )
            ).scalar_one_or_none()
            return count or 0
        finally:
            session.close()

    def record_calls(self, call_date: date, count: int) -> int:
        """Add ``count`` API calls to ``call_date`` and return the new total.

        Uses a PostgreSQL ``INSERT ... ON CONFLICT (call_date) DO UPDATE`` so a
        concurrent or restarted run accumulates into the same daily row. A
        non-positive ``count`` is a no-op that still returns the current total.

        Args:
            call_date: The calendar day to increment.
            count: Number of API calls (pages) to add.

        Returns:
            The day's ``call_count`` after the increment.
        """
        if count <= 0:
            return self.today_call_count(call_date)

        statement = (
            pg_insert(OpenCorpApiDailyCallRow)
            .values(call_date=call_date, call_count=count)
            .on_conflict_do_update(
                index_elements=[OpenCorpApiDailyCallRow.call_date],
                set_={
                    "call_count": OpenCorpApiDailyCallRow.call_count + count,
                    "updated_at": func.now(),
                },
            )
            .returning(OpenCorpApiDailyCallRow.call_count)
        )
        session = self._session_factory()
        try:
            new_total = session.execute(statement).scalar_one()
            session.commit()
            return new_total
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def company_details(
        self, jurisdiction_code: str, company_number: str
    ) -> OpenCorpCompanyDetail | None:
        """Return a cached company-detail record, or ``None`` when absent.

        Args:
            jurisdiction_code: OpenCorporates jurisdiction code.
            company_number: Registry-assigned company number.

        Returns:
            The cached :class:`OpenCorpCompanyDetail` with ``from_cache=True``,
            or ``None`` when no row matches the key.
        """
        session = self._session_factory()
        try:
            detail_row = session.execute(
                select(OpenCorpCompanyDetailRow).where(
                    OpenCorpCompanyDetailRow.jurisdiction_code == jurisdiction_code,
                    OpenCorpCompanyDetailRow.company_number == company_number,
                )
            ).scalar_one_or_none()
            if detail_row is None:
                return None
            return self._detail_from_row(detail_row)
        finally:
            session.close()

    def save_company_details(self, detail: OpenCorpCompanyDetail) -> uuid.UUID:
        """Upsert a company-detail record keyed by jurisdiction and number.

        Uses a PostgreSQL ``INSERT ... ON CONFLICT DO UPDATE`` on the
        ``uq_opencorp_detail_co`` constraint, refreshing every data column and
        ``fetched_at`` on conflict.

        Args:
            detail: The company-detail record to persist.

        Returns:
            The UUID primary key of the inserted or updated row.
        """
        insert_values = self._detail_to_columns(detail)
        insert_values["id"] = uuid4()
        update_values: dict[str, Any] = {
            column: insert_values[column] for column in self._detail_data_columns()
        }
        update_values["fetched_at"] = func.now()

        statement = (
            pg_insert(OpenCorpCompanyDetailRow)
            .values(**insert_values)
            .on_conflict_do_update(
                constraint=_DETAIL_UNIQUE_CONSTRAINT, set_=update_values
            )
            .returning(OpenCorpCompanyDetailRow.id)
        )
        session = self._session_factory()
        try:
            row_id = session.execute(statement).scalar_one()
            session.commit()
            return row_id
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _detail_data_columns() -> tuple[str, ...]:
        """Return the non-key data column names on the detail table.

        Returns:
            The scalar and section column names updated on conflict.
        """
        return _DETAIL_SCALAR_COLUMNS + _DETAIL_SECTION_COLUMNS

    @staticmethod
    def _company_to_columns(company: OpenCorpCompany) -> dict[str, Any]:
        """Map a company model to search-company table column values.

        Args:
            company: The company model to persist.

        Returns:
            A mapping of column name to value for ``OpenCorpCompanyRow``.
        """
        return {
            "name": company.name,
            "company_number": company.company_number,
            "jurisdiction_code": company.jurisdiction_code,
            "incorporation_date": company.incorporation_date,
            "dissolution_date": company.dissolution_date,
            "company_type": company.company_type,
            "registry_url": company.registry_url,
            "inactive": company.inactive,
            "current_status": company.current_status,
            "opencorporates_url": company.opencorporates_url,
            "previous_names": company.previous_names,
            "registered_address_in_full": company.registered_address_in_full,
            "registered_address_locality": (
                (company.registered_address or {}).get("locality")
            ),
            "industry_codes": company.industry_codes,
        }

    @staticmethod
    def _company_from_row(row: Any) -> OpenCorpCompany:
        """Map a search-company table row back to a company model.

        Args:
            row: An ``OpenCorpCompanyRow`` (or attribute-compatible object).

        Returns:
            The reconstructed :class:`OpenCorpCompany`.
        """
        return OpenCorpCompany(
            name=row.name,
            company_number=row.company_number,
            jurisdiction_code=row.jurisdiction_code,
            incorporation_date=row.incorporation_date,
            dissolution_date=row.dissolution_date,
            company_type=row.company_type,
            registry_url=row.registry_url,
            inactive=row.inactive,
            current_status=row.current_status,
            opencorporates_url=row.opencorporates_url,
            previous_names=row.previous_names or [],
            registered_address=(
                {"locality": row.registered_address_locality}
                if row.registered_address_locality
                else None
            ),
            registered_address_in_full=row.registered_address_in_full,
            industry_codes=row.industry_codes or [],
        )

    @staticmethod
    def _detail_to_columns(detail: OpenCorpCompanyDetail) -> dict[str, Any]:
        """Map a detail model to detail table column values.

        The ``number_of_employees`` value is stored as text because upstream
        registries surface either an integer or a range string.

        Args:
            detail: The detail model to persist.

        Returns:
            A mapping of column name to value for ``OpenCorpCompanyDetailRow``.
        """
        employees = detail.number_of_employees
        columns: dict[str, Any] = {
            "jurisdiction_code": detail.jurisdiction_code,
            "company_number": detail.company_number,
            "name": detail.name,
            "incorporation_date": detail.incorporation_date,
            "dissolution_date": detail.dissolution_date,
            "company_type": detail.company_type,
            "registry_url": detail.registry_url,
            "inactive": detail.inactive,
            "current_status": detail.current_status,
            "opencorporates_url": detail.opencorporates_url,
            "agent_name": detail.agent_name,
            "agent_address": detail.agent_address,
            "number_of_employees": None if employees is None else str(employees),
            "native_company_number": detail.native_company_number,
            "registered_address_in_full": detail.registered_address_in_full,
        }
        for section in _DETAIL_SECTION_COLUMNS:
            columns[section] = getattr(detail, section)
        return columns

    @staticmethod
    def _detail_from_row(row: Any) -> OpenCorpCompanyDetail:
        """Map a detail table row back to a detail model.

        Args:
            row: An ``OpenCorpCompanyDetailRow`` (or attribute-compatible object).

        Returns:
            The reconstructed :class:`OpenCorpCompanyDetail`.
        """
        fields: dict[str, Any] = {
            "jurisdiction_code": row.jurisdiction_code,
            "company_number": row.company_number,
            "name": row.name,
            "incorporation_date": row.incorporation_date,
            "dissolution_date": row.dissolution_date,
            "company_type": row.company_type,
            "registry_url": row.registry_url,
            "inactive": row.inactive,
            "current_status": row.current_status,
            "opencorporates_url": row.opencorporates_url,
            "agent_name": row.agent_name,
            "agent_address": row.agent_address,
            "number_of_employees": row.number_of_employees,
            "native_company_number": row.native_company_number,
            "registered_address_in_full": row.registered_address_in_full,
            "from_cache": True,
        }
        for section in _DETAIL_SECTION_COLUMNS:
            fields[section] = getattr(row, section)
        return OpenCorpCompanyDetail(**fields)


__all__ = [
    "Base",
    "OpenCorpApiDailyCallRow",
    "OpenCorpCompanyDetailRow",
    "OpenCorpCompanyRow",
    "OpenCorpIndexRepository",
    "OpenCorpQuery",
]
