"""Reconcile the human case index against extracted facts (pure, DB-free).

The human index is the hand-maintained workbook of Commission decisions
(``settings.HUMAN_INDEX_XLSX_PATH``). Four of its columns have a counterpart
among the extracted facts:

=========================  ==================================================
Sheet column               Extracted fact(s)
=========================  ==================================================
Z  Formal Decision         ``case_commission_infringement`` (yes/no only)
AD Nature of Conduct       ``infringement_category_hor_vert_lic_joint`` plus
                           the three boolean category facts
AO Responsible             ``case_commissioner``
   Commissioner
I  firms                   ``companies_investigated``
=========================  ==================================================

Both sides are noisy: the index has typing slips and group-level firm lists,
the extraction has OCR misspellings and gaps. So the comparison never decides
who is right. It normalises both values, classifies the pair with a
:class:`ReconStatus` and records why in ``detail``; a human adjudicates the
conflicts. Agreement of two independent labellers is what qualifies a value as
a gold-set seed.

Exports:
    HumanIndexRow, ExtractedFacts, ReconResult -- the typed boundary values.
    ReconTarget, ReconStatus, SizeBand -- the closed vocabularies.
    reconcile_row -- one index row against one decision's facts.
    size_band -- content-size band of a decision.
    RECON_FACT_NAMES -- every fact the comparison reads.

Design doc: ``docs/human_index2facts_recon.md``.
"""

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pipelines.graph.heideltime_actor_resolution import company_stem
from tools.country_normalizer import to_iso_alpha2

logger = logging.getLogger(__name__)

FACT_INFRINGEMENT = "case_commission_infringement"
FACT_CONDUCT = "infringement_category_hor_vert_lic_joint"
FACT_DOMINANCE = "infringement_category_abuse_of_dominance"
FACT_PROCEDURAL = "infringement_category_procedural"
FACT_MEMBER_STATES = "infringement_category_member_states"
FACT_COMMISSIONER = "case_commissioner"
FACT_COMPANIES = "companies_investigated"

#: Every fact name the comparison reads; the loader fetches exactly these.
RECON_FACT_NAMES: tuple[str, ...] = (
    FACT_INFRINGEMENT,
    FACT_CONDUCT,
    FACT_DOMINANCE,
    FACT_PROCEDURAL,
    FACT_MEMBER_STATES,
    FACT_COMMISSIONER,
    FACT_COMPANIES,
)

_CONDUCT_FACTS: tuple[str, ...] = (
    FACT_CONDUCT,
    FACT_DOMINANCE,
    FACT_PROCEDURAL,
    FACT_MEMBER_STATES,
)

_TRUE = "true"
_FALSE = "false"
_PLACEHOLDERS: frozenset[str] = frozenset({"", "na", "n/a", "none", "null", "unknown"})

# Formal Decision: the sheet spells seven outcomes twelve ways.
_DECISION_INFRINGEMENT = "infringement"
_DECISION_INTERIM = "interim_measure"
_DECISION_CLASSES: dict[str, str] = {
    "infringement": _DECISION_INFRINGEMENT,
    "exemption": "exemption",
    "negative clearance": "negative_clearance",
    # The sheet's own Indicator_Decision files non-applicability under 1.
    "non-applicability": "negative_clearance",
    "exemption and negative clearance": "exemption_and_negative_clearance",
    "decision with commitments": "commitments",
    "interim measure": _DECISION_INTERIM,
}
_DECISION_PREFIXES: tuple[tuple[str, str], ...] = (("renewal of exemption", "exemption"),)

