"""Cartel period inheritance for follow-up decisions (fact ``cartel_period``).

An amending or re-adopting decision (e.g. ``DEC-AT39168-20110331``, which only
corrects the fine of the 2007 Fasteners decision) does not restate the
infringement period, so :class:`HeideltimeCartelPeriodModule` finds none. The
period lives in the original decision of the same Commission case
(``39168_757_6``: ``1977-01-15 : 2003-01-13``). This module picks that donor
deterministically; no LLM is involved.

Rules (design: ``docs/Data Quest Cartel Period Inheritance.md``):

1. The target's own year comes from its id (``DEC-ATnnnnn-YYYYMMDD``,
   ``CELEX-3YYYY...``). Stored decision dates are not used: for exactly this
   population they often hold the amending decision's date (ISSUES #275).
2. The target must cite an earlier Commission decision, ``C(YYYY) NNNN`` with
   YYYY before its own year. A later, separate decision of the same case (CSV
   1976 vs 1978) cites none and inherits nothing.
3. A donor shares a 5-digit case number, is another document, has a parseable
   ``START : END`` period, is not gated False as an infringement decision, and
   its conduct ended no later than the latest cited year.
4. Choice: the donor whose id year is a cited year; else the only donor; else
   the union (earliest start, latest end) at lower trust.
"""

import json
import logging
import re
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from config.settings import settings
from pipelines.heideltime.event_dates import PartialDate, parse_partial_date

logger = logging.getLogger(__name__)

# FactOutput.source and fact_source_link.extraction_method of an inherited
# period. Facts carrying it never serve as donors: a twin follow-up processed
# earlier in the same run must not pass its borrowed value on.
INHERITANCE_METHOD = "period_inheritance"

# PostgreSQL regex for a Commission document number, capturing its year:
# 'C(2007) 4257', 'C(2009)8682 final', 'C (2013) 8512'.
CITATION_REGEX = r"C ?\((\d{4})\) ?\d{3,5}"

# Case number embedded in a celex_id: '39168_757_6', 'DEC-AT39168-20110331',
# 'AT_37990_9687627_5129_3'. CELEX-3... ids carry a year, not a case number.
_ID_CASE_NUMBER = re.compile(r"^(?:DEC-AT|AT_)?(\d{5})(?:[_-]|$)")
# Case number in a case id or title: 'IV/39168', 'AT.39914', 'COMP/E-1/39.168',
# 'COMP/C.38.238/B.2'. Exactly five digits, optionally dotted after two.
_REF_CASE_NUMBER = re.compile(r"(?<!\d)(\d{2})\.?(\d{3})(?!\d)")
# Own year of a document, from its id.
_ID_YEAR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^DEC-AT\d{5}-(\d{4})\d{4}"),
    re.compile(r"^CELEX-3(\d{4})"),
)
_PERIOD_SEPARATOR = ":"
_GATE_FALSE = "false"


class InheritanceBasis(StrEnum):
    """Why a donor was chosen; decides the lineage trust rank."""

    CITED_DECISION = "cited_decision"
    SINGLE_SIBLING = "single_sibling"
    SIBLING_UNION = "sibling_union"


class PeriodDonor(BaseModel):
    """A decision that could pass its cartel period on.

    Attributes:
        fact_id: Id of the donor's ``cartel_period`` facts row.
        celex_id: The donor decision.
        date_range: Its stored period, ``START : END``.
        gate: Its ``case_commission_infringement`` value, None when absent.
        case_numbers: Its normalised 5-digit case numbers.
    """

    model_config = ConfigDict(frozen=True)

    fact_id: UUID = Field(description="Id of the donor's cartel_period facts row")
    celex_id: str = Field(description="Donor decision id")
    date_range: str = Field(description="Donor period as 'START : END'")
    gate: str | None = Field(default=None, description="Donor's infringement gate value; None when absent")
    case_numbers: frozenset[str] = Field(description="Donor's normalised 5-digit case numbers")


class InheritedPeriod(BaseModel):
    """The period a follow-up decision borrows, with its donors.

    Attributes:
        date_range: The period, ``START : END`` at the donors' precision.
        basis: How the donor(s) were chosen.
        donors: Every donor the period was taken from (several for a union).
    """

    model_config = ConfigDict(frozen=True)

    date_range: str = Field(description="Inherited period as 'START : END'")
    basis: InheritanceBasis = Field(description="How the donor(s) were chosen")
    donors: list[PeriodDonor] = Field(description="Donors the period was taken from")


