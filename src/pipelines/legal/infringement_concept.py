"""Canonical EU-infringement-concept normalization across treaty renumberings.

The EU competition-law articles were renumbered twice: the EEC Treaty became the
EC Treaty at Amsterdam (in force 1999) and the EC Treaty became the TFEU at
Lisbon (in force end of 2009). The same legal concept therefore carries three
different article numbers depending on the decision's treaty era:

==================================  =====  =====  ======
concept                              EEC     EC    TFEU
==================================  =====  =====  ======
anticompetitive agreement / cartel    85     81     101
abuse of dominance                     86     82     102
public undertaking / member state      90     86     106
==================================  =====  =====  ======

Only ``Article 86`` is ambiguous: it denotes abuse of dominance under the EEC
Treaty but public undertakings under the EC Treaty. Every other token maps to a
single concept without consulting the date, so the era is needed *only* to
disambiguate that one token.

Era resolution is a lookup over :class:`ProvisionMap` — an immutable view of
the provision ontology (``provision``, ``treaty``, validity window,
``canonical_concept``) — rather than inline date arithmetic. The ontology is
also materialized as ``celex_facts.legal_provisions``
(``migrate_legal_provisions.sql``) for SQL-side joins;
``pipelines.rag.pgvector.provision_repository.load_provision_map`` loads it
once per process. This module ships :data:`DEFAULT_PROVISION_MAP`, built from
the same rows, so it stays pure (no DB, no LLM): every function is
deterministic and unit-testable in isolation, and the same mapping backs the
ingestion module, the rule-engine condition function, and the book
cross-check.

The treaty era is taken from the decision date when available, and otherwise
falls back to the year embedded in the CELEX id (``CELEX-31990D0645`` -> 1990;
see the ``project_celex_id_storage_format`` memory). Only when neither is known
does ``Article 86`` stay ambiguous and emit both candidate concepts.
"""

import logging
import re
from datetime import date
from enum import StrEnum
from typing import Iterable, NamedTuple

from config.settings import settings

logger = logging.getLogger(__name__)


class InfringementConcept(StrEnum):
    """Treaty-era-independent EU competition infringement concept."""

    ANTICOMPETITIVE_AGREEMENT = "anticompetitive_agreement"  # 85 / 81 / 101
    ABUSE_OF_DOMINANCE = "abuse_of_dominance"  # 86 (EEC) / 82 / 102
    PUBLIC_UNDERTAKING = "public_undertaking"  # 90 / 86 (EC) / 106


class TreatyEra(StrEnum):
    """Which treaty's article numbering was in force for a decision."""

    EEC = "eec"  # before Amsterdam
    EC = "ec"  # Amsterdam .. Lisbon
    TFEU = "tfeu"  # after Lisbon
    EEA = "eea"  # EEA Agreement (Articles 53/54); data-only, excluded from
    # runtime concept resolution — see provision_repository.RUNTIME_TREATIES


class ProvisionRow(NamedTuple):
    """One provision of the legal-provision ontology.

    Mirrors a row of ``celex_facts.legal_provisions`` restricted to the
    treaty lineage used at runtime (eec/ec/tfeu).
    """

    article_number: int
    treaty: TreatyEra
    valid_from: date  # inclusive
    valid_to: date | None  # EXCLUSIVE; None = in force
    canonical_concept: InfringementConcept