# Nature of Conduct: sheet labels, the sheet's Indicator_Conduct codes (used
# when a decision has several natures and the label cell is left empty), and
# the values the category facts emit, all folded onto one vocabulary.
_CONDUCT_LABELS: dict[str, str] = {
    "horizontal": "horizontal",
    "vertical": "vertical",
    "jv": "joint_venture",
    "licensing": "license",
    "dominance": "dominance",
    "procedural": "procedural",
    "member": "member_states",
}
_CONDUCT_CODES: dict[str, str] = {
    "1": "horizontal",
    "2": "dominance",
    "3": "license",
    "4": "vertical",
    "5": "joint_venture",
    "6": "procedural",
    "7": "member_states",
}
_CONDUCT_FACT_VALUES: dict[str, str] = {
    "horizontal": "horizontal",
    "vertical": "vertical",
    "license": "license",
    "joint venture": "joint_venture",
}
_CONDUCT_BOOLEAN_FACTS: dict[str, str] = {
    FACT_DOMINANCE: "dominance",
    FACT_PROCEDURAL: "procedural",
    FACT_MEMBER_STATES: "member_states",
}

# A surname counts as the same person when it is a truncation of the other
# ("Malfa" / "Malfatti") or close enough to be an OCR slip ("Andriesen").
_SURNAME_MIN_PREFIX = 4
_SURNAME_MIN_RATIO = 0.8

# Firm matching: squashed names must be this long before containment counts,
# and only tokens this long are compared fuzzily ("Quantas" / "Qantas").
_SQUASH_MIN_LENGTH = 4
_FUZZY_TOKEN_MIN_LENGTH = 5
_FUZZY_TOKEN_MIN_RATIO = 0.85
# Leading tokens that never identify a firm on their own.
_HEAD_STOPWORDS: frozenset[str] = frozenset(
    {"the", "la", "le", "les", "de", "der", "die", "das", "and", "of", "compagnie", "societe"}
)
# Words skipped when a name is abbreviated to its initials ("SNCF", "EFAMA").
_INITIALS_STOPWORDS: frozenset[str] = frozenset(
    # "l" and "d" are the elided articles of "l'Ordre", "d'Espérance".
    {"the", "la", "le", "les", "l", "de", "des", "du", "d", "der", "die", "das", "van", "and",
     "et", "of"}
)
_ACRONYM_MIN_LENGTH = 3
_ACRONYM_PREFIX_MIN_LENGTH = 4
# Appended to a list item to ask company_stem whether the item holds any
# token besides legal forms ("Inc", "N.A").
_STEM_PROBE = "zzprobe"
# Country notes the index writes as their own list item ("Mueller;USA; IMI;UK").
# Bare two- and three-letter codes are not accepted: "IMI", "MOL" or "SAS" are firms.
_COUNTRY_SHORT_ALIASES: frozenset[str] = frozenset({"uk", "usa", "us"})
_COUNTRY_MIN_LENGTH = 4

_PARENTHESISED_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_COUNTRY_SPLIT_RE = re.compile(r"/|\band\b")
_CODE_RE = re.compile(r"\d")


class ReconTarget(StrEnum):
    """The human-index column family a recon row compares."""

    FORMAL_DECISION = "formal_decision"
    NATURE_OF_CONDUCT = "nature_of_conduct"
    RESPONSIBLE_COMMISSIONER = "responsible_commissioner"
    FIRMS = "firms"


class ReconStatus(StrEnum):
    """Outcome of comparing one human value with its extracted counterpart."""

    AGREE = "agree"
    CONFLICT = "conflict"
    #: Firms only: every human firm is covered, the extraction names more.
    EXTRACTION_SUPERSET = "extraction_superset"
    HUMAN_BLANK = "human_blank"
    EXTRACTION_BLANK = "extraction_blank"
    #: The human value has no extracted counterpart to compare with.
    NOT_COMPARABLE = "not_comparable"


class SizeBand(StrEnum):
    """Content-size band of a decision, by tokens stored in the chunk table."""

    SMALL = "S"
    MEDIUM = "M"
    LARGE = "L"


#: Sheet column (letter and header) each target is read from.
SHEET_COLUMNS: dict[ReconTarget, str] = {
    ReconTarget.FORMAL_DECISION: "Z Formal Decision",
    ReconTarget.NATURE_OF_CONDUCT: "AD Nature of Conduct",
    ReconTarget.RESPONSIBLE_COMMISSIONER: "AO Responsible Commissioner",
    ReconTarget.FIRMS: "I firms",
}


