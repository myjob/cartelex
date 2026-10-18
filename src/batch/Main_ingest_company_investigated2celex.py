"""
Batch ETL: flatten ``companies_investigated`` facts into the
``celex_facts.company_investigated2celex_id`` roster table.

For each CELEX document this reads the stored ``companies_investigated`` fact,
parses its ``value_json`` (a JSON array whose first key per element carries the
company name), and inserts one row per distinct company. It is a pure ETL: no
LLM or DSPy pipeline is involved. The ``BatchJobRunner`` driver is reused so the
script accepts the standard CELEX-selection CLI (``--filter-celex-id``,
``--celex-list-file``, ``--limit``, ``--dry-run``).

Company names arrive spelled inconsistently, and two steps here absorb that.
Some ``companies_investigated`` names carry the registered address concatenated
onto the name ("Praoil, Strada 2, ... Italy"); the address suffix is stripped
before storage so the roster holds a name a commercial register can match. And
the enrichment facts are produced by pipelines that name the same company
differently, so enrichment is joined through a normalizing cascade rather than
by exact string -- see :func:`derive_clean_company_name` and
:func:`match_enrichment`.

Re-runs reconcile rather than skip: the current fact is authoritative, and the
stored rows are diffed against it by company name, so a corrected or re-extracted
fact takes effect. Names that survive the edit keep their row -- and with it the
row id every foreign link and every already-resolved enrichment column hang off
-- while new names are inserted and dropped names are deleted, cascading their
OpenCorporates links. Over an unchanged fact this is a no-op, so re-runs stay
idempotent. See :meth:`CompanyInvestigatedIngestor.process_celex`.
"""

import json
import logging
import re
import unicodedata
from collections import Counter

from batch.BatchJobRunner import BatchJobRunner
from pipelines.corp_index.jurisdiction_resolver import (
    GleifSubdivision,
    JurisdictionResolver,
    fold_match_key,
)
from pipelines.rag.pgvector.company_investigated_repository import (
    CompanyInvestigatedRepository,
    InvestigatedCompany,
)
from pipelines.rag.pgvector.fact_repository import FactRepository

logger = logging.getLogger(__name__)

FACT_NAME = "companies_investigated"
ADDRESS_FACT_NAME = "companies_address"
COUNTRY_FACT_NAME = "companies_country"

# Enrichment placeholder emitted by the extractor for a missing detail; treated
# as "no value" so the column is left NULL rather than storing the literal "NA".
MISSING_VALUE = "NA"

# Country whose roster rows are resolved against the German commercial register
# (Handelsregister); iso_country_code stored on the roster is ISO 3166-1.
HANDELSREGISTER_COUNTRY = "DE"

# Trailing separators left behind once an address suffix is stripped off a
# company name ("Praoil, Strada 2, ... Italy" -> "Praoil," -> "Praoil").
_NAME_TAIL_SEPARATORS = " ,;"

# One trailing parenthetical alias, as fact_context appends to a company name in
# the companies_country fact ("Toyoda Gosei Co., Ltd. (TOYODA GOSEI)"). Nesting
# is impossible by construction: the inner class excludes both parentheses.
_ALIAS_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")