class ProvisionMap:
    """Immutable lookup over the provision ontology.

    Replaces the previous hardcoded article->concept dict and the
    boundary-date arithmetic: eras derive from the rows' validity windows and
    concepts from their ``canonical_concept``.
    """

    def __init__(self, rows: Iterable[ProvisionRow]) -> None:
        """Build the lookup structures from ontology rows.

        Parameters
        ----------
        rows:
            Provision rows; each treaty's rows must share one validity window.

        Raises
        ------
        ValueError
            If *rows* is empty or a treaty has inconsistent validity windows.
        """
        self._rows = tuple(rows)
        if not self._rows:
            raise ValueError("ProvisionMap requires at least one provision row")

        self._by_article: dict[int, tuple[ProvisionRow, ...]] = {}
        windows: dict[TreatyEra, tuple[date, date | None]] = {}
        for row in self._rows:
            existing = self._by_article.get(row.article_number, ())
            self._by_article[row.article_number] = existing + (row,)
            window = (row.valid_from, row.valid_to)
            if windows.setdefault(row.treaty, window) != window:
                raise ValueError(
                    f"Inconsistent validity window for treaty {row.treaty}"
                )
        # Eras ordered chronologically by the start of their window.
        self._eras: tuple[tuple[TreatyEra, date, date | None], ...] = tuple(
            (treaty, valid_from, valid_to)
            for treaty, (valid_from, valid_to) in sorted(
                windows.items(), key=lambda item: item[1][0]
            )
        )

    @classmethod
    def from_rows(cls, rows: Iterable[ProvisionRow]) -> "ProvisionMap":
        """Build a map from ontology rows (DB or static).

        Parameters
        ----------
        rows:
            Provision rows.

        Returns
        -------
        ProvisionMap
            The immutable lookup.
        """
        return cls(rows)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProvisionMap):
            return NotImplemented
        return sorted(self._rows) == sorted(other._rows)

    def __hash__(self) -> int:
        return hash(tuple(sorted(self._rows)))

    def era_for_date(self, decision_date: date) -> TreatyEra:
        """Resolve the treaty era in force on *decision_date*.

        Dates before the first window still resolve to the earliest era (the
        numbering did not exist earlier, so an earlier date can only mean
        that era's documents).

        Parameters
        ----------
        decision_date:
            The decision date.

        Returns
        -------
        TreatyEra
            The era whose validity window covers the date.
        """
        for treaty, _valid_from, valid_to in self._eras:
            if valid_to is None or decision_date < valid_to:
                return treaty
        return self._eras[-1][0]

    def era_for_year(self, year: int) -> TreatyEra:
        """Resolve the treaty era from a bare year (CELEX-id fallback).

        A boundary year is assigned to the era covering its majority: the
        Amsterdam cut-off (April) puts 1999 into the EC era, the Lisbon
        cut-off (November) keeps 2009 in the EC era — reproducing the
        original date-arithmetic behavior.

        Parameters
        ----------
        year:
            The decision year.

        Returns
        -------
        TreatyEra
            The era for that year.
        """
        for treaty, _valid_from, valid_to in self._eras:
            if valid_to is None:
                return treaty
            if year < valid_to.year:
                return treaty
            if year == valid_to.year and valid_to.month >= 7:
                return treaty
        return self._eras[-1][0]

    def concepts_for_article(
        self, article: int, era: TreatyEra | None
    ) -> set[InfringementConcept]:
        """Resolve the concept(s) an article number denotes.

        An article carrying a single concept across all treaties is
        era-independent. An article with era-dependent concepts (Article 86)
        resolves by *era*; when the era is unknown — or no row exists for
        that era (an anachronistic reference) — all candidate concepts are
        emitted (explicit, bounded ambiguity).

        Parameters
        ----------
        article:
            The article number token.
        era:
            The treaty era, or ``None`` when unknown.

        Returns
        -------
        set[InfringementConcept]
            The denoted concepts; empty when the article is not a competition
            provision in the ontology.
        """
        rows = self._by_article.get(article, ())
        if not rows:
            return set()
        concepts = {row.canonical_concept for row in rows}
        if len(concepts) == 1 or era is None:
            return concepts
        era_concepts = {
            row.canonical_concept for row in rows if row.treaty is era
        }
        return era_concepts or concepts


# The EEC Treaty's entry into force — start of the earliest validity window.
_EEC_ENTRY_INTO_FORCE = date(1958, 1, 1)