class PeriodDonorStore(Protocol):
    """Fact-side reads of the inheritance (``FactRepository``)."""

    def select_case_refs(self, celex_id: str, title_fact_name: str) -> list[str]:
        """Return the case ids and case-title values of one document."""
        ...

    def select_period_donor_rows(
        self, fact_name: str, gate_fact_name: str, title_fact_name: str, exclude_method: str
    ) -> list[dict[str, Any]]:
        """Return every non-NA period fact not itself produced by ``exclude_method``."""
        ...


class CitationStore(Protocol):
    """Chunk-side read of the inheritance (``ChunkRepository``)."""

    def select_regex_matches(self, celex_id: str, regex: str) -> list[str]:
        """Return the distinct first capture groups of ``regex`` in a document."""
        ...


def case_numbers(celex_id: str, refs: list[str]) -> frozenset[str]:
    """Return the normalised 5-digit case numbers of a document.

    Args:
        celex_id: The document id; its embedded case number counts.
        refs: Case ids (``case_celex_id.case_id``) and case-title values.
            Prefix-only junk such as ``COMP/E`` carries no number and drops out.

    Returns:
        The distinct case numbers, e.g. ``{"39168"}``; empty when none is found.
    """
    numbers: set[str] = set()
    if match := _ID_CASE_NUMBER.match(celex_id):
        numbers.add(match.group(1))
    for ref in refs:
        if isinstance(ref, str):
            numbers.update(head + tail for head, tail in _REF_CASE_NUMBER.findall(ref))
    return frozenset(numbers)


def id_year(celex_id: str) -> int | None:
    """Return the year a document id carries, or None.

    Args:
        celex_id: ``DEC-ATnnnnn-YYYYMMDD[_n]`` or ``CELEX-3YYYY...``; other id
            shapes carry no year.

    Returns:
        The four-digit year, or None.
    """
    for pattern in _ID_YEAR_PATTERNS:
        if match := pattern.match(celex_id):
            return int(match.group(1))
    return None


def cited_years(matches: list[str], before: int) -> set[int]:
    """Return the years of cited Commission decisions older than the target.

    Args:
        matches: Captured years of ``C(YYYY) NNNN`` citations in the target.
        before: The target's own year; its own document number is not a citation.

    Returns:
        Plausible cited years strictly before ``before``.
    """
    years: set[int] = set()
    for match in matches:
        if isinstance(match, str) and match.isdigit():
            year = int(match)
            if settings.HEIDELTIME_MIN_YEAR <= year < before:
                years.add(year)
    return years


def parse_period(value: str | None) -> tuple[PartialDate, PartialDate] | None:
    """Parse a stored ``START : END`` period into its two bounds.

    Args:
        value: The period text, each side ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``.

    Returns:
        ``(start, end)``, or None when either side is missing or not a date.
    """
    if not isinstance(value, str) or _PERIOD_SEPARATOR not in value:
        return None
    start_text, end_text = (side.strip() for side in value.split(_PERIOD_SEPARATOR, 1))
    start, end = parse_partial_date(start_text), parse_partial_date(end_text)
    if start is None or end is None:
        return None
    return start, end


def _is_gated_false(gate: str | None) -> bool:
    """Return True when the infringement gate explicitly says False."""
    return isinstance(gate, str) and gate.strip().casefold() == _GATE_FALSE


def _union(donors: list[PeriodDonor]) -> str:
    """Return the earliest start and latest end over donors, as stored text."""
    sides = [
        (bounds, [side.strip() for side in donor.date_range.split(_PERIOD_SEPARATOR, 1)])
        for donor in donors
        if (bounds := parse_period(donor.date_range)) is not None
    ]
    start = min(sides, key=lambda item: item[0][0].first_day)[1][0]
    end = max(sides, key=lambda item: item[0][1].last_day)[1][1]
    return f"{start} {_PERIOD_SEPARATOR} {end}"