def extract_company_names(value_json: str | None) -> list[str]:
    """Parse a ``companies_investigated`` ``value_json`` into company names.

    The stored value is a JSON array of dicts; the company name is the first
    key's value of each dict (``next(iter(row.values()))``), matching the
    roster convention in ``universal_modules._load_roster_entities``. The key
    itself varies between ``"company"`` and ``"companies"``; any secondary keys
    (e.g. ``location``, ``reference``) are ignored. JSON ``\\uXXXX`` escapes are
    decoded by ``json.loads`` into readable unicode; names are NFKC-normalized
    and stripped. Blank names are dropped and duplicates within the document are
    collapsed, preserving first-seen order.

    Parameters
    ----------
    value_json:
        The raw ``value_json`` string, or None.

    Returns
    -------
    list[str]
        Distinct, cleaned company names in first-seen order.
    """
    if not value_json or not value_json.strip():
        return []

    try:
        rows = json.loads(value_json)
    except (ValueError, TypeError):
        logger.warning("Could not parse %s value_json: %r", FACT_NAME, value_json[:200])
        return []

    if not isinstance(rows, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row:
            continue
        raw_name = next(iter(row.values()))
        name = unicodedata.normalize("NFKC", str(raw_name or "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def extract_enrichment_map(value_json: str | None) -> dict[str, str]:
    """Parse an enrichment fact ``value_json`` into a company-name lookup.

    Enrichment facts (``companies_address``, ``companies_country``) store a JSON
    array of two-key dicts. The FIRST value is the company name (matching the
    ``companies_investigated`` roster convention) and the SECOND value is the
    enrichment payload (address or ISO country code). Key names vary across facts
    (``company`` vs ``companies_investigated``), so both are read positionally.

    Names are NFKC-normalized and stripped to match the roster keys. Blank names,
    blank values, and the ``NA`` missing-value placeholder are dropped. On
    duplicate names the first non-blank value wins.

    Parameters
    ----------
    value_json:
        The raw ``value_json`` string, or None.

    Returns
    -------
    dict[str, str]
        Mapping of company name to enrichment value.
    """
    if not value_json or not value_json.strip():
        return {}

    try:
        rows = json.loads(value_json)
    except (ValueError, TypeError):
        logger.warning("Could not parse enrichment value_json: %r", value_json[:200])
        return {}

    if not isinstance(rows, list):
        return {}

    enrichment: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or len(row) < 2:
            continue
        values = list(row.values())
        name = unicodedata.normalize("NFKC", str(values[0] or "")).strip()
        value = unicodedata.normalize("NFKC", str(values[1] or "")).strip()
        if not name or not value or value == MISSING_VALUE:
            continue
        enrichment.setdefault(name, value)
    return enrichment


def derive_clean_company_name(company_name: str, address: str | None) -> str:
    """Strip a trailing registered address off a company name.

    ``companies_investigated`` sometimes stores the name concatenated with the
    address ("Praoil, Strada 2, Pal. F7, 20090 Assago, Milan, Italy"). Such a
    name matches no commercial register and does not join to enrichment facts
    that emit the bare name, so the address suffix is removed here.

    The removal is deterministic, never a guess at where a name ends: the
    address is stripped only when the ``companies_address`` fact says it is
    exactly the tail of the name.

    Parameters
    ----------
    company_name:
        The verbatim name from ``companies_investigated``.
    address:
        The address matched to that name, or None when unknown.

    Returns
    -------
    str
        The name without its address suffix, or ``company_name`` unchanged when
        the address is unknown, is not a proper suffix, or is the entire name.
    """
    if not address or company_name == address:
        return company_name
    if not company_name.endswith(address):
        return company_name
    stripped = company_name[: -len(address)].rstrip(_NAME_TAIL_SEPARATORS)
    # A name that is nothing but its address carries no company at all; keeping
    # the original is less wrong than storing an empty string.
    return stripped or company_name


def strip_alias_suffix(company_name: str) -> str:
    """Drop one trailing parenthetical alias off a company name.

    ``companies_country`` is emitted by ``fact_context``, which names a company
    more fully than the roster does by appending the short form the decision
    uses ("Toyoda Gosei Co., Ltd. (TOYODA GOSEI)"). Removing that tail turns the
    enrichment key back into the bare legal name the roster holds.

    Only a trailing parenthetical is removed, and only one: an interior
    parenthetical is part of the name ("Air America (Air Specialities Corp.)"
    is left alone only when nothing follows it) and nested parentheses do not
    occur in this fact.

    Parameters
    ----------
    company_name:
        The enrichment key as the fact spells it.

    Returns
    -------
    str
        The name without its trailing alias, stripped of surrounding
        whitespace; unchanged when there is no trailing parenthetical.
    """
    return _ALIAS_SUFFIX.sub("", company_name).strip()


def build_enrichment_index(enrichment: dict[str, str]) -> dict[str, str]:
    """Index an enrichment map by the folded form of its company names.

    Each key is registered twice: as folded verbatim, and as folded with its
    trailing alias removed (see :func:`strip_alias_suffix`), so that a key
    naming the company more fully than the roster does still resolves. Both
    variants carry the same value, so the extra entry can only add a hit -- it
    cannot make one source key disagree with itself.

    Built once per CELEX document and reused for every company in it, so the
    fold is not recomputed per lookup. On two names that fold to the same key the
    first wins, matching the "first value wins" rule of
    :func:`extract_enrichment_map`.

    Parameters
    ----------
    enrichment:
        Company name to enrichment value, as returned by
        :func:`extract_enrichment_map`.

    Returns
    -------
    dict[str, str]
        Folded company name to enrichment value. Names that fold to an empty
        key (pure punctuation) are dropped.
    """
    index: dict[str, str] = {}
    for name, value in enrichment.items():
        for variant in (name, strip_alias_suffix(name)):
            folded = fold_match_key(variant)
            if folded:
                index.setdefault(folded, value)
    return index


def match_enrichment(
    enrichment: dict[str, str],
    folded_index: dict[str, str],
    *candidate_names: str,
) -> str | None:
    """Look an enrichment value up by company name, tolerating name variance.

    Enrichment facts are produced by different pipelines that do not agree on how
    a company is named: ``detail_retriever`` echoes the roster entity verbatim,
    while ``fact_context`` lets the model re-emit a cleaned name. An exact-string
    lookup therefore silently drops correctly extracted values, so the match
    falls back through progressively looser rules:

    1. verbatim key,
    2. :func:`~pipelines.corp_index.jurisdiction_resolver.fold_match_key` exact
       (case, accent and punctuation insensitive) -- against an index that also
       carries each key with its trailing alias stripped, so
       "Toyoda Gosei Co., Ltd. (TOYODA GOSEI)" is reachable by the bare name,
    3. folded prefix -- the enrichment name is a leading whole-word run of the
       candidate ("Praoil" matches "Praoil, Strada 2, ... Italy"),
    4. folded reverse prefix -- the candidate is a leading whole-word run of the
       enrichment name ("Autoliv, Inc." matches
       "Autoliv, Inc. and Autoliv Japan Ltd. (AUTOLIV)"), for the keys that name
       the company more fully than the roster does in a shape rule 2 cannot
       normalize away.

    Both prefix rules resolve only when every matching key agrees on one value; a
    genuine ambiguity returns None rather than a guess. They run last and in this
    order, tighter before looser.

    Parameters
    ----------
    enrichment:
        Company name to enrichment value.
    folded_index:
        The same map indexed by folded name, from :func:`build_enrichment_index`.
    *candidate_names:
        Names to try, most trustworthy first (e.g. the cleaned name, then the
        verbatim roster name).

    Returns
    -------
    str | None
        The enrichment value, or None when no candidate resolves unambiguously.
    """
    for name in candidate_names:
        if name in enrichment:
            return enrichment[name]

    folded_candidates = [
        folded for folded in map(fold_match_key, candidate_names) if folded
    ]
    for folded in folded_candidates:
        if folded in folded_index:
            return folded_index[folded]

    for folded in folded_candidates:
        values = {
            value
            for indexed, value in folded_index.items()
            if folded == indexed or folded.startswith(f"{indexed} ")
        }
        if len(values) == 1:
            return values.pop()

    # The enrichment name can instead be the longer of the two: fact_context
    # re-emits "Autoliv, Inc. and Autoliv Japan Ltd. (AUTOLIV)" for a roster row
    # named "Autoliv, Inc.". Same single-value guard -- a run of keys that
    # extend the candidate but disagree on the value returns None.
    for folded in folded_candidates:
        values = {
            value
            for indexed, value in folded_index.items()
            if indexed.startswith(f"{folded} ")
        }
        if len(values) == 1:
            return values.pop()
    return None


class CompanyInvestigatedIngestor:
    """Reads ``companies_investigated`` facts and loads the roster table.

    Repositories are injected (Provisioner pattern) so the ingestor is testable
    with mocks at the DB boundary.

    Parameters
    ----------
    fact_repo:
        Source of ``companies_investigated`` facts.
    company_repo:
        Sink for the flattened company roster.
    """

    def __init__(
        self,
        fact_repo: FactRepository,
        company_repo: CompanyInvestigatedRepository,
    ) -> None:
        self.fact_repo = fact_repo
        self.company_repo = company_repo

    def process_celex(
        self, celex_id: str, context=None, dry_run: bool = False
    ) -> dict:
        """Reconcile the investigated-company roster for a single CELEX document.

        The current ``companies_investigated`` fact is authoritative, so a
        re-extracted or hand-corrected fact takes effect instead of being
        skipped: the stored rows are diffed against it by company name.

        - A name the fact still carries **keeps its existing row**. The row id
          is never rewritten, so every foreign link into it -- the OpenCorporates
          links in ``company_investigated_celex_id2opencorp_index_company`` -- and
          every enrichment column already resolved (``fk_lei``,
          ``fk_opencorp_idx``, ``fk_handelsregister``, ``jurisdication_code``)
          survive untouched. Only its NULL ``address`` / ``iso_country_code``
          columns are backfilled.
        - A name the fact newly carries is inserted, with NULL enrichment for
          the downstream stages to resolve.
        - A name the fact no longer carries is **deleted**, discarding that
          row's enrichment and cascading its OpenCorporates links away.

        Re-runs stay idempotent: over an unchanged fact there is nothing to
        insert or delete. A fact that yields no names at all is treated as
        ``empty`` and leaves the stored rows alone, so a failed extraction
        cannot silently empty a roster.

        Args:
            celex_id: CELEX identifier of the document to process.
            context: Unused; retained for :class:`BatchJobRunner` compatibility.
            dry_run: When True, no rows are written; the plan is logged instead.

        Returns:
            A per-CELEX result dict with a ``status`` field, one of ``no_fact``,
            ``empty``, ``dry_run`` or ``reconciled``. The ``dry_run`` and
            ``reconciled`` results also carry the ``inserted``, ``deleted``,
            ``kept`` and ``links_discarded`` counts.
        """
        del context  # state lives on self

        facts = self.fact_repo.select_fact4celex(celex_id, FACT_NAME)
        if not facts:
            return {"status": "no_fact", "celex_id": celex_id}

        names = extract_company_names(facts[-1].value_json)
        if not names:
            return {"status": "empty", "celex_id": celex_id}

        # The cleaned names are what the table holds, so the diff must run over
        # them -- not the verbatim fact names, which differ wherever an address
        # was stripped. _resolve_clean_names guarantees they are distinct.
        companies = self._build_roster(celex_id, names)
        stored_names = self.company_repo.select_company_names(celex_id)
        desired_names = {company.company_name for company in companies}

        to_insert = [c for c in companies if c.company_name not in stored_names]
        to_backfill = [c for c in companies if c.company_name in stored_names]
        to_delete = sorted(stored_names - desired_names)
        links_discarded = self.company_repo.count_opencorp_links(celex_id, to_delete)

        counts = {
            "inserted": len(to_insert),
            "deleted": len(to_delete),
            "kept": len(to_backfill),
            "links_discarded": links_discarded,
        }

        if dry_run:
            logger.info(
                "[DRY RUN] %s: would insert %d, delete %d (discarding %d "
                "OpenCorporates link(s)), keep %d company row(s). "
                "Insert: %s. Delete: %s.",
                celex_id,
                len(to_insert),
                len(to_delete),
                links_discarded,
                len(to_backfill),
                [company.company_name for company in to_insert],
                to_delete,
            )
            return {"status": "dry_run", "celex_id": celex_id, **counts}

        # Retire first: a name the fact dropped must leave before its
        # replacement is inserted, and its links cascade with it.
        deleted = self.company_repo.delete_companies(celex_id, to_delete)
        if deleted:
            logger.warning(
                "Retired %d roster row(s) for %s no longer named by the fact, "
                "discarding %d OpenCorporates link(s): %s",
                deleted,
                celex_id,
                links_discarded,
                to_delete,
            )

        inserted = self.company_repo.insert_companies(celex_id, to_insert)
        # Surviving rows keep whatever enrichment they already hold; only their
        # NULL address / iso_country_code columns are filled from the facts.
        backfilled = self.company_repo.update_missing_enrichment(celex_id, to_backfill)
        logger.info(
            "Reconciled %s: %d inserted, %d deleted, %d kept (%d backfilled).",
            celex_id,
            inserted,
            deleted,
            len(to_backfill),
            backfilled,
        )
        return {
            "status": "reconciled",
            "celex_id": celex_id,
            **counts,
            "backfilled": backfilled,
        }

    def _build_roster(
        self, celex_id: str, names: list[str]
    ) -> list[InvestigatedCompany]:
        """Assemble enriched roster records for a CELEX document.

        The ``companies_investigated`` names are authoritative as to *which*
        companies exist, but not always as to how they are spelled: some are
        stored with the registered address concatenated onto the name. Each name
        is therefore cleaned of its address suffix (see
        :func:`derive_clean_company_name`) before being stored, so the roster
        holds a name a commercial register can actually match.

        Enrichment is then matched per company through :func:`match_enrichment`,
        which tolerates the naming disagreement between the pipelines that
        produce the two facts -- ``companies_address`` (``detail_retriever``,
        echoes the verbatim roster entity) and ``companies_country``
        (``fact_context``, re-emits a cleaned name). An unmatched or missing
        enrichment stays None (NULL). Enrichment facts are optional: a CELEX
        without them still yields one record per company.

        Args:
            celex_id: CELEX identifier of the document being processed.
            names: Authoritative company names from ``companies_investigated``.

        Returns:
            One :class:`InvestigatedCompany` per name, in roster order.
        """
        address_facts = self.fact_repo.select_fact4celex(celex_id, ADDRESS_FACT_NAME)
        country_facts = self.fact_repo.select_fact4celex(celex_id, COUNTRY_FACT_NAME)
        address_map = (
            extract_enrichment_map(address_facts[-1].value_json)
            if address_facts
            else {}
        )
        country_map = (
            extract_enrichment_map(country_facts[-1].value_json)
            if country_facts
            else {}
        )
        address_index = build_enrichment_index(address_map)
        country_index = build_enrichment_index(country_map)

        # Addresses are matched against the verbatim roster name, which is the
        # key detail_retriever echoed back, and are what the name cleaning needs.
        addresses = [
            match_enrichment(address_map, address_index, name) for name in names
        ]
        clean_names = self._resolve_clean_names(celex_id, names, addresses)
        return [
            InvestigatedCompany(
                company_name=clean_name,
                address=address,
                # The cleaned name is tried first because companies_country is
                # keyed by the bare name; the verbatim name is the fallback.
                iso_country_code=match_enrichment(
                    country_map, country_index, clean_name, name
                ),
            )
            for name, clean_name, address in zip(
                names, clean_names, addresses, strict=True
            )
        ]

    @staticmethod
    def _resolve_clean_names(
        celex_id: str, names: list[str], addresses: list[str | None]
    ) -> list[str]:
        """Strip address suffixes while keeping names distinct in the document.

        ``company_investigated2celex_id`` carries UNIQUE (company_name,
        celex_id), so two names that clean to the same string would collide on
        insert -- which happens when one company is listed once per site, each
        entry naming a different plant address. Such names keep their original
        (address-bearing) form so both rows survive and neither site is lost;
        every other name is cleaned.

        Args:
            celex_id: CELEX identifier, for the collision log line.
            names: Verbatim company names, in roster order.
            addresses: The address matched to each name, positionally aligned.

        Returns:
            One name per input, cleaned where that stays unambiguous.
        """
        candidates = [
            derive_clean_company_name(name, address)
            for name, address in zip(names, addresses, strict=True)
        ]
        candidate_counts = Counter(candidates)

        resolved: list[str] = []
        for name, candidate in zip(names, candidates, strict=True):
            if candidate != name and candidate_counts[candidate] > 1:
                logger.warning(
                    "Keeping the address-bearing name %r for %s: cleaning it to "
                    "%r would collide with another company in this document.",
                    name,
                    celex_id,
                    candidate,
                )
                resolved.append(name)
            else:
                resolved.append(candidate)
        return resolved

    def enrich_handelsregister(
        self,
        iso_country_code: str = HANDELSREGISTER_COUNTRY,
        dry_run: bool = False,
    ) -> dict:
        """Resolve ``fk_handelsregister`` for a country's roster rows.

        Selects every roster row for ``iso_country_code``, resolves the distinct
        company names against ``celex_facts.handelsregister_companies`` by exact
        name in a single set-based query, and writes the register
        ``company_number`` into ``fk_handelsregister`` -- or the ``NA``
        placeholder when the name is not found. This is a bulk, cross-CELEX pass
        (not per-CELEX) and overwrites any existing ``fk_handelsregister`` so
        re-runs pick up newly added register entries.

        Args:
            iso_country_code: ISO 3166-1 country code to enrich (default ``DE``).
            dry_run: When True, resolve and report but write nothing.

        Returns:
            A result dict with ``status`` (``no_rows``, ``dry_run`` or
            ``enriched``) plus ``rows``, ``matched`` and ``unmatched`` counts.
        """
        roster_rows = self.company_repo.select_roster_by_country(iso_country_code)
        if not roster_rows:
            return {"status": "no_rows", "country": iso_country_code, "rows": 0}

        distinct_names = list({company_name for _, company_name in roster_rows})
        number_by_name = self.company_repo.resolve_handelsregister_numbers(
            distinct_names
        )
        assignments: list[tuple[object, str]] = [
            (roster_id, number_by_name.get(company_name, MISSING_VALUE))
            for roster_id, company_name in roster_rows
        ]

        matched = sum(1 for _, value in assignments if value != MISSING_VALUE)
        unmatched = len(assignments) - matched

        if dry_run:
            logger.info(
                "[DRY RUN] Would set fk_handelsregister on %d %s row(s): "
                "%d matched, %d NA.",
                len(assignments),
                iso_country_code,
                matched,
                unmatched,
            )
            return {
                "status": "dry_run",
                "country": iso_country_code,
                "rows": len(assignments),
                "matched": matched,
                "unmatched": unmatched,
            }

        updated = self.company_repo.set_handelsregister(assignments)
        logger.info(
            "Set fk_handelsregister on %d %s row(s): %d matched, %d NA.",
            updated,
            iso_country_code,
            matched,
            unmatched,
        )
        return {
            "status": "enriched",
            "country": iso_country_code,
            "rows": updated,
            "matched": matched,
            "unmatched": unmatched,
        }

    def enrich_lei(self, dry_run: bool = False) -> dict:
        """Resolve ``fk_lei`` for the whole roster from GLEIF.

        Selects every roster row (LEI is a global identifier, so unlike
        Handelsregister this is not country-scoped), resolves the distinct
        company names against ``celex_facts.corp_lei`` by uppercase substring
        match in a single set-based query, and writes the ``lei`` code into
        ``fk_lei`` -- or the ``NA`` placeholder when the name does not resolve.
        This is a bulk, cross-CELEX pass that overwrites any existing ``fk_lei``
        so re-runs pick up register growth.

        Args:
            dry_run: When True, resolve and report but write nothing.

        Returns:
            A result dict with ``status`` (``no_rows``, ``dry_run`` or
            ``enriched``) plus ``rows``, ``matched`` and ``unmatched`` counts.
        """
        roster_rows = self.company_repo.select_all_roster()
        if not roster_rows:
            return {"status": "no_rows", "rows": 0}

        distinct_names = list({company_name for _, company_name in roster_rows})
        lei_by_name = self.company_repo.resolve_lei_codes(distinct_names)
        assignments: list[tuple[object, str]] = [
            (roster_id, lei_by_name.get(company_name, MISSING_VALUE))
            for roster_id, company_name in roster_rows
        ]

        matched = sum(1 for _, value in assignments if value != MISSING_VALUE)
        unmatched = len(assignments) - matched

        if dry_run:
            logger.info(
                "[DRY RUN] Would set fk_lei on %d row(s): %d matched, %d NA.",
                len(assignments),
                matched,
                unmatched,
            )
            return {
                "status": "dry_run",
                "rows": len(assignments),
                "matched": matched,
                "unmatched": unmatched,
            }

        updated = self.company_repo.set_lei(assignments)
        logger.info(
            "Set fk_lei on %d row(s): %d matched, %d NA.", updated, matched, unmatched
        )
        return {
            "status": "enriched",
            "rows": updated,
            "matched": matched,
            "unmatched": unmatched,
        }


    def enrich_jurisdiction(self, dry_run: bool = False) -> dict:
        """Resolve ``jurisdication_code`` for the whole roster.

        Selects every roster row with its address, country and (where one was
        matched) the legal jurisdiction of its GLEIF LEI record, then resolves a
        canonical ISO 3166-2 jurisdiction per row via
        :class:`~pipelines.corp_index.jurisdiction_resolver.JurisdictionResolver`.
        This is a bulk, cross-CELEX pass that overwrites any existing value, so
        re-runs pick up newly backfilled addresses and LEI matches.

        Unlike the ``fk_*`` stages there is no ``NA`` placeholder: a row whose
        jurisdiction cannot be determined is written as NULL, so a consumer can
        distinguish "unknown" from "the country is the jurisdiction" and decline
        to search rather than record a false negative.

        Args:
            dry_run: When True, resolve and report but write nothing.

        Returns:
            A result dict with ``status`` (``no_rows``, ``dry_run`` or
            ``enriched``) plus ``rows``, ``matched`` (a jurisdiction was
            resolved), ``unmatched`` (left NULL) and ``subdivisions`` (resolved
            to a subdivision rather than a bare country) counts.
        """
        roster_rows = self.company_repo.select_jurisdiction_inputs()
        if not roster_rows:
            return {"status": "no_rows", "rows": 0}

        resolver = JurisdictionResolver(
            [
                GleifSubdivision(
                    jurisdiction_code=code,
                    jurisdiction_name=name,
                    iso_country_code=country,
                )
                for code, name, country in self.company_repo.select_gleif_subdivisions()
            ]
        )
        assignments: list[tuple[object, str | None]] = [
            (
                roster_id,
                resolver.resolve(iso_country_code, address, lei_jurisdiction),
            )
            for roster_id, iso_country_code, address, lei_jurisdiction in roster_rows
        ]

        matched = sum(1 for _, code in assignments if code is not None)
        subdivisions = sum(1 for _, code in assignments if code and "-" in code)
        unmatched = len(assignments) - matched
        summary = {
            "rows": len(assignments),
            "matched": matched,
            "unmatched": unmatched,
            "subdivisions": subdivisions,
        }

        if dry_run:
            logger.info(
                "[DRY RUN] Would set jurisdication_code on %d row(s): %d resolved "
                "(%d to a subdivision), %d left NULL.",
                len(assignments),
                matched,
                subdivisions,
                unmatched,
            )
            return {"status": "dry_run", **summary}

        updated = self.company_repo.set_jurisdiction_code(assignments)
        logger.info(
            "Set jurisdication_code on %d row(s): %d resolved (%d to a "
            "subdivision), %d left NULL.",
            updated,
            matched,
            subdivisions,
            unmatched,
        )
        return {"status": "enriched", **summary}


def main() -> None:
    """Entry point: run the full investigated-company pipeline.

    Every invocation runs all stages in order, honouring the standard CLI
    (including ``--dry-run``):

    1. Per-CELEX: load / backfill the roster from the company facts
       (``process_celex`` over the selected CELEX set).
    2. Bulk, cross-CELEX: resolve ``fk_handelsregister`` for the German roster
       rows (``enrich_handelsregister``).
    3. Bulk, cross-CELEX: resolve ``fk_lei`` for the whole roster from GLEIF
       (``enrich_lei``).
    4. Bulk, cross-CELEX: resolve ``jurisdication_code`` from each row's address,
       LEI and country (``enrich_jurisdiction``). Runs last because it reads the
       ``fk_lei`` written by stage 3 as a secondary signal.

    Further bulk finalisation steps are appended to ``post_stages`` as they are
    added, so ``main`` always runs everything.
    """
    runner = BatchJobRunner(
        description=(
            "Load companies_investigated facts into "
            "celex_facts.company_investigated2celex_id, then run bulk enrichment."
        )
    )
    ingestor = CompanyInvestigatedIngestor(
        fact_repo=FactRepository(),
        company_repo=CompanyInvestigatedRepository(),
    )

    def enrich_handelsregister_stage(args) -> None:
        """Bulk post-stage: resolve fk_handelsregister for DE roster rows."""
        result = ingestor.enrich_handelsregister(dry_run=args.dry_run)
        logger.info("Handelsregister enrichment result: %s", result)

    def enrich_lei_stage(args) -> None:
        """Bulk post-stage: resolve fk_lei for the whole roster from GLEIF."""
        result = ingestor.enrich_lei(dry_run=args.dry_run)
        logger.info("LEI enrichment result: %s", result)

    def enrich_jurisdiction_stage(args) -> None:
        """Bulk post-stage: resolve jurisdication_code for the whole roster."""
        result = ingestor.enrich_jurisdiction(dry_run=args.dry_run)
        logger.info("Jurisdiction enrichment result: %s", result)

    runner.run(
        ingestor.process_celex,
        post_stages=[
            enrich_handelsregister_stage,
            enrich_lei_stage,
            enrich_jurisdiction_stage,
        ],
    )


if __name__ == "__main__":
    main()


"""
# Full pipeline (roster load + bulk enrichment). All stages honour --dry-run.

# Dry run on a single known CELEX (no writes; enrichment reports counts only):
PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_company_investigated2celex.py \
    --filter-celex-id CELEX-31964D0599 \
    --limit 1 \
    --dry-run

# Curated set:
PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_company_investigated2celex.py \
    --celex-list-file /path/to/celex_ids.txt

# Full run over the master sheet (celexes without the fact return no_fact),
# followed by the fk_handelsregister enrichment over all DE roster rows:
PYTHONPATH=src .venv/bin/python src/batch/Main_ingest_company_investigated2celex.py
"""