# Treaty-era cut-offs, parsed once from settings so the ontology and the
# legacy date-coupled YAML conditions agree on the boundaries. valid_to is
# EXCLUSIVE: valid_from <= d < valid_to.
_AMSTERDAM_BOUNDARY = date.fromisoformat(settings.TREATY_BOUNDARY_AMSTERDAM)
_LISBON_BOUNDARY = date.fromisoformat(settings.TREATY_BOUNDARY_LISBON)

# The core renumbering lineage, mirroring the eec/ec/tfeu seed rows of
# celex_facts.legal_provisions (migrate_legal_provisions.sql). The EEA rows
# seeded there are data-only and deliberately absent here — including them
# would make Article 53/54 tokens newly resolve (user decision 2026-07-05).
_STATIC_PROVISION_ROWS: tuple[ProvisionRow, ...] = (
    ProvisionRow(85, TreatyEra.EEC, _EEC_ENTRY_INTO_FORCE, _AMSTERDAM_BOUNDARY,
                 InfringementConcept.ANTICOMPETITIVE_AGREEMENT),
    ProvisionRow(86, TreatyEra.EEC, _EEC_ENTRY_INTO_FORCE, _AMSTERDAM_BOUNDARY,
                 InfringementConcept.ABUSE_OF_DOMINANCE),
    ProvisionRow(90, TreatyEra.EEC, _EEC_ENTRY_INTO_FORCE, _AMSTERDAM_BOUNDARY,
                 InfringementConcept.PUBLIC_UNDERTAKING),
    ProvisionRow(81, TreatyEra.EC, _AMSTERDAM_BOUNDARY, _LISBON_BOUNDARY,
                 InfringementConcept.ANTICOMPETITIVE_AGREEMENT),
    ProvisionRow(82, TreatyEra.EC, _AMSTERDAM_BOUNDARY, _LISBON_BOUNDARY,
                 InfringementConcept.ABUSE_OF_DOMINANCE),
    ProvisionRow(86, TreatyEra.EC, _AMSTERDAM_BOUNDARY, _LISBON_BOUNDARY,
                 InfringementConcept.PUBLIC_UNDERTAKING),
    ProvisionRow(101, TreatyEra.TFEU, _LISBON_BOUNDARY, None,
                 InfringementConcept.ANTICOMPETITIVE_AGREEMENT),
    ProvisionRow(102, TreatyEra.TFEU, _LISBON_BOUNDARY, None,
                 InfringementConcept.ABUSE_OF_DOMINANCE),
    ProvisionRow(106, TreatyEra.TFEU, _LISBON_BOUNDARY, None,
                 InfringementConcept.PUBLIC_UNDERTAKING),
)

# Pure in-process default; the DB-loaded map (provision_repository) must equal
# this — an integration test guards against drift.
DEFAULT_PROVISION_MAP = ProvisionMap.from_rows(_STATIC_PROVISION_ROWS)

# An "Article N" mention (case-insensitive), capturing the number.
_ARTICLE_RE = re.compile(r"Article\s+(\d+)", re.IGNORECASE)
# A CELEX id's year: "CELEX-" + one sector digit + the four-digit year.
_CELEX_YEAR_RE = re.compile(r"CELEX-\d(\d{4})")


def parse_article_tokens(text: str | None) -> set[int]:
    """Extract the distinct article numbers mentioned in a text.

    Parameters
    ----------
    text:
        A ``law_article_all_relevant`` value such as
        ``"Article 101, Article 102"`` (or ``None`` / empty).

    Returns
    -------
    set[int]
        The distinct integers from ``Article N`` mentions; empty when none.
    """
    if not text:
        return set()
    return {int(match) for match in _ARTICLE_RE.findall(text)}


