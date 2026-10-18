"""
DAO for the ``celex_facts.company_investigated2celex_id`` roster table.

Persists one row per investigated company per CELEX document, derived from the
``companies_investigated`` fact and enriched with the registered address
(``companies_address`` fact) and ISO country code (``companies_country`` fact).
The remaining enrichment columns (``fk_lei``, ``fk_opencorp_idx``,
``fk_handelsregister``) are left NULL here; they are populated by downstream
enrichment steps. This DAO also owns the roster side of the OpenCorporates
resolution: selecting targets, linking roster rows to OpenCorporates hits in
the N:M table, and marking ``fk_opencorp_idx``.

All queries go through the shared engine from ``session.py`` (single
connection pool), following the same pattern as ``CompanyRepository``.

Exports:
    InvestigatedCompany — input VO carrying a company name and its optional
    address / ISO country code enrichment.
    CompanyInvestigatedRepository — persistence service for the investigated
    company roster.
"""

import logging
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from pipelines.corp_index.opencorp_resolution_ladder import OpenCorpTarget
from pipelines.rag.pgvector.session import make_engine
from pipelines.rag.pgvector.vector_config import DATABASE_URL

logger = logging.getLogger(__name__)

# German umlaut / ligature expansions applied before generic accent folding, so
# "Müller" and "Mueller" normalize to the same key (ae/oe/ue/ss convention).
_UMLAUT_EXPANSIONS: tuple[tuple[str, str], ...] = (
    ("ä", "ae"),
    ("ö", "oe"),
    ("ü", "ue"),
    ("ß", "ss"),
    ("æ", "ae"),
    ("œ", "oe"),
)
# Remaining single-char Latin accents folded to their base letter (source and
# target must stay the same length -- PostgreSQL ``translate`` is 1:1).
_ACCENT_SOURCE = "àáâãåèéêëìíîïòóôõøùúûçñ"
_ACCENT_TARGET = "aaaaaeeeeiiiiooooouuucn"

# Shortest roster name (in characters) accepted as a LEI substring needle. Names
# shorter than this match too many ``corp_lei.legal_name`` rows to be useful (and
# cannot use the trigram index below 3 chars), so they are skipped -> "NA".
MIN_LEI_NEEDLE_LENGTH = 4


def _handelsregister_match_key(column_sql: str) -> str:
    """Build a SQL expression that normalizes ``column_sql`` for fuzzy matching.

    The key is case-, accent-, umlaut-, punctuation-, symbol- and
    whitespace-insensitive: it lowercases the value, expands German umlauts and
    ligatures (``ä->ae`` ...), folds remaining Latin accents to their base
    letter, and strips every character that is not ``[a-z0-9]``. This is not a
    similarity metric -- it only makes spelling-variant strings compare equal.
    Applied identically to both sides of the join so the comparison is symmetric.

    Parameters
    ----------
    column_sql:
        A trusted SQL column expression to normalize (e.g. ``"h.name"``). Never
        interpolate untrusted input here -- callers pass fixed identifiers only.

    Returns
    -------
    str
        A SQL expression string.
    """
    expr = f"lower({column_sql})"
    for source, replacement in _UMLAUT_EXPANSIONS:
        expr = f"replace({expr}, '{source}', '{replacement}')"
    expr = f"translate({expr}, '{_ACCENT_SOURCE}', '{_ACCENT_TARGET}')"
    return f"regexp_replace({expr}, '[^a-z0-9]', '', 'g')"


class InvestigatedCompany(BaseModel):
    """One investigated company with its optional enrichment fields.

    Input VO for :meth:`CompanyInvestigatedRepository.insert_companies`. The
    company name is authoritative (sourced from the ``companies_investigated``
    fact); ``address`` and ``iso_country_code`` are enrichment values matched by
    company name from the ``companies_address`` / ``companies_country`` facts and
    are ``None`` when unknown, mapping to a NULL column.

    Attributes
    ----------
    company_name:
        Verbatim company / association name.
    address:
        Registered or postal address, or None when unknown.
    iso_country_code:
        ISO 3166-1 country code, or None when unknown.
    """

    model_config = {"frozen": True}

    company_name: str = Field(description="Verbatim company / association name.")
    address: str | None = Field(
        default=None, description="Registered/postal address, or None when unknown."
    )
    iso_country_code: str | None = Field(
        default=None, description="ISO 3166-1 country code, or None when unknown."
    )