def select_inheritance(
    target_celex_id: str,
    target_case_numbers: frozenset[str] | set[str],
    cited: set[int],
    donors: list[PeriodDonor],
) -> InheritedPeriod | None:
    """Choose the period a follow-up decision inherits, if any.

    Args:
        target_celex_id: The decision without a period of its own.
        target_case_numbers: Its normalised case numbers.
        cited: Years of the earlier decisions it cites (see :func:`cited_years`).
        donors: Candidate donors across the corpus.

    Returns:
        The inherited period, or None when the target cites no earlier
        decision or no donor is eligible.
    """
    if not cited or not target_case_numbers:
        return None
    latest_cited = max(cited)
    eligible = sorted(
        (
            donor
            for donor in donors
            if donor.celex_id != target_celex_id
            and donor.case_numbers & target_case_numbers
            and not _is_gated_false(donor.gate)
            and (bounds := parse_period(donor.date_range)) is not None
            and bounds[1].year <= latest_cited
        ),
        key=lambda donor: donor.celex_id,
    )
    if not eligible:
        return None
    named = [donor for donor in eligible if id_year(donor.celex_id) in cited]
    if len(named) == 1:
        return InheritedPeriod(date_range=named[0].date_range, basis=InheritanceBasis.CITED_DECISION, donors=named)
    if not named and len(eligible) == 1:
        return InheritedPeriod(date_range=eligible[0].date_range, basis=InheritanceBasis.SINGLE_SIBLING, donors=eligible)
    pool = named or eligible
    return InheritedPeriod(date_range=_union(pool), basis=InheritanceBasis.SIBLING_UNION, donors=pool)


def _stored_date_range(value_json: Any) -> str | None:
    """Return the ``date_range`` of a stored period payload, or None.

    Args:
        value_json: ``[{"date_range": "..."}]`` as JSON text or decoded; the
            legacy bare-string shape is accepted too.

    Returns:
        The period text, or None for an unreadable payload.
    """
    payload = value_json
    if isinstance(value_json, str):
        try:
            payload = json.loads(value_json)
        except ValueError:
            return None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0].get("date_range")
    elif isinstance(payload, dict):
        payload = payload.get("date_range")
    return payload if isinstance(payload, str) else None


class PeriodInheritor:
    """Find the period a follow-up decision inherits from its original decision.

    The cheap target-side checks (id year, earlier citation, case number) run
    first; the corpus-wide donor read happens only for a genuine follow-up.
    """

    def __init__(self, fact_store: PeriodDonorStore, chunk_store: CitationStore) -> None:
        """Inject the fact and chunk stores.

        Args:
            fact_store: Reads case references and donor period facts.
            chunk_store: Reads ``C(YYYY) NNNN`` citations from the target's text.
        """
        self.fact_store = fact_store
        self.chunk_store = chunk_store

    def find(self, celex_id: str, fact_name: str) -> InheritedPeriod | None:
        """Return the inherited period of ``celex_id``, or None.

        Args:
            celex_id: The decision whose own text yielded no period.
            fact_name: The period fact to read from donors (``cartel_period``).

        Returns:
            The inherited period with its donors, or None.
        """
        own_year = id_year(celex_id)
        if own_year is None:
            return None
        cited = cited_years(self.chunk_store.select_regex_matches(celex_id, CITATION_REGEX), before=own_year)
        if not cited:
            return None
        title_fact = settings.PERIOD_INHERITANCE_CASE_TITLE_FACT
        keys = case_numbers(celex_id, self.fact_store.select_case_refs(celex_id, title_fact))
        if not keys:
            return None
        rows = self.fact_store.select_period_donor_rows(
            fact_name, settings.PERIOD_INHERITANCE_GATE_FACT, title_fact, INHERITANCE_METHOD
        )
        donors = [
            PeriodDonor(
                fact_id=row["fact_id"],
                celex_id=row["celex_id"],
                date_range=date_range,
                gate=row.get("gate"),
                case_numbers=case_numbers(row["celex_id"], list(row.get("case_refs") or [])),
            )
            for row in rows
            if (date_range := _stored_date_range(row.get("value_json"))) is not None
        ]
        inherited = select_inheritance(celex_id, keys, cited, donors)
        if inherited is not None:
            logger.info(
                "%s %s: inherited %s from %s (%s; cited %s)",
                fact_name, celex_id, inherited.date_range,
                ", ".join(donor.celex_id for donor in inherited.donors),
                inherited.basis.value, sorted(cited),
            )
        return inherited