def celex_year(celex_id: str | None) -> int | None:
    """Return the four-digit year encoded in a CELEX id, or ``None``.

    CELEX ids are ``"CELEX-"`` + a sector digit + a four-digit year + a
    descriptor (e.g. ``CELEX-31990D0645`` -> 1990). Non-CELEX surfaces (the
    corpus also has ``AT_``/numeric ids) and out-of-range years yield ``None``.

    Parameters
    ----------
    celex_id:
        The document identifier as stored.

    Returns
    -------
    int | None
        The decision year when parseable and within the plausibility window,
        else ``None``.
    """
    if not celex_id:
        return None
    match = _CELEX_YEAR_RE.search(celex_id)
    if not match:
        return None
    year = int(match.group(1))
    if year < settings.CELEX_MIN_YEAR or year > settings.CELEX_MAX_YEAR:
        return None
    return year


def parse_decision_date(value: date | str | None) -> date | None:
    """Parse a stored decision-date fact value to a ``date``, or ``None``.

    Accepts a ``date``, an ISO ``YYYY-MM-DD`` string (leading 10 chars), or a
    placeholder like ``"NA"`` / ``""`` (which yields ``None``).

    Parameters
    ----------
    value:
        The ``eu_commisssion_decision_date`` fact value.

    Returns
    -------
    date | None
        The parsed date, or ``None`` when absent or non-date.
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def treaty_era(
    decision_date: date | None,
    celex_year_value: int | None,
    provision_map: ProvisionMap | None = None,
) -> TreatyEra | None:
    """Resolve the treaty era from a decision date, falling back to CELEX year.

    Parameters
    ----------
    decision_date:
        The parsed decision date, or ``None`` when unknown.
    celex_year_value:
        The year parsed from the CELEX id, used only when ``decision_date`` is
        ``None``.
    provision_map:
        Ontology to resolve against; defaults to :data:`DEFAULT_PROVISION_MAP`.

    Returns
    -------
    TreatyEra | None
        The treaty era, or ``None`` when neither signal is available.
    """
    ontology = provision_map or DEFAULT_PROVISION_MAP
    if decision_date is not None:
        return ontology.era_for_date(decision_date)
    if celex_year_value is not None:
        return ontology.era_for_year(celex_year_value)
    return None


def articles_to_concepts(
    articles: Iterable[int],
    era: TreatyEra | None,
    provision_map: ProvisionMap | None = None,
) -> set[InfringementConcept]:
    """Map article numbers to canonical concepts for a treaty era.

    Parameters
    ----------
    articles:
        Article numbers (e.g. from :func:`parse_article_tokens`).
    era:
        The treaty era, needed only to resolve the ``Article 86`` token.
    provision_map:
        Ontology to resolve against; defaults to :data:`DEFAULT_PROVISION_MAP`.

    Returns
    -------
    set[InfringementConcept]
        The concepts denoted by the articles; empty when none are competition
        provisions.
    """
    ontology = provision_map or DEFAULT_PROVISION_MAP
    concepts: set[InfringementConcept] = set()
    for article in articles:
        concepts |= ontology.concepts_for_article(article, era)
    return concepts


def concepts_for(
    article_text: str | None,
    decision_date_value: date | str | None,
    celex_id: str | None,
    provision_map: ProvisionMap | None = None,
) -> set[InfringementConcept]:
    """Resolve the canonical infringement concept(s) for a case.

    Composition used by every call site: parse the article tokens, resolve the
    treaty era (decision date, then CELEX-year fallback), and map to concepts.

    Parameters
    ----------
    article_text:
        The ``law_article_all_relevant`` value.
    decision_date_value:
        The ``eu_commisssion_decision_date`` value (date, ISO string, or
        placeholder).
    celex_id:
        The document id, used for the year fallback when the date is absent.
    provision_map:
        Ontology to resolve against; defaults to :data:`DEFAULT_PROVISION_MAP`.

    Returns
    -------
    set[InfringementConcept]
        The canonical concepts; empty when no competition article is present.
    """
    articles = parse_article_tokens(article_text)
    if not articles:
        return set()
    era = treaty_era(
        parse_decision_date(decision_date_value), celex_year(celex_id), provision_map
    )
    return articles_to_concepts(articles, era, provision_map)