class OpenCorpLink(BaseModel):
    """One roster-row-to-OpenCorporates-hit link with its resolution provenance.

    Input VO for :meth:`CompanyInvestigatedRepository.insert_opencorp_links`.
    The provenance fields are what make a link auditable after the fact: the
    same roster row can be reached by a verbatim search inside its own
    jurisdiction or by an unfiltered search of a stripped name, and those are
    very different claims about the same pair of ids.

    Attributes
    ----------
    fk_company_investigated:
        Roster row id.
    fk_opencorp_index_company:
        Cached OpenCorporates company row id.
    resolution_rung:
        Slug of the ladder rung that produced the match.
    resolution_query:
        The relaxed query text actually sent for that rung.
    resolution_jurisdiction:
        Jurisdiction the rung searched under, or None when unfiltered.
    match_score:
        The hit's score, in [0, 1]. 1.0 for an unscored rung, where the
        jurisdiction filter rather than the scorer supplied the precision.
    """

    model_config = {"frozen": True}

    fk_company_investigated: uuid.UUID = Field(description="Roster row id.")
    fk_opencorp_index_company: uuid.UUID = Field(
        description="Cached OpenCorporates company row id."
    )
    resolution_rung: str = Field(description="Ladder rung that produced the match.")
    resolution_query: str = Field(description="Relaxed query text that was sent.")
    resolution_jurisdiction: str | None = Field(
        default=None, description="Jurisdiction searched, None when unfiltered."
    )
    match_score: float = Field(default=1.0, description="Hit score in [0, 1].")


