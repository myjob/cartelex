"""Persistence layer for the LLM-asserted company index.

Owns ``celex_facts.llm_index_companies``: one row per investigated company
name, holding the profile a cloud LLM asserted for it after every register
lookup had failed. Kept separate from
:mod:`pipelines.corp_index.opencorp_repository` -- that module is the cache for
what OpenCorporates *answered*, this one stores what a model *claims*, and the
two must not be confused at the schema level either.

Nothing here is written back to the roster. ``fk_lei = 'NA'`` on
``company_investigated2celex_id`` keeps meaning "GLEIF has no such company";
an identifier in this table means "a model recalled one", which is a weaker
statement and stays labelled as such by the ``llm_model_id`` column.

Exports:
    LlmCompanyIndexRepository -- DAO for the LLM company index table.
"""

import logging
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    String,
    Text,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config.settings import settings
from pipelines.corp_index.models import LlmCompanyIndexRow, LlmCompanyIndexTarget

logger = logging.getLogger(__name__)

Base = declarative_base()

_SCHEMA = settings.DB_SCHEMA

# Roster marker for "OpenCorporates was asked and returned nothing", which is
# the residue this pipeline exists to work through.
MARKER_NO_HIT = "NA"

# The two fields whose absence marks a stored profile as impoverished: what
# became of the company, and how old it is. Neither is supplied by any register
# in this pipeline, so an 'NA' in both means the row cost a cloud call and
# answered nothing the fact pipeline wanted.
#
# The predicate coalesces because these columns are tri-state in the same way
# the roster's are, and for once NULL and 'NA' must be treated alike: NULL means
# "written before this column existed" (1,550 rows on 2026-08-02) and 'NA' means
# "asked and did not know" (11). Selecting on 'NA' alone would retry 11 rows
# where 680 are unknown.
_UNKNOWN_PROFILE_COLUMNS: tuple[str, ...] = (
    "life_cycle_status",
    "established_since",
)

# Every column a re-run may overwrite, which is every column except the two the
# batch does not own: company_name is the functional key the conflict matched
# on, and created_at records when the company first entered the table. A field
# missing from this tuple would silently keep its first answer forever, so it
# doubles as the source of truth for the ADD COLUMN backfill below.
_PROFILE_COLUMNS: tuple[str, ...] = (
    "canonical_name",
    "abbreviation",
    "legal_form",
    "established_since",
    "life_cycle_status",
    "succession",
    "products_services",
    "geo_market",
    "headquarter_address",
    "country",
    "jurisdiction",
    "lei",
    "euid",
    "bris",
    "handelsregister",
    "siren",
    "generic_corp_id",
    "generic_corp_registrar",
    "llm_model_id",
)


def _unknown_profile_condition() -> str:
    """Return the SQL selecting roster rows whose stored profile stayed unknown.

    The inverse of the resume anti-join: instead of "no profile exists", this is
    "a profile exists and it answered nothing useful".

    Returns:
        An ``EXISTS`` predicate, parameterized on ``:marker_no_hit``.
    """
    unknown_predicates = " AND ".join(
        f"coalesce(stored.{column_name}, :marker_no_hit) = :marker_no_hit"
        for column_name in _UNKNOWN_PROFILE_COLUMNS
    )
    return (
        f"EXISTS (SELECT 1 FROM {_SCHEMA}.llm_index_companies stored "
        f"WHERE stored.company_name = view_row.company_name "
        f"AND {unknown_predicates})"
    )