class HumanIndexRow(BaseModel):
    """One decision as recorded in the human index workbook.

    Blank and whitespace-only cells arrive as ``None``.
    """

    model_config = ConfigDict(frozen=True)

    celex_id: str = Field(description="Decision identifier from the 'celex' column")
    source_row: int = Field(ge=2, description="1-based row number in the worksheet")
    title: str | None = Field(default=None, description="Case title as indexed")
    decision_year: int | None = Field(default=None, description="Decision year as indexed")
    formal_decision: str | None = Field(default=None, description="Column Z, as written")
    nature_of_conduct: str | None = Field(default=None, description="Column AD, as written")
    conduct_indicator: str | None = Field(
        default=None, description="Column AE codes; the only source for multi-nature decisions"
    )
    responsible_commissioner: str | None = Field(default=None, description="Column AO, as written")
    firms: str | None = Field(default=None, description="Column I, semicolon-separated")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Turn empty and whitespace-only cells into ``None``."""
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class ExtractedFacts(BaseModel):
    """The extracted facts of one decision that the comparison reads."""

    model_config = ConfigDict(frozen=True)

    values: dict[str, str] = Field(
        default_factory=dict, description="Fact name to stored value_md, for single-value facts"
    )
    roster: list[str] = Field(
        default_factory=list, description="Entity names of the companies_investigated roster"
    )


class ReconResult(BaseModel):
    """One (decision, target) comparison, ready to be stored."""

    model_config = ConfigDict(frozen=True)

    celex_id: str = Field(description="Decision identifier")
    target: ReconTarget = Field(description="Column family compared")
    sheet_column: str = Field(description="Sheet column letter and header")
    fact_names: str = Field(description="Comma-separated fact names consulted")
    human_raw: str | None = Field(default=None, description="Human cell as written")
    human_norm: str | None = Field(default=None, description="Human value, normalised")
    extracted_raw: str | None = Field(default=None, description="Extracted value as stored")
    extracted_norm: str | None = Field(default=None, description="Extracted value, normalised")
    status: ReconStatus = Field(description="Outcome of the comparison")
    detail: dict[str, object] = Field(
        default_factory=dict, description="Why the status was assigned (lists, ratios, notes)"
    )


def _fold(text: str) -> str:
    """Return *text* lower-cased with accents removed."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _is_placeholder(value: str | None) -> bool:
    """Return whether *value* is missing or a no-answer marker such as ``NA``."""
    return value is None or value.strip().casefold() in _PLACEHOLDERS


def size_band(content_tokens: int | None, medium_from: int, large_from: int) -> SizeBand | None:
    """Return the content-size band of a decision.

    Args:
        content_tokens: Tokens stored for the decision; ``None`` or 0 when it
            has no chunks.
        medium_from: Token count above which a decision is medium.
        large_from: Token count above which a decision is large.

    Returns:
        The band, or ``None`` when the decision has no stored content.
    """
    if not content_tokens:
        return None
    if content_tokens > large_from:
        return SizeBand.LARGE
    if content_tokens > medium_from:
        return SizeBand.MEDIUM
    return SizeBand.SMALL


# ---------------------------------------------------------------------------
# Formal Decision
# ---------------------------------------------------------------------------


def normalise_formal_decision(value: str) -> str | None:
    """Map a Formal Decision spelling onto its decision class.

    Args:
        value: The cell as written, e.g. ``"Renewal of exemption"``.

    Returns:
        The class name, or ``None`` for a spelling the mapping does not know.
    """
    folded = _fold(value).strip()
    if folded in _DECISION_CLASSES:
        return _DECISION_CLASSES[folded]
    for prefix, decision_class in _DECISION_PREFIXES:
        if folded.startswith(prefix):
            return decision_class
    return None