class CompanyInvestigatedRepository:
    """DAO for ``celex_facts.company_investigated2celex_id``.

    Parameters
    ----------
    db_url:
        PostgreSQL connection string. Defaults to ``DATABASE_URL`` from
        ``vector_config.py`` / environment.
    """

    def __init__(self, db_url: str = DATABASE_URL) -> None:
        self.engine = make_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)

    def select_company_names(self, celex_id: str) -> set[str]:
        """Return the company names already stored for a CELEX.

        The loader diffs this against the names the current
        ``companies_investigated`` fact yields, so that a re-extracted fact is
        reconciled into the table rather than skipped. ``(company_name,
        celex_id)`` is UNIQUE, so a name identifies at most one row here.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.

        Returns
        -------
        set[str]
            Stored company names; empty when the CELEX was never loaded.
        """
        sql = text(
            "SELECT company_name FROM celex_facts.company_investigated2celex_id "
            "WHERE celex_id = :celex_id"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"celex_id": celex_id}).scalars().all()
        return {name for name in rows if name is not None}

    def count_opencorp_links(self, celex_id: str, company_names: list[str]) -> int:
        """Count OpenCorporates links held by the named roster rows.

        ``company_investigated_celex_id2opencorp_index_company.fk_company_investigated``
        is ``ON DELETE CASCADE``, so deleting a roster row silently discards its
        links. This reports how many would go, letting the loader log the loss
        (and a dry run predict it) instead of it happening unannounced.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        company_names:
            Names of the roster rows about to be deleted. Empty is a no-op.

        Returns
        -------
        int
            The number of link rows that a delete would cascade away.
        """
        if not company_names:
            return 0

        sql = text("""
            SELECT COUNT(*)
            FROM celex_facts.company_investigated_celex_id2opencorp_index_company
            WHERE fk_company_investigated IN (
                SELECT id FROM celex_facts.company_investigated2celex_id
                WHERE celex_id = :celex_id
                  AND company_name = ANY(:company_names)
            )
        """)
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    sql, {"celex_id": celex_id, "company_names": list(company_names)}
                ).scalar()
                or 0
            )

    def delete_companies(self, celex_id: str, company_names: list[str]) -> int:
        """Delete the named roster rows of a CELEX.

        Used to retire rows whose company name no longer appears in the
        ``companies_investigated`` fact. Every enrichment those rows carried is
        lost with them, including their OpenCorporates links, which cascade --
        see :meth:`count_opencorp_links`.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        company_names:
            Names of the rows to delete. An empty list is a no-op.

        Returns
        -------
        int
            The number of roster rows deleted.
        """
        if not company_names:
            return 0

        sql = text("""
            DELETE FROM celex_facts.company_investigated2celex_id
            WHERE celex_id = :celex_id
              AND company_name = ANY(:company_names)
        """)
        with self.engine.begin() as conn:
            result = conn.execute(
                sql, {"celex_id": celex_id, "company_names": list(company_names)}
            )
        return result.rowcount

    def insert_companies(
        self, celex_id: str, companies: list[InvestigatedCompany]
    ) -> int:
        """Insert one row per investigated company for a CELEX document.

        The ``address`` and ``iso_country_code`` enrichment columns are populated
        from each :class:`InvestigatedCompany`; a ``None`` value is written as
        NULL. The remaining enrichment columns (``fk_lei``, ``fk_opencorp_idx``,
        ``fk_handelsregister``) are omitted and default to NULL. All rows are
        written in a single transaction.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        companies:
            Investigated companies to persist. An empty list is a no-op.

        Returns
        -------
        int
            The number of rows inserted.
        """
        if not companies:
            return 0

        sql = text("""
            INSERT INTO celex_facts.company_investigated2celex_id
            (id, company_name, celex_id, address, iso_country_code)
            VALUES (
                gen_random_uuid(), :company_name, :celex_id, :address,
                :iso_country_code
            )
        """)
        params = [
            {
                "company_name": company.company_name,
                "celex_id": celex_id,
                "address": company.address,
                "iso_country_code": company.iso_country_code,
            }
            for company in companies
        ]
        with self.engine.begin() as conn:
            conn.execute(sql, params)
        return len(params)

    def update_missing_enrichment(
        self, celex_id: str, companies: list[InvestigatedCompany]
    ) -> int:
        """Backfill NULL ``address`` / ``iso_country_code`` on existing rows.

        For each company, fills only the columns that are currently NULL with the
        supplied non-NULL value (``COALESCE`` semantics), matched by
        ``(celex_id, company_name)``. Existing non-NULL enrichment is preserved
        and a NULL supplied value leaves the column untouched. Rows already fully
        populated are not counted. All updates run in a single transaction.

        Parameters
        ----------
        celex_id:
            The CELEX document identifier.
        companies:
            Investigated companies whose enrichment should backfill existing
            rows. An empty list is a no-op.

        Returns
        -------
        int
            The number of rows actually updated.
        """
        if not companies:
            return 0

        sql = text("""
            UPDATE celex_facts.company_investigated2celex_id
            SET address = COALESCE(address, :address),
                iso_country_code = COALESCE(iso_country_code, :iso_country_code)
            WHERE celex_id = :celex_id
              AND company_name = :company_name
              AND (
                  (address IS NULL AND CAST(:address AS text) IS NOT NULL)
                  OR (iso_country_code IS NULL
                      AND CAST(:iso_country_code AS text) IS NOT NULL)
              )
        """)
        updated = 0
        with self.engine.begin() as conn:
            for company in companies:
                result = conn.execute(
                    sql,
                    {
                        "company_name": company.company_name,
                        "celex_id": celex_id,
                        "address": company.address,
                        "iso_country_code": company.iso_country_code,
                    },
                )
                updated += result.rowcount
        return updated

    def select_roster_by_country(
        self, iso_country_code: str
    ) -> list[tuple[object, str]]:
        """Return the roster rows for a given ISO country code.

        Parameters
        ----------
        iso_country_code:
            ISO 3166-1 country code to filter on (e.g. ``"DE"``).

        Returns
        -------
        list[tuple[object, str]]
            ``(id, company_name)`` pairs; ``id`` is the row's UUID (kept as the
            driver's native type so it binds back cleanly on update).
        """
        sql = text(
            "SELECT id, company_name "
            "FROM celex_facts.company_investigated2celex_id "
            "WHERE iso_country_code = :iso_country_code"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"iso_country_code": iso_country_code})
            return [(row.id, row.company_name) for row in rows]

    def select_all_roster(self) -> list[tuple[object, str]]:
        """Return every roster row, regardless of country.

        Used by country-agnostic enrichment (e.g. LEI, which is a global
        identifier) where the German-only filter of
        :meth:`select_roster_by_country` does not apply.

        Returns
        -------
        list[tuple[object, str]]
            ``(id, company_name)`` pairs; ``id`` is the row's UUID.
        """
        sql = text(
            "SELECT id, company_name FROM celex_facts.company_investigated2celex_id"
        )
        with self.engine.connect() as conn:
            return [(row.id, row.company_name) for row in conn.execute(sql)]

    def select_gleif_subdivisions(self) -> list[tuple[str, str, str]]:
        """Return every ISO 3166-2 subdivision from the GLEIF reference table.

        These rows seed :class:`~pipelines.corp_index.jurisdiction_resolver.JurisdictionResolver`;
        country-level rows are excluded because a country needs no resolution.

        Returns
        -------
        list[tuple[str, str, str]]
            ``(jurisdiction_code, jurisdiction_name, iso_country_code)`` triples,
            ordered by code for a stable resolver build.
        """
        sql = text(
            "SELECT jurisdiction_code, jurisdiction_name, iso_country_code "
            "FROM celex_facts.gleif_jurisdication2countrycode "
            "WHERE is_subdivision "
            "ORDER BY jurisdiction_code"
        )
        with self.engine.connect() as conn:
            return [
                (row.jurisdiction_code, row.jurisdiction_name, row.iso_country_code)
                for row in conn.execute(sql)
            ]

    def select_jurisdiction_inputs(
        self,
    ) -> list[tuple[object, str | None, str | None, str | None]]:
        """Return the per-row inputs the jurisdiction resolver needs.

        Joins each roster row to its matched GLEIF LEI record (when
        ``fk_lei`` holds a real LEI rather than the ``NA`` placeholder) so the
        register's ``legal_jurisdiction`` is available as a secondary signal.
        The join is a LEFT JOIN: most investigated companies have no LEI match,
        often because they were dissolved long ago.

        Returns
        -------
        list[tuple[object, str | None, str | None, str | None]]
            ``(id, iso_country_code, address, legal_jurisdiction)`` per roster
            row; ``id`` is the row's UUID so it binds back cleanly on update.
        """
        sql = text(
            "SELECT roster.id AS id, "
            "       roster.iso_country_code AS iso_country_code, "
            "       roster.address AS address, "
            "       corp_lei.legal_jurisdiction AS legal_jurisdiction "
            "FROM celex_facts.company_investigated2celex_id roster "
            "LEFT JOIN celex_facts.corp_lei "
            "       ON corp_lei.lei = roster.fk_lei"
        )
        with self.engine.connect() as conn:
            return [
                (row.id, row.iso_country_code, row.address, row.legal_jurisdiction)
                for row in conn.execute(sql)
            ]

    def set_jurisdiction_code(self, assignments: list[tuple[object, str]]) -> int:
        """Set ``jurisdication_code`` on roster rows.

        Written by the roster loader's jurisdiction stage. Unlike the ``fk_*``
        enrichment columns there is no ``NA`` placeholder: an unresolved
        jurisdiction stays NULL, so a consumer can tell "no jurisdiction could be
        determined" from "resolved to the country itself".

        Parameters
        ----------
        assignments:
            ``(id, jurisdication_code)`` pairs, where ``id`` is a roster row
            UUID. An empty list is a no-op. All rows are written in a single
            transaction.

        Returns
        -------
        int
            The number of rows written.
        """
        return self._set_fk_column("jurisdication_code", assignments)

    def select_opencorp_targets(
        self,
        iso_country_code: str | None = None,
        all_corps: bool = False,
        retry_markers: tuple[str, ...] = (),
        include_missing_country: bool = False,
    ) -> list[OpenCorpTarget]:
        """Return companies awaiting OpenCorporates resolution, ready to search.

        Reads ``celex_facts.company_investigated2celex_id`` directly rather than
        the roster view. The view is a six-column ``DISTINCT`` projection that
        cannot supply the per-row ``address`` or ``celex_id`` the resolution
        ladder scores against -- and its definition lives in a scratch script
        that has already been replayed once without ``jurisdication_code``,
        breaking every run. Selecting from the table removes both problems.

        Eligibility is ``fk_opencorp_idx IS NULL`` (never looked up), widened by
        ``retry_markers`` to reopen rows already marked. Unless ``all_corps`` is
        set, rows are further restricted to ones that failed LEI or
        Handelsregister resolution. Because processed rows get marked, this
        doubles as the batch's resume cursor.

        The grain is distinct ``(company_name, iso_country_code,
        jurisdication_code)``: one name can appear under two jurisdictions
        (different CELEX documents giving different addresses) and each needs
        its own search. Values that vary across the documents sharing a grain
        are aggregated -- any known address, the union of the documents'
        industries, and the *latest* decision year, which is the most permissive
        choice for the ladder's temporal veto and so cannot reject a hit that
        some other document in the group would have accepted.

        Parameters
        ----------
        iso_country_code:
            ISO 3166-1 country code to filter on, or None for every country.
        all_corps:
            When True, skip the ``fk_lei`` / ``fk_handelsregister`` check.
        retry_markers:
            ``fk_opencorp_idx`` values to reopen, e.g. ``("NA",)``. Empty means
            only never-looked-up rows are returned.
        include_missing_country:
            When True, also return rows with no ``iso_country_code``. They are
            excluded by default because every rung would have to search
            unfiltered, but they are the largest unresolved group on the roster
            (829 of 1,088 unmarked rows on 2026-08-01) and an unfiltered search
            does resolve them -- a country-less ``CEJI-Interlude`` returns
            exactly one hit, registered ``fr``.

        Returns
        -------
        list[OpenCorpTarget]
            Targets ordered by country then name for a stable resume order.
        """
        conditions = []
        params: dict[str, object] = {}
        if not include_missing_country:
            conditions.append("iso_country_code IS NOT NULL")
        if retry_markers:
            conditions.append(
                "(fk_opencorp_idx IS NULL OR fk_opencorp_idx = ANY(:retry_markers))"
            )
            params["retry_markers"] = list(retry_markers)
        else:
            conditions.append("fk_opencorp_idx IS NULL")
        if not all_corps:
            conditions.append("(fk_lei = 'NA' OR fk_handelsregister = 'NA')")
        if iso_country_code is not None:
            conditions.append("iso_country_code = :iso_country_code")
            params["iso_country_code"] = iso_country_code

        sql = text(f"""
            WITH eligible AS (
                SELECT company_name, iso_country_code, jurisdication_code,
                       address, celex_id
                FROM celex_facts.company_investigated2celex_id
                WHERE {" AND ".join(conditions)}
            )
            SELECT
                eligible.company_name,
                eligible.iso_country_code,
                eligible.jurisdication_code,
                max(eligible.address) AS address,
                max(substring(eligible.celex_id from 'CELEX-3([0-9]{{4}})')::int)
                    AS decision_year,
                array_remove(array_agg(DISTINCT industry.term), NULL) AS industries
            FROM eligible
            LEFT JOIN LATERAL (
                SELECT DISTINCT element->>'industry' AS term
                FROM celex_facts.facts
                CROSS JOIN LATERAL
                    jsonb_array_elements(facts.value_json::jsonb) AS element
                WHERE facts.celex_id = eligible.celex_id
                  AND facts.name = 'companies_industry'
                  AND facts.value_json IS NOT NULL
                  AND left(facts.value_json, 1) = '['
            ) AS industry ON TRUE
            GROUP BY 1, 2, 3
            ORDER BY 2, 1
        """)

        with self.engine.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            OpenCorpTarget(
                company_name=row.company_name,
                iso_country_code=row.iso_country_code,
                jurisdication_code=row.jurisdication_code,
                address=row.address,
                industries=tuple(row.industries or ()),
                decision_year=row.decision_year,
            )
            for row in rows
        ]

    def roster_ids_for_company(
        self, company_name: str, iso_country_code: str | None
    ) -> list[uuid.UUID]:
        """Return roster row IDs for a company name within one country.

        These are the ``company_investigated2celex_id`` rows an OpenCorporates
        hit is linked to and whose ``fk_opencorp_idx`` marker is then set. The
        country constraint keeps a same-named company in another jurisdiction
        from being linked to this country's OpenCorporates hits.

        Matched with ``IS NOT DISTINCT FROM`` so that ``None`` selects the rows
        that carry no country, rather than the nothing that ``= NULL`` returns.
        Those rows are searchable in a relaxed run, and a target that cannot find
        its own roster rows would be searched, linked to nothing, and marked on
        nothing -- so it would come back on every subsequent run forever.

        Parameters
        ----------
        company_name:
            Verbatim roster company name.
        iso_country_code:
            ISO 3166-1 country code the rows must carry, or None for the rows
            that carry none.

        Returns
        -------
        list[uuid.UUID]
            The matching roster row UUIDs.
        """
        sql = text(
            "SELECT id FROM celex_facts.company_investigated2celex_id "
            "WHERE company_name = :company_name "
            "  AND iso_country_code IS NOT DISTINCT FROM :iso_country_code"
        )
        with self.engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "company_name": company_name,
                    "iso_country_code": iso_country_code,
                },
            )
            return [row.id for row in rows]

    def insert_opencorp_links(self, links: list[OpenCorpLink]) -> int:
        """Link roster rows to OpenCorporates company rows (N:M), idempotently.

        Writes one row per link into
        ``celex_facts.company_investigated_celex_id2opencorp_index_company``,
        carrying the resolution provenance so the match stays explainable: a hit
        found verbatim under the company's own jurisdiction is a much stronger
        claim than one found by an unfiltered search of a stripped name, and
        only the recorded rung distinguishes them.

        A ``UNIQUE (fk_company_investigated, fk_opencorp_index_company)``
        constraint plus ``ON CONFLICT DO NOTHING`` makes re-runs idempotent, so
        the returned count reflects requested links, not rows actually inserted
        -- and a re-run does not rewrite the provenance of an existing link.
        All links are written in a single transaction.

        Parameters
        ----------
        links:
            The links to write. An empty list is a no-op.

        Returns
        -------
        int
            The number of links submitted.
        """
        if not links:
            return 0

        sql = text("""
            INSERT INTO
                celex_facts.company_investigated_celex_id2opencorp_index_company
                (id, fk_company_investigated, fk_opencorp_index_company,
                 resolution_rung, resolution_query, resolution_jurisdiction,
                 match_score)
            VALUES (gen_random_uuid(), :fk_company_investigated,
                    :fk_opencorp_index_company, :resolution_rung,
                    :resolution_query, :resolution_jurisdiction, :match_score)
            ON CONFLICT (fk_company_investigated, fk_opencorp_index_company)
            DO NOTHING
        """)
        params = [link.model_dump() for link in links]
        with self.engine.begin() as conn:
            conn.execute(sql, params)
        return len(params)

    def resolve_handelsregister_numbers(
        self, company_names: list[str]
    ) -> dict[str, str]:
        """Resolve company names to Handelsregister ``company_number``s (fuzzy).

        Runs a single set-based query (one pass over
        ``celex_facts.handelsregister_companies``) that matches on a normalized
        key tolerant to casing, umlauts, accents, punctuation, symbols and
        whitespace -- see :func:`_handelsregister_match_key`. Matching is not a
        similarity metric; it only makes spelling-variant strings compare equal.
        ``handelsregister_companies.name`` is not indexed, so the normalized key
        is computed inline over the whole table in one seq scan (a per-name
        lookup would scan millions of rows each).

        A verbatim match is preferred over a fuzzy-only match for the same input
        name (exact wins), and among equally-ranked register entries the lowest
        ``company_number`` wins. Input names that normalize to an empty key, and
        names with no match, are absent from the result.

        Parameters
        ----------
        company_names:
            Company names to resolve. An empty list yields an empty map.

        Returns
        -------
        dict[str, str]
            Mapping of input company name to its resolved ``company_number``.
        """
        if not company_names:
            return {}

        target_key = _handelsregister_match_key("company_name")
        register_key = _handelsregister_match_key("register.name")
        sql = text(f"""
            WITH targets AS (
                SELECT company_name, {target_key} AS match_key
                FROM unnest(CAST(:company_names AS text[])) AS company_name
            )
            SELECT DISTINCT ON (targets.company_name)
                   targets.company_name AS company_name,
                   register.company_number AS company_number
            FROM targets
            JOIN celex_facts.handelsregister_companies register
              ON {register_key} = targets.match_key
            WHERE targets.match_key <> ''
            ORDER BY targets.company_name,
                     (register.name = targets.company_name) DESC,
                     register.company_number
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(sql, {"company_names": company_names})
            return {row.company_name: row.company_number for row in rows}

    def resolve_lei_codes(self, company_names: list[str]) -> dict[str, str]:
        """Resolve company names to GLEIF ``lei`` codes by substring match.

        For each name, finds ``celex_facts.corp_lei`` rows whose (always
        uppercase) ``legal_name`` *contains* the uppercased name as a substring
        (``legal_name LIKE '%NAME%'``) -- GLEIF legal names are usually fuller
        than the roster's short form, so a contains-match lifts recall. The
        search rides the ``legal_name`` trigram index; LIKE wildcards in the name
        are escaped (``ESCAPE '!'``). Names shorter than
        :data:`MIN_LEI_NEEDLE_LENGTH` are skipped (they match too broadly).

        An exact ``legal_name`` match is preferred; otherwise the shortest
        matching ``legal_name`` wins (closest to the needle), then the lowest
        ``lei`` for determinism. Names with no match are absent from the result.

        Parameters
        ----------
        company_names:
            Company names to resolve. An empty list yields an empty map.

        Returns
        -------
        dict[str, str]
            Mapping of input company name to its resolved ``lei`` code.
        """
        if not company_names:
            return {}

        sql = text("""
            WITH targets AS (
                SELECT company_name,
                       upper(company_name) AS needle,
                       '%' || replace(replace(replace(
                           upper(company_name), '!', '!!'), '%', '!%'), '_', '!_'
                       ) || '%' AS pattern
                FROM unnest(CAST(:company_names AS text[])) AS company_name
            )
            SELECT DISTINCT ON (targets.company_name)
                   targets.company_name AS company_name,
                   corp_lei.lei AS lei
            FROM targets
            JOIN celex_facts.corp_lei
              ON corp_lei.legal_name LIKE targets.pattern ESCAPE '!'
            WHERE length(targets.needle) >= :min_needle_length
            ORDER BY targets.company_name,
                     (corp_lei.legal_name = targets.needle) DESC,
                     length(corp_lei.legal_name),
                     corp_lei.lei
        """)
        with self.engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    "company_names": company_names,
                    "min_needle_length": MIN_LEI_NEEDLE_LENGTH,
                },
            )
            return {row.company_name: row.lei for row in rows}

    def set_handelsregister(self, assignments: list[tuple[object, str]]) -> int:
        """Set ``fk_handelsregister`` on roster rows.

        Parameters
        ----------
        assignments:
            ``(id, fk_handelsregister)`` pairs, where ``id`` is a roster row
            UUID. An empty list is a no-op. All rows are written in a single
            transaction.

        Returns
        -------
        int
            The number of rows written.
        """
        return self._set_fk_column("fk_handelsregister", assignments)

    def set_lei(self, assignments: list[tuple[object, str]]) -> int:
        """Set ``fk_lei`` on roster rows.

        Parameters
        ----------
        assignments:
            ``(id, fk_lei)`` pairs, where ``id`` is a roster row UUID. An empty
            list is a no-op. All rows are written in a single transaction.

        Returns
        -------
        int
            The number of rows written.
        """
        return self._set_fk_column("fk_lei", assignments)

    def set_opencorp_idx(self, assignments: list[tuple[object, str]]) -> int:
        """Set ``fk_opencorp_idx`` on roster rows.

        Used to mark rows after an OpenCorporates pass: ``'LINKED'`` when the
        company resolved to one or more OpenCorporates hits, ``'NA'`` when the
        lookup found nothing (mirroring the ``fk_lei`` / ``fk_handelsregister``
        ``'NA'`` convention). Marking makes the row leave
        ``company_investigated2lei2corpidx2hr_vw`` so re-runs skip it.

        Parameters
        ----------
        assignments:
            ``(id, fk_opencorp_idx)`` pairs, where ``id`` is a roster row UUID.
            An empty list is a no-op. All rows are written in a single
            transaction.

        Returns
        -------
        int
            The number of rows written.
        """
        return self._set_fk_column("fk_opencorp_idx", assignments)

    # Enrichment columns that _set_fk_column is allowed to write. The column
    # name is interpolated into SQL, so it must never come from outside this set.
    _FK_COLUMNS = frozenset(
        {
            "fk_handelsregister",
            "fk_lei",
            "fk_opencorp_idx",
            "jurisdication_code",
        }
    )

    def _set_fk_column(
        self, column: str, assignments: list[tuple[object, str]]
    ) -> int:
        """Write ``column`` on roster rows from ``(id, value)`` pairs.

        Parameters
        ----------
        column:
            Target column name; must be a member of :attr:`_FK_COLUMNS`.
        assignments:
            ``(id, value)`` pairs. An empty list is a no-op.

        Returns
        -------
        int
            The number of rows written.

        Raises
        ------
        ValueError
            If ``column`` is not a whitelisted enrichment FK column.
        """
        if column not in self._FK_COLUMNS:
            raise ValueError(f"Refusing to update non-whitelisted column: {column!r}")
        if not assignments:
            return 0

        sql = text(
            f"UPDATE celex_facts.company_investigated2celex_id "
            f"SET {column} = :value WHERE id = :id"
        )
        params = [{"id": roster_id, "value": value} for roster_id, value in assignments]
        with self.engine.begin() as conn:
            conn.execute(sql, params)
        return len(params)