class LlmIndexCompany(Base):
    """One LLM-asserted company profile, keyed by the roster company name."""

    __tablename__ = "llm_index_companies"
    __table_args__ = (
        Index("ix_llm_index_companies_country", "country"),
        Index("ix_llm_index_companies_lei", "lei"),
        {"schema": _SCHEMA},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # The functional key: the verbatim roster name that was looked up, so the
    # table joins straight onto company_investigated2celex_id.company_name.
    company_name = Column(Text, nullable=False, unique=True)

    canonical_name = Column(Text, nullable=True)
    abbreviation = Column(Text, nullable=True)
    legal_form = Column(Text, nullable=True)
    # Free text, not a date: the model's honest answer is often a bare year or a
    # circa year, and a date column would force it to invent a day and a month.
    established_since = Column(Text, nullable=True)
    life_cycle_status = Column(Text, nullable=True)
    # Who holds the entity now, and what event put it there. The reason most of
    # these companies are unresolvable is that they were absorbed decades ago;
    # without this column that answer had nowhere to go and came back as 'NA'.
    succession = Column(Text, nullable=True)
    products_services = Column(Text, nullable=True)
    geo_market = Column(Text, nullable=True)
    headquarter_address = Column(Text, nullable=True)
    country = Column(Text, nullable=True)
    jurisdiction = Column(Text, nullable=True)

    # Natural keys issued by registers, hence varchar rather than uuid. 'NA'
    # when the model had none -- never NULL, so "the model was asked and did not
    # know" stays distinct from "no row exists for this company".
    lei = Column(String(64), nullable=True)
    euid = Column(String(64), nullable=True)
    bris = Column(String(64), nullable=True)
    handelsregister = Column(String(64), nullable=True)
    siren = Column(String(64), nullable=True)
    # The catch-all for every registrar with no column of its own (the Belgian
    # CBE, the Dutch KVK, VAT, Companies House, RCS, CVR, ...). A number is
    # uninterpretable and unverifiable without the registrar that issued it, so
    # the two travel together and are written together.
    generic_corp_id = Column(String(64), nullable=True)
    generic_corp_registrar = Column(Text, nullable=True)

    # Which model made the claim. Recorded per row because the answer is only
    # as good as its author, and the configured corp_index model changes.
    llm_model_id = Column(String(128), nullable=False)

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class LlmCompanyIndexRepository:
    """Data-access object for ``celex_facts.llm_index_companies``."""

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
        """Create or bring forward the LLM company index table.

        Idempotent: delegates to ``Base.metadata.create_all``, which issues
        ``CREATE TABLE IF NOT EXISTS``, and then adds any profile column the
        stored table does not have yet. The equivalent hand-written DDL lives in
        ``migrate_llm_index_companies.sql`` for the DBA path.
        """
        Base.metadata.create_all(self.engine)
        self._add_missing_profile_columns()

    def _add_missing_profile_columns(self) -> None:
        """Add profile columns an already-created table is missing.

        ``create_all`` stops at ``CREATE TABLE IF NOT EXISTS``: a table created
        before a column existed never gains it, and the next upsert fails on an
        unknown column instead. The statements are driven off
        :data:`_PROFILE_COLUMNS` and the ORM's own types, so a column added to
        the model cannot be forgotten here, and every one is ``ADD COLUMN IF NOT
        EXISTS`` -- a no-op on a table that is already current.

        Nullability is deliberately not reproduced: a column added to a
        populated table cannot be ``NOT NULL`` without a default, and the only
        such column (``llm_model_id``) has existed since the table did.
        """
        table_name = f"{_SCHEMA}.{LlmIndexCompany.__tablename__}"
        dialect = self.engine.dialect
        with self.engine.begin() as conn:
            for column_name in _PROFILE_COLUMNS:
                column_type = LlmIndexCompany.__table__.columns[column_name].type
                conn.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        f"ADD COLUMN IF NOT EXISTS {column_name} "
                        f"{column_type.compile(dialect)}"
                    )
                )
        logger.debug("Profile columns verified on %s", table_name)

    def select_targets(
        self,
        marker: str | None = MARKER_NO_HIT,
        iso_country_code: str | None = None,
        include_existing: bool = False,
        retry_unknown: bool = False,
        min_name_length: int = 3,
        limit: int = 0,
    ) -> list[LlmCompanyIndexTarget]:
        """Return roster companies awaiting an LLM profile.

        Reads ``celex_facts.company_investigated2lei2corpidx2hr_vw`` and groups
        it to one row per ``company_name`` -- the view is a ``DISTINCT``
        projection in which one name can appear several times with different
        addresses or countries, while this table holds one profile per name.
        Values that vary within a name are aggregated with ``max()``, which is
        arbitrary but deterministic; the same choice ``select_opencorp_targets``
        makes. A confirmed LEI or Handelsregister number is preferred over the
        ``'NA'`` marker so the authoritative value reaches the model when any
        row carries one.

        Companies already profiled are anti-joined out unless
        ``include_existing``, which is the whole resume mechanism: an
        interrupted run continues rather than restarts, and a completed one is
        a no-op.

        Args:
            marker: ``fk_opencorp_idx`` value to select, ``'NA'`` (companies
                OpenCorporates could not resolve) by default. ``None`` selects
                every row of the view regardless of marker.
            iso_country_code: ISO 3166-1 country code to restrict to, or None
                for every country.
            include_existing: When True, also return companies that already
                have a stored profile, so their answer is re-asked.
            retry_unknown: When True, return *only* companies whose stored
                profile left both its life cycle status and its founding year
                unknown -- the rows that cost a call and answered nothing the
                fact pipeline wanted. Implies ``include_existing``, since every
                such company is by definition already stored, and overrides it.
            min_name_length: Shortest company name still worth asking about.
                Shorter names, and names with no alphanumeric character at all
                (the view holds a literal ``'[...]'``), are extraction junk that
                no model can profile.
            limit: Return at most this many companies; ``0`` for all.

        Returns:
            Targets ordered by company name, for a stable resume order.
        """
        conditions = [
            "char_length(btrim(company_name)) >= :min_name_length",
            "btrim(company_name) ~ '[[:alnum:]]'",
        ]
        params: dict[str, object] = {"min_name_length": min_name_length}
        if marker is not None:
            conditions.append("fk_opencorp_idx = :marker")
            params["marker"] = marker
        if iso_country_code is not None:
            conditions.append("iso_country_code = :iso_country_code")
            params["iso_country_code"] = iso_country_code
        # Mutually exclusive by construction: one selects rows with no stored
        # profile, the other rows with a stored but useless one. Emitting both
        # would ask for a company that is simultaneously absent and present.
        if retry_unknown:
            conditions.append(_unknown_profile_condition())
            params["marker_no_hit"] = MARKER_NO_HIT
        elif not include_existing:
            conditions.append(
                f"NOT EXISTS (SELECT 1 FROM {_SCHEMA}.llm_index_companies stored "
                "WHERE stored.company_name = view_row.company_name)"
            )

        limit_clause = ""
        if limit > 0:
            limit_clause = "LIMIT :limit"
            params["limit"] = limit

        sql = text(f"""
            WITH eligible AS (
                SELECT company_name, address, iso_country_code, jurisdication_code,
                       fk_lei, fk_handelsregister
                FROM {_SCHEMA}.company_investigated2lei2corpidx2hr_vw AS view_row
                WHERE {" AND ".join(conditions)}
            )
            SELECT
                company_name,
                max(address) AS address,
                max(iso_country_code) AS iso_country_code,
                max(jurisdication_code) AS jurisdication_code,
                max(fk_lei) FILTER (WHERE fk_lei <> '{MARKER_NO_HIT}') AS lei,
                max(fk_handelsregister) FILTER (
                    WHERE fk_handelsregister <> '{MARKER_NO_HIT}'
                ) AS handelsregister
            FROM eligible
            GROUP BY company_name
            ORDER BY company_name
            {limit_clause}
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            LlmCompanyIndexTarget(
                company_name=row.company_name,
                address=row.address,
                iso_country_code=row.iso_country_code,
                jurisdication_code=row.jurisdication_code,
                lei=row.lei,
                handelsregister=row.handelsregister,
            )
            for row in rows
        ]

    def upsert_company(self, row: LlmCompanyIndexRow) -> uuid.UUID:
        """Persist one company profile, replacing any profile stored for it.

        ``company_name`` is the functional key, so a re-run under a different
        model overwrites the profile in place and stamps ``updated_at`` rather
        than accumulating rows. Every column except that key and ``created_at``
        is refreshed, including the ones the new answer left at ``'NA'``: a
        second pass states the model's whole current answer, not the union of it
        with a stale one, so a value that disappears is visible rather than
        preserved by omission. ``created_at`` keeps recording when the company
        first entered the table.

        Args:
            row: The validated profile row to write.

        Returns:
            The primary key of the inserted or updated row.
        """
        columns = row.to_columns()
        statement = (
            pg_insert(LlmIndexCompany)
            .values(id=uuid.uuid4(), **columns)
            .on_conflict_do_update(
                index_elements=[LlmIndexCompany.company_name],
                set_={
                    column: columns[column] for column in _PROFILE_COLUMNS
                }
                | {"updated_at": func.now()},
            )
            .returning(LlmIndexCompany.id)
        )

        session = self._session_factory()
        try:
            row_id = session.execute(statement).scalar_one()
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        logger.debug("Stored LLM company profile for %r (%s)", row.company_name, row_id)
        return row_id