def reconcile_formal_decision(row: HumanIndexRow, facts: ExtractedFacts) -> ReconResult:
    """Compare column Z with ``case_commission_infringement``.

    Only the infringement yes/no is comparable: no fact records exemption,
    negative clearance or commitments as such. Interim measures are left out
    because the fact's question does not say how they count.

    Args:
        row: The human index row.
        facts: The decision's extracted facts.

    Returns:
        The comparison result.
    """
    human_raw = row.formal_decision
    extracted_raw = facts.values.get(FACT_INFRINGEMENT)
    human_class = normalise_formal_decision(human_raw) if human_raw else None
    extracted_bool = (extracted_raw or "").strip().casefold()
    extracted_norm = extracted_bool if extracted_bool in (_TRUE, _FALSE) else None
    detail: dict[str, object] = {}

    if human_raw is None:
        status = ReconStatus.HUMAN_BLANK
    elif human_class is None:
        status = ReconStatus.NOT_COMPARABLE
        detail["reason"] = "unknown formal decision spelling"
    elif human_class == _DECISION_INTERIM:
        status = ReconStatus.NOT_COMPARABLE
        detail["reason"] = "interim measure is neither a finding nor a clearance"
    elif extracted_norm is None:
        status = ReconStatus.EXTRACTION_BLANK
        if not _is_placeholder(extracted_raw):
            detail["reason"] = "extracted value is not True/False"
    else:
        expected = _TRUE if human_class == _DECISION_INFRINGEMENT else _FALSE
        detail["expected_infringement"] = expected
        status = ReconStatus.AGREE if expected == extracted_norm else ReconStatus.CONFLICT

    return ReconResult(
        celex_id=row.celex_id,
        target=ReconTarget.FORMAL_DECISION,
        sheet_column=SHEET_COLUMNS[ReconTarget.FORMAL_DECISION],
        fact_names=FACT_INFRINGEMENT,
        human_raw=human_raw,
        human_norm=human_class,
        extracted_raw=extracted_raw,
        extracted_norm=extracted_norm,
        status=status,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Nature of Conduct
# ---------------------------------------------------------------------------


def human_conduct_categories(row: HumanIndexRow) -> set[str]:
    """Return the conduct categories the index assigns to a decision.

    The label cell wins. Decisions with several natures leave it empty and
    carry only codes (``"1, 2"``); parenthesised codes (``"1 (4)"``) mark a
    secondary aspect and are ignored.

    Args:
        row: The human index row.

    Returns:
        Category names; empty when neither cell is usable.
    """
    if row.nature_of_conduct:
        category = _CONDUCT_LABELS.get(_fold(row.nature_of_conduct).strip())
        return {category} if category else set()
    if row.conduct_indicator:
        primary = _PARENTHESISED_RE.sub(" ", row.conduct_indicator)
        return {_CONDUCT_CODES[c] for c in _CODE_RE.findall(primary) if c in _CONDUCT_CODES}
    return set()


def extracted_conduct_categories(facts: ExtractedFacts) -> set[str]:
    """Return the conduct categories the category facts assert.

    Args:
        facts: The decision's extracted facts.

    Returns:
        The agreement category, if any, plus every boolean category that is True.
    """
    categories: set[str] = set()
    agreement = _CONDUCT_FACT_VALUES.get((facts.values.get(FACT_CONDUCT) or "").strip().casefold())
    if agreement:
        categories.add(agreement)
    for fact_name, category in _CONDUCT_BOOLEAN_FACTS.items():
        if (facts.values.get(fact_name) or "").strip().casefold() == _TRUE:
            categories.add(category)
    return categories


def reconcile_nature_of_conduct(row: HumanIndexRow, facts: ExtractedFacts) -> ReconResult:
    """Compare column AD (or its codes) with the infringement category facts.

    The category facts are independent, so the extraction can assert several
    at once (Intel: vertical and dominance). The pair agrees when every human
    category is among the extracted ones; the surplus is listed in ``detail``.

    Args:
        row: The human index row.
        facts: The decision's extracted facts.

    Returns:
        The comparison result.
    """
    human_raw = row.nature_of_conduct or row.conduct_indicator
    human = human_conduct_categories(row)
    extracted = extracted_conduct_categories(facts)
    present = [f"{name}={facts.values[name]}" for name in _CONDUCT_FACTS if name in facts.values]
    detail: dict[str, object] = {}

    if human_raw is None:
        status = ReconStatus.HUMAN_BLANK
    elif not human:
        status = ReconStatus.NOT_COMPARABLE
        detail["reason"] = "unknown nature of conduct"
    elif not extracted:
        status = ReconStatus.EXTRACTION_BLANK
    else:
        missing = sorted(human - extracted)
        extra = sorted(extracted - human)
        if missing:
            detail["missing"] = missing
        if extra:
            detail["extra"] = extra
        status = ReconStatus.CONFLICT if missing else ReconStatus.AGREE

    return ReconResult(
        celex_id=row.celex_id,
        target=ReconTarget.NATURE_OF_CONDUCT,
        sheet_column=SHEET_COLUMNS[ReconTarget.NATURE_OF_CONDUCT],
        fact_names=",".join(_CONDUCT_FACTS),
        human_raw=human_raw,
        human_norm="+".join(sorted(human)) or None,
        extracted_raw="; ".join(present) or None,
        extracted_norm="+".join(sorted(extracted)) or None,
        status=status,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Responsible Commissioner
# ---------------------------------------------------------------------------


def _name_tokens(name: str) -> list[str]:
    """Return the folded alphanumeric tokens of a person name, role note removed."""
    without_role = _PARENTHESISED_RE.sub(" ", name)
    return [token for token in _NON_ALNUM_RE.split(_fold(without_role)) if token]


def _surname_similarity(expected: str, candidate: str) -> float:
    """Return how closely *candidate* matches the surname *expected* (0..1).

    A truncation of at least ``_SURNAME_MIN_PREFIX`` characters counts as a
    full match: the extractor cuts names at chunk borders.
    """
    if expected == candidate:
        return 1.0
    shorter, longer = sorted((expected, candidate), key=len)
    if len(shorter) >= _SURNAME_MIN_PREFIX and longer.startswith(shorter):
        return 1.0
    return SequenceMatcher(None, expected, candidate).ratio()


def reconcile_commissioner(row: HumanIndexRow, facts: ExtractedFacts) -> ReconResult:
    """Compare column AO with ``case_commissioner`` on the surname.

    The index appends a role note (``"(M)"``, ``"(P)"``, ``"(VP)"``) and the
    extraction varies in casing and first names, so only the surname is
    compared, tolerant of truncation and OCR slips.

    Args:
        row: The human index row.
        facts: The decision's extracted facts.

    Returns:
        The comparison result.
    """
    human_raw = row.responsible_commissioner
    extracted_raw = facts.values.get(FACT_COMMISSIONER)
    human_tokens = _name_tokens(human_raw) if human_raw else []
    surname = human_tokens[-1] if human_tokens else None
    extracted_tokens = [] if _is_placeholder(extracted_raw) else _name_tokens(extracted_raw or "")
    extracted_norm: str | None = " ".join(extracted_tokens) or None
    detail: dict[str, object] = {}

    if surname is None:
        status = ReconStatus.HUMAN_BLANK
    elif not extracted_tokens:
        status = ReconStatus.EXTRACTION_BLANK
    else:
        ratio, best = max((_surname_similarity(surname, tok), tok) for tok in extracted_tokens)
        if ratio >= _SURNAME_MIN_RATIO:
            status = ReconStatus.AGREE
            extracted_norm = best
            if best != surname:
                detail["match"] = "fuzzy"
                detail["extracted_surname"] = best
        else:
            status = ReconStatus.CONFLICT

    return ReconResult(
        celex_id=row.celex_id,
        target=ReconTarget.RESPONSIBLE_COMMISSIONER,
        sheet_column=SHEET_COLUMNS[ReconTarget.RESPONSIBLE_COMMISSIONER],
        fact_names=FACT_COMMISSIONER,
        human_raw=human_raw,
        human_norm=surname,
        extracted_raw=extracted_raw,
        extracted_norm=extracted_norm,
        status=status,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Firms
# ---------------------------------------------------------------------------


def _is_country_note(item: str) -> bool:
    """Return whether a list item only names countries (``"UK/The Netherlands"``)."""
    parts = [part.strip() for part in _COUNTRY_SPLIT_RE.split(item) if part.strip()]
    if not parts:
        return False
    for part in parts:
        name = part[4:] if part.casefold().startswith("the ") else part
        folded = name.casefold()
        long_enough = len(folded) >= _COUNTRY_MIN_LENGTH or folded in _COUNTRY_SHORT_ALIASES
        if not long_enough or to_iso_alpha2(name) is None:
            return False
    return True


def _has_name_token(item: str) -> bool:
    """Return whether *item* holds a token other than legal forms and initials."""
    return len(company_stem(f"{_fold(item)} {_STEM_PROBE}")) > 1


def parse_human_firms(cell: str | None) -> list[str]:
    """Split the index's firms cell into firm names.

    Parenthesised notes are removed first because they contain semicolons
    (``"ArcelorMittal (L;F;B;I)"``); list items that only name countries are
    dropped (``"Mueller;USA; IMI;UK"``); an item that is only a legal form
    belongs to the name before it (``"Citibank; N.A"``).

    Args:
        cell: Column I as written, or ``None``.

    Returns:
        Firm names in sheet order, without notes and country items.
    """
    if not cell:
        return []
    firms: list[str] = []
    for raw_item in _PARENTHESISED_RE.sub(" ", cell).split(";"):
        item = " ".join(raw_item.strip(" .,").split())
        if not item or _is_country_note(item):
            continue
        # One word only: "SAS AB" is all legal forms too, and is an airline.
        if firms and " " not in item and not _has_name_token(item):
            firms[-1] = f"{firms[-1]} {item}"
        else:
            firms.append(item)
    return firms


def _stem(name: str) -> tuple[str, ...]:
    """Return the discriminating tokens of a firm name, accents folded."""
    return tuple(token for token in company_stem(_fold(name)) if len(token) > 1)


def _tokens_match(left: str, right: str) -> bool:
    """Return whether two name tokens are equal or a plausible misspelling."""
    if left == right:
        return True
    if min(len(left), len(right)) < _FUZZY_TOKEN_MIN_LENGTH:
        return False
    return SequenceMatcher(None, left, right).ratio() >= _FUZZY_TOKEN_MIN_RATIO


def _initials(name: str) -> str:
    """Return the initials of a name's words, articles and conjunctions skipped."""
    words = (word for word in _NON_ALNUM_RE.split(_fold(name)) if word)
    return "".join(word[0] for word in words if word not in _INITIALS_STOPWORDS)


def _is_acronym_of(short_stem: tuple[str, ...], long_name: str) -> bool:
    """Return whether a one-token name is the acronym of *long_name*.

    An acronym of ``_ACRONYM_PREFIX_MIN_LENGTH`` letters or more may stop
    before the last words: "SNCF" leaves out the final "français".
    """
    if len(short_stem) != 1 or len(short_stem[0]) < _ACRONYM_MIN_LENGTH:
        return False
    acronym, initials = short_stem[0], _initials(long_name)
    if len(acronym) >= _ACRONYM_PREFIX_MIN_LENGTH:
        return initials.startswith(acronym)
    return acronym == initials


def _same_firm(human_name: str, extracted_name: str) -> bool:
    """Return whether a human firm name and an extracted entity denote one group.

    Any one of four tests suffices:

    * the squashed names contain one another (``"InBev NV"`` / ``"InBevNV"``,
      ``"Frabo"`` / ``"FRA.BO S.p.A"``);
    * the first distinctive token of the human name occurs in the extracted
      name (``"Saint-Gobain"`` / ``"Saint-Gobain Sekurit France SA"``);
    * the extracted name is a single token that occurs in the human name
      (``"BDZ"`` / ``"Bundesverband der Deutschen Zementindustrie eV-BDZ"``);
    * one name is the acronym of the other (``"SNCF"``).

    The tests are deliberately conservative: a missed match becomes a
    conflict a human reviews, a false match would pass as agreement.
    """
    human_stem, extracted_stem = _stem(human_name), _stem(extracted_name)
    if not human_stem or not extracted_stem:
        return False
    human_squash, extracted_squash = "".join(human_stem), "".join(extracted_stem)
    if min(len(human_squash), len(extracted_squash)) >= _SQUASH_MIN_LENGTH and (
        human_squash in extracted_squash or extracted_squash in human_squash
    ):
        return True
    head = next((tok for tok in human_stem if tok not in _HEAD_STOPWORDS), human_stem[0])
    if any(_tokens_match(head, token) for token in extracted_stem):
        return True
    if len(extracted_stem) == 1 and extracted_stem[0] in human_stem:
        return True
    return _is_acronym_of(human_stem, extracted_name) or _is_acronym_of(extracted_stem, human_name)


def _alternatives(human_firm: str) -> list[str]:
    """Return the spellings under which a human firm entry may appear.

    The index joins group aliases with a slash (``"Asahi/AGC Flat Glass"``);
    each part and the whole entry are tried.
    """
    parts = [part.strip() for part in human_firm.split("/") if part.strip()]
    return [human_firm, *parts] if len(parts) > 1 else [human_firm]


def reconcile_firms(row: HumanIndexRow, facts: ExtractedFacts) -> ReconResult:
    """Compare column I with the ``companies_investigated`` roster.

    The comparison is asymmetric on purpose. The index often names a group
    where the decision lists each addressed legal entity, so the test is
    coverage: every human firm must be matched by at least one extracted
    entity. Extracted entities matching no human firm are reported as a
    superset, not as a conflict.

    Args:
        row: The human index row.
        facts: The decision's extracted facts.

    Returns:
        The comparison result.
    """
    human_firms = parse_human_firms(row.firms)
    roster = facts.roster
    detail: dict[str, object] = {"human_count": len(human_firms), "extracted_count": len(roster)}

    if not human_firms:
        status = ReconStatus.HUMAN_BLANK
    elif not roster:
        status = ReconStatus.EXTRACTION_BLANK
    else:
        claimed: set[int] = set()
        missing: list[str] = []
        for firm in human_firms:
            hits = {
                index
                for index, entity in enumerate(roster)
                if any(_same_firm(alt, entity) for alt in _alternatives(firm))
            }
            if hits:
                claimed |= hits
            else:
                missing.append(firm)
        extra = [entity for index, entity in enumerate(roster) if index not in claimed]
        if missing:
            detail["missing"] = missing
        if extra:
            detail["extra"] = extra
        if missing:
            status = ReconStatus.CONFLICT
        elif extra:
            status = ReconStatus.EXTRACTION_SUPERSET
        else:
            status = ReconStatus.AGREE

    return ReconResult(
        celex_id=row.celex_id,
        target=ReconTarget.FIRMS,
        sheet_column=SHEET_COLUMNS[ReconTarget.FIRMS],
        fact_names=FACT_COMPANIES,
        human_raw=row.firms,
        human_norm="; ".join(human_firms) or None,
        extracted_raw="; ".join(roster) or None,
        extracted_norm=None,
        status=status,
        detail=detail,
    )


def reconcile_row(row: HumanIndexRow, facts: ExtractedFacts) -> list[ReconResult]:
    """Compare one human index row with one decision's extracted facts.

    Args:
        row: The human index row.
        facts: The decision's extracted facts; empty when it has none.

    Returns:
        One result per :class:`ReconTarget`, in enum order.
    """
    return [
        reconcile_formal_decision(row, facts),
        reconcile_nature_of_conduct(row, facts),
        reconcile_commissioner(row, facts),
        reconcile_firms(row, facts),
    ]
